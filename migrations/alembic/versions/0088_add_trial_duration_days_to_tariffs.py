"""add trial_duration_days to tariffs

Per-tariff override of the trial length. NULL means "use the global
trial-duration setting". Previously the bot's tariff editor called
``update_tariff(..., trial_duration_days=...)`` against a column that did
not exist on the ``tariffs`` table (and the CRUD did not accept the kwarg),
so the value was never persisted. This migration adds the column; the model
and CRUD were updated in the same change.

Revision ID: 0088
Revises: 0087
Create Date: 2026-05-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0088'
down_revision: Union[str, None] = '0087'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tariffs', sa.Column('trial_duration_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('tariffs', 'trial_duration_days')
