"""Admin routes for payment method configuration in cabinet."""

import time
from datetime import datetime

import structlog
from aiogram import Bot
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.services.payment_method_config_service import (
    _get_method_defaults,
    get_all_configs,
    get_all_promo_groups,
    get_config_by_method_id,
    update_config,
    update_sort_order,
)
from app.services.payment_service import PaymentService

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/payment-methods', tags=['Cabinet Admin Payment Methods'])


# ============ Schemas ============


class SubOptionInfo(BaseModel):
    id: str
    name: str


class PaymentMethodConfigResponse(BaseModel):
    method_id: str
    sort_order: int
    is_enabled: bool
    display_name: str | None = None
    default_display_name: str
    sub_options: dict | None = None
    available_sub_options: list[SubOptionInfo] | None = None
    min_amount_kopeks: int | None = None
    max_amount_kopeks: int | None = None
    default_min_amount_kopeks: int
    default_max_amount_kopeks: int
    user_type_filter: str
    first_topup_filter: str
    promo_group_filter_mode: str
    allowed_promo_group_ids: list[int] = Field(default_factory=list)
    open_url_direct: bool = False
    is_provider_configured: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class PaymentMethodConfigUpdateRequest(BaseModel):
    is_enabled: bool | None = None
    display_name: str | None = Field(default=None, description='Null to reset to default')
    sub_options: dict[str, bool] | None = None
    min_amount_kopeks: int | None = Field(default=None, ge=0)
    max_amount_kopeks: int | None = Field(default=None, ge=0)
    user_type_filter: str | None = Field(default=None, pattern='^(all|telegram|email)$')

    @field_validator('sub_options', mode='before')
    @classmethod
    def validate_sub_options(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if not v:
            return None
        if len(v) > 20:
            raise ValueError('sub_options cannot have more than 20 keys')
        for key in v:
            if not isinstance(key, str) or len(key) > 50:
                raise ValueError('sub_options keys must be strings of at most 50 characters')
        return v

    first_topup_filter: str | None = Field(default=None, pattern='^(any|yes|no)$')
    promo_group_filter_mode: str | None = Field(default=None, pattern='^(all|selected)$')
    allowed_promo_group_ids: list[int] | None = None
    open_url_direct: bool | None = None
    # Allow explicitly resetting display_name to null
    reset_display_name: bool = False
    reset_min_amount: bool = False
    reset_max_amount: bool = False


class SortOrderRequest(BaseModel):
    method_ids: list[str]


class PromoGroupSimple(BaseModel):
    id: int
    name: str

    class Config:
        from_attributes = True


# ============ Helpers ============


def _enrich_config(config, defaults: dict) -> PaymentMethodConfigResponse:
    """Enrich a PaymentMethodConfig with env-var defaults."""
    method_def = defaults.get(config.method_id, {})

    available_sub_options = None
    raw_options = method_def.get('available_sub_options')
    if raw_options:
        available_sub_options = [SubOptionInfo(**opt) for opt in raw_options]

    return PaymentMethodConfigResponse(
        method_id=config.method_id,
        sort_order=config.sort_order,
        is_enabled=config.is_enabled,
        display_name=config.display_name,
        default_display_name=method_def.get('default_display_name', config.method_id),
        sub_options=config.sub_options,
        available_sub_options=available_sub_options,
        min_amount_kopeks=config.min_amount_kopeks,
        max_amount_kopeks=config.max_amount_kopeks,
        default_min_amount_kopeks=method_def.get('default_min', 1000),
        default_max_amount_kopeks=method_def.get('default_max', 10000000),
        user_type_filter=config.user_type_filter,
        first_topup_filter=config.first_topup_filter,
        promo_group_filter_mode=config.promo_group_filter_mode,
        allowed_promo_group_ids=[pg.id for pg in config.allowed_promo_groups],
        open_url_direct=bool(getattr(config, 'open_url_direct', False)),
        is_provider_configured=method_def.get('is_configured', False),
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


# ============ Routes ============


@router.get('', response_model=list[PaymentMethodConfigResponse])
async def list_payment_methods(
    admin: User = Depends(require_permission('payment_methods:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all payment method configurations."""
    configs = await get_all_configs(db)
    defaults = _get_method_defaults()
    return [_enrich_config(c, defaults) for c in configs]


@router.get('/promo-groups', response_model=list[PromoGroupSimple])
async def list_promo_groups(
    admin: User = Depends(require_permission('payment_methods:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """List all promo groups for filter selector."""
    groups = await get_all_promo_groups(db)
    return [PromoGroupSimple(id=g.id, name=g.name) for g in groups]


@router.get('/{method_id}', response_model=PaymentMethodConfigResponse)
async def get_payment_method(
    method_id: str,
    admin: User = Depends(require_permission('payment_methods:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get a single payment method configuration."""
    config = await get_config_by_method_id(db, method_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Payment method not found: {method_id}',
        )
    defaults = _get_method_defaults()
    return _enrich_config(config, defaults)


@router.put('/order')
async def update_payment_methods_order(
    request: SortOrderRequest,
    admin: User = Depends(require_permission('payment_methods:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Batch update sort order for payment methods."""
    await update_sort_order(db, request.method_ids)
    logger.info('Admin updated payment methods order', admin_id=admin.id, method_ids=request.method_ids)
    return {'success': True}


@router.put('/{method_id}', response_model=PaymentMethodConfigResponse)
async def update_payment_method(
    method_id: str,
    request: PaymentMethodConfigUpdateRequest,
    admin: User = Depends(require_permission('payment_methods:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update a payment method configuration."""
    # Build update data dict
    data = {}

    if request.is_enabled is not None:
        data['is_enabled'] = request.is_enabled

    if request.reset_display_name:
        data['display_name'] = None
    elif request.display_name is not None:
        data['display_name'] = request.display_name.strip() or None

    if request.sub_options is not None:
        data['sub_options'] = request.sub_options

    if request.reset_min_amount:
        data['min_amount_kopeks'] = None
    elif request.min_amount_kopeks is not None:
        data['min_amount_kopeks'] = request.min_amount_kopeks

    if request.reset_max_amount:
        data['max_amount_kopeks'] = None
    elif request.max_amount_kopeks is not None:
        data['max_amount_kopeks'] = request.max_amount_kopeks

    if request.user_type_filter is not None:
        data['user_type_filter'] = request.user_type_filter

    if request.first_topup_filter is not None:
        data['first_topup_filter'] = request.first_topup_filter

    if request.promo_group_filter_mode is not None:
        data['promo_group_filter_mode'] = request.promo_group_filter_mode

    if request.open_url_direct is not None:
        data['open_url_direct'] = request.open_url_direct

    promo_group_ids = request.allowed_promo_group_ids

    config = await update_config(db, method_id, data, promo_group_ids)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Payment method not found: {method_id}',
        )

    logger.info('Admin updated payment method config', admin_id=admin.id, method_id=method_id)

    defaults = _get_method_defaults()
    return _enrich_config(config, defaults)


# ============ Provider test schemas ============


class PaymentTestRequest(BaseModel):
    """Body for creating a real low-value test payment.

    A test payment is a real, payable transaction for a small amount, so the
    caller must explicitly confirm before it is created.
    """

    confirm: bool = Field(
        default=False,
        description='Must be true to create a real (low-value) test payment.',
    )


class PaymentTestResponse(BaseModel):
    provider: str
    amount_kopeks: int
    # Identifiers for the follow-up status check (provider-specific).
    local_payment_id: int | None = None
    order_id: str | None = None
    provider_payment_id: str | None = None
    # Primary payable link plus any provider-specific alternatives.
    payment_url: str | None = None
    extra_urls: dict[str, str] = Field(default_factory=dict)
    # How to check status afterwards (None if the provider exposes no checker).
    status_check_param: str | None = None
    notes: str | None = None


class PaymentTestStatusResponse(BaseModel):
    provider: str
    status: str | None = None
    is_paid: bool = False
    raw: dict | None = None


# ============ Provider test helpers ============

# Providers whose test flow the bot implements via PaymentService and that we
# can therefore mirror here. Each entry declares whether the provider is
# enabled, the low-value test amount (kopeks), whether a Bot instance is
# required, and the param the status checker keys off.
_TEST_PROVIDERS: dict[str, dict[str, object]] = {
    'yookassa': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'mulenpay': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'pal24': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'cryptobot': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'freekassa': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'kassa_ai': {'requires_bot': False, 'status_param': 'local_payment_id'},
    'riopay': {'requires_bot': False, 'status_param': 'order_id'},
    'stars': {'requires_bot': True, 'status_param': None},
}


def _provider_enabled(provider: str) -> bool:
    checks = {
        'yookassa': settings.is_yookassa_enabled,
        'mulenpay': settings.is_mulenpay_enabled,
        'pal24': settings.is_pal24_enabled,
        'cryptobot': settings.is_cryptobot_enabled,
        'freekassa': settings.is_freekassa_enabled,
        'kassa_ai': settings.is_kassa_ai_enabled,
        'riopay': settings.is_riopay_enabled,
        'stars': lambda: bool(settings.TELEGRAM_STARS_ENABLED),
    }
    check = checks.get(provider)
    return bool(check()) if check else False


async def _create_test_payment(
    provider: str,
    payment_service: PaymentService,
    db: AsyncSession,
    admin: User,
) -> PaymentTestResponse:
    """Create one real low-value test payment, mirroring the bot test buttons."""
    language = getattr(admin, 'language', None) or settings.DEFAULT_LANGUAGE

    if provider == 'yookassa':
        amount_kopeks = 10 * 100
        result = await payment_service.create_yookassa_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж (админ, кабинет)',
            metadata={
                'user_telegram_id': str(getattr(admin, 'telegram_id', '') or ''),
                'purpose': 'admin_test_payment',
                'provider': 'yookassa',
            },
        )
        if not result or not result.get('confirmation_url'):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create YooKassa test payment',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            provider_payment_id=str(result.get('yookassa_payment_id') or '') or None,
            payment_url=result['confirmation_url'],
            status_check_param='local_payment_id',
        )

    if provider == 'mulenpay':
        amount_kopeks = 1 * 100
        result = await payment_service.create_mulenpay_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж MulenPay (админ, кабинет)',
            language=language,
        )
        if not result or not result.get('payment_url'):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create MulenPay test payment',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            provider_payment_id=str(result.get('mulen_payment_id') or '') or None,
            payment_url=result['payment_url'],
            status_check_param='local_payment_id',
        )

    if provider == 'pal24':
        amount_kopeks = 10 * 100
        result = await payment_service.create_pal24_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж PayPalych (админ, кабинет)',
            language=language or 'ru',
        )
        if not result:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create PayPalych test payment',
            )
        sbp_url = result.get('sbp_url') or result.get('transfer_url') or result.get('link_url')
        card_url = result.get('card_url')
        fallback_url = result.get('link_page_url') or result.get('link_url')
        primary = sbp_url or card_url or fallback_url
        if not primary:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to obtain PayPalych payment link',
            )
        extra: dict[str, str] = {}
        if sbp_url:
            extra['sbp_url'] = sbp_url
        if card_url and card_url != sbp_url:
            extra['card_url'] = card_url
        if fallback_url and fallback_url not in (sbp_url, card_url):
            extra['link_page_url'] = fallback_url
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            provider_payment_id=str(result.get('bill_id') or '') or None,
            payment_url=primary,
            extra_urls=extra,
            status_check_param='local_payment_id',
        )

    if provider == 'cryptobot':
        amount_rubles = 100.0
        try:
            from app.utils.currency_converter import currency_converter

            current_rate = await currency_converter.get_usd_to_rub_rate()
        except Exception as error:
            logger.warning('Failed to fetch USD/RUB rate for test payment', error=str(error))
            current_rate = None
        if not current_rate or current_rate <= 0:
            current_rate = 100.0
        amount_usd = round(amount_rubles / current_rate, 2)
        if amount_usd < 1:
            amount_usd = 1.0
        result = await payment_service.create_cryptobot_payment(
            db=db,
            user_id=admin.id,
            amount_usd=amount_usd,
            asset=settings.CRYPTOBOT_DEFAULT_ASSET,
            description=f'Тестовый платеж CryptoBot {amount_rubles:.0f} (админ, кабинет)',
            payload=f'admin_cryptobot_test_{admin.id}_{int(time.time())}',
        )
        if not result:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create CryptoBot test payment',
            )
        payment_url = (
            result.get('bot_invoice_url')
            or result.get('mini_app_invoice_url')
            or result.get('web_app_invoice_url')
        )
        if not payment_url:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to obtain CryptoBot payment link',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=int(amount_rubles * 100),
            local_payment_id=result.get('local_payment_id'),
            provider_payment_id=str(result.get('invoice_id') or '') or None,
            payment_url=payment_url,
            status_check_param='local_payment_id',
            notes=f'asset={result.get("asset")}, amount_usd={amount_usd:.2f}',
        )

    if provider == 'freekassa':
        amount_kopeks = settings.FREEKASSA_MIN_AMOUNT_KOPEKS
        result = await payment_service.create_freekassa_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж Freekassa (админ, кабинет)',
            email=getattr(admin, 'email', None),
            language=language,
        )
        if not result or not result.get('payment_url'):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create Freekassa test payment',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            order_id=str(result.get('order_id') or '') or None,
            payment_url=result['payment_url'],
            status_check_param='local_payment_id',
        )

    if provider == 'kassa_ai':
        amount_kopeks = settings.KASSA_AI_MIN_AMOUNT_KOPEKS
        result = await payment_service.create_kassa_ai_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж Kassa AI (админ, кабинет)',
            email=getattr(admin, 'email', None),
            language=language,
        )
        if not result or not result.get('payment_url'):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create Kassa AI test payment',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            order_id=str(result.get('order_id') or '') or None,
            payment_url=result['payment_url'],
            status_check_param='local_payment_id',
        )

    if provider == 'riopay':
        amount_kopeks = settings.RIOPAY_MIN_AMOUNT_KOPEKS
        result = await payment_service.create_riopay_payment(
            db=db,
            user_id=admin.id,
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж RioPay (админ, кабинет)',
            email=getattr(admin, 'email', None),
            language=language,
        )
        if not result or not result.get('payment_url'):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create RioPay test payment',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            local_payment_id=result.get('local_payment_id'),
            order_id=str(result.get('order_id') or '') or None,
            payment_url=result['payment_url'],
            status_check_param='order_id',
        )

    if provider == 'stars':
        stars_rate = settings.get_stars_rate()
        amount_kopeks = max(1, int(round(stars_rate * 100)))
        payload = f'admin_stars_test_{admin.id}_{int(time.time())}'
        invoice_link = await payment_service.create_stars_invoice(
            amount_kopeks=amount_kopeks,
            description='Тестовый платеж Telegram Stars (админ, кабинет)',
            payload=payload,
        )
        if not invoice_link:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail='Failed to create Telegram Stars test invoice',
            )
        return PaymentTestResponse(
            provider=provider,
            amount_kopeks=amount_kopeks,
            payment_url=invoice_link,
            status_check_param=None,
            notes='Telegram Stars invoices have no server-side status checker; '
            'verify via the bot after paying.',
        )

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f'Unsupported test provider: {provider}',
    )


