"""Add lifecycle state for configurable Learning & Development categories.

Revision ID: 0003_learning_category_lifecycle
Revises: 0002_learning_development
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003_learning_category_lifecycle"
down_revision: Union[str, None] = "0002_learning_development"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "learning_categories" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("learning_categories")}
    if "is_active" not in columns:
        op.add_column("learning_categories", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "learning_categories" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("learning_categories")}
    if "is_active" in columns:
        op.drop_column("learning_categories", "is_active")
