"""
Migration 0005 — добавляет поле info_text в таблицу settings
"""
from alembic import op
import sqlalchemy as sa


def upgrade():
    try:
        op.add_column('settings', sa.Column(
            'info_text', sa.Text(),
            nullable=True,
            server_default=(
                "ℹ️ <b>Информация о сервисе</b>\n\n"
                "Здесь будет информация о вашем VPN сервисе.\n"
                "Настройте этот текст в панели администратора."
            )
        ))
    except Exception:
        pass  # column may already exist


def downgrade():
    try:
        op.drop_column('settings', 'info_text')
    except Exception:
        pass
