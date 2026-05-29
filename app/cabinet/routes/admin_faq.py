"""Admin routes for managing the bot's FAQ (faq_pages + faq_settings tables).

This manages the BOT's FAQ system (per-language pages shown in the «Инфо»
section), NOT the cabinet's separate `info_pages` table. The two are
currently disconnected; consolidating them is a future architecture
decision.

All business logic lives in `FaqService` (the same service the Telegram
bot handler uses); these endpoints are thin authenticated wrappers.
"""

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import FaqPage, User
from app.services.faq_service import FaqService
from app.utils.validators import get_html_help_text, validate_html_tags

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/faq', tags=['Cabinet Admin FAQ'])

TITLE_MAX_LENGTH = 255
CONTENT_MAX_LENGTH = 6000


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class FaqPageResponse(BaseModel):
    """A single FAQ page."""

    id: int
    language: str
    title: str
    content: str
    display_order: int
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class FaqOverviewResponse(BaseModel):
    """Per-language FAQ overview: global toggle state, stats and all pages."""

    language: str
    is_enabled: bool  # global show/hide toggle for this language
    has_setting: bool  # whether a faq_settings row exists for this language
    total_pages: int
    active_pages: int
    pages: list[FaqPageResponse]


class FaqGlobalToggleRequest(BaseModel):
    """Explicitly set the global FAQ enabled state for a language."""

    enabled: bool


class FaqGlobalToggleResponse(BaseModel):
    """Response after changing the global FAQ enabled state."""

    language: str
    is_enabled: bool


class FaqPageCreateRequest(BaseModel):
    """Create a new FAQ page for a language."""

    title: str = Field(..., min_length=1, max_length=TITLE_MAX_LENGTH)
    content: str = Field(..., min_length=1, max_length=CONTENT_MAX_LENGTH)
    is_active: bool = True


class FaqPageUpdateRequest(BaseModel):
    """Patch a FAQ page. Only provided fields are changed."""

    title: str | None = Field(None, min_length=1, max_length=TITLE_MAX_LENGTH)
    content: str | None = Field(None, min_length=1, max_length=CONTENT_MAX_LENGTH)
    is_active: bool | None = None


class FaqPageTogglePayload(BaseModel):
    """Response after toggling a page's active flag."""

    page: FaqPageResponse


class FaqMoveDirection(BaseModel):
    """Direction for a reorder move."""

    direction: str = Field(..., pattern='^(up|down)$')


class FaqHtmlHelpResponse(BaseModel):
    """HTML formatting help text (mirrors the bot's help screen)."""

    help_html: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_page_response(page: FaqPage) -> FaqPageResponse:
    return FaqPageResponse(
        id=page.id,
        language=page.language,
        title=page.title,
        content=page.content,
        display_order=page.display_order,
        is_active=page.is_active,
        created_at=getattr(page, 'created_at', None),
        updated_at=getattr(page, 'updated_at', None),
    )


def _validate_title(title: str) -> str:
    """Replicate the bot handler's title validation. Returns the trimmed title."""
    trimmed = (title or '').strip()
    if not trimmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Заголовок не может быть пустым.',
        )
    if len(trimmed) > TITLE_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Заголовок слишком длинный. Максимум {TITLE_MAX_LENGTH} символов.',
        )
    return trimmed


def _validate_content(content: str) -> str:
    """Replicate the bot handler's content validation (length + HTML tags)."""
    raw = content or ''
    if len(raw) > CONTENT_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Текст слишком длинный. Максимум {CONTENT_MAX_LENGTH} символов.',
        )
    if not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Текст не может быть пустым.',
        )
    is_valid, error_message = validate_html_tags(raw)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Ошибка в HTML: {error_message}',
        )
    return raw


async def _load_admin_page(db: AsyncSession, page_id: int, language: str) -> FaqPage:
    """Load a page for admin editing (active + inactive, no language fallback)."""
    page = await FaqService.get_page(
        db,
        page_id,
        language,
        fallback=False,
        include_inactive=True,
    )
    if not page:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='FAQ page not found',
        )
    return page


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@router.get('/html-help', response_model=FaqHtmlHelpResponse)
async def faq_html_help(
    admin: User = Depends(require_permission('faq:read')),
):
    """Get the HTML formatting help text (same content the bot shows)."""
    try:
        return FaqHtmlHelpResponse(help_html=get_html_help_text())
    except Exception as exc:
        logger.error('Failed to build FAQ HTML help', error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load HTML help: {exc!s}',
        )


