"""Admin routes for viewing and downloading the bot system log file.

Ports the bot-admin "Системные логи" feature (see
``app/handlers/admin/system_logs.py``) to the web cabinet. There is no
reusable service for this capability in the bot — it reads
``settings.LOG_FILE`` directly — so the file IO is performed here behind an
authenticated, permission-gated endpoint. Reads are size-limited to protect
the API from huge log files.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/system-logs', tags=['Cabinet Admin System Logs'])

# Hard cap on how many bytes we read from the end of the log file for the
# tail preview. Protects the API from accidentally loading multi-GB logs.
MAX_TAIL_BYTES = 1_000_000  # 1 MB
DEFAULT_TAIL_LINES = 200
MIN_TAIL_LINES = 1
MAX_TAIL_LINES = 5000


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class LogMeta(BaseModel):
    """Metadata about the resolved log file."""

    path: str
    exists: bool
    size_bytes: int = 0
    last_modified: datetime | None = None


class LogTailResponse(BaseModel):
    """Tail preview of the log file plus metadata."""

    meta: LogMeta
    lines: list[str]
    returned_lines: int
    requested_lines: int
    # True when the requested tail was clipped by MAX_TAIL_BYTES (i.e. the
    # file is larger than what we are willing to read for a preview).
    truncated: bool = False


# --------------------------------------------------------------------------- #
# Helpers (synchronous file IO — always called via asyncio.to_thread)
# --------------------------------------------------------------------------- #


def _resolve_log_path() -> Path:
    """Resolve the configured log file path, mirroring the bot handler."""
    log_path = Path(settings.LOG_FILE)
    if not log_path.is_absolute():
        log_path = Path.cwd() / log_path
    return log_path


def _read_meta(log_path: Path) -> LogMeta:
    """Read filesystem metadata for the log file."""
    if not log_path.exists() or not log_path.is_file():
        return LogMeta(path=str(log_path), exists=False)

    stats = log_path.stat()
    return LogMeta(
        path=str(log_path),
        exists=True,
        size_bytes=stats.st_size,
        last_modified=datetime.fromtimestamp(stats.st_mtime, tz=UTC),
    )


def _read_tail(log_path: Path, max_lines: int) -> tuple[list[str], bool]:
    """Read the last ``max_lines`` lines, capped at MAX_TAIL_BYTES.

    Returns a tuple of (lines, truncated) where ``truncated`` is True when the
    file was larger than MAX_TAIL_BYTES and the tail was clipped.
    """
    stats = log_path.stat()
    truncated = False

    with log_path.open('rb') as handle:
        if stats.st_size > MAX_TAIL_BYTES:
            handle.seek(-MAX_TAIL_BYTES, 2)
            truncated = True
        raw = handle.read()

    text = raw.decode('utf-8', errors='ignore')
    # If we clipped mid-line at the start, drop the partial first line so the
    # preview only shows complete lines.
    all_lines = text.splitlines()
    if truncated and all_lines:
        all_lines = all_lines[1:]

    return all_lines[-max_lines:], truncated


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get('/meta', response_model=LogMeta)
async def get_log_meta(
    admin: User = Depends(require_permission('logs:read')),
    db=Depends(get_cabinet_db),
):
    """Return metadata (path, existence, size, last-modified) for the log file."""
    try:
        log_path = _resolve_log_path()
        meta = await asyncio.to_thread(_read_meta, log_path)
        logger.info('Admin read system log meta', admin_id=admin.id, exists=meta.exists)
        return meta
    except Exception as error:
        logger.error('Failed to read system log meta', error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to read log metadata',
        )


@router.get('/tail', response_model=LogTailResponse)
async def get_log_tail(
    lines: int = Query(
        DEFAULT_TAIL_LINES,
        ge=MIN_TAIL_LINES,
        le=MAX_TAIL_LINES,
        description='Number of trailing lines to return.',
    ),
    admin: User = Depends(require_permission('logs:read')),
    db=Depends(get_cabinet_db),
):
    """Return the last ``lines`` lines of the log file plus metadata.

    The read is capped at MAX_TAIL_BYTES; if the file is larger, only the tail
    of that window is read and ``truncated`` is set in the response.
    """
    try:
        log_path = _resolve_log_path()
        meta = await asyncio.to_thread(_read_meta, log_path)

        if not meta.exists:
            return LogTailResponse(
                meta=meta,
                lines=[],
                returned_lines=0,
                requested_lines=lines,
                truncated=False,
            )

        tail_lines, truncated = await asyncio.to_thread(_read_tail, log_path, lines)

        logger.info(
            'Admin read system log tail',
            admin_id=admin.id,
            requested_lines=lines,
            returned_lines=len(tail_lines),
            truncated=truncated,
        )

        return LogTailResponse(
            meta=meta,
            lines=tail_lines,
            returned_lines=len(tail_lines),
            requested_lines=lines,
            truncated=truncated,
        )
    except Exception as error:
        logger.error('Failed to read system log tail', error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to read log file',
        )


@router.get('/download')
async def download_log(
    admin: User = Depends(require_permission('logs:read')),
    db=Depends(get_cabinet_db),
):
    """Download the full log file as an attachment."""
    try:
        log_path = _resolve_log_path()

        if not await asyncio.to_thread(lambda: log_path.exists() and log_path.is_file()):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Log file not found',
            )

        logger.info('Admin downloaded system log', admin_id=admin.id, path=str(log_path))

        return FileResponse(
            path=log_path,
            media_type='text/plain; charset=utf-8',
            filename=log_path.name,
            headers={'Cache-Control': 'no-store'},
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to download system log', error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to download log file',
        )
