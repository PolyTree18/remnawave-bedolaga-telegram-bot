"""Admin routes for the database backup system in the cabinet.

Thin authenticated wrappers around ``app.services.backup_service.backup_service``
(the SAME singleton the Telegram bot admin panel uses). All heavy lifting —
dumping/restoring the database, archive handling, path-traversal protection,
old-backup cleanup — lives in the service. These endpoints only authenticate,
validate input at the boundary, and shape clean JSON for the frontend.

Backup settings (``BACKUP_*``) are persisted via ``SystemSettingsService`` —
the same path the cabinet settings UI uses — which writes to the DB and then
calls ``backup_service.reload_settings_from_db()`` + restarts the auto-backup
scheduler. The bot handler instead mutates the service in-memory only, so the
cabinet path is the more correct one and is intentionally preferred here.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.backup_service import backup_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/backup', tags=['Cabinet Admin Backup'])


# --- Constants -------------------------------------------------------------

# Mirror the bot's upload constraints (handlers/admin/backup.py).
ALLOWED_BACKUP_EXTENSIONS: tuple[str, ...] = ('.json', '.json.gz', '.tar.gz', '.tar')
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024  # 50 MB, same cap as the bot

# Settings keys persisted via SystemSettingsService. Each maps a clean request
# field to its BACKUP_* system-setting key.
_SETTINGS_KEY_MAP: dict[str, str] = {
    'auto_backup_enabled': 'BACKUP_AUTO_ENABLED',
    'backup_interval_hours': 'BACKUP_INTERVAL_HOURS',
    'backup_time': 'BACKUP_TIME',
    'max_backups_keep': 'BACKUP_MAX_KEEP',
    'compression_enabled': 'BACKUP_COMPRESSION',
    'include_logs': 'BACKUP_INCLUDE_LOGS',
}


# --- Schemas ---------------------------------------------------------------


class BackupListItem(BaseModel):
    """A single backup file as surfaced by ``backup_service.get_backup_list``."""

    filename: str
    timestamp: str | None = None
    tables_count: int | str | None = None
    total_records: int | str | None = None
    compressed: bool = False
    file_size_bytes: int = 0
    file_size_mb: float = 0.0
    created_by: int | str | None = None
    database_type: str | None = None
    version: str | None = None
    corrupted: bool = False
    error: str | None = None


class BackupListResponse(BaseModel):
    """Paginated list of backups (newest first)."""

    items: list[BackupListItem]
    total: int
    page: int
    per_page: int
    total_pages: int


class BackupCreateRequest(BaseModel):
    """Options for creating a new backup."""

    compress: bool = True
    include_logs: bool | None = None  # None → fall back to current settings


class BackupCreateResponse(BaseModel):
    """Result of a create-backup operation."""

    success: bool
    message: str
    filename: str | None = None


class BackupDeleteResponse(BaseModel):
    """Result of a delete-backup operation."""

    success: bool
    message: str
    filename: str


class BackupRestoreRequest(BaseModel):
    """Options for restoring from an existing backup file.

    ``clear_existing=True`` is the DESTRUCTIVE wipe-then-restore mode: it drops
    existing data before importing. ``False`` is the append/merge mode.
    """

    clear_existing: bool = False


class BackupRestoreResponse(BaseModel):
    """Result of a restore operation."""

    success: bool
    message: str
    filename: str
    cleared_existing: bool


class BackupSettingsResponse(BaseModel):
    """Current backup-system settings (read-only snapshot)."""

    auto_backup_enabled: bool
    backup_interval_hours: int
    backup_time: str
    max_backups_keep: int
    compression_enabled: bool
    include_logs: bool
    backup_location: str


class BackupSettingsUpdateRequest(BaseModel):
    """Partial update of backup settings. Only provided fields are changed."""

    auto_backup_enabled: bool | None = None
    backup_interval_hours: int | None = Field(None, ge=1, le=24 * 30)
    backup_time: str | None = Field(None, pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    max_backups_keep: int | None = Field(None, ge=1, le=1000)
    compression_enabled: bool | None = None
    include_logs: bool | None = None


# --- Helpers ---------------------------------------------------------------


def _settings_to_response(settings_obj) -> BackupSettingsResponse:
    """Convert the service ``BackupSettings`` dataclass to a response model."""
    data = asdict(settings_obj)
    return BackupSettingsResponse(
        auto_backup_enabled=bool(data.get('auto_backup_enabled', False)),
        backup_interval_hours=int(data.get('backup_interval_hours', 24)),
        backup_time=str(data.get('backup_time', '03:00')),
        max_backups_keep=int(data.get('max_backups_keep', 7)),
        compression_enabled=bool(data.get('compression_enabled', True)),
        include_logs=bool(data.get('include_logs', False)),
        backup_location=str(data.get('backup_location', '')),
    )


def _validate_backup_filename(filename: str) -> str:
    """Reject obviously malicious/empty filenames at the boundary.

    The service performs the authoritative path-traversal check (resolving the
    path against ``backup_dir``); this is a cheap fail-fast on top of it.
    """
    cleaned = (filename or '').strip()
    if not cleaned or '/' in cleaned or '\\' in cleaned or cleaned in {'.', '..'}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid backup filename',
        )
    return cleaned


# --- Endpoints -------------------------------------------------------------


@router.get('', response_model=BackupListResponse)
async def list_backups(
    page: int = 1,
    per_page: int = 20,
    admin: User = Depends(require_permission('backup:read')),
):
    """List available backups (newest first), paginated."""
    try:
        page = max(page, 1)
        if per_page < 1 or per_page > 200:
            per_page = 20

        backups = await backup_service.get_backup_list()
        total = len(backups)
        total_pages = (total + per_page - 1) // per_page if total else 0

        start = (page - 1) * per_page
        end = start + per_page
        page_slice = backups[start:end]

        items = [BackupListItem(**backup) for backup in page_slice]

        return BackupListResponse(
            items=items,
            total=total,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
        )
    except Exception as exc:
        logger.error('Failed to list backups', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list backups: {exc!s}',
        ) from exc


@router.get('/settings', response_model=BackupSettingsResponse)
async def get_backup_settings(
    admin: User = Depends(require_permission('backup:read')),
):
    """Read the current backup-system settings."""
    try:
        settings_obj = await backup_service.get_backup_settings()
        return _settings_to_response(settings_obj)
    except Exception as exc:
        logger.error('Failed to read backup settings', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to read backup settings: {exc!s}',
        ) from exc


@router.put('/settings', response_model=BackupSettingsResponse)
async def update_backup_settings(
    request: BackupSettingsUpdateRequest,
    admin: User = Depends(require_permission('backup:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update backup settings (persisted to DB + applied to the scheduler).

    Uses ``bot_configuration_service.set_value`` — the same persistence path the
    cabinet settings UI uses — which writes the ``BACKUP_*`` keys and triggers
    ``backup_service.reload_settings_from_db()`` plus an auto-backup scheduler
    restart so changes take effect without a bot restart.
    """
    try:
        from app.services.system_settings_service import bot_configuration_service

        provided = request.model_dump(exclude_unset=True)
        if not provided:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='No settings provided',
            )

        applied: dict[str, object] = {}
        for field, value in provided.items():
            if value is None:
                continue
            setting_key = _SETTINGS_KEY_MAP.get(field)
            if setting_key is None:
                continue
            await bot_configuration_service.set_value(db, setting_key, value)
            applied[field] = value

        await db.commit()

        logger.info('Admin updated backup settings', admin_id=admin.id, applied=applied)

        settings_obj = await backup_service.get_backup_settings()
        return _settings_to_response(settings_obj)
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.error('Failed to update backup settings', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update backup settings: {exc!s}',
        ) from exc


