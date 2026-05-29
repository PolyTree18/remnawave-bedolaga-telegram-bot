"""Schemas for admin contests management in cabinet.

Covers two distinct contest domains exposed by the bot admin panel:

* Referral contests (``ReferralContest``) — competitions where referrers
  earn "scores" for inviting / converting referrals, with virtual
  ("ghost") participants, leaderboards and payment-sync tooling.
* Daily game contests (``ContestTemplate`` / ``ContestRound``) — recurring
  mini-game rounds driven by editable templates.
"""

from datetime import datetime, time

from pydantic import BaseModel, Field


# ── Referral contests ──────────────────────────────────────────────────


class ReferralContestListItem(BaseModel):
    """Referral contest item for the list view."""

    id: int
    title: str
    contest_type: str
    is_active: bool
    start_at: datetime
    end_at: datetime
    timezone: str
    prize_text: str | None = None
    daily_summary_time: time | None = None
    daily_summary_times: str | None = None
    last_daily_summary_date: datetime | None = None
    final_summary_sent: bool
    created_at: datetime | None = None


class ReferralContestListResponse(BaseModel):
    """Paginated list of referral contests."""

    contests: list[ReferralContestListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class LeaderboardEntry(BaseModel):
    """A single leaderboard row (real or virtual participant)."""

    rank: int
    display_name: str
    score: int
    total_amount_kopeks: int
    total_amount_rubles: float
    is_virtual: bool


class VirtualParticipantItem(BaseModel):
    """A virtual ("ghost") participant of a referral contest."""

    id: int
    contest_id: int
    display_name: str
    referral_count: int
    total_amount_kopeks: int
    total_amount_rubles: float
    created_at: datetime | None = None


class ReferralContestDetailResponse(BaseModel):
    """Detailed referral contest payload."""

    id: int
    title: str
    description: str | None = None
    contest_type: str
    is_active: bool
    start_at: datetime
    end_at: datetime
    timezone: str
    prize_text: str | None = None
    daily_summary_time: time | None = None
    daily_summary_times: str | None = None
    last_daily_summary_date: datetime | None = None
    final_summary_sent: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Aggregates
    total_events: int
    virtual_referral_count: int
    can_delete: bool
    leaderboard: list[LeaderboardEntry]
    virtual_participants: list[VirtualParticipantItem]


class ReferralContestCreateRequest(BaseModel):
    """Request to create a referral contest.

    Mirrors :func:`create_referral_contest`. Datetimes are stored in UTC by
    the bot; the frontend should send timezone-aware ISO 8601 values (or
    UTC) and the human-facing timezone label separately.
    """

    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    prize_text: str | None = None
    contest_type: str = Field(
        default='referral_paid',
        description="One of 'referral_paid' (referral must purchase) or 'referral_registered' (registration is enough).",
    )
    start_at: datetime
    end_at: datetime
    # CSV "HH:MM" or "HH:MM,HH:MM"; if omitted defaults to a single
    # daily_summary_time of 12:00.
    daily_summary_time: time | None = None
    daily_summary_times: str | None = Field(
        default=None,
        description='Comma-separated HH:MM list, e.g. "12:00,18:00".',
    )
    timezone: str = Field(default='UTC', max_length=64)


class ReferralContestUpdateRequest(BaseModel):
    """Partial update of a referral contest (only summary scheduling).

    The bot UI only exposes editing of the daily-summary schedule for an
    existing contest; everything else is set at creation time.
    """

    daily_summary_time: time | None = None
    daily_summary_times: str | None = Field(
        default=None,
        description='Comma-separated HH:MM list, e.g. "12:00,18:00". Send empty string to clear.',
    )


class ReferralContestToggleResponse(BaseModel):
    """Response after toggling contest active state."""

    id: int
    is_active: bool
    message: str


class ReferralContestDeleteResponse(BaseModel):
    """Response after deleting a contest."""

    id: int
    message: str


class ParticipantStat(BaseModel):
    """Per-referrer stats row used by the detailed-stats endpoint."""

    referrer_id: int
    full_name: str
    total_referrals: int
    paid_referrals: int
    unpaid_referrals: int
    total_paid_amount_kopeks: int
    total_paid_amount_rubles: float


class ReferralContestStatsResponse(BaseModel):
    """Detailed referral contest statistics."""

    contest_id: int
    total_participants: int
    total_invited: int
    paid_count: int
    unpaid_count: int
    total_paid_amount_kopeks: int
    subscription_total_kopeks: int
    deposit_total_kopeks: int
    virtual_count: int
    virtual_referrals: int
    participants: list[ParticipantStat]


class ContestSyncResponse(BaseModel):
    """Result of cleanup + payment-sync of a referral contest."""

    contest_id: int
    # Cleanup phase
    deleted_events: int
    remaining_events: int
    total_before: int
    # Sync phase
    total_events: int
    filtered_out_events: int
    updated: int
    skipped: int
    paid_count: int
    unpaid_count: int
    subscription_total_kopeks: int
    deposit_total_kopeks: int
    message: str


class VirtualParticipantCreateRequest(BaseModel):
    """Create a single virtual participant."""

    display_name: str = Field(min_length=1, max_length=200)
    referral_count: int = Field(ge=1)
    total_amount_kopeks: int = Field(default=0, ge=0)


class VirtualParticipantUpdateRequest(BaseModel):
    """Update a virtual participant's referral count."""

    referral_count: int = Field(ge=1)


class VirtualParticipantsListResponse(BaseModel):
    """List of virtual participants for a contest."""

    contest_id: int
    participants: list[VirtualParticipantItem]


class VirtualParticipantDeleteResponse(BaseModel):
    """Response after deleting a virtual participant."""

    id: int
    contest_id: int
    message: str


# ── Daily game contests (templates / rounds) ────────────────────────────


class DailyContestTemplateItem(BaseModel):
    """Daily contest template summary."""

    id: int
    name: str
    slug: str
    description: str | None = None
    prize_type: str
    prize_value: str
    max_winners: int
    attempts_per_user: int
    times_per_day: int
    schedule_times: str | None = None
    cooldown_hours: int
    is_enabled: bool
    has_active_round: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DailyContestTemplateListResponse(BaseModel):
    """List of daily contest templates."""

    templates: list[DailyContestTemplateItem]
    total: int


class DailyContestTemplateDetailResponse(DailyContestTemplateItem):
    """Detailed daily contest template (includes JSON payload)."""

    payload: dict | None = None
    active_round: 'DailyContestRoundInfo | None' = None


class DailyContestRoundInfo(BaseModel):
    """Active round info for a template."""

    id: int
    template_id: int
    status: str
    starts_at: datetime
    ends_at: datetime
    winners_count: int
    max_winners: int
    attempts_per_user: int


class DailyContestTemplateUpdateRequest(BaseModel):
    """Partial update of a daily contest template.

    Only the fields the bot admin UI allows editing are supported.
    ``payload`` accepts a free-form JSON object (game settings).
    """

    prize_type: str | None = Field(default=None, max_length=20)
    prize_value: str | None = Field(default=None, max_length=50)
    max_winners: int | None = Field(default=None, ge=1)
    attempts_per_user: int | None = Field(default=None, ge=1)
    times_per_day: int | None = Field(default=None, ge=1)
    schedule_times: str | None = Field(
        default=None,
        description='Comma-separated HH:MM list (local TZ), e.g. "10:00,20:00".',
    )
    cooldown_hours: int | None = Field(default=None, ge=1)
    payload: dict | None = None


class DailyContestToggleResponse(BaseModel):
    """Response after toggling a daily template enabled state."""

    id: int
    is_enabled: bool
    message: str


class DailyContestRoundActionResponse(BaseModel):
    """Response after starting / closing a round."""

    template_id: int
    round_id: int | None = None
    status: str
    message: str


class DailyContestBulkActionResponse(BaseModel):
    """Response after a bulk round operation (start-all / close-all)."""

    affected: int
    message: str


# Resolve forward refs.
DailyContestTemplateDetailResponse.model_rebuild()
