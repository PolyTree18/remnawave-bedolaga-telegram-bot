"""Admin routes for managing legal documents (public offer, privacy policy, service rules) in cabinet.

Ports the bot-admin features from:
- app/handlers/admin/public_offer.py   -> PublicOfferService
- app/handlers/admin/privacy_policy.py -> PrivacyPolicyService
- app/handlers/admin/rules.py          -> app.database.crud.rules

Each document exposes, where applicable:
- GET current content + enabled status + metadata
- PUT to edit text (HTML-validated, 4000-char cap)
- POST toggle visibility (offer / privacy only)
- POST clear / reset-to-default (rules only)

This file deliberately does NOT collide with admin_policies.py, which is
RBAC access-control (unrelated to these public-facing legal documents).
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.rules import (
    clear_all_rules,
    create_or_update_rules,
    get_current_rules_content,
    get_rules_by_language,
)
from app.database.models import User
from app.services.privacy_policy_service import PrivacyPolicyService
from app.services.public_offer_service import PublicOfferService
from app.utils.validators import validate_html_tags

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/legal-documents', tags=['Cabinet Admin Legal Documents'])

# Mirror the bot handlers: HTML-formatted text capped at 4000 characters.
MAX_CONTENT_LENGTH = 4000


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LegalDocumentResponse(BaseModel):
    """Public offer / privacy policy current state."""

    document: str = Field(description="Document key: 'offer' or 'privacy'")
    language: str
    content: str = Field(default='', description='Raw HTML content (empty if never set)')
    has_content: bool = Field(description='True if non-empty content exists')
    is_enabled: bool = Field(description='Whether the document is shown to users')
    is_visible_to_users: bool = Field(
        description='True only when enabled AND non-empty (what users actually see)',
    )
    updated_at: str | None = Field(default=None, description='ISO-8601 timestamp of last update')


class RulesDocumentResponse(BaseModel):
    """Service rules current state.

    Rules have no per-document enable flag (unlike offer/privacy); when no
    custom rules exist, a built-in default text is returned and `is_custom`
    is False.
    """

    document: str = Field(default='rules')
    language: str
    content: str = Field(description='Effective rules HTML (custom text, or built-in default)')
    is_custom: bool = Field(description='True if admin-defined rules exist; False means default text')
    updated_at: str | None = Field(default=None, description='ISO-8601 timestamp of last update (custom only)')


class DocumentUpdateRequest(BaseModel):
    """Request to set/replace a document's text."""

    content: str = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)


class DocumentToggleResponse(BaseModel):
    """Response after toggling visibility (offer / privacy)."""

    document: str
    language: str
    is_enabled: bool
    message: str


class RulesClearResponse(BaseModel):
    """Response after clearing/resetting rules to default."""

    document: str = Field(default='rules')
    language: str
    cleared: bool = Field(description='True if any custom rules were removed')
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_language(language: str | None) -> str:
    """Normalize the requested language, defaulting to the configured default."""
    return PublicOfferService.normalize_language(language or settings.DEFAULT_LANGUAGE)


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        return value.isoformat()
    except Exception:
        return None


def _validate_content(content: str) -> None:
    """Apply the same guards the bot applies before saving."""
    if len(content) > MAX_CONTENT_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Content too long. Maximum {MAX_CONTENT_LENGTH} characters.',
        )

    is_valid, error_message = validate_html_tags(content)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid HTML: {error_message}',
        )


# ---------------------------------------------------------------------------
# Public offer
# ---------------------------------------------------------------------------


