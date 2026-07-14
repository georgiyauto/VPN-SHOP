"""v4: family groups, support tickets, protocols, server geo, notif settings

Revision ID: 0004_v4_features
Revises: 0003_v3_features  (поменяй на актуальный revision ID вашего проекта)
Create Date: 2026-04-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers
revision = '0004_v4_features'
down_revision = None  # ← ЗАМЕНИТЕ на реальный ID последней миграции
branch_labels = None
depends_on = None


def upgrade():
    # ── family_groups ────────────────────────────────────────────────────────
    op.create_table(
        'family_groups',
        sa.Column('id',          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('owner_id',    sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('plan_id',     sa.Integer(),    sa.ForeignKey('plans.id'), nullable=False),
        sa.Column('name',        sa.String(100), nullable=True),
        sa.Column('max_members', sa.Integer(), default=5),
        sa.Column('status',      sa.String(20), default='active'),
        sa.Column('expires_at',  sa.DateTime(), nullable=True),
        sa.Column('created_at',  sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_family_groups_owner', 'family_groups', ['owner_id'])

    # ── family_members ───────────────────────────────────────────────────────
    op.create_table(
        'family_members',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('group_id',   sa.Integer(), sa.ForeignKey('family_groups.id'), nullable=False),
        sa.Column('user_id',    sa.BigInteger(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('xray_uuid',   UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column('nickname',   sa.String(100), nullable=True),
        sa.Column('protocol',   sa.String(20), default='vless'),
        sa.Column('joined_at',  sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_family_members_group', 'family_members', ['group_id'])
    op.create_index('ix_family_members_user',  'family_members', ['user_id'])

    # ── support_tickets ──────────────────────────────────────────────────────
    op.create_table(
        'support_tickets',
        sa.Column('id',                sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id',           sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('text',              sa.Text(), nullable=False),
        sa.Column('status',            sa.String(20), default='open'),
        sa.Column('admin_id',          sa.BigInteger(), nullable=True),
        sa.Column('answer',            sa.Text(), nullable=True),
        sa.Column('forwarded_msg_id',  sa.Integer(), nullable=True),
        sa.Column('created_at',        sa.DateTime(), server_default=sa.func.now()),
        sa.Column('answered_at',       sa.DateTime(), nullable=True),
    )
    op.create_index('ix_support_tickets_user',   'support_tickets', ['user_id'])
    op.create_index('ix_support_tickets_status', 'support_tickets', ['status'])

    # ── server_protocols ─────────────────────────────────────────────────────
    op.create_table(
        'server_protocols',
        sa.Column('id',           sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('server_id',    sa.Integer(), sa.ForeignKey('servers.id'), nullable=False),
        sa.Column('protocol',     sa.String(20), nullable=False),
        sa.Column('inbound_id',   sa.Integer(), nullable=False),
        sa.Column('port',         sa.Integer(), nullable=True),
        sa.Column('enabled',      sa.Boolean(), default=True),
        sa.Column('extra_config', sa.Text(), nullable=True),
    )
    op.create_index('ix_server_protocols_server', 'server_protocols', ['server_id'])

    # ── user_protocol_choices ────────────────────────────────────────────────
    op.create_table(
        'user_protocol_choices',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id',    sa.BigInteger(), sa.ForeignKey('users.id'), nullable=False, unique=True),
        sa.Column('protocol',   sa.String(20), default='vless'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )

    # ── Новые колонки в servers ───────────────────────────────────────────────
    op.add_column('servers', sa.Column('lat',          sa.Float(), nullable=True))
    op.add_column('servers', sa.Column('lng',          sa.Float(), nullable=True))
    op.add_column('servers', sa.Column('city',         sa.String(100), nullable=True))
    op.add_column('servers', sa.Column('country_code', sa.String(5),   nullable=True))

    # ── Новые колонки в settings ──────────────────────────────────────────────
    op.add_column('settings', sa.Column('status_channel_id',      sa.String(100), nullable=True))
    op.add_column('settings', sa.Column('status_channel_alerts',  sa.Boolean(),   server_default='true'))
    op.add_column('settings', sa.Column('status_channel_sni_alerts', sa.Boolean(), server_default='true'))
    op.add_column('settings', sa.Column('support_chat_id',        sa.String(100), nullable=True))
    op.add_column('settings', sa.Column('family_plans_enabled',   sa.Boolean(),   server_default='true'))
    op.add_column('settings', sa.Column('default_protocol',       sa.String(20),  server_default="'vless'"))

    # ── Google OAuth columns on users (v12 addition) ─────────────────────────
    op.add_column('users', sa.Column('google_id',    sa.String(100), nullable=True))
    op.add_column('users', sa.Column('google_email', sa.String(200), nullable=True))
    op.add_column('users', sa.Column('web_token',    sa.String(100), nullable=True))
    op.add_column('users', sa.Column('web_password_hash', sa.String(200), nullable=True))

    # Unique index on google_id
    try:
        op.create_index('ix_users_google_id', 'users', ['google_id'], unique=True)
    except Exception:
        pass  # index may already exist


def downgrade():
    op.drop_column('users', 'web_password_hash')
    op.drop_column('users', 'web_token')
    op.drop_column('users', 'google_email')
    op.drop_column('users', 'google_id')
    op.drop_column('settings', 'default_protocol')
    op.drop_column('settings', 'family_plans_enabled')
    op.drop_column('settings', 'support_chat_id')
    op.drop_column('settings', 'status_channel_sni_alerts')
    op.drop_column('settings', 'status_channel_alerts')
    op.drop_column('settings', 'status_channel_id')

    op.drop_column('servers', 'country_code')
    op.drop_column('servers', 'city')
    op.drop_column('servers', 'lng')
    op.drop_column('servers', 'lat')

    op.drop_table('user_protocol_choices')
    op.drop_table('server_protocols')
    op.drop_table('support_tickets')
    op.drop_table('family_members')
    op.drop_table('family_groups')
