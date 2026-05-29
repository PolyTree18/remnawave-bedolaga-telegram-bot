"""Admin routes for managing user main-menu messages in cabinet.

These messages are shown to users in the bot main menu (between the
subscription info and the action buttons). Active messages are picked at
random; inactive ones are hidden. This module is a thin authenticated
wrapper over the shared ``app.database.crud.user_message`` CRUD that the
bot admin handlers use, so business logic stays in one place.
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user_message import (
    create_user_message,
    delete_user_message,
    get_all_user_messages,
    get_user_message_by_id,
    get_user_messages_count,
    get_user_messages_stats,
    toggle_user_message_status,
    update_user_message,
)
from app.database.models import User, UserMessage
from app.utils.validators import sanitize_html, validate_html_tags

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/user-messages', tags=['Cabinet Admin User Messages'])

MAX_MESSAGE_LENGTH = 4000


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class UserMessageItem(BaseModel):
    """A single user main-menu message."""

    id: int
    message_text: str
    safe_html: str  # sanitized HTML safe to render in the UI preview
    is_active: bool
    sort_order: int
    created_by: int | None = None
    created_at: object | None = None  # datetime, serialized by FastAPI
    updated_at: object | None = None

    class Config:
        from_attributes = True


class UserMessageListResponse(BaseModel):
    """Paginated list of user messages."""

    items: list[UserMessageItem]
    total: int
    offset: int
    limit: int


class UserMessageStatsResponse(BaseModel):
    """Aggregate counts of user messages."""

    total_messages: int
    active_messages: int
    inactive_messages: int


class UserMessageCreateRequest(BaseModel):
    """Request body to create a new user message."""

    message_text: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)
    is_active: bool = True
    sort_order: int = Field(0, ge=0)


class UserMessageUpdateRequest(BaseModel):
    """Request body to edit an existing user message's text and/or order."""

    message_text: str | None = Field(None, min_length=1, max_length=MAX_MESSAGE_LENGTH)
    sort_order: int | None = Field(None, ge=0)


class UserMessageToggleResponse(BaseModel):
    """Response after toggling a message's active state."""

    id: int
    is_active: bool
    message: str


class UserMessageDeleteResponse(BaseModel):
    """Response after deleting a message."""

    id: int
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(message: UserMessage) -> UserMessageItem:
    """Build the API representation of a message, including sanitized HTML."""
    return UserMessageItem(
        id=message.id,
        message_text=message.message_text,
        safe_html=sanitize_html(message.message_text),
        is_active=bool(message.is_active),
        sort_order=message.sort_order or 0,
        created_by=message.created_by,
        created_at=message.created_at,
        updated_at=message.updated_at,
    )


