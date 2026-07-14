from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime,
    Float, Text, ForeignKey, ARRAY, func, Enum
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship
import uuid
import enum


class ProtocolType(str, enum.Enum):
    VLESS = "vless"
    VMESS = "vmess"
    TROJAN = "trojan"
    SHADOWSOCKS = "shadowsocks"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)          # Telegram user_id
    username = Column(String(100), nullable=True)
    full_name = Column(String(200), nullable=True)
    sub_token = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False)
    xray_uuid = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False)
    balance = Column(Float, default=0.0)
    partner_balance = Column(Float, default=0.0)       # партнёрский баланс (для вывода)
    partner_earned = Column(Float, default=0.0)        # всего заработано за всё время
    referral_code = Column(String(20), unique=True, nullable=True)
    referred_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    is_banned = Column(Boolean, default=False)
    trial_used = Column(Boolean, default=False)
    language = Column(String(5), default="ru")           # ru / en
    created_at = Column(DateTime, server_default=func.now())

    # Авторизация через сайт
    web_password_hash = Column(String(200), nullable=True)
    web_token = Column(String(100), nullable=True)
    google_id = Column(String(100), unique=True, nullable=True)
    google_email = Column(String(200), nullable=True)

    subscriptions = relationship("Subscription", back_populates="user")
    payments = relationship("Payment", back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    status = Column(String(20), default="active")       # active / expired / banned
    traffic_limit_gb = Column(Float, nullable=True)     # None = безлимит
    traffic_used_gb = Column(Float, default=0.0)
    started_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime, nullable=True)        # None = бессрочно

    user = relationship("User", back_populates="subscriptions")
    plan = relationship("Plan")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)           # "Базовый · 1 месяц"
    description = Column(Text, nullable=True)
    price_rub = Column(Float, nullable=False)
    duration_days = Column(Integer, nullable=False)      # 30 / 90 / 365
    traffic_gb = Column(Float, nullable=True)            # None = безлимит
    max_devices = Column(Integer, default=1)             # limitIp в Xray node-agent
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class Server(Base):
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(100), nullable=False)          # "🇩🇪 Germany 1"
    flag = Column(String(10), default="🌍")

    # Подключение к панели Xray node-agent
    node_url = Column(String(255), nullable=True)       # http://10.0.0.2:8090
    node_path = Column(String(100), default="/")
    node_token = Column(String(255), nullable=True)
    node_cert = Column(Text, nullable=True)              # pinned CA/server certificate for HTTPS nodes
    inbound_id = Column(Integer, default=1)

    # SSH для авто-установки
    ssh_host = Column(String(255), nullable=True)
    ssh_port = Column(Integer, default=22)
    ssh_user = Column(String(100), default="root")
    ssh_password = Column(String(255), nullable=True)    # пароль или None если ключ
    ssh_key = Column(Text, nullable=True)                # приватный SSH ключ

    # Если Xray node-agent уже установлен — можно передать готовые учётные данные
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    install_status = Column(String(30), default="pending")  # pending/installing/ready/error
    install_log = Column(Text, nullable=True)
    # Геолокация (для Mini App карты)
    lat = Column(Float, nullable=True)                   # широта
    lng = Column(Float, nullable=True)                   # долгота
    city = Column(String(100), nullable=True)            # «Frankfurt am Main»
    country_code = Column(String(5), nullable=True)      # «DE»
    created_at = Column(DateTime, server_default=func.now())

    # SNI ротация
    sni_rotation_enabled = Column(Boolean, default=False)
    current_sni = Column(String(255), nullable=True)
    sni_last_rotated = Column(DateTime, nullable=True)

    # Мониторинг
    is_online = Column(Boolean, default=True)
    last_checked = Column(DateTime, nullable=True)
    last_online = Column(DateTime, nullable=True)


class SniRotationLog(Base):
    """История ротаций SNI."""
    __tablename__ = "sni_rotation_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    new_sni = Column(String(255), nullable=False)
    fingerprint = Column(String(50), nullable=True)
    success = Column(Boolean, default=True)
    error = Column(Text, nullable=True)
    rotated_at = Column(DateTime, server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    method = Column(String(30), nullable=False)          # heleket/cryptopay/manual/card_link
    amount = Column(Float, nullable=False)
    status = Column(String(20), default="pending")       # pending/paid/failed/cancelled
    external_id = Column(String(255), nullable=True)     # ID платежа во внешней системе
    created_at = Column(DateTime, server_default=func.now())
    paid_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="payments")
    plan = relationship("Plan")


