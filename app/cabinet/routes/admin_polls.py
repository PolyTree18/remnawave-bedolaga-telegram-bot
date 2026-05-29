"""Admin routes for managing polls in the cabinet.

Thin authenticated wrappers around the same poll CRUD / service the bot uses
(`app.database.crud.poll`, `app.services.poll_service`) plus the broadcast
audience-segment helpers (`app.handlers.admin.messages`). Business logic stays
in those modules — these endpoints only validate input, enforce RBAC and shape
the response for the web cabinet.

A separate user-facing router lives in ``polls.py`` (participation only); this
file is the admin catalog and is mounted under ``/admin/polls``.
"""

import structlog
from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.poll import (
    create_poll,
    delete_poll,
    get_poll_by_id,
    get_poll_statistics,
    list_polls,
)
from app.database.models import Poll, User
from app.handlers.admin.messages import (
    get_custom_users,
    get_custom_users_count,
    get_target_display_name,
    get_target_users,
    get_target_users_count,
)
from app.services.poll_service import send_poll_to_users
from app.utils.validators import validate_html_tags

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/polls', tags=['Cabinet Admin Polls'])


# Audience segments understood by app.handlers.admin.messages helpers.
# Standard targets resolve via get_target_users / get_target_users_count.
_STANDARD_TARGETS: frozenset[str] = frozenset(
    {
        'all',
        'active',
        'trial',
        'no',
        'expiring',
        'expired',
        'active_zero',
        'trial_zero',
        'zero',
    }
)
# Custom criteria resolve via get_custom_users / get_custom_users_count.
_CUSTOM_CRITERIA: frozenset[str] = frozenset(
    {
        'today',
        'week',
        'month',
        'active_today',
        'inactive_week',
        'inactive_month',
        'referrals',
        'direct',
    }
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PollOptionInput(BaseModel):
    """A single answer option within a question (admin create payload)."""

    text: str = Field(..., min_length=1, max_length=500)


class PollQuestionInput(BaseModel):
    """A question with its ordered answer options (admin create payload)."""

    text: str = Field(..., min_length=1, max_length=1000)
    options: list[str] = Field(..., min_length=2)


class PollCreateRequest(BaseModel):
    """Request to create a new poll with ordered questions/options."""

    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(
        None,
        description='Optional HTML description. Validated against the same allow-list the bot uses.',
    )
    reward_enabled: bool = False
    reward_amount_kopeks: int = Field(0, ge=0, description='Reward in kopeks; ignored when reward_enabled is false.')
    questions: list[PollQuestionInput] = Field(..., min_length=1)


class PollOptionOut(BaseModel):
    """Answer option in a poll structure response."""

    id: int
    text: str
    order: int


class PollQuestionOut(BaseModel):
    """Question (with ordered options) in a poll structure response."""

    id: int
    text: str
    order: int
    options: list[PollOptionOut]


class PollListItem(BaseModel):
    """Poll summary for the admin catalog list."""

    id: int
    title: str
    description: str | None
    reward_enabled: bool
    reward_amount_kopeks: int
    questions_count: int
    created_by: int | None
    created_at: str
    updated_at: str | None = None


class PollListResponse(BaseModel):
    """Response with the full poll catalog (newest first)."""

    polls: list[PollListItem]
    total: int


class PollDetailResponse(BaseModel):
    """Full poll structure: meta + ordered questions and options."""

    id: int
    title: str
    description: str | None
    reward_enabled: bool
    reward_amount_kopeks: int
    created_by: int | None
    created_at: str
    updated_at: str | None = None
    questions: list[PollQuestionOut]


class PollStatsOptionOut(BaseModel):
    """Per-option answer tally in poll statistics."""

    id: int
    text: str
    count: int


class PollStatsQuestionOut(BaseModel):
    """Per-question breakdown in poll statistics."""

    id: int
    text: str
    order: int
    options: list[PollStatsOptionOut]


class PollStatsResponse(BaseModel):
    """Aggregated poll statistics (invited / completed / reward paid)."""

    poll_id: int
    total_responses: int
    completed_responses: int
    reward_sum_kopeks: int
    questions: list[PollStatsQuestionOut]


class AudienceSegmentInfo(BaseModel):
    """An audience segment the frontend can offer in the send dialog."""

    value: str  # e.g. 'all' or 'custom_inactive_week'
    label: str  # human-readable display name


class AudienceOptionsResponse(BaseModel):
    """All audience segments available for poll distribution."""

    standard: list[AudienceSegmentInfo]
    custom: list[AudienceSegmentInfo]


class PollSendRequest(BaseModel):
    """Request to distribute a poll to an audience segment.

    ``target`` is either a standard segment (e.g. ``all``, ``active``) or a
    custom criterion prefixed with ``custom_`` (e.g. ``custom_inactive_week``).
    """

    target: str = Field(..., min_length=1)


class PollAudiencePreviewResponse(BaseModel):
    """Preview of how many users a given target will reach."""

    target: str
    label: str
    user_count: int


class PollSendResponse(BaseModel):
    """Result of a poll distribution run."""

    poll_id: int
    target: str
    label: str
    sent: int
    failed: int
    skipped: int
    total: int


class PollDeleteResponse(BaseModel):
    """Result of deleting a poll."""

    id: int
    deleted: bool
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(value) -> str | None:
    """Serialize a datetime to ISO-8601, tolerating None."""
    return value.isoformat() if value else None


def _serialize_list_item(poll: Poll) -> PollListItem:
    return PollListItem(
        id=poll.id,
        title=poll.title,
        description=poll.description,
        reward_enabled=poll.reward_enabled,
        reward_amount_kopeks=poll.reward_amount_kopeks,
        questions_count=len(poll.questions),
        created_by=poll.created_by,
        created_at=_iso(poll.created_at) or '',
        updated_at=_iso(poll.updated_at),
    )


def _serialize_detail(poll: Poll) -> PollDetailResponse:
    questions = [
        PollQuestionOut(
            id=question.id,
            text=question.text,
            order=question.order,
            options=[
                PollOptionOut(id=option.id, text=option.text, order=option.order)
                for option in sorted(question.options, key=lambda o: o.order)
            ],
        )
        for question in sorted(poll.questions, key=lambda q: q.order)
    ]
    return PollDetailResponse(
        id=poll.id,
        title=poll.title,
        description=poll.description,
        reward_enabled=poll.reward_enabled,
        reward_amount_kopeks=poll.reward_amount_kopeks,
        created_by=poll.created_by,
        created_at=_iso(poll.created_at) or '',
        updated_at=_iso(poll.updated_at),
        questions=questions,
    )


def _resolve_target(target: str) -> tuple[bool, str]:
    """Validate a target string.

    Returns ``(is_custom, criterion_or_target)``. Raises HTTPException(400)
    when the target is not a known segment.
    """
    if target.startswith('custom_'):
        criterion = target[len('custom_') :]
        if criterion not in _CUSTOM_CRITERIA:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Unknown custom audience criterion: {criterion!r}',
            )
        return True, criterion
    if target not in _STANDARD_TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown audience target: {target!r}',
        )
    return False, target


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get('', response_model=PollListResponse)
async def list_all_polls(
    admin: User = Depends(require_permission('polls:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List the full poll catalog (newest first)."""
    try:
        polls = await list_polls(db)
        items = [_serialize_list_item(poll) for poll in polls]
        return PollListResponse(polls=items, total=len(items))
    except Exception as error:
        logger.error('Failed to list polls', admin_id=admin.id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list polls: {error!s}',
        )


@router.get('/audiences', response_model=AudienceOptionsResponse)
async def get_audience_options(
    admin: User = Depends(require_permission('polls:read')),
):
    """List the audience segments available for poll distribution."""
    try:
        standard = [
            AudienceSegmentInfo(value=value, label=get_target_display_name(value))
            for value in sorted(_STANDARD_TARGETS)
        ]
        custom = [
            AudienceSegmentInfo(
                value=f'custom_{criterion}',
                label=get_target_display_name(f'custom_{criterion}'),
            )
            for criterion in sorted(_CUSTOM_CRITERIA)
        ]
        return AudienceOptionsResponse(standard=standard, custom=custom)
    except Exception as error:
        logger.error('Failed to build audience options', admin_id=admin.id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to build audience options: {error!s}',
        )


@router.post('', response_model=PollDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_new_poll(
    request: PollCreateRequest,
    admin: User = Depends(require_permission('polls:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a poll with ordered questions and options.

    Mirrors the bot's create flow: optional HTML description (validated against
    the same allow-list), optional reward, and one or more questions each with
    at least two options.
    """
    try:
        # Validate HTML description with the exact validator the bot uses.
        if request.description:
            is_valid, error_message = validate_html_tags(request.description)
            if not is_valid:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Invalid HTML in description: {error_message}',
                )

        # Defensive: ensure every question keeps at least two non-empty options
        # after trimming (create_poll silently drops blanks).
        questions_payload: list[dict] = []
        for idx, question in enumerate(request.questions, start=1):
            options = [opt.strip() for opt in question.options if opt and opt.strip()]
            if len(options) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Question #{idx} needs at least two non-empty options',
                )
            if not question.text.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Question #{idx} text cannot be empty',
                )
            questions_payload.append({'text': question.text.strip(), 'options': options})

        reward_amount = request.reward_amount_kopeks if request.reward_enabled else 0
        poll = await create_poll(
            db,
            title=request.title.strip(),
            description=request.description,
            reward_enabled=request.reward_enabled,
            reward_amount_kopeks=reward_amount,
            created_by=admin.id,
            questions=questions_payload,
        )

        logger.info('Admin created poll', admin_id=admin.id, poll_id=poll.id, questions=len(poll.questions))

        # create_poll only refreshes `questions`; reload with options eagerly
        # loaded so the detail serializer has the full structure.
        full_poll = await get_poll_by_id(db, poll.id)
        if not full_poll:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Poll was created but could not be reloaded',
            )
        return _serialize_detail(full_poll)
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to create poll', admin_id=admin.id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to create poll: {error!s}',
        )


@router.get('/{poll_id}', response_model=PollDetailResponse)
async def get_poll_detail(
    poll_id: int,
    admin: User = Depends(require_permission('polls:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the full structure of a single poll."""
    try:
        poll = await get_poll_by_id(db, poll_id)
        if not poll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Poll not found')
        return _serialize_detail(poll)
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to load poll', admin_id=admin.id, poll_id=poll_id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load poll: {error!s}',
        )


@router.get('/{poll_id}/statistics', response_model=PollStatsResponse)
async def get_poll_stats(
    poll_id: int,
    admin: User = Depends(require_permission('polls:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get aggregated statistics for a poll (responses, completions, reward paid)."""
    try:
        poll = await get_poll_by_id(db, poll_id)
        if not poll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Poll not found')

        stats = await get_poll_statistics(db, poll_id)
        questions = [
            PollStatsQuestionOut(
                id=question['id'],
                text=question['text'],
                order=question['order'],
                options=[
                    PollStatsOptionOut(id=opt['id'], text=opt['text'], count=opt['count'])
                    for opt in question['options']
                ],
            )
            for question in stats['questions']
        ]
        return PollStatsResponse(
            poll_id=poll_id,
            total_responses=stats['total_responses'],
            completed_responses=stats['completed_responses'],
            reward_sum_kopeks=stats['reward_sum_kopeks'],
            questions=questions,
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to load poll statistics', admin_id=admin.id, poll_id=poll_id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load poll statistics: {error!s}',
        )


@router.get('/{poll_id}/audience-preview', response_model=PollAudiencePreviewResponse)
async def preview_poll_audience(
    poll_id: int,
    target: str,
    admin: User = Depends(require_permission('polls:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Count how many users a given target would reach (for the send dialog)."""
    try:
        poll = await get_poll_by_id(db, poll_id)
        if not poll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Poll not found')

        is_custom, criterion = _resolve_target(target)
        if is_custom:
            user_count = await get_custom_users_count(db, criterion)
        else:
            user_count = await get_target_users_count(db, criterion)

        return PollAudiencePreviewResponse(
            target=target,
            label=get_target_display_name(target),
            user_count=user_count,
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to preview poll audience', admin_id=admin.id, poll_id=poll_id, target=target, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to preview audience: {error!s}',
        )


@router.post('/{poll_id}/send', response_model=PollSendResponse)
async def send_poll(
    poll_id: int,
    request: PollSendRequest,
    admin: User = Depends(require_permission('polls:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Distribute a poll invitation to an audience segment.

    Reuses the broadcast audience-segment helpers to resolve recipients and the
    bot's ``send_poll_to_users`` service to dispatch. Users who already have a
    response for this poll, or who have no Telegram chat, are skipped by the
    service.
    """
    bot: Bot | None = None
    try:
        poll = await get_poll_by_id(db, poll_id)
        if not poll:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Poll not found')

        is_custom, criterion = _resolve_target(request.target)
        if is_custom:
            users = await get_custom_users(db, criterion)
        else:
            users = await get_target_users(db, criterion)

        bot = create_bot()
        result = await send_poll_to_users(bot, db, poll, users)

        logger.info(
            'Admin sent poll',
            admin_id=admin.id,
            poll_id=poll_id,
            target=request.target,
            sent=result.get('sent'),
            failed=result.get('failed'),
            skipped=result.get('skipped'),
        )

        return PollSendResponse(
            poll_id=poll_id,
            target=request.target,
            label=get_target_display_name(request.target),
            sent=result.get('sent', 0),
            failed=result.get('failed', 0),
            skipped=result.get('skipped', 0),
            total=result.get('total', 0),
        )
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to send poll', admin_id=admin.id, poll_id=poll_id, target=request.target, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to send poll: {error!s}',
        )
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as close_error:  # pragma: no cover - defensive cleanup
                logger.warning('Failed to close bot session after poll send', error=close_error)


@router.delete('/{poll_id}', response_model=PollDeleteResponse)
async def delete_existing_poll(
    poll_id: int,
    admin: User = Depends(require_permission('polls:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a poll and all its questions, options, responses and answers."""
    try:
        success = await delete_poll(db, poll_id)
        if not success:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Poll not found')

        logger.info('Admin deleted poll', admin_id=admin.id, poll_id=poll_id)
        return PollDeleteResponse(id=poll_id, deleted=True, message='Poll deleted')
    except HTTPException:
        raise
    except Exception as error:
        logger.error('Failed to delete poll', admin_id=admin.id, poll_id=poll_id, error=error)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete poll: {error!s}',
        )
