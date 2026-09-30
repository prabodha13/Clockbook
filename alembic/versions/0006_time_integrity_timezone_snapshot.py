"""snapshot member timezone on time integrity audit entries

Revision ID: 0006_time_integrity_timezone_snapshot
Revises: 0005_time_integrity_audit
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_time_integrity_timezone_snapshot"
down_revision = "0005_time_integrity_audit"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "time_integrity_audit_entries" not in tables:
        return
    columns = {c["name"] for c in inspector.get_columns("time_integrity_audit_entries")}
    if "recorded_timezone_name" not in columns:
        op.add_column("time_integrity_audit_entries", sa.Column("recorded_timezone_name", sa.String(), nullable=True))

    # Best-effort backfill for existing audit rows. New rows always capture this value immutably
    # at RecordedAt; historical rows cannot prove the timezone that was selected before this migration.
    if "members" in tables:
        if bind.dialect.name == "postgresql":
            op.execute(sa.text("""
                UPDATE time_integrity_audit_entries tia
                SET recorded_timezone_name = COALESCE(NULLIF(m.timezone_name, ''), 'UTC')
                FROM members m
                WHERE tia.member_id = m.id AND tia.recorded_timezone_name IS NULL
            """))
        else:
            op.execute(sa.text("""
                UPDATE time_integrity_audit_entries
                SET recorded_timezone_name = COALESCE(
                    (SELECT NULLIF(m.timezone_name, '') FROM members m WHERE m.id = time_integrity_audit_entries.member_id),
                    'UTC'
                )
                WHERE recorded_timezone_name IS NULL
            """))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "time_integrity_audit_entries" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("time_integrity_audit_entries")}
    if "recorded_timezone_name" in columns:
        op.drop_column("time_integrity_audit_entries", "recorded_timezone_name")
