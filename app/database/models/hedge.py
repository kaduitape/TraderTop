"""Tabelas do Hedge AI.

As quatro vivem no mesmo arquivo — fora da convencao de um conceito por
modulo — porque sao um agregado so: um setup tem versoes, uma sessao
aponta para uma versao, e cada analise registra qual versao respondeu.
Separa-las espalharia por quatro arquivos um ciclo de chaves estrangeiras
que so faz sentido lido junto.

## Por que versao, e nao UPDATE

No modo de acompanhamento o operador ja tem dinheiro na mesa. Reescrever
entrada ou niveis apagaria aquilo contra o que as analises anteriores
foram feitas — e o historico passaria a mentir sobre o que foi previsto.
Cada alteracao cria `HedgeSetupVersion` nova; `current_version` aponta
para a vigente e as antigas permanecem.

## Isolamento

`HedgeSetup.user_id` e NOT NULL com `ondelete=CASCADE`. Diferente de
`api_tokens.created_by_user_id`, que e auditoria e pode ficar orfa, aqui
o dono E a autorizacao: um setup sem dono seria um setup que qualquer
chave poderia ler.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin


class HedgeSetup(Base, TimestampMixin):
    """A identidade de uma operacao coberta. Os parametros vivem na versao."""

    __tablename__ = "hedge_setups"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)

    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="SIMULATION")
    """SIMULATION ou TRACKING."""

    objective: Mapped[str] = mapped_column(
        String(32), nullable=False, default="FAVOR_DESK_TAKE"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    """DESK_TAKE, DESK_STOP, TIMEOUT ou MANUAL. Encerrado nao volta a
    receber previsao: a operacao acabou e continuar estimando produziria
    numero sobre evento ja decidido."""

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_hedge_setups_user_name"),
    )


class HedgeSetupVersion(Base, TimestampMixin):
    """Os parametros congelados de uma versao. Nunca sofre UPDATE."""

    __tablename__ = "hedge_setup_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int] = mapped_column(
        ForeignKey("hedge_setups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # --- perna da mesa -----------------------------------------------------
    desk_symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    desk_is_long: Mapped[bool] = mapped_column(Boolean, nullable=False)
    desk_entry: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    desk_take: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    desk_stop: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    desk_contracts: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    level_unit: Mapped[str] = mapped_column(String(10), nullable=False, default="PRICE")
    """Em que unidade os niveis foram DIGITADOS. Guardado junto porque a
    conversao para preco precisa ser reproduzivel: so o preco final nao
    permite reconstruir o que o operador quis dizer."""

    desk_tick_size: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    desk_tick_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    desk_point: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    desk_commission: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    desk_slippage: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )

    # --- perna da corretora ------------------------------------------------
    broker_symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=1
    )
    broker_fixed_lots: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), nullable=True
    )
    """Lote fixo na corretora. NULL = derivar da exposicao.

    Existe porque o operador frequentemente JA tem a posicao aberta com um
    tamanho que alguem impos. Nesse caso `coverage_ratio` vira resultado,
    nao entrada — e o motor informa quanto aquele lote cobre de fato."""

    price_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    """Quantas unidades de preco da corretora por 1 da mesa. NULL exige
    que os simbolos sejam equivalentes — ver `contracts.mapping_for`."""

    broker_commission: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    broker_slippage: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0
    )
    account_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")

    # --- prazo -------------------------------------------------------------
    horizon_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    deadline_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Horario limite absoluto. Guardado em UTC sempre; o fuso de exibicao
    e preferencia de tela e nao entra em conta nenhuma."""

    display_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )
    data_max_age_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )

    # --- acompanhamento ----------------------------------------------------
    mt5_position_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True)
    desk_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    """Identificacao informada a mao da operacao na mesa. E uma anotacao
    do operador, nao prova de que a posicao existe — o sistema nunca
    afirma acompanhar uma posicao que nao consegue verificar."""

    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("setup_id", "version", name="uq_hedge_version"),
    )


class HedgeSession(Base, TimestampMixin):
    """Um terminal + grafico + simbolo + setup.

    As quatro partes entram na identidade porque dois graficos do mesmo
    ativo no mesmo terminal sao duas sessoes distintas, e misturar os
    snapshots dos dois faria uma previsao responder sobre o outro.
    """

    __tablename__ = "hedge_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    setup_id: Mapped[int] = mapped_column(
        ForeignKey("hedge_setups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    api_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), nullable=True
    )

    terminal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chart_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)

    connector_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Maior sequencia ja aceita. Snapshot com numero menor chegou fora de
    ordem e e descartado — a rede nao garante ordem, e aplicar um estado
    velho sobre um novo faria o grafico voltar no tempo."""

    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="WAITING_DATA")


class HedgeAnalysis(Base, TimestampMixin):
    """Uma resposta entregue. Base do historico e de treino futuro.

    Guarda `setup_version` junto: sem isso, avaliar calibracao depois
    misturaria previsoes feitas sob parametros diferentes como se fossem
    a mesma coisa.
    """

    __tablename__ = "hedge_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    setup_id: Mapped[int] = mapped_column(
        ForeignKey("hedge_setups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    setup_version: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("hedge_sessions.id", ondelete="SET NULL"), nullable=True
    )

    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    """Quando o TERMINAL capturou. Diferente de `created_at`, que e quando
    o servidor gravou — a diferenca entre os dois e a latencia, e ela
    importa para saber se a previsao valia no momento em que apareceu."""

    current_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    feed: Mapped[str] = mapped_column(String(16), nullable=False, default="MT5")

    model_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    forecast_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unavailable_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)

    take_first: Mapped[Decimal | None] = mapped_column(Numeric(9, 8), nullable=True)
    stop_first: Mapped[Decimal | None] = mapped_column(Numeric(9, 8), nullable=True)
    no_barrier: Mapped[Decimal | None] = mapped_column(Numeric(9, 8), nullable=True)
    """NULL quando nao houve previsao. Nunca zero: zero seria lido como
    "estimamos que nao acontece", que e uma afirmacao diferente."""

    financial_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    financial_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    jev_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Classificacao de contexto do Jev, quando houve. Em modo observacao
    ela NAO entra na previsao — fica gravada ao lado para que a influencia
    possa ser avaliada fora da amostra antes de ser permitida."""

    jev_confidence: Mapped[Decimal | None] = mapped_column(Numeric(9, 8), nullable=True)

    __table_args__ = (
        Index("ix_hedge_analyses_setup_created", "setup_id", "created_at"),
    )