@router.post('', response_model=BackupCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_backup(
    request: BackupCreateRequest,
    admin: User = Depends(require_permission('backup:manage')),
):
    """Create a new full backup of the database and data directory.

    Long-running operation: the service exports every table and packs an
    archive. Returns the resulting filename on success.
    """
    try:
        created_by = admin.telegram_id or admin.id
        success, message, file_path = await backup_service.create_backup(
            created_by=created_by,
            compress=request.compress,
            include_logs=request.include_logs,
        )

        filename = None
        if file_path:
            from pathlib import Path

            filename = Path(file_path).name

        if not success:
            logger.warning('Backup creation failed', admin_id=admin.id, message=message)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=message,
            )

        logger.info('Admin created backup', admin_id=admin.id, filename=filename)

        return BackupCreateResponse(success=True, message=message, filename=filename)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to create backup', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to create backup: {exc!s}',
        ) from exc


@router.get('/{filename}/download')
async def download_backup(
    filename: str,
    admin: User = Depends(require_permission('backup:read')),
):
    """Download a backup file by name."""
    try:
        cleaned = _validate_backup_filename(filename)

        backup_path = await asyncio.to_thread((backup_service.backup_dir / cleaned).resolve)
        backup_dir_resolved = await asyncio.to_thread(backup_service.backup_dir.resolve)

        import os

        if not str(backup_path).startswith(str(backup_dir_resolved) + os.sep):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid backup filename',
            )

        if not await asyncio.to_thread(backup_path.is_file):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Backup file not found',
            )

        logger.info('Admin downloaded backup', admin_id=admin.id, filename=cleaned)

        return FileResponse(
            str(backup_path),
            media_type='application/octet-stream',
            filename=cleaned,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to download backup', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to download backup: {exc!s}',
        ) from exc


