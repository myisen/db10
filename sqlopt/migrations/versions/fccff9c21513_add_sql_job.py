"""add sql_job

Revision ID: fccff9c21513
Revises: 65c0a3488f01
Create Date: 2026-09-29 05:33:32.182649

Note: 原始 autogenerate 把 initial 已建表重复了一遍，这里只留新增的 sql_job。
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fccff9c21513'
down_revision = '65c0a3488f01'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'sql_job',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('since_days', sa.Integer(), nullable=True),
        sa.Column('trigger_type', sa.String(length=16), nullable=False),
        sa.Column('interval_hours', sa.Integer(), nullable=True),
        sa.Column('interval_minutes', sa.Integer(), nullable=True),
        sa.Column('interval_seconds', sa.Integer(), nullable=True),
        sa.Column('cron_expr', sa.String(length=128), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_run_status', sa.String(length=16), nullable=True),
        sa.Column('last_run_message', sa.Text(), nullable=True),
        sa.Column('next_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['target_id'], ['sql_target.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('sql_job')
