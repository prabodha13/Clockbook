"""ClockBook baseline marker.

Revision ID: 0001_clockbook_baseline
Revises: None

This revision is intentionally schema-neutral. Existing ClockBook deployments already
have a live schema produced by the legacy idempotent startup migrations. After backing up
and verifying that schema, operators stamp this revision. All NEW schema changes after
adoption should be represented by real Alembic revisions rather than silent startup DDL.
"""

from typing import Sequence, Union

revision: str = "0001_clockbook_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
