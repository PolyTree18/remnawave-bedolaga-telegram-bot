"""Admin routes for managing the post-registration welcome text in the cabinet.

Ports the bot admin handler `app/handlers/admin/welcome_text.py` to the web
cabinet: read current text (with default fallback + enabled status), set/edit
the text (replicating the handler's HTML validation), toggle the enabled flag,
reset to the built-in default, list available placeholders, and render a
preview with placeholder substitution.

Business logic stays in `app.database.crud.welcome_text`; these endpoints are
thin authenticated wrappers. The HTML-tag validation mirrors the bot handler's
`validate_html_tags` so the cabinet enforces the same Telegram-HTML rules.
"""

import re

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.welcome_text import (
    get_available_placeholders,
    get_current_welcome_text_or_default,
    get_current_welcome_text_settings,
    replace_placeholders,
    set_welcome_text,
    toggle_welcome_text_status,
)
from app.database.models import User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/welcome-text', tags=['Cabinet Admin Welcome Text'])

# Telegram parse_mode="HTML" supported tags. Mirrors the bot handler whitelist.
_ALLOWED_HTML_TAGS = {
    'b',
    'strong',
    'i',
    'em',
    'u',
    'ins',
    's',
    'strike',
    'del',
    'code',
    'pre',
    'a',
}

_MIN_LENGTH = 10
_MAX_LENGTH = 4000

# Placeholder values used for the preview, matching the bot handler's TestUser.
_PREVIEW_FIRST_NAME = 'Иван'
_PREVIEW_USERNAME = 'test_user'


class _PreviewUser:
    """Lightweight user stand-in for placeholder substitution previews."""

    def __init__(self, first_name: str, username: str | None) -> None:
        self.first_name = first_name
        self.username = username


def validate_html_tags(text: str) -> tuple[bool, str | None]:
    """Validate Telegram-HTML markup in ``text``.

    Faithful port of the bot handler's ``validate_html_tags``: allows only the
    Telegram-supported tag whitelist, requires a valid ``href`` (http/https/tg
    scheme) on ``<a>`` tags, and checks tag pairing via a stack. Placeholders
    (``{...}``) are stripped before tag scanning so they are not mistaken for
    tags.

    Returns ``(True, None)`` on success or ``(False, <error message>)``.
    """
    placeholder_pattern = r'\{[^{}]+\}'
    clean_text = re.sub(placeholder_pattern, '', text)

    tag_pattern = r'<(/?)([a-zA-Z]+)(\s[^>]*)?>'
    tags_with_pos = [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(tag_pattern, clean_text)]

    # 1. Whitelist + <a href> validation.
    for closing, tag, attrs in tags_with_pos:
        tag_lower = tag.lower()

        if tag_lower not in _ALLOWED_HTML_TAGS:
            return (
                False,
                f'Неподдерживаемый HTML-тег: <{tag}>. Используйте только теги: {", ".join(sorted(_ALLOWED_HTML_TAGS))}',
            )

        if tag_lower == 'a':
            if closing:
                continue
            if not attrs or 'href=' not in attrs.lower():
                return False, "Тег <a> должен содержать атрибут href, например: <a href='URL'>ссылка</a>"

            href_match = re.search(r'href\s*=\s*[\'"]([^\'"]+)[\'"]', attrs, re.IGNORECASE)
            if href_match:
                url = href_match.group(1)
                if not re.match(r'^https?://|^tg://', url, re.IGNORECASE):
                    return False, f'URL в теге <a> должен начинаться с http://, https:// или tg://. Найдено: {url}'
            else:
                return False, 'Не удалось извлечь URL из атрибута href тега <a>'

    # 2. Tag pairing via stack.
    stack: list[str] = []
    for closing, tag, _attrs in tags_with_pos:
        tag_lower = tag.lower()
        if tag_lower not in _ALLOWED_HTML_TAGS:
            continue

        if closing:
            if not stack:
                return False, f'Лишний закрывающий тег: </{tag}>'
            last_opening_tag = stack.pop()
            if last_opening_tag.lower() != tag_lower:
                return False, f'Тег </{tag}> не соответствует открывающему тегу <{last_opening_tag}>'
        else:
            stack.append(tag)

    if stack:
        unclosed_tags = ', '.join([f'<{tag}>' for tag in stack])
        return False, f'Незакрытые теги: {unclosed_tags}'

    return True, None


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class PlaceholderInfo(BaseModel):
    """A single available placeholder and its human-readable description."""

    key: str
    description: str


class WelcomeTextResponse(BaseModel):
    """Current welcome-text settings.

    ``id`` is ``None`` when no row exists yet and the built-in default text is
    being shown; in that case ``is_default`` is ``True``.
    """

    id: int | None = None
    text: str
    is_enabled: bool
    is_default: bool
    placeholders: list[PlaceholderInfo]


class WelcomeTextUpdateRequest(BaseModel):
    """Request to set / replace the welcome text."""

    text: str = Field(..., min_length=_MIN_LENGTH, max_length=_MAX_LENGTH)


class WelcomeTextToggleResponse(BaseModel):
    """Response after toggling the enabled flag."""

    is_enabled: bool
    message: str