@router.delete('/{filename}', response_model=BackupDeleteResponse)
async def delete_backup(
    filename: str,
    admin: User = Depends(require_permission('backup:manage')),
):
    """Delete a backup file. DESTRUCTIVE: the file is permanently removed."""
    try:
        cleaned = _validate_backup_filename(filename)

        success, message = await backup_service.delete_backup(cleaned)

        if not success:
            # Service returns False for not-found / invalid name — surface as 404.
            logger.warning('Backup delete failed', admin_id=admin.id, filename=cleaned, message=message)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=message,
            )

        logger.info('Admin deleted backup', admin_id=admin.id, filename=cleaned)

        return BackupDeleteResponse(success=True, message=message, filename=cleaned)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to delete backup', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete backup: {exc!s}',
        ) from exc


@router.post('/{filename}/restore', response_model=BackupRestoreResponse)
async def restore_backup(
    filename: str,
    request: BackupRestoreRequest,
    admin: User = Depends(require_permission('backup:manage')),
):
    """Restore from an existing backup file.

    DESTRUCTIVE. With ``clear_existing=true`` the database is wiped before the
    import (full replace); with ``false`` the backup is merged/appended into the
    current data. Strongly recommend creating a fresh backup first.
    """
    try:
        cleaned = _validate_backup_filename(filename)

        backup_path = await asyncio.to_thread((backup_service.backup_dir / cleaned).resolve)
        backup_dir_resolved = await asyncio.to_thread(backup_service.backup_dir.resolve)

        import os

        if not str(backup_path).startswith(str(backup_dir_resolved) + os.sep):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid backup filename',
            )

        if not await asyncio.to_thread(backup_path.is_file):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Backup file not found',
            )

        logger.warning(
            'Admin started backup restore',
            admin_id=admin.id,
            filename=cleaned,
            clear_existing=request.clear_existing,
        )

        success, message = await backup_service.restore_backup(
            str(backup_path),
            clear_existing=request.clear_existing,
        )

        if not success:
            logger.error('Backup restore failed', admin_id=admin.id, filename=cleaned, message=message)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=message,
            )

        logger.info('Admin restored backup', admin_id=admin.id, filename=cleaned)

        return BackupRestoreResponse(
            success=True,
            message=message,
            filename=cleaned,
            cleared_existing=request.clear_existing,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to restore backup', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to restore backup: {exc!s}',
        ) from exc


@router.post('/upload-restore', response_model=BackupRestoreResponse)
async def upload_and_restore_backup(
    file: UploadFile = File(...),
    clear_existing: bool = False,
    admin: User = Depends(require_permission('backup:manage')),
):
    """Upload a backup file and immediately restore from it.

    DESTRUCTIVE. Accepts ``.json``, ``.json.gz``, ``.tar.gz`` or ``.tar`` up to
    50 MB. ``clear_existing=true`` wipes the database before importing; ``false``
    merges. The uploaded file is staged inside ``backup_dir`` (with an
    ``uploaded_`` prefix, mirroring the bot) and remains there as a list-visible
    backup afterward.
    """
    try:
        original_name = (file.filename or '').strip()
        if not original_name or not any(original_name.endswith(ext) for ext in ALLOWED_BACKUP_EXTENSIONS):
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail='Unsupported file type. Allowed: .json, .json.gz, .tar.gz, .tar',
            )

        # Read with a hard budget (read one extra byte to detect oversize).
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        await file.close()

        if not data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Empty file',
            )
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail='File too large (maximum 50MB)',
            )

        # Stage inside backup_dir. Strip any path components from the client
        # filename and prefix to match the bot's behavior + keep it list-visible.
        from pathlib import Path

        safe_name = Path(original_name).name
        staged_name = f'uploaded_{safe_name}' if not safe_name.startswith('backup_') else safe_name
        staged_path = await asyncio.to_thread((backup_service.backup_dir / staged_name).resolve)
        backup_dir_resolved = await asyncio.to_thread(backup_service.backup_dir.resolve)

        # Path-traversal guard: the resolved staged path must stay inside backup_dir.
        if not staged_path.is_relative_to(backup_dir_resolved):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid filename',
            )

        await asyncio.to_thread(staged_path.write_bytes, data)

        logger.warning(
            'Admin uploaded backup for restore',
            admin_id=admin.id,
            filename=staged_name,
            size_bytes=len(data),
            clear_existing=clear_existing,
        )

        success, message = await backup_service.restore_backup(
            str(staged_path),
            clear_existing=clear_existing,
        )

        if not success:
            logger.error('Uploaded backup restore failed', admin_id=admin.id, filename=staged_name, message=message)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=message,
            )

        logger.info('Admin restored uploaded backup', admin_id=admin.id, filename=staged_name)

        return BackupRestoreResponse(
            success=True,
            message=message,
            filename=staged_name,
            cleared_existing=clear_existing,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to upload-and-restore backup', admin_id=admin.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to upload and restore backup: {exc!s}',
        ) from exc
