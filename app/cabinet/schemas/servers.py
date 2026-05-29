"""Schemas for server management in cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field


class PromoGroupInfo(BaseModel):
    """Promo group info for server."""

    id: int
    name: str
    is_selected: bool = False


class ServerListItem(BaseModel):
    """Server item for list view."""

    id: int
    squad_uuid: str
    display_name: str
    original_name: str | None = None
    country_code: str | None = None
    is_available: bool
    is_trial_eligible: bool
    price_kopeks: int
    price_rubles: float
    max_users: int | None = None
    current_users: int
    sort_order: int
    is_full: bool
    availability_status: str
    created_at: datetime

    class Config:
        from_attributes = True


class ServerListResponse(BaseModel):
    """Response with list of servers."""

    servers: list[ServerListItem]
    total: int


class ServerDetailResponse(BaseModel):
    """Detailed server response."""

    id: int
    squad_uuid: str
    display_name: str
    original_name: str | None = None
    country_code: str | None = None
    description: str | None = None
    is_available: bool
    is_trial_eligible: bool
    price_kopeks: int
    price_rubles: float
    max_users: int | None = None
    current_users: int
    sort_order: int
    is_full: bool
    availability_status: str
    promo_groups: list[PromoGroupInfo]
    active_subscriptions: int
    tariffs_using: list[str]  # Names of tariffs using this server
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class ServerUpdateRequest(BaseModel):
    """Request to update a server."""

    display_name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    country_code: str | None = Field(None, max_length=5)
    is_available: bool | None = None
    is_trial_eligible: bool | None = None
    price_kopeks: int | None = Field(None, ge=0)
    max_users: int | None = Field(None, ge=0)
    sort_order: int | None = Field(None, ge=0)
    promo_group_ids: list[int] | None = None


class ServerToggleResponse(BaseModel):
    """Response after toggling server."""

    id: int
    is_available: bool
    message: str


class ServerTrialToggleResponse(BaseModel):
    """Response after toggling trial eligibility."""

    id: int
    is_trial_eligible: bool
    message: str


class ServerStatsResponse(BaseModel):
    """Server statistics."""

    id: int
    display_name: str
    squad_uuid: str
    current_users: int
    max_users: int | None
    active_subscriptions: int
    trial_subscriptions: int
    usage_percent: float | None = None


class ServerSyncResponse(BaseModel):
    """Response after syncing with RemnaWave."""

    created: int
    updated: int
    removed: int
    message: str


class ServerSyncRequest(BaseModel):
    """Request to sync servers."""

    force: bool = False  # Force sync even if recently synced


class ServerDeleteRequest(BaseModel):
    """Request body for deleting a server (destructive op needs explicit confirm)."""

    confirm: bool = Field(False, description='Must be true to actually delete the server')


class ServerDeleteResponse(BaseModel):
    """Response after deleting a server."""

    id: int
    deleted: bool
    message: str


class ConnectedSubscriptionInfo(BaseModel):
    """Per-subscription status for a user connected to the squad."""

    id: int
    status: str
    status_display: str
    is_active: bool
    is_trial: bool
    tariff_name: str | None = None
    end_date: datetime | None = None


class ConnectedUserItem(BaseModel):
    """A user connected to the squad with their subscription statuses."""

    id: int
    telegram_id: int | None = None
    username: str | None = None
    full_name: str
    subscriptions: list[ConnectedSubscriptionInfo]


class ServerConnectedUsersResponse(BaseModel):
    """Paginated list of users connected to a squad."""

    server_id: int
    squad_uuid: str
    display_name: str
    users: list[ConnectedUserItem]
    total: int
    page: int
    limit: int
    total_pages: int


class ServerSyncCountersResponse(BaseModel):
    """Response after recalculating server user counters."""

    updated_servers: int
    message: str
