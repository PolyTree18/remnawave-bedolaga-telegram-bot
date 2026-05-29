"""Admin routes for managing contests in the cabinet.

This brings the bot's admin contest tooling (``app/handlers/admin/contests.py``
and ``app/handlers/admin/daily_contests.py``) to the web cabinet as a thin,
authenticated wrapper around the same services and CRUD functions.

Two domains are covered:

* Referral contests — CRUD + lifecycle (list / create / detail / toggle /
  delete / stats / leaderboard / payment-sync) plus virtual ("ghost")
  participant management.
* Daily game-contest templates — list / detail / toggle / edit /
  start-round / close-round / close-all / start-all.

All contest endpoints are gated by ``settings.is_contests_enabled()``
(``CONTESTS_ENABLED`` env var). When disabled they return HTTP 404 so the
frontend can hide the section, mirroring the bot's no-op behaviour.
"""

from datetime import UTC, datetime, time, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.contest import (
    create_round,
    finish_round,
    get_active_round_by_template,
    get_active_rounds,
    get_template_by_id,
    list_templates,
    update_template_fields,
)
from app.database.crud.referral_contest import (
    add_virtual_participant,
    create_referral_contest,
    delete_referral_contest,
    delete_virtual_participant,
    get_contest_events_count,
    get_contest_leaderboard_with_virtual,
    get_referral_contest,
    get_referral_contests_count,
    list_referral_contests,
    list_virtual_participants,
    toggle_referral_contest,
    update_referral_contest,
    update_virtual_participant_count,
)
from app.database.models import (
    ContestRound,
    ReferralContest,
    ReferralContestVirtualParticipant,
    User,
)
from app.services.contest_rotation_service import contest_rotation_service
from app.services.referral_contest_service import referral_contest_service

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.admin_contests import (
    ContestSyncResponse,
    DailyContestBulkActionResponse,
    DailyContestRoundActionResponse,
    DailyContestRoundInfo,
    DailyContestTemplateDetailResponse,
    DailyContestTemplateItem,
    DailyContestTemplateListResponse,
    DailyContestTemplateUpdateRequest,
    DailyContestToggleResponse,
    LeaderboardEntry,
    ParticipantStat,
    ReferralContestCreateRequest,
    ReferralContestDeleteResponse,
    ReferralContestDetailResponse,
    ReferralContestListItem,
    ReferralContestListResponse,
    ReferralContestStatsResponse,
    ReferralContestToggleResponse,
    ReferralContestUpdateRequest,
    VirtualParticipantCreateRequest,
    VirtualParticipantDeleteResponse,
    VirtualParticipantItem,
    VirtualParticipantsListResponse,
    VirtualParticipantUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/contests', tags=['Cabinet Admin Contests'])

# Non-null DB column default mirroring ReferralContest.daily_summary_time.
_DEFAULT_SUMMARY_TIME = time(hour=12, minute=0)


def _ensure_contests_enabled() -> None:
    """Raise 404 when the contests feature is disabled (matches bot no-op)."""
    if not settings.is_contests_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contests feature is disabled (CONTESTS_ENABLED is off).',
        )


