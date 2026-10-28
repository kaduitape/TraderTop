"""Hedge AI: setups versionados, sessoes, analises e escopos nas chaves

Quatro tabelas novas e duas colunas em `api_tokens`.

`hedge_setup_versions` existe para que alterar uma operacao em
acompanhamento NAO reescreva aquilo contra o que as analises anteriores
foram feitas. Sem ela, mudar um stop faria todo o historico passar a
descrever uma operacao que nunca existiu.

`api_tokens.scopes` nasce vazio nas chaves ja emitidas. Vazio significa
"chave legada do Pulso": ela continua funcionando exatamente como antes e
nao alcanca nada do Hedge. Preencher o default com as permissoes novas
daria ao parque instalado um acesso que ninguem concedeu.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("scopes", sa.String(length=255), nullable=False, server_default=""),
    )

    op.create_table(
        "hedge_setups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="SIMULATION"),
        sa.Column(
            "objective", sa.String(length=32), nullable=False, server_default="FAVOR_DESK_TAKE"
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_hedge_setups_user_name"),
    )
    op.create_index("ix_hedge_setups_user_id", "hedge_setups", ["user_id"])

    op.create_table(
        "hedge_setup_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("setup_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("desk_symbol", sa.String(length=40), nullable=False),
        sa.Column("desk_is_long", sa.Boolean(), nullable=False),
        sa.Column("desk_entry", sa.Numeric(20, 8), nullable=False),
        sa.Column("desk_take", sa.Numeric(20, 8), nullable=False),
        sa.Column("desk_stop", sa.Numeric(20, 8), nullable=False),
        sa.Column("desk_contracts", sa.Numeric(18, 8), nullable=False),
        sa.Column("level_unit", sa.String(length=10), nullable=False, server_default="PRICE"),
        sa.Column("desk_tick_size", sa.Numeric(18, 10), nullable=False),
        sa.Column("desk_tick_value", sa.Numeric(18, 8), nullable=False),
        sa.Column("desk_point", sa.Numeric(18, 10), nullable=False),
        sa.Column("desk_commission", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("desk_slippage", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("broker_symbol", sa.String(length=40), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(10, 4), nullable=False, server_default="1"),
        sa.Column("price_ratio", sa.Numeric(18, 8), nullable=True),
        sa.Column("broker_commission", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("broker_slippage", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column(
            "account_currency", sa.String(length=8), nullable=False, server_default="USD"
        ),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("deadline_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "display_timezone", sa.String(length=64), nullable=False, server_default="UTC"
        ),
        sa.Column(
            "data_max_age_seconds", sa.Integer(), nullable=False, server_default="30"
        ),
        sa.Column("mt5_position_ticket", sa.Integer(), nullable=True),
        sa.Column("desk_reference", sa.String(length=120), nullable=True),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["setup_id"], ["hedge_setups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("setup_id", "version", name="uq_hedge_version"),
    )
    op.create_index(
        "ix_hedge_setup_versions_setup_id", "hedge_setup_versions", ["setup_id"]
    )

    op.create_table(
        "hedge_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_key", sa.String(length=128), nullable=False),
        sa.Column("setup_id", sa.Integer(), nullable=False),
        sa.Column("api_token_id", sa.Integer(), nullable=True),
        sa.Column("terminal_id", sa.String(length=64), nullable=False),
        sa.Column("chart_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column(
            "connector_version", sa.String(length=32), nullable=False, server_default=""
        ),
        sa.Column("last_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "state", sa.String(length=32), nullable=False, server_default="WAITING_DATA"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["setup_id"], ["hedge_setups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["api_token_id"], ["api_tokens.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_key", name="uq_hedge_sessions_key"),
    )
    op.create_index("ix_hedge_sessions_setup_id", "hedge_sessions", ["setup_id"])

    op.create_table(
        "hedge_analyses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("setup_id", sa.Integer(), nullable=False),
        sa.Column("setup_version", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("bid", sa.Numeric(20, 8), nullable=True),
        sa.Column("ask", sa.Numeric(20, 8), nullable=True),
        sa.Column("feed", sa.String(length=16), nullable=False, server_default="MT5"),
        sa.Column("model_kind", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column(
            "forecast_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("unavailable_reason", sa.String(length=32), nullable=True),
        sa.Column("take_first", sa.Numeric(9, 8), nullable=True),
        sa.Column("stop_first", sa.Numeric(9, 8), nullable=True),
        sa.Column("no_barrier", sa.Numeric(9, 8), nullable=True),
        sa.Column(
            "financial_available", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("financial_json", sa.Text(), nullable=True),
        sa.Column("jev_state", sa.String(length=32), nullable=True),
        sa.Column("jev_confidence", sa.Numeric(9, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["setup_id"], ["hedge_setups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["hedge_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hedge_analyses_setup_id", "hedge_analyses", ["setup_id"])
    op.create_index(
        "ix_hedge_analyses_setup_created", "hedge_analyses", ["setup_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_hedge_analyses_setup_created", table_name="hedge_analyses")
    op.drop_index("ix_hedge_analyses_setup_id", table_name="hedge_analyses")
    op.drop_table("hedge_analyses")
    op.drop_index("ix_hedge_sessions_setup_id", table_name="hedge_sessions")
    op.drop_table("hedge_sessions")
    op.drop_index("ix_hedge_setup_versions_setup_id", table_name="hedge_setup_versions")
    op.drop_table("hedge_setup_versions")
    op.drop_index("ix_hedge_setups_user_id", table_name="hedge_setups")
    op.drop_table("hedge_setups")
    op.drop_column("api_tokens", "scopes")