class BalanceLog(Base):
    """История операций с балансом пользователя."""
    __tablename__ = "balance_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    amount = Column(Float, nullable=False)           # + пополнение, - списание
    comment = Column(String(255), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Broadcast(Base):
    """История рассылок."""
    __tablename__ = "broadcasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(BigInteger, nullable=False)
    target = Column(String(20), default="all")       # all / active
    msg_type = Column(String(20), nullable=False)    # text / photo / video / document
    text = Column(Text, nullable=True)
    file_id = Column(String(255), nullable=True)
    caption = Column(Text, nullable=True)
    button_text = Column(String(100), nullable=True)
    button_url = Column(String(500), nullable=True)
    sent_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


class PromoCode(Base):
    """Промокоды для скидок."""
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False)       # SALE30
    discount_type = Column(String(10), default="percent")        # percent / fixed
    discount_value = Column(Float, nullable=False)               # 30 (%) или 100 (руб)
    max_uses = Column(Integer, nullable=True)                    # None = безлимит
    uses_count = Column(Integer, default=0)
    valid_until = Column(DateTime, nullable=True)                # None = бессрочно
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class Settings(Base):
    """Единственная строка — глобальные настройки бота"""
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)

    # ── Ручная оплата ───────────────────────────────────────
    manual_payment_enabled = Column(Boolean, default=True)
    manual_payment_text = Column(Text, default=(
        "💬 <b>Оплата через поддержку</b>\n\n"
        "Напишите мне: @{support}\n\n"
        "Сообщите, что хотите оплатить тариф на сумму <b>{amount} руб</b> — "
        "я пополню ваш баланс, после чего вы переводите мне на карту."
    ))
    support_username = Column(String(100), default="your_support")

    # ── Прочие тексты ───────────────────────────────────────
    welcome_text = Column(Text, default=(
        "👤 {name}\n\n"
        "📋 Подписка: {sub_status}\n\n"
        "Выберите действие:"
    ))
    sub_issued_text = Column(Text, default=(
        "✅ <b>Подписка активна!</b>\n\n"
        "Твои серверы уже готовы. Нажми кнопку ниже — "
        "они добавятся в клиент автоматически."
    ))
    info_text = Column(Text, default=(
        "ℹ️ <b>Информация о сервисе</b>\n\n"
        "Здесь будет информация о вашем VPN сервисе.\n"
        "Настройте этот текст в панели администратора."
    ))

    # ── Юридические ссылки ──────────────────────────────────
    privacy_policy_url = Column(String(500), nullable=True)
    terms_of_service_url = Column(String(500), nullable=True)

    # ── Card link ───────────────────────────────────────────
    card_link_enabled = Column(Boolean, default=False)
    card_link_url = Column(String(500), nullable=True)
    card_link_text = Column(Text, default="💳 Оплатить картой онлайн")

    # ── Heleket ─────────────────────────────────────────────
    heleket_enabled = Column(Boolean, default=False)

    # ── CryptoPay ───────────────────────────────────────────
    cryptopay_enabled = Column(Boolean, default=False)

    # ── Реферальная система ─────────────────────────────────
    referral_enabled = Column(Boolean, default=True)
    referral_percent = Column(Float, default=10.0)       # % от суммы платежа рефералу
    referral_days_reward = Column(Integer, default=7)    # дней за реферала

    # ── Пробный период ──────────────────────────────────────
    trial_enabled = Column(Boolean, default=True)
    trial_days = Column(Integer, default=3)
    trial_traffic_gb = Column(Float, default=5.0)

    # ── Telegram Stars ──────────────────────────────────────
    stars_enabled = Column(Boolean, default=False)
    stars_rate = Column(Integer, default=60)             # 60 Stars = 1 месяц базовый

    # ── Продление со скидкой ────────────────────────────────
    renewal_discount_enabled = Column(Boolean, default=True)
    renewal_discount_percent = Column(Integer, default=10)
    renewal_remind_days = Column(Integer, default=3)

    # ── Промокоды ───────────────────────────────────────────
    promo_enabled = Column(Boolean, default=True)

    # ── Мониторинг серверов ─────────────────────────────────
    server_monitoring_enabled = Column(Boolean, default=True)

    # ── Баланс ──────────────────────────────────────────────
    balance_payment_enabled = Column(Boolean, default=True)

    # ── Автобэкап ───────────────────────────────────────────
    auto_backup_enabled = Column(Boolean, default=True)
    auto_backup_interval_hours = Column(Integer, default=24)

    # ── Принудительная подписка на канал ────────────────────
    channel_required = Column(Boolean, default=False)
    channel_id = Column(String(100), nullable=True)      # @channel_username
    channel_url = Column(String(255), nullable=True)     # https://t.me/channel

    # ── Статус-канал (Feature: Telegram status channel) ──────
    status_channel_id = Column(String(100), nullable=True)   # @channel или chat_id
    status_channel_alerts = Column(Boolean, default=True)     # постить при падении
    status_channel_sni_alerts = Column(Boolean, default=True) # постить при SNI-ротации

    # ── Тикет-система поддержки ──────────────────────────────
    support_chat_id = Column(String(100), nullable=True)
    support_operator_ids = Column(String(500), nullable=True)
    support_bot_token = Column(String(200), nullable=True)     # токен бота поддержки
    support_bot_username = Column(String(100), nullable=True)  # @username бота поддержки
    support_forum_chat_id = Column(String(100), nullable=True) # forum chat куда форвардить тикеты

    # ── Название проекта ─────────────────────────────────────
    project_name = Column(String(100), default="⚡ VPN ⚡")     # отображается в подписке

    # ── СБП (Platega.io) ─────────────────────────────────────────────────────
    sbp_enabled     = Column(Boolean, default=False)
    sbp_merchant_id = Column(String(200), nullable=True)
    sbp_secret_key  = Column(String(200), nullable=True)

    # ── Семейные тарифы ──────────────────────────────────────
    family_plans_enabled = Column(Boolean, default=True)

    # ── Протоколы ────────────────────────────────────────────
    default_protocol = Column(String(20), default="vless")