class WelcomeTextPreviewRequest(BaseModel):
    """Optional overrides for the preview sample user."""

    first_name: str | None = Field(None, max_length=255)
    username: str | None = Field(None, max_length=255)


class WelcomeTextPreviewResponse(BaseModel):
    """Rendered preview with placeholders substituted."""

    is_enabled: bool
    preview: str
    first_name: str
    username: str | None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _placeholder_list() -> list[PlaceholderInfo]:
    return [PlaceholderInfo(key=key, description=desc) for key, desc in get_available_placeholders().items()]


async def _build_response(db: AsyncSession) -> WelcomeTextResponse:
    settings = await get_current_welcome_text_settings(db)
    return WelcomeTextResponse(
        id=settings['id'],
        text=settings['text'],
        is_enabled=settings['is_enabled'],
        is_default=settings['id'] is None,
        placeholders=_placeholder_list(),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get('', response_model=WelcomeTextResponse)
async def get_welcome_text(
    admin: User = Depends(require_permission('welcome_text:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Return the current welcome text, enabled status and available placeholders.

    Falls back to the built-in default text (``is_default=True``, ``id=None``)
    when no welcome text has been configured yet.
    """
    try:
        return await _build_response(db)
    except Exception as e:
        logger.error('Failed to load welcome text', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load welcome text',
        )


@router.get('/placeholders', response_model=list[PlaceholderInfo])
async def list_placeholders(
    admin: User = Depends(require_permission('welcome_text:read')),
):
    """Return the list of placeholders that can be used in the welcome text."""
    try:
        return _placeholder_list()
    except Exception as e:
        logger.error('Failed to list welcome-text placeholders', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to list placeholders',
        )


@router.put('', response_model=WelcomeTextResponse)
async def update_welcome_text(
    request: WelcomeTextUpdateRequest,
    admin: User = Depends(require_permission('welcome_text:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Set / replace the welcome text.

    Replicates the bot handler validation: 10-4000 chars and Telegram-HTML tag
    rules (whitelist, ``<a href>`` scheme, tag pairing). The enabled status is
    preserved by the underlying service.
    """
    text = request.text.strip()

    if len(text) < _MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Текст слишком короткий! Минимум {_MIN_LENGTH} символов.',
        )
    if len(text) > _MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Текст слишком длинный! Максимум {_MAX_LENGTH} символов.',
        )

    is_valid, error_msg = validate_html_tags(text)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Ошибка в HTML-разметке: {error_msg}',
        )

    try:
        success = await set_welcome_text(db, text, admin.id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Ошибка при сохранении текста. Попробуйте еще раз.',
            )

        logger.info('Admin updated welcome text', admin_id=admin.id)
        return await _build_response(db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update welcome text', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update welcome text',
        )


@router.post('/toggle', response_model=WelcomeTextToggleResponse)
async def toggle_welcome_text(
    admin: User = Depends(require_permission('welcome_text:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle whether the welcome text is sent to newly registered users.

    If no welcome text exists yet, the service creates one from the default and
    enables it.
    """
    try:
        new_status = await toggle_welcome_text_status(db, admin.id)

        status_text = 'enabled' if new_status else 'disabled'
        logger.info('Admin toggled welcome text', admin_id=admin.id, status_text=status_text)

        return WelcomeTextToggleResponse(
            is_enabled=new_status,
            message=f'Welcome text {status_text}',
        )
    except Exception as e:
        logger.error('Failed to toggle welcome text', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle welcome text',
        )


@router.post('/reset', response_model=WelcomeTextResponse)
async def reset_welcome_text(
    admin: User = Depends(require_permission('welcome_text:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Reset the welcome text to the built-in default."""
    try:
        default_text = await get_current_welcome_text_or_default()
        success = await set_welcome_text(db, default_text, admin.id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Ошибка при сбросе текста. Попробуйте еще раз.',
            )

        logger.info('Admin reset welcome text to default', admin_id=admin.id)
        return await _build_response(db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to reset welcome text', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to reset welcome text',
        )


@router.post('/preview', response_model=WelcomeTextPreviewResponse)
async def preview_welcome_text(
    request: WelcomeTextPreviewRequest | None = None,
    admin: User = Depends(require_permission('welcome_text:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Render a preview of the current welcome text with placeholders substituted.

    Uses the default sample user (``Иван`` / ``@test_user``) like the bot
    preview, unless overrides are supplied in the request body. If welcome
    messages are disabled the preview text is empty and ``is_enabled`` is
    ``False``.
    """
    try:
        first_name = (request.first_name if request else None) or _PREVIEW_FIRST_NAME
        username = (request.username if request else None) or _PREVIEW_USERNAME

        settings = await get_current_welcome_text_settings(db)
        is_enabled = settings['is_enabled']

        preview_text = ''
        if is_enabled:
            preview_text = replace_placeholders(settings['text'], _PreviewUser(first_name, username))

        return WelcomeTextPreviewResponse(
            is_enabled=is_enabled,
            preview=preview_text,
            first_name=first_name,
            username=username,
        )
    except Exception as e:
        logger.error('Failed to render welcome-text preview', error=e, admin_id=admin.id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to render preview',
        )
