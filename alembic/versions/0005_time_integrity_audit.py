"""add time integrity audit evidence tables

Revision ID: 0005_time_integrity_audit
Revises: 0004_additional_member_permissions
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_time_integrity_audit"
down_revision = "0004_additional_member_permissions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "active_presence_intervals" not in tables:
        op.create_table(
            "active_presence_intervals",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("member_id", sa.String(), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
            sa.Column("work_date", sa.Date(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_presence_intervals_tenant_member_date", "active_presence_intervals", ["tenant_id", "member_id", "work_date"])
        op.create_index("ix_presence_intervals_tenant_member_end", "active_presence_intervals", ["tenant_id", "member_id", "ended_at"])

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "time_integrity_audit_entries" not in tables:
        op.create_table(
            "time_integrity_audit_entries",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("entry_group_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("event_kind", sa.String(), nullable=False, server_default="recorded"),
            sa.Column("task_id", sa.String(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
            sa.Column("member_id", sa.String(), nullable=False),
            sa.Column("member_name", sa.String(), nullable=False, server_default=""),
            sa.Column("submitted_pod_id", sa.String(), nullable=True),
            sa.Column("work_date", sa.Date(), nullable=False),
            sa.Column("client_id", sa.String(), nullable=True),
            sa.Column("client_name", sa.String(), nullable=False, server_default=""),
            sa.Column("task_name", sa.String(), nullable=False, server_default=""),
            sa.Column("entry_source", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("net_active_presence_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("automatically_tracked_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("recovered_allocated_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("prior_manual_allocated_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("available_unallocated_active_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("manual_duration_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("unreconciled_manual_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("original_value_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("current_value_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("reason_note", sa.Text(), nullable=False, server_default=""),
            sa.Column("recovery_batch_id", sa.String(), nullable=True),
            sa.Column("recovery_allocation_index", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_time_integrity_tenant_member_date", "time_integrity_audit_entries", ["tenant_id", "member_id", "work_date"])
        op.create_index("ix_time_integrity_tenant_group_revision", "time_integrity_audit_entries", ["tenant_id", "entry_group_id", "revision"])
        op.create_index("ix_time_integrity_tenant_pod_date", "time_integrity_audit_entries", ["tenant_id", "submitted_pod_id", "work_date"])
        op.create_index("ix_time_integrity_tenant_recorded", "time_integrity_audit_entries", ["tenant_id", "recorded_at"])
        op.create_index("ix_time_integrity_audit_entries_entry_group_id", "time_integrity_audit_entries", ["entry_group_id"])
        op.create_index("ix_time_integrity_audit_entries_task_id", "time_integrity_audit_entries", ["task_id"])
        op.create_index("ix_time_integrity_audit_entries_member_id", "time_integrity_audit_entries", ["member_id"])


def downgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "time_integrity_audit_entries" in tables:
        op.drop_table("time_integrity_audit_entries")
    if "active_presence_intervals" in tables:
        op.drop_table("active_presence_intervals")