# ─────────────────────────────────────────────────────────────────────────────
# НОВЫЕ МОДЕЛИ v4
# ─────────────────────────────────────────────────────────────────────────────

class FamilyGroup(Base):
    """Групповая/семейная подписка — один владелец, несколько UUID."""
    __tablename__ = "family_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    name = Column(String(100), nullable=True)               # «Семья Ивановых»
    max_members = Column(Integer, default=5)
    status = Column(String(20), default="active")           # active / expired
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    owner = relationship("User", foreign_keys=[owner_id])
    members = relationship("FamilyMember", back_populates="group")


class FamilyMember(Base):
    """Участник семейной группы."""
    __tablename__ = "family_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("family_groups.id"), nullable=False)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)  # None если слот свободен
    xray_uuid = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False)
    nickname = Column(String(100), nullable=True)           # «Мама», «Папа», «Дочка»
    protocol = Column(String(20), default="vless")          # выбранный протокол
    joined_at = Column(DateTime, server_default=func.now())

    group = relationship("FamilyGroup", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class SupportTicket(Base):
    """Тикет поддержки — /support Текст жалобы."""
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    text = Column(Text, nullable=False)
    status = Column(String(20), default="open")             # open / answered / closed
    admin_id = Column(BigInteger, nullable=True)            # кто ответил
    answer = Column(Text, nullable=True)
    forwarded_msg_id = Column(Integer, nullable=True)
    subject = Column(String(200), nullable=True)
    forum_chat_id = Column(String(100), nullable=True)
    message_thread_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    answered_at = Column(DateTime, nullable=True)

    user = relationship("User", foreign_keys=[user_id])


class SupportMessage(Base):
    """Сообщения внутри тикета (история переписки)."""
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False)
    sender = Column(String(20), nullable=False)   # user / admin / note
    content = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    ticket = relationship("SupportTicket", foreign_keys=[ticket_id])


class ServerProtocol(Base):
    """Настройки протокола на конкретном сервере (inbound_id под каждый протокол)."""
    __tablename__ = "server_protocols"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)
    protocol = Column(String(20), nullable=False)           # vless/vmess/trojan/shadowsocks
    inbound_id = Column(Integer, nullable=False)
    port = Column(Integer, nullable=True)
    enabled = Column(Boolean, default=True)
    extra_config = Column(Text, nullable=True)              # JSON доп. настроек (ss-method и т.д.)

    server = relationship("Server", foreign_keys=[server_id])


class UserProtocolChoice(Base):
    """Выбранный пользователем протокол (или протокол участника семейной группы)."""
    __tablename__ = "user_protocol_choices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    protocol = Column(String(20), default="vless")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", foreign_keys=[user_id])


# ─── Серверы тарифа (many-to-many) ───────────────────────────────────────────

class PlanServer(Base):
    """Привязка серверов к тарифу. Если записей нет — используются все серверы."""
    __tablename__ = "plan_servers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    server_id = Column(Integer, ForeignKey("servers.id"), nullable=False)


# ─── SBP / Platega поля добавляются через миграцию 0010 ──────────────────────
# Settings.sbp_enabled      Boolean
# Settings.sbp_merchant_id  String(200)
# Settings.sbp_secret_key   String(200)
# Payment.pay_url            String(500)


class ReferralLog(Base):
    """История реферальных начислений."""
    __tablename__ = "referral_logs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    payer_id    = Column(BigInteger, ForeignKey("users.id"), nullable=False)
    amount      = Column(Float, nullable=False)           # начислено рефереру (₽)
    source      = Column(String(200), nullable=True)      # откуда: «тариф X» / «пополнение» и т.д.
    created_at  = Column(DateTime, server_default=func.now())

    referrer = relationship("User", foreign_keys=[referrer_id])
    payer    = relationship("User", foreign_keys=[payer_id])
