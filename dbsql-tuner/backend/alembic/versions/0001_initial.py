"""Initial schema — M1 MVP.

Revises: (bootstrap)
Revised: -
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oracle_instances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="1521"),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("read_user", sa.String(length=100), nullable=False),
        sa.Column("read_password", sa.String(length=512), nullable=False),
        sa.Column("sandbox_user", sa.String(length=100), nullable=True),
        sa.Column("sandbox_password", sa.String(length=512), nullable=True),
        sa.Column("oracle_version", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("last_checked", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "top_sql_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("oracle_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_time", sa.DateTime(), nullable=False),
        sa.Column("sql_id", sa.String(length=13), nullable=True),
        sa.Column("hash_value", sa.BigInteger(), nullable=True),
        sa.Column("child_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sql_text", sa.Text(), nullable=True),
        sa.Column("executions", sa.BigInteger(), nullable=True),
        sa.Column("elapsed_time", sa.Float(), nullable=True),
        sa.Column("cpu_time", sa.Float(), nullable=True),
        sa.Column("buffer_gets", sa.BigInteger(), nullable=True),
        sa.Column("disk_reads", sa.BigInteger(), nullable=True),
        sa.Column("parse_calls", sa.BigInteger(), nullable=True),
        sa.Column("module", sa.String(length=64), nullable=True),
        sa.Column("first_load_time", sa.DateTime(), nullable=True),
        sa.Column("metric_rank", sa.Integer(), nullable=True),
        sa.UniqueConstraint("instance_id", "snapshot_time", "sql_id", name="uq_top_sql"),
    )
    op.create_index("ix_top_sql_snapshots_instance", "top_sql_snapshots", ["instance_id"])
    op.create_index("ix_top_sql_snapshots_time", "top_sql_snapshots", ["snapshot_time"])
    op.create_index("ix_top_sql_snapshots_sqlid", "top_sql_snapshots", ["sql_id"])

    op.create_table(
        "optimization_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("oracle_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sql_id", sa.String(length=13), nullable=True),
        sa.Column("original_sql", sa.Text(), nullable=False),
        sa.Column("optimized_sql", sa.Text(), nullable=True),
        sa.Column("findings_json", sa.Text(), nullable=True),
        sa.Column("suggestions_json", sa.Text(), nullable=True),
        sa.Column("before_metrics_json", sa.Text(), nullable=True),
        sa.Column("after_metrics_json", sa.Text(), nullable=True),
        sa.Column("improvement_pct", sa.Float(), nullable=True),
        sa.Column("operator", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_opt_records_instance", "optimization_records", ["instance_id"])
    op.create_index("ix_opt_records_sqlid", "optimization_records", ["sql_id"])


def downgrade() -> None:
    op.drop_index("ix_opt_records_sqlid", table_name="optimization_records")
    op.drop_index("ix_opt_records_instance", table_name="optimization_records")
    op.drop_table("optimization_records")
    op.drop_index("ix_top_sql_snapshots_sqlid", table_name="top_sql_snapshots")
    op.drop_index("ix_top_sql_snapshots_time", table_name="top_sql_snapshots")
    op.drop_index("ix_top_sql_snapshots_instance", table_name="top_sql_snapshots")
    op.drop_table("top_sql_snapshots")
    op.drop_table("oracle_instances")
