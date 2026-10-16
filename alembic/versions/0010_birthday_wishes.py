"""Tenant-scoped birthday wish library.

Revision ID: 0010_birthday_wishes
Revises: 0009_birthday_perspectives
"""
from alembic import op
import sqlalchemy as sa
revision = "0010_birthday_wishes"
down_revision = "0009_birthday_perspectives"
branch_labels = None
depends_on = None

def upgrade():
    if "birthday_wishes" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table("birthday_wishes",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("text", sa.String(350), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False))
        op.create_index("ix_birthday_wishes_tenant_id", "birthday_wishes", ["tenant_id"])

def downgrade():
    op.drop_table("birthday_wishes")
