"""Admin routes for managing maintenance mode in cabinet.

These endpoints are a thin authenticated wrapper around the singleton
``maintenance_service``. CRITICAL: editing ``settings.MAINTENANCE_MODE``
does NOT flip the runtime maintenance flag, so all toggling here must go
through ``maintenance_service.enable_maintenance`` / ``disable_maintenance``
directly (which is exactly what the bot handler does).
"""

from datetime import datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.database.models import User
from app.services.maintenance_service import maintenance_service

from ..dependencies import require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/maintenance', tags=['Cabinet Admin Maintenance'])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MaintenanceStatusResponse(BaseModel):
    """Runtime maintenance status as reported by maintenance_service."""

    is_active: bool
    enabled_at: datetime | None = None
    last_check: datetime | None = None
    reason: str | None = None
    auto_enabled: bool
    api_status: bool
    consecutive_failures: int
    monitoring_active: bool
    monitoring_configured: bool
    auto_enable_configured: bool
    check_interval: int
    bot_connected: bool


class PanelStatusResponse(BaseModel):
    """Summary of the Remnawave panel health (from RemnaWaveService)."""

    status: str
    description: str
    response_time: float
    api_available: bool
    nodes_status: str
    users_online: int
    last_check: datetime | None = None
    has_issues: bool
    recommendation: str | None = None
    error: str | None = None


class EnableMaintenanceRequest(BaseModel):
    """Request to enable maintenance mode."""

    reason: str | None = Field(None, max_length=200)


class MaintenanceToggleResponse(BaseModel):
    """Result of an enable/disable maintenance action."""

    success: bool
    is_active: bool
    message: str


class MonitoringToggleResponse(BaseModel):
    """Result of a start/stop monitoring action."""

    success: bool
    monitoring_active: bool
    message: str


class ForceCheckResponse(BaseModel):
    """Result of a forced API availability check."""

    success: bool
    api_available: bool
    response_time: float
    checked_at: datetime | None = None
    consecutive_failures: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_response() -> MaintenanceStatusResponse:
    """Build a status response from the current maintenance_service state."""
    info = maintenance_service.get_status_info()
    return MaintenanceStatusResponse(
        is_active=info['is_active'],
        enabled_at=info['enabled_at'],
        last_check=info['last_check'],
        reason=info['reason'],
        auto_enabled=info['auto_enabled'],
        api_status=info['api_status'],
        consecutive_failures=info['consecutive_failures'],
        monitoring_active=info['monitoring_active'],
        monitoring_configured=info['monitoring_configured'],
        auto_enable_configured=info['auto_enable_configured'],
        check_interval=info['check_interval'],
        bot_connected=info['bot_connected'],
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get('/status', response_model=MaintenanceStatusResponse)
async def get_maintenance_status(
    admin: User = Depends(require_permission('maintenance:read')),
):
    """Get the current runtime maintenance status."""
    try:
        return _status_response()
    except Exception as e:
        logger.error('Failed to read maintenance status', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to read maintenance status: {e!s}',
        )


@router.get('/panel-status', response_model=PanelStatusResponse)
async def get_panel_status(
    admin: User = Depends(require_permission('maintenance:read')),
):
    """Get a summary of the Remnawave panel health."""
    try:
        from app.services.remnawave_service import RemnaWaveService

        rw_service = RemnaWaveService()
        if not rw_service.is_configured:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='RemnaWave is not configured',
            )

        summary = await rw_service.get_panel_status_summary()
        return PanelStatusResponse(
            status=summary.get('status', 'error'),
            description=summary.get('description', ''),
            response_time=summary.get('response_time', 0),
            api_available=summary.get('api_available', False),
            nodes_status=summary.get('nodes_status', 'неизвестно'),
            users_online=summary.get('users_online', 0),
            last_check=summary.get('last_check'),
            has_issues=summary.get('has_issues', True),
            recommendation=summary.get('recommendation'),
            error=summary.get('error'),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to read panel status', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to read panel status: {e!s}',
        )


@router.post('/enable', response_model=MaintenanceToggleResponse)
async def enable_maintenance(
    request: EnableMaintenanceRequest,
    admin: User = Depends(require_permission('maintenance:manage')),
):
    """Enable maintenance mode (flips the runtime flag, not just config)."""
    try:
        reason = request.reason.strip() if request.reason else None
        if reason == '':
            reason = None

        success = await maintenance_service.enable_maintenance(reason=reason, auto=False)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to enable maintenance mode',
            )

        logger.warning('Admin enabled maintenance mode', admin_id=admin.id, reason=reason)
        return MaintenanceToggleResponse(
            success=True,
            is_active=maintenance_service.is_maintenance_active(),
            message='Maintenance mode enabled',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to enable maintenance mode', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to enable maintenance mode: {e!s}',
        )


@router.post('/disable', response_model=MaintenanceToggleResponse)
async def disable_maintenance(
    admin: User = Depends(require_permission('maintenance:manage')),
):
    """Disable maintenance mode (flips the runtime flag, not just config)."""
    try:
        success = await maintenance_service.disable_maintenance()
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to disable maintenance mode',
            )

        logger.info('Admin disabled maintenance mode', admin_id=admin.id)
        return MaintenanceToggleResponse(
            success=True,
            is_active=maintenance_service.is_maintenance_active(),
            message='Maintenance mode disabled',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to disable maintenance mode', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to disable maintenance mode: {e!s}',
        )


@router.post('/monitoring/start', response_model=MonitoringToggleResponse)
async def start_monitoring(
    admin: User = Depends(require_permission('maintenance:manage')),
):
    """Start the API monitoring loop."""
    try:
        success = await maintenance_service.start_monitoring()
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to start monitoring',
            )

        logger.info('Admin started maintenance monitoring', admin_id=admin.id)
        return MonitoringToggleResponse(
            success=True,
            monitoring_active=maintenance_service.get_status_info()['monitoring_active'],
            message='Monitoring started',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to start monitoring', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to start monitoring: {e!s}',
        )


@router.post('/monitoring/stop', response_model=MonitoringToggleResponse)
async def stop_monitoring(
    admin: User = Depends(require_permission('maintenance:manage')),
):
    """Stop the API monitoring loop."""
    try:
        success = await maintenance_service.stop_monitoring()
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to stop monitoring',
            )

        logger.info('Admin stopped maintenance monitoring', admin_id=admin.id)
        return MonitoringToggleResponse(
            success=True,
            monitoring_active=maintenance_service.get_status_info()['monitoring_active'],
            message='Monitoring stopped',
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error('Failed to stop monitoring', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to stop monitoring: {e!s}',
        )


@router.post('/check-api', response_model=ForceCheckResponse)
async def force_api_check(
    admin: User = Depends(require_permission('maintenance:manage')),
):
    """Force an immediate Remnawave API availability check."""
    try:
        result = await maintenance_service.force_api_check()
        logger.info(
            'Admin forced API check',
            admin_id=admin.id,
            success=result.get('success'),
            api_available=result.get('api_available'),
        )
        return ForceCheckResponse(
            success=result.get('success', False),
            api_available=result.get('api_available', False),
            response_time=result.get('response_time', 0),
            checked_at=result.get('checked_at'),
            consecutive_failures=result.get('consecutive_failures', 0),
            error=result.get('error'),
        )
    except Exception as e:
        logger.error('Failed to force API check', admin_id=admin.id, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'Failed to force API check: {e!s}',
        )
