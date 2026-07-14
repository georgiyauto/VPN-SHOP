"""
Hysteria2 migration — v31→v32

Что меняется:
  1. servers.default_protocol  — дефолт меняется на 'hysteria2'
  2. server_protocols          — убираем старые vless-записи, добавляем hysteria2

Revision ID: 0012_hysteria2
Revises: 0011_referral_percent
Create Date: 2026-05-27
"""

from alembic import op
import sqlalchemy as sa

revision = '0012_hysteria2'
down_revision = '0011_referral_percent'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Меняем дефолт default_protocol у серверов
    op.execute(
        "UPDATE servers SET default_protocol = 'hysteria2' WHERE default_protocol = 'vless'"
    )

    # 2. Меняем дефолт поля в схеме
    op.alter_column(
        'servers', 'default_protocol',
        existing_type=sa.String(20),
        server_default='hysteria2',
    )

    # 3. В таблице server_protocols меняем protocol='vless' → 'hysteria2'
    #    (если записи уже есть — обновляем, не удаляем, чтобы не потерять inbound_id)
    op.execute(
        "UPDATE server_protocols SET protocol = 'hysteria2' WHERE protocol = 'vless'"
    )

    # 4. В user_protocol_choices сбрасываем всех на hysteria2
    op.execute(
        "UPDATE user_protocol_choices SET protocol = 'hysteria2' WHERE protocol = 'vless'"
    )

    # 5. В family_members то же самое
    op.execute(
        "UPDATE family_members SET protocol = 'hysteria2' WHERE protocol = 'vless'"
    )


def downgrade():
    op.execute(
        "UPDATE servers SET default_protocol = 'vless' WHERE default_protocol = 'hysteria2'"
    )
    op.alter_column(
        'servers', 'default_protocol',
        existing_type=sa.String(20),
        server_default='vless',
    )
    op.execute(
        "UPDATE server_protocols SET protocol = 'vless' WHERE protocol = 'hysteria2'"
    )
    op.execute(
        "UPDATE user_protocol_choices SET protocol = 'vless' WHERE protocol = 'hysteria2'"
    )
    op.execute(
        "UPDATE family_members SET protocol = 'vless' WHERE protocol = 'hysteria2'"
    )