@router.get('', response_model=FaqOverviewResponse)
async def get_faq_overview(
    language: str,
    admin: User = Depends(require_permission('faq:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the FAQ overview for a language: toggle state, stats and all pages."""
    try:
        normalized = FaqService.normalize_language(language)

        setting = await FaqService.get_setting(db, language, fallback=False)
        pages = await FaqService.get_pages(
            db,
            language,
            include_inactive=True,
            fallback=False,
        )

        page_items = [_to_page_response(page) for page in pages]
        active_pages = sum(1 for page in pages if page.is_active)

        # Mirror the bot's semantics: if no setting row exists yet, FAQ is
        # considered enabled by default (creating the first page enables it).
        is_enabled = setting.is_enabled if setting else True

        return FaqOverviewResponse(
            language=normalized,
            is_enabled=is_enabled,
            has_setting=setting is not None,
            total_pages=len(page_items),
            active_pages=active_pages,
            pages=page_items,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to load FAQ overview', language=language, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load FAQ overview: {exc!s}',
        )


@router.get('/pages/{page_id}', response_model=FaqPageResponse)
async def get_faq_page(
    page_id: int,
    language: str,
    admin: User = Depends(require_permission('faq:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single FAQ page (active or inactive) by id."""
    try:
        page = await _load_admin_page(db, page_id, language)
        return _to_page_response(page)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to load FAQ page', page_id=page_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load FAQ page: {exc!s}',
        )


# ---------------------------------------------------------------------------
# Global toggle (per language)
# ---------------------------------------------------------------------------


@router.post('/toggle', response_model=FaqGlobalToggleResponse)
async def toggle_faq_global(
    language: str,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle the global FAQ show/hide state for a language."""
    try:
        setting = await FaqService.toggle_enabled(db, language)
        logger.info(
            'Admin toggled FAQ visibility',
            admin_id=admin.id,
            language=setting.language,
            is_enabled=setting.is_enabled,
        )
        return FaqGlobalToggleResponse(
            language=setting.language,
            is_enabled=setting.is_enabled,
        )
    except Exception as exc:
        logger.error('Failed to toggle FAQ visibility', language=language, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to toggle FAQ: {exc!s}',
        )


@router.put('/enabled', response_model=FaqGlobalToggleResponse)
async def set_faq_global_enabled(
    language: str,
    request: FaqGlobalToggleRequest,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Explicitly set the global FAQ enabled state for a language."""
    try:
        setting = await FaqService.set_enabled(db, language, request.enabled)
        logger.info(
            'Admin set FAQ visibility',
            admin_id=admin.id,
            language=setting.language,
            is_enabled=setting.is_enabled,
        )
        return FaqGlobalToggleResponse(
            language=setting.language,
            is_enabled=setting.is_enabled,
        )
    except Exception as exc:
        logger.error('Failed to set FAQ visibility', language=language, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to set FAQ visibility: {exc!s}',
        )


# ---------------------------------------------------------------------------
# Page CRUD
# ---------------------------------------------------------------------------


@router.post('/pages', response_model=FaqPageResponse, status_code=status.HTTP_201_CREATED)
async def create_faq_page(
    language: str,
    request: FaqPageCreateRequest,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new FAQ page for a language (HTML content is validated)."""
    try:
        title = _validate_title(request.title)
        content = _validate_content(request.content)

        page = await FaqService.create_page(
            db,
            language=language,
            title=title,
            content=content,
            is_active=request.is_active,
        )
        logger.info(
            'Admin created FAQ page',
            admin_id=admin.id,
            page_id=page.id,
            language=page.language,
            content_length=len(content),
        )
        return _to_page_response(page)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to create FAQ page', language=language, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to create FAQ page: {exc!s}',
        )


@router.put('/pages/{page_id}', response_model=FaqPageResponse)
async def update_faq_page(
    page_id: int,
    language: str,
    request: FaqPageUpdateRequest,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Edit a FAQ page's title and/or content and/or active flag."""
    try:
        page = await _load_admin_page(db, page_id, language)

        title: str | None = None
        content: str | None = None

        if request.title is not None:
            title = _validate_title(request.title)
        if request.content is not None:
            content = _validate_content(request.content)

        if title is None and content is None and request.is_active is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='No fields to update.',
            )

        updated = await FaqService.update_page(
            db,
            page,
            title=title,
            content=content,
            is_active=request.is_active,
        )
        logger.info(
            'Admin updated FAQ page',
            admin_id=admin.id,
            page_id=updated.id,
            language=updated.language,
        )
        return _to_page_response(updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to update FAQ page', page_id=page_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update FAQ page: {exc!s}',
        )


@router.post('/pages/{page_id}/toggle', response_model=FaqPageTogglePayload)
async def toggle_faq_page(
    page_id: int,
    language: str,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle a single page's active flag."""
    try:
        page = await _load_admin_page(db, page_id, language)
        updated = await FaqService.update_page(db, page, is_active=not page.is_active)
        logger.info(
            'Admin toggled FAQ page',
            admin_id=admin.id,
            page_id=updated.id,
            is_active=updated.is_active,
        )
        return FaqPageTogglePayload(page=_to_page_response(updated))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to toggle FAQ page', page_id=page_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to toggle FAQ page: {exc!s}',
        )


@router.delete('/pages/{page_id}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_faq_page(
    page_id: int,
    language: str,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a FAQ page and compact the remaining pages' display order."""
    try:
        page = await _load_admin_page(db, page_id, language)
        page_language = page.language

        await FaqService.delete_page(db, page.id)

        # Replicate the bot handler: re-sequence the remaining pages so
        # display_order stays gap-free (1..N).
        remaining = await FaqService.get_pages(
            db,
            page_language,
            include_inactive=True,
            fallback=False,
        )
        if remaining:
            remaining_sorted = sorted(remaining, key=lambda item: (item.display_order, item.id))
            await FaqService.reorder_pages(db, page_language, remaining_sorted)

        logger.info(
            'Admin deleted FAQ page',
            admin_id=admin.id,
            page_id=page_id,
            language=page_language,
        )
        return
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to delete FAQ page', page_id=page_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete FAQ page: {exc!s}',
        )


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@router.post('/pages/{page_id}/move', response_model=FaqOverviewResponse)
async def move_faq_page(
    page_id: int,
    language: str,
    request: FaqMoveDirection,
    admin: User = Depends(require_permission('faq:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Move a page up or down in display order (swap with its neighbor)."""
    try:
        page_language = FaqService.normalize_language(language)

        pages = await FaqService.get_pages(
            db,
            language,
            include_inactive=True,
            fallback=False,
        )
        if not pages:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='No FAQ pages found for this language',
            )

        pages_sorted = sorted(pages, key=lambda item: (item.display_order, item.id))
        index = next((i for i, p in enumerate(pages_sorted) if p.id == page_id), None)
        if index is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='FAQ page not found',
            )

        if request.direction == 'up' and index > 0:
            pages_sorted[index - 1], pages_sorted[index] = pages_sorted[index], pages_sorted[index - 1]
        elif request.direction == 'down' and index < len(pages_sorted) - 1:
            pages_sorted[index + 1], pages_sorted[index] = pages_sorted[index], pages_sorted[index + 1]
        else:
            # Already at the boundary; nothing to do, return current state.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Page is already at the edge; cannot move further.',
            )

        await FaqService.reorder_pages(db, language, pages_sorted)

        logger.info(
            'Admin reordered FAQ page',
            admin_id=admin.id,
            page_id=page_id,
            direction=request.direction,
            language=page_language,
        )

        # Return the fresh overview so the frontend can re-render the list.
        refreshed = await FaqService.get_pages(
            db,
            language,
            include_inactive=True,
            fallback=False,
        )
        setting = await FaqService.get_setting(db, language, fallback=False)
        page_items = [_to_page_response(p) for p in refreshed]
        return FaqOverviewResponse(
            language=page_language,
            is_enabled=setting.is_enabled if setting else True,
            has_setting=setting is not None,
            total_pages=len(page_items),
            active_pages=sum(1 for p in refreshed if p.is_active),
            pages=page_items,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to move FAQ page', page_id=page_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to move FAQ page: {exc!s}',
        )