def _as_utc(dt_value: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC."""
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=UTC)
    return dt_value.astimezone(UTC)


def _normalized_contest_end(contest: ReferralContest) -> datetime:
    """Return contest end normalised to end-of-day for midnight values (UTC)."""
    end = _as_utc(contest.end_at)
    if end.hour == 0 and end.minute == 0 and end.second == 0:
        end = end.replace(hour=23, minute=59, second=59, microsecond=999999)
    return end


def _contest_can_delete(contest: ReferralContest) -> bool:
    """Only finished (inactive AND ended) contests may be deleted."""
    return not contest.is_active and _normalized_contest_end(contest) < datetime.now(UTC)


def _referral_list_item(contest: ReferralContest) -> ReferralContestListItem:
    return ReferralContestListItem(
        id=contest.id,
        title=contest.title,
        contest_type=contest.contest_type,
        is_active=contest.is_active,
        start_at=contest.start_at,
        end_at=contest.end_at,
        timezone=contest.timezone,
        prize_text=contest.prize_text,
        daily_summary_time=contest.daily_summary_time,
        daily_summary_times=contest.daily_summary_times,
        last_daily_summary_date=(
            datetime.combine(contest.last_daily_summary_date, time.min) if contest.last_daily_summary_date else None
        ),
        final_summary_sent=contest.final_summary_sent,
        created_at=contest.created_at,
    )


def _leaderboard_entries(rows: list[tuple[str, int, int, bool]]) -> list[LeaderboardEntry]:
    return [
        LeaderboardEntry(
            rank=idx,
            display_name=name,
            score=score,
            total_amount_kopeks=amount,
            total_amount_rubles=amount / 100,
            is_virtual=is_virtual,
        )
        for idx, (name, score, amount, is_virtual) in enumerate(rows, start=1)
    ]


def _virtual_item(vp: ReferralContestVirtualParticipant) -> VirtualParticipantItem:
    return VirtualParticipantItem(
        id=vp.id,
        contest_id=vp.contest_id,
        display_name=vp.display_name,
        referral_count=vp.referral_count,
        total_amount_kopeks=vp.total_amount_kopeks,
        total_amount_rubles=vp.total_amount_kopeks / 100,
        created_at=vp.created_at,
    )


def _round_info(round_obj: ContestRound) -> DailyContestRoundInfo:
    return DailyContestRoundInfo(
        id=round_obj.id,
        template_id=round_obj.template_id,
        status=round_obj.status,
        starts_at=round_obj.starts_at,
        ends_at=round_obj.ends_at,
        winners_count=round_obj.winners_count,
        max_winners=round_obj.max_winners,
        attempts_per_user=round_obj.attempts_per_user,
    )


# ── Referral contests ──────────────────────────────────────────────────


@router.get('/referral', response_model=ReferralContestListResponse)
async def list_referral_contests_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    contest_type: str | None = Query(None),
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List referral contests (paginated)."""
    _ensure_contests_enabled()
    try:
        total = await get_referral_contests_count(db, contest_type=contest_type)
        offset = (page - 1) * page_size
        contests = await list_referral_contests(
            db,
            limit=page_size,
            offset=offset,
            contest_type=contest_type,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)

        return ReferralContestListResponse(
            contests=[_referral_list_item(c) for c in contests],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to list referral contests', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list contests: {e!s}',
        )


@router.post('/referral', response_model=ReferralContestDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_referral_contest_endpoint(
    request: ReferralContestCreateRequest,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new referral contest."""
    _ensure_contests_enabled()

    if request.contest_type not in ('referral_paid', 'referral_registered'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="contest_type must be 'referral_paid' or 'referral_registered'.",
        )

    start_at = _as_utc(request.start_at)
    end_at = _as_utc(request.end_at)
    if end_at <= start_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='end_at must be after start_at.',
        )

    summary_time = request.daily_summary_time or _DEFAULT_SUMMARY_TIME

    try:
        contest = await create_referral_contest(
            db,
            title=request.title,
            description=request.description,
            prize_text=request.prize_text,
            contest_type=request.contest_type,
            start_at=start_at,
            end_at=end_at,
            daily_summary_time=summary_time,
            daily_summary_times=request.daily_summary_times,
            timezone_name=request.timezone or 'UTC',
            created_by=admin.id,
        )
        logger.info('Admin created referral contest', admin_id=admin.id, contest_id=contest.id)
        return await _build_referral_detail(db, contest.id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to create referral contest', admin_id=admin.id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to create contest: {e!s}',
        )


async def _build_referral_detail(db: AsyncSession, contest_id: int) -> ReferralContestDetailResponse:
    """Assemble the detailed referral-contest payload."""
    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    leaderboard = await get_contest_leaderboard_with_virtual(db, contest_id, limit=10)
    virtual_list = await list_virtual_participants(db, contest_id)
    virtual_referral_count = sum(vp.referral_count for vp in virtual_list)
    total_events = await get_contest_events_count(db, contest_id) + virtual_referral_count

    return ReferralContestDetailResponse(
        id=contest.id,
        title=contest.title,
        description=contest.description,
        contest_type=contest.contest_type,
        is_active=contest.is_active,
        start_at=contest.start_at,
        end_at=contest.end_at,
        timezone=contest.timezone,
        prize_text=contest.prize_text,
        daily_summary_time=contest.daily_summary_time,
        daily_summary_times=contest.daily_summary_times,
        last_daily_summary_date=(
            datetime.combine(contest.last_daily_summary_date, time.min) if contest.last_daily_summary_date else None
        ),
        final_summary_sent=contest.final_summary_sent,
        created_at=contest.created_at,
        updated_at=contest.updated_at,
        total_events=total_events,
        virtual_referral_count=virtual_referral_count,
        can_delete=_contest_can_delete(contest),
        leaderboard=_leaderboard_entries(leaderboard),
        virtual_participants=[_virtual_item(vp) for vp in virtual_list],
    )


@router.get('/referral/{contest_id}', response_model=ReferralContestDetailResponse)
async def get_referral_contest_endpoint(
    contest_id: int,
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed referral contest info (with leaderboard and ghosts)."""
    _ensure_contests_enabled()
    try:
        return await _build_referral_detail(db, contest_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to get referral contest', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load contest: {e!s}',
        )


@router.put('/referral/{contest_id}', response_model=ReferralContestDetailResponse)
async def update_referral_contest_endpoint(
    contest_id: int,
    request: ReferralContestUpdateRequest,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a referral contest's daily-summary schedule."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    updates: dict[str, object] = {}
    if request.daily_summary_time is not None:
        updates['daily_summary_time'] = request.daily_summary_time
    if request.daily_summary_times is not None:
        # Empty string clears the multi-time schedule (None in DB).
        updates['daily_summary_times'] = request.daily_summary_times or None

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No editable fields provided.',
        )

    try:
        await update_referral_contest(db, contest, **updates)
        logger.info('Admin updated referral contest', admin_id=admin.id, contest_id=contest_id)
        return await _build_referral_detail(db, contest_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update referral contest', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update contest: {e!s}',
        )


@router.post('/referral/{contest_id}/toggle', response_model=ReferralContestToggleResponse)
async def toggle_referral_contest_endpoint(
    contest_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle a referral contest active/inactive."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        new_state = not contest.is_active
        await toggle_referral_contest(db, contest, new_state)
        logger.info(
            'Admin toggled referral contest',
            admin_id=admin.id,
            contest_id=contest_id,
            is_active=new_state,
        )
        return ReferralContestToggleResponse(
            id=contest_id,
            is_active=new_state,
            message='Contest activated' if new_state else 'Contest deactivated',
        )
    except Exception as e:
        logger.error('Failed to toggle referral contest', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to toggle contest: {e!s}',
        )


@router.delete('/referral/{contest_id}', response_model=ReferralContestDeleteResponse)
async def delete_referral_contest_endpoint(
    contest_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a finished referral contest.

    Only contests that are inactive AND already ended may be deleted, matching
    the bot's guard.
    """
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    if not _contest_can_delete(contest):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Only finished (inactive and ended) contests can be deleted.',
        )

    try:
        await delete_referral_contest(db, contest)
        logger.info('Admin deleted referral contest', admin_id=admin.id, contest_id=contest_id)
        return ReferralContestDeleteResponse(id=contest_id, message='Contest deleted')
    except Exception as e:
        logger.error('Failed to delete referral contest', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete contest: {e!s}',
        )


@router.get('/referral/{contest_id}/leaderboard', response_model=list[LeaderboardEntry])
async def get_referral_contest_leaderboard(
    contest_id: int,
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the merged (real + virtual) leaderboard for a contest."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        rows = await get_contest_leaderboard_with_virtual(db, contest_id, limit=limit)
        return _leaderboard_entries(rows)
    except Exception as e:
        logger.error('Failed to load leaderboard', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load leaderboard: {e!s}',
        )


@router.get('/referral/{contest_id}/stats', response_model=ReferralContestStatsResponse)
async def get_referral_contest_stats(
    contest_id: int,
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed referral contest statistics (per-participant breakdown)."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        stats = await referral_contest_service.get_detailed_contest_stats(db, contest_id)
        virtual = await list_virtual_participants(db, contest_id)
        virtual_referrals = sum(vp.referral_count for vp in virtual)

        participants = [
            ParticipantStat(
                referrer_id=p['referrer_id'],
                full_name=p['full_name'] or '',
                total_referrals=p['total_referrals'],
                paid_referrals=p['paid_referrals'],
                unpaid_referrals=p['unpaid_referrals'],
                total_paid_amount_kopeks=p['total_paid_amount'],
                total_paid_amount_rubles=p['total_paid_amount'] / 100,
            )
            for p in stats.get('participants', [])
        ]

        return ReferralContestStatsResponse(
            contest_id=contest_id,
            total_participants=stats.get('total_participants', 0),
            total_invited=stats.get('total_invited', 0),
            paid_count=stats.get('paid_count', 0),
            unpaid_count=stats.get('unpaid_count', 0),
            total_paid_amount_kopeks=stats.get('total_paid_amount', 0),
            subscription_total_kopeks=stats.get('subscription_total', 0),
            deposit_total_kopeks=stats.get('deposit_total', 0),
            virtual_count=len(virtual),
            virtual_referrals=virtual_referrals,
            participants=participants,
        )
    except Exception as e:
        logger.error('Failed to load contest stats', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load stats: {e!s}',
        )


@router.post('/referral/{contest_id}/sync', response_model=ContestSyncResponse)
async def sync_referral_contest(
    contest_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Clean up invalid events then re-sync contest event amounts with payments.

    Two-phase, mirroring the bot: cleanup of out-of-period events, then a
    payment re-sync of the surviving events.
    """
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        cleanup_stats = await referral_contest_service.cleanup_contest(db, contest_id)
        if 'error' in cleanup_stats:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f'Cleanup failed: {cleanup_stats["error"]}',
            )

        sync_stats = await referral_contest_service.sync_contest(db, contest_id)
        if 'error' in sync_stats:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f'Sync failed: {sync_stats["error"]}',
            )

        logger.info('Admin synced referral contest', admin_id=admin.id, contest_id=contest_id)

        return ContestSyncResponse(
            contest_id=contest_id,
            deleted_events=cleanup_stats.get('deleted', 0),
            remaining_events=cleanup_stats.get('remaining', 0),
            total_before=cleanup_stats.get('total_before', 0),
            total_events=sync_stats.get('total_events', 0),
            filtered_out_events=sync_stats.get('filtered_out_events', 0),
            updated=sync_stats.get('updated', 0),
            skipped=sync_stats.get('skipped', 0),
            paid_count=sync_stats.get('paid_count', 0),
            unpaid_count=sync_stats.get('unpaid_count', 0),
            subscription_total_kopeks=sync_stats.get('subscription_total', 0),
            deposit_total_kopeks=sync_stats.get('deposit_total', 0),
            message='Sync completed',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to sync referral contest', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to sync contest: {e!s}',
        )


# ── Virtual ("ghost") participants ──────────────────────────────────────


@router.get('/referral/{contest_id}/virtual', response_model=VirtualParticipantsListResponse)
async def list_virtual_participants_endpoint(
    contest_id: int,
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List virtual participants of a contest."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        vps = await list_virtual_participants(db, contest_id)
        return VirtualParticipantsListResponse(
            contest_id=contest_id,
            participants=[_virtual_item(vp) for vp in vps],
        )
    except Exception as e:
        logger.error('Failed to list virtual participants', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list virtual participants: {e!s}',
        )


@router.post(
    '/referral/{contest_id}/virtual',
    response_model=VirtualParticipantItem,
    status_code=status.HTTP_201_CREATED,
)
async def add_virtual_participant_endpoint(
    contest_id: int,
    request: VirtualParticipantCreateRequest,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Add a single virtual ("ghost") participant to a contest."""
    _ensure_contests_enabled()

    contest = await get_referral_contest(db, contest_id)
    if not contest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Contest not found',
        )

    try:
        vp = await add_virtual_participant(
            db,
            contest_id,
            request.display_name,
            request.referral_count,
            request.total_amount_kopeks,
        )
        logger.info(
            'Admin added virtual participant',
            admin_id=admin.id,
            contest_id=contest_id,
            vp_id=vp.id,
        )
        return _virtual_item(vp)
    except Exception as e:
        logger.error('Failed to add virtual participant', contest_id=contest_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to add virtual participant: {e!s}',
        )


@router.put('/referral/virtual/{participant_id}', response_model=VirtualParticipantItem)
async def update_virtual_participant_endpoint(
    participant_id: int,
    request: VirtualParticipantUpdateRequest,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a virtual participant's referral count."""
    _ensure_contests_enabled()

    try:
        vp = await update_virtual_participant_count(db, participant_id, request.referral_count)
        if not vp:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Virtual participant not found',
            )
        logger.info(
            'Admin updated virtual participant',
            admin_id=admin.id,
            vp_id=participant_id,
        )
        return _virtual_item(vp)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update virtual participant', vp_id=participant_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update virtual participant: {e!s}',
        )


@router.delete('/referral/virtual/{participant_id}', response_model=VirtualParticipantDeleteResponse)
async def delete_virtual_participant_endpoint(
    participant_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a virtual participant."""
    _ensure_contests_enabled()

    # Resolve contest_id before deletion so the response is informative.
    vps_lookup = await db.get(ReferralContestVirtualParticipant, participant_id)
    if not vps_lookup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Virtual participant not found',
        )
    contest_id = vps_lookup.contest_id

    try:
        deleted = await delete_virtual_participant(db, participant_id)
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Virtual participant not found',
            )
        logger.info(
            'Admin deleted virtual participant',
            admin_id=admin.id,
            vp_id=participant_id,
            contest_id=contest_id,
        )
        return VirtualParticipantDeleteResponse(
            id=participant_id,
            contest_id=contest_id,
            message='Virtual participant deleted',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to delete virtual participant', vp_id=participant_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to delete virtual participant: {e!s}',
        )


# ── Daily game-contest templates ────────────────────────────────────────


def _template_item(tpl, has_active_round: bool) -> DailyContestTemplateItem:
    return DailyContestTemplateItem(
        id=tpl.id,
        name=tpl.name,
        slug=tpl.slug,
        description=tpl.description,
        prize_type=tpl.prize_type,
        prize_value=tpl.prize_value,
        max_winners=tpl.max_winners,
        attempts_per_user=tpl.attempts_per_user,
        times_per_day=tpl.times_per_day,
        schedule_times=tpl.schedule_times,
        cooldown_hours=tpl.cooldown_hours,
        is_enabled=tpl.is_enabled,
        has_active_round=has_active_round,
        created_at=tpl.created_at,
        updated_at=tpl.updated_at,
    )


@router.get('/daily', response_model=DailyContestTemplateListResponse)
async def list_daily_contests_endpoint(
    enabled_only: bool = Query(False),
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List daily contest templates."""
    _ensure_contests_enabled()
    try:
        templates = await list_templates(db, enabled_only=enabled_only)
        items: list[DailyContestTemplateItem] = []
        for tpl in templates:
            active = await get_active_round_by_template(db, tpl.id)
            items.append(_template_item(tpl, has_active_round=active is not None))
        return DailyContestTemplateListResponse(templates=items, total=len(items))
    except Exception as e:
        logger.error('Failed to list daily contests', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list daily contests: {e!s}',
        )


@router.get('/daily/{template_id}', response_model=DailyContestTemplateDetailResponse)
async def get_daily_contest_endpoint(
    template_id: int,
    admin: User = Depends(require_permission('contests:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a daily contest template's full configuration."""
    _ensure_contests_enabled()

    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Template not found',
        )

    try:
        active = await get_active_round_by_template(db, template_id)
        base = _template_item(tpl, has_active_round=active is not None)
        return DailyContestTemplateDetailResponse(
            **base.model_dump(),
            payload=tpl.payload or {},
            active_round=_round_info(active) if active else None,
        )
    except Exception as e:
        logger.error('Failed to get daily contest', template_id=template_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to load daily contest: {e!s}',
        )


@router.put('/daily/{template_id}', response_model=DailyContestTemplateDetailResponse)
async def update_daily_contest_endpoint(
    template_id: int,
    request: DailyContestTemplateUpdateRequest,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a daily contest template's editable fields."""
    _ensure_contests_enabled()

    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Template not found',
        )

    if request.prize_type is not None and request.prize_type not in ('days', 'balance', 'custom'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prize_type must be one of 'days', 'balance', 'custom'.",
        )

    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No editable fields provided.',
        )

    try:
        await update_template_fields(db, tpl, **updates)
        logger.info('Admin updated daily contest template', admin_id=admin.id, template_id=template_id)
        return await get_daily_contest_endpoint(template_id, admin, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to update daily contest', template_id=template_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update daily contest: {e!s}',
        )


@router.post('/daily/{template_id}/toggle', response_model=DailyContestToggleResponse)
async def toggle_daily_contest_endpoint(
    template_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle a daily contest template enabled/disabled."""
    _ensure_contests_enabled()

    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Template not found',
        )

    try:
        new_state = not tpl.is_enabled
        await update_template_fields(db, tpl, is_enabled=new_state)
        logger.info(
            'Admin toggled daily contest template',
            admin_id=admin.id,
            template_id=template_id,
            is_enabled=new_state,
        )
        return DailyContestToggleResponse(
            id=template_id,
            is_enabled=new_state,
            message='Template enabled' if new_state else 'Template disabled',
        )
    except Exception as e:
        logger.error('Failed to toggle daily contest', template_id=template_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to toggle daily contest: {e!s}',
        )


async def _start_round_for_template(db: AsyncSession, tpl) -> ContestRound:
    """Build a round payload, persist a round and best-effort announce it.

    Reuses the rotation service's payload builder so the cabinet starts
    rounds identically to the bot. The announcement is best-effort: it
    no-ops when the rotation service has no bot instance attached (typical
    in the cabinet process).
    """
    payload = contest_rotation_service._build_payload_for_template(tpl)
    now = datetime.now(UTC)
    ends = now + timedelta(hours=tpl.cooldown_hours)
    round_obj = await create_round(
        db,
        template=tpl,
        starts_at=now,
        ends_at=ends,
        payload=payload,
    )
    try:
        await contest_rotation_service._announce_round_start(tpl, now, ends)
    except Exception as exc:
        logger.warning(
            'Round started but announcement failed (no bot attached?)',
            template_id=tpl.id,
            error=str(exc),
        )
    return round_obj


@router.post('/daily/{template_id}/start-round', response_model=DailyContestRoundActionResponse)
async def start_daily_round_endpoint(
    template_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Start a new round for a daily contest template.

    If a round is already active for this template, returns 409 (matching
    the bot's "already active" guard).
    """
    _ensure_contests_enabled()

    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Template not found',
        )

    existing = await get_active_round_by_template(db, template_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='A round is already active for this template.',
        )

    try:
        # Enabling on start mirrors the bot's start_round_now behaviour.
        if not tpl.is_enabled:
            await update_template_fields(db, tpl, is_enabled=True)
        round_obj = await _start_round_for_template(db, tpl)
        logger.info(
            'Admin started daily round',
            admin_id=admin.id,
            template_id=template_id,
            round_id=round_obj.id,
        )
        return DailyContestRoundActionResponse(
            template_id=template_id,
            round_id=round_obj.id,
            status=round_obj.status,
            message='Round started',
        )
    except Exception as e:
        logger.error('Failed to start daily round', template_id=template_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to start round: {e!s}',
        )


@router.post('/daily/{template_id}/close-round', response_model=DailyContestRoundActionResponse)
async def close_daily_round_endpoint(
    template_id: int,
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Close the active round for a daily contest template."""
    _ensure_contests_enabled()

    tpl = await get_template_by_id(db, template_id)
    if not tpl:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Template not found',
        )

    round_obj = await get_active_round_by_template(db, template_id)
    if not round_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No active round for this template.',
        )

    try:
        await finish_round(db, round_obj)
        logger.info(
            'Admin closed daily round',
            admin_id=admin.id,
            template_id=template_id,
            round_id=round_obj.id,
        )
        return DailyContestRoundActionResponse(
            template_id=template_id,
            round_id=round_obj.id,
            status='finished',
            message='Round closed',
        )
    except Exception as e:
        logger.error('Failed to close daily round', template_id=template_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to close round: {e!s}',
        )


@router.post('/daily/close-all', response_model=DailyContestBulkActionResponse)
async def close_all_daily_rounds_endpoint(
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Close every currently-active daily contest round."""
    _ensure_contests_enabled()

    try:
        active_rounds = await get_active_rounds(db)
        for rnd in active_rounds:
            await finish_round(db, rnd)
        logger.info('Admin closed all daily rounds', admin_id=admin.id, affected=len(active_rounds))
        return DailyContestBulkActionResponse(
            affected=len(active_rounds),
            message=f'Closed {len(active_rounds)} active round(s)',
        )
    except Exception as e:
        logger.error('Failed to close all daily rounds', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to close rounds: {e!s}',
        )


@router.post('/daily/start-all', response_model=DailyContestBulkActionResponse)
async def start_all_daily_rounds_endpoint(
    admin: User = Depends(require_permission('contests:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Start a round for every enabled template that has no active round."""
    _ensure_contests_enabled()

    try:
        templates = await list_templates(db, enabled_only=True)
        started = 0
        for tpl in templates:
            existing = await get_active_round_by_template(db, tpl.id)
            if existing:
                continue
            await _start_round_for_template(db, tpl)
            started += 1
        logger.info('Admin started all daily rounds', admin_id=admin.id, started=started)
        return DailyContestBulkActionResponse(
            affected=started,
            message=f'Started {started} round(s)',
        )
    except Exception as e:
        logger.error('Failed to start all daily rounds', error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to start rounds: {e!s}',
        )
