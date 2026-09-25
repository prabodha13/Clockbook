"""Add Learning & Development knowledge records and configurable categories.

Revision ID: 0002_learning_development
Revises: 0001_clockbook_baseline

The migration is intentionally tolerant of ClockBook's legacy startup create_all path:
if a rolling deploy created these new tables before Alembic is run, Alembic can still mark
this revision applied without trying to create the same tables twice.
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0002_learning_development"
down_revision: Union[str, None] = "0001_clockbook_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "learning_categories" not in tables:
        op.create_table(
            "learning_categories",
            sa.Column("tenant_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "name", name="uq_learning_categories_tenant_name"),
        )
        op.create_index("ix_learning_categories_tenant_id", "learning_categories", ["tenant_id"], unique=False)

    tables = set(sa.inspect(bind).get_table_names())
    if "learning_records" not in tables:
        op.create_table(
            "learning_records",
            sa.Column("tenant_id", sa.String(), nullable=False),
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("task_id", sa.String(), nullable=False),
            sa.Column("member_id", sa.String(), nullable=True),
            sa.Column("member_name", sa.String(), nullable=False, server_default="Unknown"),
            sa.Column("category", sa.String(), nullable=False),
            sa.Column("topic", sa.String(), nullable=False),
            sa.Column("what_i_learned", sa.Text(), nullable=False),
            sa.Column("tdm_references", sa.JSON(), nullable=True),
            sa.Column("article_references", sa.JSON(), nullable=True),
            sa.Column("duration_seconds", sa.Float(), nullable=False, server_default="0"),
            sa.Column("learned_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
            sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "task_id", name="uq_learning_records_tenant_task"),
        )
        op.create_index("ix_learning_records_tenant_id", "learning_records", ["tenant_id"], unique=False)
        op.create_index("ix_learning_records_tenant_member_date", "learning_records", ["tenant_id", "member_id", "learned_at"], unique=False)
        op.create_index("ix_learning_records_tenant_category_date", "learning_records", ["tenant_id", "category", "learned_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "learning_records" in tables:
        op.drop_table("learning_records")
    tables = set(sa.inspect(bind).get_table_names())
    if "learning_categories" in tables:
        op.drop_table("learning_categories")