def _validate_message_text(text: str) -> str:
    """Validate length + HTML markup at the API boundary.

    Raises HTTPException(422) on invalid input. Returns the trimmed text.
    """
    trimmed = text.strip()
    if not trimmed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Message text cannot be empty',
        )
    if len(trimmed) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Message is too long: {len(trimmed)} characters (max {MAX_MESSAGE_LENGTH})',
        )
    is_valid, error_message = validate_html_tags(trimmed)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Invalid HTML markup: {error_message}',
        )
    return trimmed


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get('', response_model=UserMessageListResponse)
async def list_user_messages(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    include_inactive: bool = Query(True, description='Include inactive messages in the list'),
    admin: User = Depends(require_permission('user_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a paginated list of user main-menu messages."""
    try:
        messages = await get_all_user_messages(
            db,
            offset=offset,
            limit=limit,
            include_inactive=include_inactive,
        )
        total = await get_user_messages_count(db, include_inactive=include_inactive)

        return UserMessageListResponse(
            items=[_serialize(msg) for msg in messages],
            total=total,
            offset=offset,
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to list user messages', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load user messages',
        )


@router.get('/stats', response_model=UserMessageStatsResponse)
async def get_user_messages_statistics(
    admin: User = Depends(require_permission('user_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get aggregate counts: total / active / inactive."""
    try:
        stats = await get_user_messages_stats(db)
        return UserMessageStatsResponse(
            total_messages=stats['total_messages'],
            active_messages=stats['active_messages'],
            inactive_messages=stats['inactive_messages'],
        )
    except Exception as e:
        logger.error('Failed to load user messages stats', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load user messages statistics',
        )


@router.get('/{message_id}', response_model=UserMessageItem)
async def get_single_user_message(
    message_id: int,
    admin: User = Depends(require_permission('user_messages:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single user message by ID."""
    try:
        message = await get_user_message_by_id(db, message_id)
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Message not found',
            )
        return _serialize(message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to load user message', admin_id=admin.id, message_id=message_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load user message',
        )


@router.post('', response_model=UserMessageItem, status_code=status.HTTP_201_CREATED)
async def create_new_user_message(
    request: UserMessageCreateRequest,
    admin: User = Depends(require_permission('user_messages:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new user main-menu message (HTML-validated, max 4000 chars)."""
    text = _validate_message_text(request.message_text)
    try:
        message = await create_user_message(
            db=db,
            message_text=text,
            created_by=admin.id,
            is_active=request.is_active,
            sort_order=request.sort_order,
        )
        logger.info('Admin created user message', admin_id=admin.id, message_id=message.id)
        return _serialize(message)
    except ValueError as e:
        # CRUD re-validates HTML and raises ValueError on bad markup
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Invalid HTML markup: {e!s}',
        )
    except Exception as e:
        logger.error('Failed to create user message', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to create user message',
        )


@router.put('/{message_id}', response_model=UserMessageItem)
async def edit_user_message(
    message_id: int,
    request: UserMessageUpdateRequest,
    admin: User = Depends(require_permission('user_messages:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Edit a message's text and/or sort order (HTML-validated, max 4000 chars)."""
    if request.message_text is None and request.sort_order is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Nothing to update: provide message_text and/or sort_order',
        )

    text: str | None = None
    if request.message_text is not None:
        text = _validate_message_text(request.message_text)

    try:
        existing = await get_user_message_by_id(db, message_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Message not found',
            )

        updated = await update_user_message(
            db=db,
            message_id=message_id,
            message_text=text,
            sort_order=request.sort_order,
        )
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Message not found',
            )

        logger.info('Admin edited user message', admin_id=admin.id, message_id=message_id)
        return _serialize(updated)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Invalid HTML markup: {e!s}',
        )
    except Exception as e:
        logger.error('Failed to edit user message', admin_id=admin.id, message_id=message_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to edit user message',
        )


@router.post('/{message_id}/toggle', response_model=UserMessageToggleResponse)
async def toggle_user_message_active(
    message_id: int,
    admin: User = Depends(require_permission('user_messages:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle a message's active state (active <-> inactive)."""
    try:
        message = await toggle_user_message_status(db, message_id)
        if not message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Message not found',
            )

        state = 'activated' if message.is_active else 'deactivated'
        logger.info('Admin toggled user message', admin_id=admin.id, message_id=message_id, is_active=message.is_active)
        return UserMessageToggleResponse(
            id=message.id,
            is_active=bool(message.is_active),
            message=f'Message {state}',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to toggle user message', admin_id=admin.id, message_id=message_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle user message',
        )


@router.delete('/{message_id}', response_model=UserMessageDeleteResponse)
async def remove_user_message(
    message_id: int,
    admin: User = Depends(require_permission('user_messages:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a user message permanently."""
    try:
        success = await delete_user_message(db, message_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Message not found',
            )

        logger.info('Admin deleted user message', admin_id=admin.id, message_id=message_id)
        return UserMessageDeleteResponse(
            id=message_id,
            message='Message deleted',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to delete user message', admin_id=admin.id, message_id=message_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete user message',
        )
