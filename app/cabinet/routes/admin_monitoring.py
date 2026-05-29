"""Admin routes for the monitoring subsystem in the cabinet.

Thin authenticated wrappers around the same services the Telegram bot
admin panel uses (``app/handlers/admin/monitoring.py``):

* ``monitoring_service`` — start/stop the monitoring loop, force-check
  subscriptions, status, logs.
* ``nalogo_queue_service`` (+ ``NaloGoService``) — NaloGO receipt queue:
  view/force-process the send queue and manage the manual-verification
  pending list (verify/retry/clear).
* ``NotificationSettingsService`` — runtime win-back / expiry notification
  settings persisted to ``data/notification_settings.json``.

The receipt-reconciliation report (payments-without-receipts) is parsed
from the payments log file directly in the bot handler with no backing
service, so it is replicated minimally here.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.monitoring_service import monitoring_service
from app.services.nalogo_queue_service import nalogo_queue_service
from app.services.nalogo_service import NaloGoService
from app.services.notification_settings_service import NotificationSettingsService

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/monitoring', tags=['Cabinet Admin Monitoring'])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MonitoringStats24h(BaseModel):
    """24-hour event statistics for the monitoring loop."""

    total_events: int
    successful: int
    failed: int
    success_rate: float


class MonitoringEvent(BaseModel):
    """A recent monitoring log event (summary form)."""

    type: str
    message: str
    success: bool
    created_at: datetime


class MonitoringStatusResponse(BaseModel):
    """Current monitoring loop status."""

    is_running: bool
    last_update: datetime | None = None
    interval_minutes: int
    notifications_enabled: bool
    nalogo_enabled: bool
    stats_24h: MonitoringStats24h
    recent_events: list[MonitoringEvent]


class MonitoringActionResponse(BaseModel):
    """Generic result of a start/stop action."""

    is_running: bool
    message: str


class ForceCheckResponse(BaseModel):
    """Result of a forced subscription check."""

    expired: int
    expiring: int
    autopay_ready: int
    checked_at: datetime
    message: str


class MonitoringLogItem(BaseModel):
    """A single monitoring log row."""

    id: int
    event_type: str
    message: str
    data: dict[str, Any] | None = None
    is_success: bool
    created_at: datetime


class MonitoringLogsResponse(BaseModel):
    """Paginated monitoring logs."""

    logs: list[MonitoringLogItem]
    total: int
    page: int
    per_page: int
    event_types: list[str]


class ClearLogsResponse(BaseModel):
    """Result of clearing monitoring logs."""

    deleted: int
    message: str


# --- NaloGO queue -----------------------------------------------------------


class NalogoQueuedReceipt(BaseModel):
    """A receipt currently sitting in the send queue."""

    payment_id: str | None = None
    amount: float = 0.0
    name: str | None = None
    attempts: int = 0
    created_at: str | None = None


class NalogoPendingReceipt(BaseModel):
    """A receipt awaiting manual verification."""

    payment_id: str | None = None
    amount: float = 0.0
    name: str | None = None
    created_at: str | None = None
    error: str | None = None


class NalogoQueueStatusResponse(BaseModel):
    """NaloGO queue + pending-verification status."""

    running: bool
    check_interval_seconds: int
    receipt_delay_seconds: int
    max_attempts: int
    queue_length: int
    total_amount: float
    queued_receipts: list[NalogoQueuedReceipt]
    pending_verification_count: int
    pending_verification_amount: float
    pending_verification_receipts: list[NalogoPendingReceipt]


class NalogoForceProcessResponse(BaseModel):
    """Result of forcing the NaloGO queue to flush."""

    message: str
    was_in_queue: int = 0
    remaining: int = 0
    processed: int = 0


class NalogoPendingListResponse(BaseModel):
    """List of receipts awaiting manual verification."""

    receipts: list[NalogoPendingReceipt]
    count: int
    total_amount: float


class NalogoVerifyRequest(BaseModel):
    """Mark a pending receipt as already created in nalog.ru."""

    payment_id: str = Field(..., min_length=1)
    receipt_uuid: str | None = None


class NalogoVerifyResponse(BaseModel):
    """Result of marking a pending receipt as verified."""

    removed: bool
    payment_id: str
    message: str


class NalogoRetryRequest(BaseModel):
    """Retry sending a pending receipt to nalog.ru."""

    payment_id: str = Field(..., min_length=1)


class NalogoRetryResponse(BaseModel):
    """Result of retrying a pending receipt."""

    success: bool
    payment_id: str
    receipt_uuid: str | None = None
    message: str


class NalogoClearResponse(BaseModel):
    """Result of clearing the pending-verification queue."""

    cleared: int
    message: str


# --- Receipt reconciliation -------------------------------------------------


class ReconcileDateBucket(BaseModel):
    """Payments-without-receipts grouped by date."""

    date: str
    count: int
    amount: float


class ReconcileMissingPayment(BaseModel):
    """A single payment that has no matching receipt in the logs."""

    payment_id: str
    date: str
    time: str | None = None
    user_id: str | None = None
    amount: float


class ReconcileReport(BaseModel):
    """Receipt reconciliation report derived from the payments log."""

    log_found: bool
    log_path: str
    total_payments: int
    total_receipts: int
    missing_count: int
    missing_amount: float
    by_date: list[ReconcileDateBucket]
    missing_payments: list[ReconcileMissingPayment]


# --- Notification settings --------------------------------------------------


class NotificationWaveSettings(BaseModel):
    """Discount wave (second / third) settings."""

    enabled: bool
    discount_percent: int
    valid_hours: int


class NotificationSettingsResponse(BaseModel):
    """Win-back / expiry notification settings."""

    globally_enabled: bool
    trial_channel_unsubscribed_enabled: bool
    expired_1d_enabled: bool
    second_wave: NotificationWaveSettings
    third_wave: NotificationWaveSettings
    third_wave_trigger_days: int


class NotificationSettingsUpdateRequest(BaseModel):
    """Partial update of notification settings.

    Only fields that are provided are applied. Ranges mirror the bot
    handler validation: percent 0-100, hours 1-168, trigger days >= 2.
    """

    trial_channel_unsubscribed_enabled: bool | None = None
    expired_1d_enabled: bool | None = None
    second_wave_enabled: bool | None = None
    second_wave_discount_percent: int | None = Field(None, ge=0, le=100)
    second_wave_valid_hours: int | None = Field(None, ge=1, le=168)
    third_wave_enabled: bool | None = None
    third_wave_discount_percent: int | None = Field(None, ge=0, le=100)
    third_wave_valid_hours: int | None = Field(None, ge=1, le=168)
    third_wave_trigger_days: int | None = Field(None, ge=2, le=60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_notification_settings_response() -> NotificationSettingsResponse:
    """Read the current notification settings via the service."""
    return NotificationSettingsResponse(
        globally_enabled=NotificationSettingsService.are_notifications_globally_enabled(),
        trial_channel_unsubscribed_enabled=NotificationSettingsService.is_trial_channel_unsubscribed_enabled(),
        expired_1d_enabled=NotificationSettingsService.is_expired_1d_enabled(),
        second_wave=NotificationWaveSettings(
            enabled=NotificationSettingsService.is_second_wave_enabled(),
            discount_percent=NotificationSettingsService.get_second_wave_discount_percent(),
            valid_hours=NotificationSettingsService.get_second_wave_valid_hours(),
        ),
        third_wave=NotificationWaveSettings(
            enabled=NotificationSettingsService.is_third_wave_enabled(),
            discount_percent=NotificationSettingsService.get_third_wave_discount_percent(),
            valid_hours=NotificationSettingsService.get_third_wave_valid_hours(),
        ),
        third_wave_trigger_days=NotificationSettingsService.get_third_wave_trigger_days(),
    )


def _resolve_payments_log_path() -> Path:
    """Resolve the payments log file path (mirrors the bot handler)."""
    log_file_path = Path(settings.LOG_FILE).resolve()
    current_dir = log_file_path.parent / 'current'
    return current_dir / settings.LOG_PAYMENTS_FILE


def _reconcile_from_payments_log(payments_log: Path) -> ReconcileReport:
    """Parse the payments log and report payments lacking receipts.

    Replicates the bot handler's log-based reconciliation: there is no
    reusable service for this, so the minimal parsing logic is duplicated
    here. Runs synchronously and is intended to be called via
    ``asyncio.to_thread``.
    """
    if not payments_log.exists():
        return ReconcileReport(
            log_found=False,
            log_path=str(payments_log),
            total_payments=0,
            total_receipts=0,
            missing_count=0,
            missing_amount=0.0,
            by_date=[],
            missing_payments=[],
        )

    payment_pattern = re.compile(
        r'(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}).*Успешно обработан платеж YooKassa '
        r'([a-f0-9-]+).*пользователь (\d+).*на ([\d.]+)₽'
    )
    receipt_pattern = re.compile(r'Чек NaloGO создан для платежа ([a-f0-9-]+)')

    payments: dict[str, dict[str, Any]] = {}
    receipts: set[str] = set()

    with open(payments_log, encoding='utf-8') as handle:
        for line in handle:
            match = payment_pattern.search(line)
            if match:
                date_str, time_str, payment_id, user_id, amount = match.groups()
                payments[payment_id] = {
                    'date': date_str,
                    'time': time_str,
                    'user_id': user_id,
                    'amount': float(amount),
                }
                continue

            match = receipt_pattern.search(line)
            if match:
                receipts.add(match.group(1))

    missing: list[ReconcileMissingPayment] = []
    by_date_map: dict[str, list[float]] = defaultdict(list)
    for payment_id, data in payments.items():
        if payment_id in receipts:
            continue
        missing.append(
            ReconcileMissingPayment(
                payment_id=payment_id,
                date=data['date'],
                time=data['time'],
                user_id=data['user_id'],
                amount=data['amount'],
            )
        )
        by_date_map[data['date']].append(data['amount'])

    missing.sort(key=lambda item: (item.date, item.time or ''), reverse=True)

    by_date = [
        ReconcileDateBucket(date=date_str, count=len(amounts), amount=sum(amounts))
        for date_str, amounts in sorted(by_date_map.items(), reverse=True)
    ]

    return ReconcileReport(
        log_found=True,
        log_path=str(payments_log),
        total_payments=len(payments),
        total_receipts=len(receipts),
        missing_count=len(missing),
        missing_amount=sum(item.amount for item in missing),
        by_date=by_date,
        missing_payments=missing,
    )


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------


@router.get('/status', response_model=MonitoringStatusResponse)
async def get_monitoring_status(
    admin: User = Depends(require_permission('monitoring:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get the current monitoring loop status and 24h statistics."""
    try:
        status_data = await monitoring_service.get_monitoring_status(db)
        stats = status_data.get('stats_24h', {})

        return MonitoringStatusResponse(
            is_running=bool(status_data.get('is_running')),
            last_update=status_data.get('last_update'),
            interval_minutes=settings.MONITORING_INTERVAL,
            notifications_enabled=bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True)),
            nalogo_enabled=settings.is_nalogo_enabled(),
            stats_24h=MonitoringStats24h(
                total_events=stats.get('total_events', 0),
                successful=stats.get('successful', 0),
                failed=stats.get('failed', 0),
                success_rate=stats.get('success_rate', 0),
            ),
            recent_events=[
                MonitoringEvent(
                    type=event['type'],
                    message=event['message'],
                    success=event['success'],
                    created_at=event['created_at'],
                )
                for event in status_data.get('recent_events', [])
            ],
        )
    except Exception as exc:
        logger.error('Failed to get monitoring status', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to get monitoring status: {exc!s}',
        )


