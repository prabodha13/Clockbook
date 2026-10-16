"""optional birthdays and daily perspectives

Revision ID: 0009_birthday_perspectives
Revises: 0008_single_active_clockbook_instance
"""
from alembic import op
import sqlalchemy as sa
revision = "0009_birthday_perspectives"
down_revision = "0008_single_active_clockbook_instance"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "members" in tables:
        columns = {c["name"] for c in sa.inspect(bind).get_columns("members")}
        for name, kind in [
            ("birthday_month", sa.Integer()), ("birthday_day", sa.Integer()),
            ("show_birthday_celebration", sa.Boolean()), ("birthday_acknowledged_year", sa.Integer()),
        ]:
            if name not in columns:
                op.add_column("members", sa.Column(name, kind, nullable=True))
        op.execute(sa.text("UPDATE members SET show_birthday_celebration = TRUE WHERE show_birthday_celebration IS NULL"))
    if "daily_perspectives" not in tables:
        op.create_table("daily_perspectives",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("text", sa.String(500), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_daily_perspectives_tenant_id", "daily_perspectives", ["tenant_id"])

def downgrade():
    op.drop_table("daily_perspectives")
    for name in ("birthday_acknowledged_year", "show_birthday_celebration", "birthday_day", "birthday_month"):
        op.drop_column("members", name)