# ============ Provider test routes ============


@router.post('/{provider}/test', response_model=PaymentTestResponse)
async def create_provider_test_payment(
    provider: str,
    request: PaymentTestRequest,
    admin: User = Depends(require_permission('payment_methods:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a real low-value test payment for a provider (mirrors the bot test buttons).

    This is a financial operation: it creates a genuine payable transaction, so
    the caller must pass ``confirm=true``.
    """
    provider = provider.lower().strip()

    if provider not in _TEST_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Unsupported test provider: {provider}',
        )

    if not request.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='confirm must be true to create a real test payment',
        )

    if not _provider_enabled(provider):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Provider is disabled: {provider}',
        )

    logger.info(
        'Admin creating provider test payment',
        admin_id=admin.id,
        provider=provider,
    )

    requires_bot = bool(_TEST_PROVIDERS[provider]['requires_bot'])
    bot: Bot | None = None
    try:
        if requires_bot:
            if not settings.BOT_TOKEN:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail='BOT_TOKEN is not configured; cannot create this test payment',
                )
            bot = Bot(token=settings.BOT_TOKEN)
        payment_service = PaymentService(bot)
        response = await _create_test_payment(provider, payment_service, db, admin)
        logger.info(
            'Admin created provider test payment',
            admin_id=admin.id,
            provider=provider,
            local_payment_id=response.local_payment_id,
            order_id=response.order_id,
        )
        return response
    except HTTPException:
        raise
    except Exception as error:
        logger.error(
            'Failed to create provider test payment',
            admin_id=admin.id,
            provider=provider,
            error=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Failed to create test payment: {error}',
        )
    finally:
        if bot is not None:
            await bot.session.close()


@router.get('/{provider}/test/status', response_model=PaymentTestStatusResponse)
async def check_provider_test_payment_status(
    provider: str,
    local_payment_id: int | None = None,
    order_id: str | None = None,
    admin: User = Depends(require_permission('payment_methods:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Check the status of a previously created provider test payment."""
    provider = provider.lower().strip()

    if provider not in _TEST_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Unsupported test provider: {provider}',
        )

    status_param = _TEST_PROVIDERS[provider]['status_param']
    if status_param is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Provider does not support a server-side status check: {provider}',
        )

    if status_param == 'local_payment_id' and local_payment_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='local_payment_id query parameter is required',
        )
    if status_param == 'order_id' and not order_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='order_id query parameter is required',
        )

    payment_service = PaymentService(None)
    try:
        if provider == 'yookassa':
            result = await payment_service.get_yookassa_payment_status(db, local_payment_id)
        elif provider == 'mulenpay':
            result = await payment_service.get_mulenpay_payment_status(db, local_payment_id)
        elif provider == 'pal24':
            result = await payment_service.get_pal24_payment_status(db, local_payment_id)
        elif provider == 'cryptobot':
            result = await payment_service.get_cryptobot_payment_status(db, local_payment_id)
        elif provider == 'freekassa':
            result = await payment_service.get_freekassa_payment_status(db, local_payment_id)
        elif provider == 'kassa_ai':
            result = await payment_service.get_kassa_ai_payment_status(db, local_payment_id)
        elif provider == 'riopay':
            result = await payment_service.check_riopay_payment_status(db, order_id)
        else:  # pragma: no cover - guarded above
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Unsupported test provider: {provider}',
            )
    except HTTPException:
        raise
    except Exception as error:
        logger.error(
            'Failed to check provider test payment status',
            admin_id=admin.id,
            provider=provider,
            error=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f'Failed to check payment status: {error}',
        )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Test payment not found',
        )

    payment = result.get('payment') if isinstance(result, dict) else None
    status_value = result.get('status') if isinstance(result, dict) else None
    if status_value is None and payment is not None:
        status_value = getattr(payment, 'status', None)

    is_paid = False
    if isinstance(result, dict) and 'is_paid' in result:
        is_paid = bool(result.get('is_paid'))
    elif payment is not None:
        is_paid = bool(getattr(payment, 'is_paid', False))

    return PaymentTestStatusResponse(
        provider=provider,
        status=status_value,
        is_paid=is_paid,
    )