@router.post('/start', response_model=MonitoringActionResponse)
async def start_monitoring_loop(
    admin: User = Depends(require_permission('monitoring:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Start the monitoring loop if it is not already running."""
    try:
        if monitoring_service.is_running:
            return MonitoringActionResponse(is_running=True, message='Monitoring is already running')

        # The cabinet has no bot instance to attach; the long-running bot
        # process owns it. Only launch the loop if a bot is already set.
        if not monitoring_service.bot:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Monitoring cannot be started from the cabinet because the bot instance is not available',
            )

        asyncio.create_task(monitoring_service.start_monitoring())

        logger.info('Admin started monitoring loop', admin_id=admin.id)
        return MonitoringActionResponse(is_running=True, message='Monitoring started')
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to start monitoring', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to start monitoring: {exc!s}',
        )


@router.post('/stop', response_model=MonitoringActionResponse)
async def stop_monitoring_loop(
    admin: User = Depends(require_permission('monitoring:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Stop the monitoring loop if it is running."""
    try:
        if not monitoring_service.is_running:
            return MonitoringActionResponse(is_running=False, message='Monitoring is already stopped')

        monitoring_service.stop_monitoring()
        logger.info('Admin stopped monitoring loop', admin_id=admin.id)
        return MonitoringActionResponse(is_running=False, message='Monitoring stopped')
    except Exception as exc:
        logger.error('Failed to stop monitoring', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to stop monitoring: {exc!s}',
        )


@router.post('/force-check', response_model=ForceCheckResponse)
async def force_check_subscriptions(
    admin: User = Depends(require_permission('monitoring:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Run a forced subscription check (expired / expiring / autopay)."""
    try:
        results = await monitoring_service.force_check_subscriptions(db)
        logger.info('Admin forced subscription check', admin_id=admin.id, results=results)
        return ForceCheckResponse(
            expired=results.get('expired', 0),
            expiring=results.get('expiring', 0),
            autopay_ready=results.get('autopay_ready', 0),
            checked_at=datetime.now(UTC),
            message='Force check completed',
        )
    except Exception as exc:
        logger.error('Failed to force-check subscriptions', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Force check failed: {exc!s}',
        )


@router.get('/logs', response_model=MonitoringLogsResponse)
async def get_monitoring_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    event_type: str | None = Query(None),
    admin: User = Depends(require_permission('monitoring:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get paginated monitoring logs with available event-type filters."""
    try:
        logs = await monitoring_service.get_monitoring_logs(
            db,
            event_type=event_type,
            page=page,
            per_page=per_page,
        )
        total = await monitoring_service.get_monitoring_logs_count(db, event_type=event_type)
        event_types = await monitoring_service.get_monitoring_event_types(db)

        return MonitoringLogsResponse(
            logs=[MonitoringLogItem(**log) for log in logs],
            total=total,
            page=page,
            per_page=per_page,
            event_types=event_types,
        )
    except Exception as exc:
        logger.error('Failed to get monitoring logs', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to get monitoring logs: {exc!s}',
        )


@router.delete('/logs', response_model=ClearLogsResponse)
async def clear_monitoring_logs(
    admin: User = Depends(require_permission('monitoring:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete all monitoring logs."""
    try:
        deleted = await monitoring_service.cleanup_old_logs(db, days=0)
        logger.info('Admin cleared monitoring logs', admin_id=admin.id, deleted=deleted)
        message = f'Deleted {deleted} log records' if deleted else 'Logs were already empty'
        return ClearLogsResponse(deleted=deleted, message=message)
    except Exception as exc:
        logger.error('Failed to clear monitoring logs', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to clear logs: {exc!s}',
        )


# ---------------------------------------------------------------------------
# NaloGO receipt queue
# ---------------------------------------------------------------------------


def _ensure_nalogo_enabled() -> None:
    if not settings.is_nalogo_enabled():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='NaloGO is not enabled',
        )


@router.get('/nalogo/queue', response_model=NalogoQueueStatusResponse)
async def get_nalogo_queue_status(
    admin: User = Depends(require_permission('monitoring:read')),
):
    """Get NaloGO send-queue and pending-verification status."""
    _ensure_nalogo_enabled()
    try:
        data = await nalogo_queue_service.get_status()
        return NalogoQueueStatusResponse(
            running=bool(data.get('running')),
            check_interval_seconds=data.get('check_interval_seconds', 0),
            receipt_delay_seconds=data.get('receipt_delay_seconds', 0),
            max_attempts=data.get('max_attempts', 0),
            queue_length=data.get('queue_length', 0),
            total_amount=data.get('total_amount', 0.0),
            queued_receipts=[
                NalogoQueuedReceipt(
                    payment_id=item.get('payment_id'),
                    amount=item.get('amount', 0.0),
                    name=item.get('name'),
                    attempts=item.get('attempts', 0),
                    created_at=item.get('created_at'),
                )
                for item in data.get('queued_receipts', [])
            ],
            pending_verification_count=data.get('pending_verification_count', 0),
            pending_verification_amount=data.get('pending_verification_amount', 0.0),
            pending_verification_receipts=[
                NalogoPendingReceipt(
                    payment_id=item.get('payment_id'),
                    amount=item.get('amount', 0.0),
                    name=item.get('name'),
                    created_at=item.get('created_at'),
                    error=item.get('error'),
                )
                for item in data.get('pending_verification_receipts', [])
            ],
        )
    except Exception as exc:
        logger.error('Failed to get NaloGO queue status', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to get NaloGO queue status: {exc!s}',
        )


@router.post('/nalogo/queue/force-process', response_model=NalogoForceProcessResponse)
async def force_process_nalogo_queue(
    admin: User = Depends(require_permission('monitoring:manage')),
):
    """Force the NaloGO queue worker to flush pending receipts now."""
    _ensure_nalogo_enabled()
    try:
        result = await nalogo_queue_service.force_process()
        if 'error' in result:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(result['error']),
            )
        logger.info('Admin force-processed NaloGO queue', admin_id=admin.id, result=result)
        return NalogoForceProcessResponse(
            message=result.get('message', 'Done'),
            was_in_queue=result.get('was_in_queue', 0),
            remaining=result.get('remaining', 0),
            processed=result.get('processed', 0),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to force-process NaloGO queue', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to process NaloGO queue: {exc!s}',
        )


@router.get('/nalogo/pending', response_model=NalogoPendingListResponse)
async def list_nalogo_pending(
    admin: User = Depends(require_permission('monitoring:read')),
):
    """List NaloGO receipts awaiting manual verification."""
    _ensure_nalogo_enabled()
    try:
        nalogo_service = NaloGoService()
        receipts = await nalogo_service.get_pending_verification_receipts()
        items = [
            NalogoPendingReceipt(
                payment_id=item.get('payment_id'),
                amount=item.get('amount', 0.0),
                name=item.get('name'),
                created_at=item.get('created_at'),
                error=item.get('error'),
            )
            for item in receipts
        ]
        return NalogoPendingListResponse(
            receipts=items,
            count=len(items),
            total_amount=sum(item.amount for item in items),
        )
    except Exception as exc:
        logger.error('Failed to list NaloGO pending receipts', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to list pending receipts: {exc!s}',
        )


@router.post('/nalogo/pending/verify', response_model=NalogoVerifyResponse)
async def verify_nalogo_pending(
    request: NalogoVerifyRequest,
    admin: User = Depends(require_permission('monitoring:manage')),
):
    """Mark a pending receipt as already created in nalog.ru and remove it."""
    _ensure_nalogo_enabled()
    try:
        nalogo_service = NaloGoService()
        removed = await nalogo_service.mark_pending_as_verified(
            request.payment_id,
            receipt_uuid=request.receipt_uuid,
            was_created=True,
        )
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Pending receipt not found',
            )
        logger.info('Admin verified NaloGO receipt', admin_id=admin.id, payment_id=request.payment_id)
        return NalogoVerifyResponse(
            removed=True,
            payment_id=request.payment_id,
            message='Receipt marked as verified',
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to verify NaloGO receipt', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to verify receipt: {exc!s}',
        )


@router.post('/nalogo/pending/retry', response_model=NalogoRetryResponse)
async def retry_nalogo_pending(
    request: NalogoRetryRequest,
    admin: User = Depends(require_permission('monitoring:manage')),
):
    """Retry sending a pending receipt to nalog.ru."""
    _ensure_nalogo_enabled()
    try:
        nalogo_service = NaloGoService()
        receipt_uuid = await nalogo_service.retry_pending_receipt(request.payment_id)
        if not receipt_uuid:
            return NalogoRetryResponse(
                success=False,
                payment_id=request.payment_id,
                receipt_uuid=None,
                message='Failed to create receipt (not found or nalog.ru unavailable)',
            )
        logger.info(
            'Admin retried NaloGO receipt',
            admin_id=admin.id,
            payment_id=request.payment_id,
            receipt_uuid=receipt_uuid,
        )
        return NalogoRetryResponse(
            success=True,
            payment_id=request.payment_id,
            receipt_uuid=receipt_uuid,
            message='Receipt created',
        )
    except Exception as exc:
        logger.error('Failed to retry NaloGO receipt', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to retry receipt: {exc!s}',
        )


@router.delete('/nalogo/pending', response_model=NalogoClearResponse)
async def clear_nalogo_pending(
    admin: User = Depends(require_permission('monitoring:manage')),
):
    """Clear the entire NaloGO manual-verification queue."""
    _ensure_nalogo_enabled()
    try:
        nalogo_service = NaloGoService()
        cleared = await nalogo_service.clear_pending_verification()
        logger.info('Admin cleared NaloGO pending queue', admin_id=admin.id, cleared=cleared)
        return NalogoClearResponse(cleared=cleared, message=f'Cleared {cleared} receipt(s)')
    except Exception as exc:
        logger.error('Failed to clear NaloGO pending queue', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to clear pending queue: {exc!s}',
        )


@router.get('/nalogo/reconcile', response_model=ReconcileReport)
async def reconcile_receipts(
    admin: User = Depends(require_permission('monitoring:read')),
):
    """Reconcile payments against created receipts using the payments log.

    Parses ``logs/current/<payments-log>`` for successful YooKassa payments
    and matching NaloGO receipt-created lines, reporting payments that have
    no receipt. There is no backing service for this in the bot, so the
    minimal parsing logic is replicated here.
    """
    try:
        payments_log = await asyncio.to_thread(_resolve_payments_log_path)
        report = await asyncio.to_thread(_reconcile_from_payments_log, payments_log)
        return report
    except Exception as exc:
        logger.error('Failed to reconcile receipts', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to reconcile receipts: {exc!s}',
        )


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------


@router.get('/notification-settings', response_model=NotificationSettingsResponse)
async def get_notification_settings(
    admin: User = Depends(require_permission('monitoring:read')),
):
    """Get win-back / expiry notification settings."""
    try:
        return _build_notification_settings_response()
    except Exception as exc:
        logger.error('Failed to get notification settings', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to get notification settings: {exc!s}',
        )


@router.put('/notification-settings', response_model=NotificationSettingsResponse)
async def update_notification_settings(
    request: NotificationSettingsUpdateRequest,
    admin: User = Depends(require_permission('monitoring:manage')),
):
    """Update win-back / expiry notification settings (partial)."""
    try:
        failures: list[str] = []

        if request.trial_channel_unsubscribed_enabled is not None:
            if not NotificationSettingsService.set_trial_channel_unsubscribed_enabled(
                request.trial_channel_unsubscribed_enabled
            ):
                failures.append('trial_channel_unsubscribed_enabled')

        if request.expired_1d_enabled is not None:
            if not NotificationSettingsService.set_expired_1d_enabled(request.expired_1d_enabled):
                failures.append('expired_1d_enabled')

        if request.second_wave_enabled is not None:
            if not NotificationSettingsService.set_second_wave_enabled(request.second_wave_enabled):
                failures.append('second_wave_enabled')

        if request.second_wave_discount_percent is not None:
            if not NotificationSettingsService.set_second_wave_discount_percent(request.second_wave_discount_percent):
                failures.append('second_wave_discount_percent')

        if request.second_wave_valid_hours is not None:
            if not NotificationSettingsService.set_second_wave_valid_hours(request.second_wave_valid_hours):
                failures.append('second_wave_valid_hours')

        if request.third_wave_enabled is not None:
            if not NotificationSettingsService.set_third_wave_enabled(request.third_wave_enabled):
                failures.append('third_wave_enabled')

        if request.third_wave_discount_percent is not None:
            if not NotificationSettingsService.set_third_wave_discount_percent(request.third_wave_discount_percent):
                failures.append('third_wave_discount_percent')

        if request.third_wave_valid_hours is not None:
            if not NotificationSettingsService.set_third_wave_valid_hours(request.third_wave_valid_hours):
                failures.append('third_wave_valid_hours')

        if request.third_wave_trigger_days is not None:
            if not NotificationSettingsService.set_third_wave_trigger_days(request.third_wave_trigger_days):
                failures.append('third_wave_trigger_days')

        if failures:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f'Failed to persist settings: {", ".join(failures)}',
            )

        logger.info('Admin updated notification settings', admin_id=admin.id)
        return _build_notification_settings_response()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('Failed to update notification settings', error=exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to update notification settings: {exc!s}',
        )
