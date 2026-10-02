"""single active ClockBook browser instance metadata

Revision ID: 0008_single_active_clockbook_instance
Revises: 0007_member_timezone_optional
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_single_active_clockbook_instance"
down_revision = "0007_member_timezone_optional"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sessions" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("sessions")}
    if "instance_id" not in columns:
        op.add_column("sessions", sa.Column("instance_id", sa.String(), nullable=True))
    if "last_seen_at" not in columns:
        op.add_column("sessions", sa.Column("last_seen_at", sa.DateTime(), nullable=True))
        op.execute(sa.text("UPDATE sessions SET last_seen_at = created_at WHERE last_seen_at IS NULL"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_sessions_instance_id ON sessions (instance_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_sessions_last_seen_at ON sessions (last_seen_at)"))

def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "sessions" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("sessions")}
    if "last_seen_at" in columns:
        op.drop_column("sessions", "last_seen_at")
    if "instance_id" in columns:
        op.drop_column("sessions", "instance_id")
