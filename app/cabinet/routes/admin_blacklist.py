"""Admin routes for blacklist, mass-ban and blocked-users cleanup in cabinet.

Wraps three bot-admin features as thin authenticated REST endpoints:

* GitHub-sourced blacklist (status / entries / force-update) — ``blacklist_service``.
* Mass-ban by a list of Telegram IDs — ``bulk_ban_service``.
* Bot-blocker scan & cleanup (DB / Remnawave / mark) — ``BlockedUsersService``.

All business logic stays in the services; these handlers only authenticate,
validate input, call the service and shape the response.
"""

from enum import Enum

import structlog
from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.models import User
from app.services.blacklist_service import blacklist_service
from app.services.blocked_users_service import (
    BlockCheckResult,
    BlockCheckStatus,
    BlockedUserAction,
    BlockedUsersService,
)
from app.services.bulk_ban_service import bulk_ban_service

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/blacklist-massban', tags=['Cabinet Admin Blacklist & Mass-Ban'])

# Match the bot's safety cap for mass-ban (bulk_ban handler limits to 1000 IDs).
MAX_BULK_BAN_IDS = 1000


_cached_bot: Bot | None = None


def _get_bot() -> Bot:
    """Lazily create and cache a Bot instance for notifications / block checks."""
    global _cached_bot
    if _cached_bot is None:
        _cached_bot = create_bot()
    return _cached_bot


# =============================================================================
# Schemas
# =============================================================================


class BlacklistEntry(BaseModel):
    """A single blacklist entry parsed from the GitHub source file."""

    telegram_id: int
    username: str | None = None
    reason: str | None = None


class BlacklistStatusResponse(BaseModel):
    """GitHub blacklist configuration & current state."""

    check_enabled: bool
    github_url: str | None = None
    update_interval_hours: int
    ignore_admins: bool
    entries_count: int


class BlacklistEntriesResponse(BaseModel):
    """All entries currently loaded from the blacklist source."""

    entries: list[BlacklistEntry]
    total: int


class BlacklistForceUpdateResponse(BaseModel):
    """Result of a forced refresh from the GitHub source."""

    success: bool
    message: str
    entries_count: int


class MassBanRequest(BaseModel):
    """Request body for mass-ban by Telegram IDs.

    ``telegram_ids`` accepts either an already-parsed list of integer IDs or,
    via ``raw_text``, the free-form text formats the bot supports (one per
    line / comma- / space-separated). At least one of the two must be set.
    """

    telegram_ids: list[int] | None = Field(
        default=None,
        description='Explicit list of Telegram IDs to ban.',
    )
    raw_text: str | None = Field(
        default=None,
        description='Free-form text of IDs (newline/comma/space separated); parsed server-side.',
    )
    reason: str = Field(
        default='Массовая блокировка администратором',
        min_length=1,
        max_length=500,
    )
    notify_users: bool = Field(
        default=True,
        description='Send each banned user a Telegram notification (best-effort).',
    )


class MassBanResponse(BaseModel):
    """Outcome report of a mass-ban operation."""

    requested: int
    banned: int
    not_found: int
    errors: int
    error_telegram_ids: list[int]
    success_rate_percent: float


class BlockedUserItem(BaseModel):
    """A user detected as having blocked the bot.

    ``user_id`` and ``remnawave_uuids`` are echoed back by the frontend in the
    cleanup call so the server can rebuild the cleanup targets without
    server-side session state.
    """

    user_id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str | None = None
    remnawave_uuid: str | None = None
    remnawave_uuids: list[str] = Field(default_factory=list)


class BlockedScanResponse(BaseModel):
    """Result of scanning users for who blocked the bot."""

    total_checked: int
    blocked_count: int
    active_users: int
    errors: int
    skipped_no_telegram: int
    scan_duration_seconds: float
    blocked_users: list[BlockedUserItem]


class BlockedCleanupAction(str, Enum):
    """Supported cleanup actions, mirroring ``BlockedUserAction``."""

    delete_from_db = 'delete_from_db'
    delete_from_remnawave = 'delete_from_remnawave'
    delete_both = 'delete_both'
    mark_as_blocked = 'mark_as_blocked'