@router.get('/offer', response_model=LegalDocumentResponse)
async def get_offer(
    language: str | None = Query(default=None, description='Language code (defaults to bot default)'),
    admin: User = Depends(require_permission('legal:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the current public offer text + enabled status."""
    try:
        lang = _resolve_language(language)
        offer = await PublicOfferService.get_offer(db, lang, fallback=False)

        content = (offer.content if offer and offer.content else '') or ''
        has_content = bool(content.strip())
        is_enabled = bool(offer.is_enabled) if offer else False

        return LegalDocumentResponse(
            document='offer',
            language=lang,
            content=content,
            has_content=has_content,
            is_enabled=is_enabled,
            is_visible_to_users=is_enabled and has_content,
            updated_at=_iso(getattr(offer, 'updated_at', None)),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to load public offer', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load public offer',
        )


@router.put('/offer', response_model=LegalDocumentResponse)
async def update_offer(
    request: DocumentUpdateRequest,
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Replace the public offer text (HTML-validated, 4000-char cap)."""
    try:
        lang = _resolve_language(language)
        _validate_content(request.content)

        await PublicOfferService.save_offer(db, lang, request.content)
        logger.info(
            'Admin updated public offer',
            admin_id=admin.id,
            language=lang,
            content_length=len(request.content),
        )

        return await get_offer(language=lang, admin=admin, db=db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update public offer', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update public offer',
        )


@router.post('/offer/toggle', response_model=DocumentToggleResponse)
async def toggle_offer(
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle public offer visibility for users."""
    try:
        lang = _resolve_language(language)
        updated = await PublicOfferService.toggle_enabled(db, lang)

        logger.info(
            'Admin toggled public offer',
            admin_id=admin.id,
            language=lang,
            is_enabled=updated.is_enabled,
        )

        return DocumentToggleResponse(
            document='offer',
            language=lang,
            is_enabled=bool(updated.is_enabled),
            message='Public offer enabled' if updated.is_enabled else 'Public offer disabled',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to toggle public offer', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle public offer',
        )


# ---------------------------------------------------------------------------
# Privacy policy
# ---------------------------------------------------------------------------


@router.get('/privacy', response_model=LegalDocumentResponse)
async def get_privacy(
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the current privacy policy text + enabled status."""
    try:
        lang = _resolve_language(language)
        policy = await PrivacyPolicyService.get_policy(db, lang, fallback=False)

        content = (policy.content if policy and policy.content else '') or ''
        has_content = bool(content.strip())
        is_enabled = bool(policy.is_enabled) if policy else False

        return LegalDocumentResponse(
            document='privacy',
            language=lang,
            content=content,
            has_content=has_content,
            is_enabled=is_enabled,
            is_visible_to_users=is_enabled and has_content,
            updated_at=_iso(getattr(policy, 'updated_at', None)),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to load privacy policy', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load privacy policy',
        )


@router.put('/privacy', response_model=LegalDocumentResponse)
async def update_privacy(
    request: DocumentUpdateRequest,
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Replace the privacy policy text (HTML-validated, 4000-char cap)."""
    try:
        lang = _resolve_language(language)
        _validate_content(request.content)

        await PrivacyPolicyService.save_policy(db, lang, request.content)
        logger.info(
            'Admin updated privacy policy',
            admin_id=admin.id,
            language=lang,
            content_length=len(request.content),
        )

        return await get_privacy(language=lang, admin=admin, db=db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update privacy policy', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update privacy policy',
        )


@router.post('/privacy/toggle', response_model=DocumentToggleResponse)
async def toggle_privacy(
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle privacy policy visibility for users."""
    try:
        lang = _resolve_language(language)
        updated = await PrivacyPolicyService.toggle_enabled(db, lang)

        logger.info(
            'Admin toggled privacy policy',
            admin_id=admin.id,
            language=lang,
            is_enabled=updated.is_enabled,
        )

        return DocumentToggleResponse(
            document='privacy',
            language=lang,
            is_enabled=bool(updated.is_enabled),
            message='Privacy policy enabled' if updated.is_enabled else 'Privacy policy disabled',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to toggle privacy policy', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to toggle privacy policy',
        )


# ---------------------------------------------------------------------------
# Service rules
# ---------------------------------------------------------------------------


@router.get('/rules', response_model=RulesDocumentResponse)
async def get_rules(
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the effective service rules (custom text, or built-in default)."""
    try:
        lang = _resolve_language(language)
        # Effective content used by the bot (falls back to a default template).
        content = await get_current_rules_content(db, lang)
        # Distinguish admin-defined rules from the built-in default.
        custom = await get_rules_by_language(db, lang)

        return RulesDocumentResponse(
            document='rules',
            language=lang,
            content=content,
            is_custom=custom is not None,
            updated_at=_iso(getattr(custom, 'updated_at', None)) if custom else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to load service rules', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load service rules',
        )


@router.put('/rules', response_model=RulesDocumentResponse)
async def update_rules(
    request: DocumentUpdateRequest,
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Replace the service rules text (HTML-validated, 4000-char cap)."""
    try:
        lang = _resolve_language(language)
        _validate_content(request.content)

        await create_or_update_rules(db=db, content=request.content, language=lang)

        # Refresh the localization cache exactly like the bot handler does.
        from app.localization.texts import clear_rules_cache, refresh_rules_cache

        clear_rules_cache()
        await refresh_rules_cache(lang)

        logger.info(
            'Admin updated service rules',
            admin_id=admin.id,
            language=lang,
            content_length=len(request.content),
        )

        return await get_rules(language=lang, admin=admin, db=db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update service rules', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to update service rules',
        )


@router.post('/rules/clear', response_model=RulesClearResponse)
async def clear_rules(
    language: str | None = Query(default=None),
    admin: User = Depends(require_permission('legal:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Clear custom service rules and reset to the built-in default text."""
    try:
        lang = _resolve_language(language)
        cleared = await clear_all_rules(db, lang)

        from app.localization.texts import clear_rules_cache

        clear_rules_cache()

        logger.info(
            'Admin cleared service rules',
            admin_id=admin.id,
            language=lang,
            cleared=cleared,
        )

        return RulesClearResponse(
            document='rules',
            language=lang,
            cleared=bool(cleared),
            message=(
                'Service rules cleared; users now see the default rules'
                if cleared
                else 'No custom rules to clear; default rules already in effect'
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to clear service rules', error=e, language=language)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to clear service rules',
        )
