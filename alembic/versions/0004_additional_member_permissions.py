"""add fine-grained member permissions

Revision ID: 0004_additional_member_permissions
Revises: 0003_learning_category_lifecycle
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_additional_member_permissions"
down_revision = "0003_learning_category_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("members")}
    if "additional_permissions" not in columns:
        op.add_column("members", sa.Column("additional_permissions", sa.JSON(), nullable=True))
    op.execute("UPDATE members SET additional_permissions = '[]' WHERE additional_permissions IS NULL")


def downgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("members")}
    if "additional_permissions" in columns:
        op.drop_column("members", "additional_permissions")