class BlockedCleanupRequest(BaseModel):
    """Request body to clean up a set of blocked users."""

    action: BlockedCleanupAction
    users: list[BlockedUserItem] = Field(min_length=1)


class BlockedCleanupResponse(BaseModel):
    """Outcome report of a cleanup operation."""

    action: BlockedCleanupAction
    deleted_from_db: int
    deleted_from_remnawave: int
    marked_as_blocked: int
    errors: list[str]


# =============================================================================
# Blacklist (GitHub-sourced) endpoints
# =============================================================================


@router.get('/blacklist/status', response_model=BlacklistStatusResponse)
async def get_blacklist_status(
    admin: User = Depends(require_permission('blacklist:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Return blacklist configuration and the number of loaded entries."""
    try:
        entries = await blacklist_service.get_all_blacklisted_users()
        return BlacklistStatusResponse(
            check_enabled=blacklist_service.is_blacklist_check_enabled(),
            github_url=blacklist_service.get_blacklist_github_url(),
            update_interval_hours=blacklist_service.get_blacklist_update_interval_hours(),
            ignore_admins=blacklist_service.should_ignore_admins(),
            entries_count=len(entries),
        )
    except Exception as e:
        logger.error('Failed to get blacklist status', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to get blacklist status: {e!s}',
        )


@router.get('/blacklist/entries', response_model=BlacklistEntriesResponse)
async def get_blacklist_entries(
    admin: User = Depends(require_permission('blacklist:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Return all entries currently loaded from the blacklist source."""
    try:
        raw_entries = await blacklist_service.get_all_blacklisted_users()
        entries = [
            BlacklistEntry(
                telegram_id=tg_id,
                username=username or None,
                reason=reason or None,
            )
            for tg_id, username, reason in raw_entries
        ]
        return BlacklistEntriesResponse(entries=entries, total=len(entries))
    except Exception as e:
        logger.error('Failed to load blacklist entries', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load blacklist entries: {e!s}',
        )


@router.post('/blacklist/force-update', response_model=BlacklistForceUpdateResponse)
async def force_update_blacklist(
    admin: User = Depends(require_permission('blacklist:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Force a refresh of the blacklist from the configured GitHub source."""
    try:
        if not blacklist_service.get_blacklist_github_url():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Blacklist GitHub URL is not configured (set BLACKLIST_GITHUB_URL in .env).',
            )

        success, message = await blacklist_service.force_update_blacklist()
        entries = await blacklist_service.get_all_blacklisted_users()

        logger.info(
            'Admin force-updated blacklist',
            admin_id=admin.id,
            success=success,
            entries_count=len(entries),
        )

        return BlacklistForceUpdateResponse(
            success=success,
            message=message,
            entries_count=len(entries),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to force-update blacklist', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update blacklist: {e!s}',
        )


# =============================================================================
# Mass-ban endpoint
# =============================================================================


@router.post('/mass-ban', response_model=MassBanResponse)
async def mass_ban_users(
    request: MassBanRequest,
    admin: User = Depends(require_permission('blacklist:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Ban a list of users by Telegram ID.

    Accepts an explicit ``telegram_ids`` list and/or free-form ``raw_text``
    (parsed server-side). Each existing user is blocked with ``reason``;
    returns a banned / not-found / errors breakdown.
    """
    try:
        # Collect IDs from both the explicit list and the parsed raw text.
        telegram_ids: list[int] = []
        seen: set[int] = set()

        for tid in request.telegram_ids or []:
            if isinstance(tid, int) and tid > 0 and tid not in seen:
                seen.add(tid)
                telegram_ids.append(tid)

        if request.raw_text:
            parsed = await bulk_ban_service.parse_telegram_ids_from_text(request.raw_text)
            for tid in parsed:
                if tid not in seen:
                    seen.add(tid)
                    telegram_ids.append(tid)

        if not telegram_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='No valid Telegram IDs provided.',
            )

        if len(telegram_ids) > MAX_BULK_BAN_IDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Too many Telegram IDs ({len(telegram_ids)}). Maximum is {MAX_BULK_BAN_IDS}.',
            )

        bot = _get_bot() if request.notify_users else None
        banned, not_found, error_ids = await bulk_ban_service.ban_users_by_telegram_ids(
            db=db,
            admin_user_id=admin.id,
            telegram_ids=telegram_ids,
            reason=request.reason,
            bot=bot,
            notify_admin=False,
            admin_name=admin.full_name,
        )

        requested = len(telegram_ids)
        success_rate = round((banned / requested) * 100, 1) if requested else 0.0

        logger.info(
            'Admin performed mass-ban',
            admin_id=admin.id,
            requested=requested,
            banned=banned,
            not_found=not_found,
            errors=len(error_ids),
        )

        return MassBanResponse(
            requested=requested,
            banned=banned,
            not_found=not_found,
            errors=len(error_ids),
            error_telegram_ids=error_ids,
            success_rate_percent=success_rate,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Mass-ban failed', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Mass-ban failed: {e!s}',
        )


# =============================================================================
# Blocked-users (bot blocker) scan & cleanup endpoints
# =============================================================================


@router.post('/blocked-users/scan', response_model=BlockedScanResponse)
async def scan_blocked_users(
    only_active: bool = True,
    admin: User = Depends(require_permission('blacklist:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Scan users to detect who has blocked the bot.

    This may take a while on large user bases (sends a typing action per user
    with concurrency limits). Returns the list of blocked users; the frontend
    echoes the returned items back to the cleanup endpoint.
    """
    try:
        service = BlockedUsersService(_get_bot())
        result = await service.scan_all_users(db, only_active=only_active)

        blocked_users = [
            BlockedUserItem(
                user_id=u.user_id,
                telegram_id=u.telegram_id,
                username=u.username,
                full_name=u.full_name,
                remnawave_uuid=u.remnawave_uuid,
                remnawave_uuids=u.remnawave_uuids,
            )
            for u in result.blocked_users
        ]

        logger.info(
            'Admin scanned for blocked users',
            admin_id=admin.id,
            total_checked=result.total_checked,
            blocked_count=result.blocked_count,
        )

        return BlockedScanResponse(
            total_checked=result.total_checked,
            blocked_count=result.blocked_count,
            active_users=result.active_users,
            errors=result.errors,
            skipped_no_telegram=result.skipped_no_telegram,
            scan_duration_seconds=round(result.scan_duration_seconds, 1),
            blocked_users=blocked_users,
        )
    except Exception as e:
        logger.error('Blocked-users scan failed', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Blocked-users scan failed: {e!s}',
        )


@router.post('/blocked-users/cleanup', response_model=BlockedCleanupResponse)
async def cleanup_blocked_users(
    request: BlockedCleanupRequest,
    admin: User = Depends(require_permission('blacklist:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Clean up a set of blocked users (delete from DB / Remnawave / mark).

    Destructive. The ``users`` list is expected to come from a prior
    ``/blocked-users/scan`` response.
    """
    try:
        action = BlockedUserAction(request.action.value)

        blocked_results = [
            BlockCheckResult(
                user_id=item.user_id,
                telegram_id=item.telegram_id,
                username=item.username,
                full_name=item.full_name or '',
                status=BlockCheckStatus.BLOCKED,
                remnawave_uuid=item.remnawave_uuid,
                remnawave_uuids=item.remnawave_uuids,
            )
            for item in request.users
        ]

        service = BlockedUsersService(_get_bot())
        result = await service.cleanup_blocked_users(db, blocked_results, action)

        logger.info(
            'Admin cleaned up blocked users',
            admin_id=admin.id,
            action=request.action.value,
            deleted_from_db=result.deleted_from_db,
            deleted_from_remnawave=result.deleted_from_remnawave,
            marked_as_blocked=result.marked_as_blocked,
            errors=len(result.errors),
        )

        return BlockedCleanupResponse(
            action=request.action,
            deleted_from_db=result.deleted_from_db,
            deleted_from_remnawave=result.deleted_from_remnawave,
            marked_as_blocked=result.marked_as_blocked,
            errors=result.errors,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Blocked-users cleanup failed', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Blocked-users cleanup failed: {e!s}',
        )
