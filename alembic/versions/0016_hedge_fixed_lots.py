"""lote fixo na corretora para o Hedge AI

O motor derivava o lote da exposicao, o que responde "quanto devo
operar". Quando a posicao JA esta aberta com um tamanho que a mesa ou a
corretora impos, a pergunta e outra: "quanto isto cobre de fato".

NULL preserva o comportamento atual — setups existentes continuam
derivando o lote.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-29

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "hedge_setup_versions",
        sa.Column("broker_fixed_lots", sa.Numeric(18, 8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hedge_setup_versions", "broker_fixed_lots")
