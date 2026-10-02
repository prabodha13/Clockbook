"""stop assigning a default member timezone

Revision ID: 0007_member_timezone_optional
Revises: 0006_time_integrity_timezone_snapshot
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_member_timezone_optional"
down_revision = "0006_time_integrity_timezone_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "members" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("members")}
    if "timezone_name" not in columns:
        op.add_column("members", sa.Column("timezone_name", sa.String(), nullable=True))
        return
    # Existing values are intentionally preserved. Only the old database-level
    # Asia/Colombo default is removed so new members remain unset until selected.
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE members ALTER COLUMN timezone_name DROP DEFAULT"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "members" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("members")}
    if "timezone_name" in columns and bind.dialect.name == "postgresql":
        op.execute(sa.text("ALTER TABLE members ALTER COLUMN timezone_name SET DEFAULT 'Asia/Colombo'"))
