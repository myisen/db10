"""add sql_plan

Revision ID: b59034a99539
Revises: fccff9c21513
Create Date: 2026-09-29 06:34:16.486232

Note: 原始 autogenerate 把 initial 已建表重复了一遍，这里只留新增的 sql_plan。
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b59034a99539'
down_revision = 'fccff9c21513'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # ===== AUTOGENERATE BUG: 以下重复 ===== 
# ===== 重复块结束 ===== 

    op.create_table(
        'sql_plan',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('plan_hash', sa.String(length=32), nullable=True),
        sa.Column('raw_tree', sa.JSON(), nullable=True),
        sa.Column('formatted_text', sa.Text(), nullable=True),
        sa.Column('db_type', sa.String(length=16), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['fingerprint'], ['sql_fingerprint.fingerprint'], ),
        sa.ForeignKeyConstraint(['target_id'], ['sql_target.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('target_id', 'fingerprint', 'plan_hash', name='uk_plan_unique'),
    )
    op.create_index('idx_plan_fp', 'sql_plan', ['fingerprint'], unique=False)

def downgrade() -> None:
    op.drop_index('idx_plan_fp', table_name='sql_plan')
    op.drop_table('sql_plan')
