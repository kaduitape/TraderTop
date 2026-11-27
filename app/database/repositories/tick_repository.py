"""Repositorio de ticks. Deduplicado por (symbol_id, timestamp, bid, ask) —
ver docs/data-model.md secao 3 para a limitacao conhecida desta estrategia
(a corretora nao fornece um identificador de sequencia por tick via MT5;
revisitar apenas se isso mudar)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import CursorResult, delete, desc, func, select
from sqlalchemy.orm import Session

from app.database.models.symbol import Symbol
from app.database.models.tick import Tick
from app.database.repositories._time_utils import as_aware_utc
from app.mt5.market_data import RawTick

_NUMERIC_8_QUANTUM = Decimal("0.00000001")


def _numeric_8(value: float | Decimal) -> Decimal:
    """Normaliza exatamente como ``Numeric(18, 8)`` antes de deduplicar.

    Precos da extensao MT5 sao floats e podem chegar como
    ``1.3315299999999999``. O MySQL grava ``1.33153000``; sem quantizar
    antes, a chave existente nao e reconhecida no ciclo seguinte.
    """
    return Decimal(str(value)).quantize(_NUMERIC_8_QUANTUM)


class TickRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_upsert(self, symbol_id: int, ticks: list[RawTick]) -> int:
        if not ticks:
            return 0

        timestamps = [t.timestamp for t in ticks]
        stmt = select(Tick.timestamp, Tick.bid, Tick.ask).where(
            Tick.symbol_id == symbol_id,
            Tick.timestamp.in_(timestamps),
        )
        existing_keys = {
            (as_aware_utc(row[0]), _numeric_8(row[1]), _numeric_8(row[2]))
            for row in self._session.execute(stmt)
        }

        new_rows = []
        seen_in_batch: set[tuple[datetime, Decimal, Decimal]] = set()
        for t in ticks:
            bid = _numeric_8(t.bid)
            ask = _numeric_8(t.ask)
            key = (t.timestamp, bid, ask)
            # `existing_keys` so cobre o que ja esta no banco -- duas
            # ticks com o MESMO (timestamp, bid, ask) dentro do proprio
            # lote buscado do MetaTrader (nenhuma delas ainda persistida)
            # tambem precisam ser deduplicadas aqui, ou a segunda bate na
            # unique constraint no INSERT (bug real, achado coletando
            # ticks reais de producao -- nao aparecia nos testes porque
            # os dados sinteticos nunca repetiam bid/ask no mesmo lote).
            if key in existing_keys or key in seen_in_batch:
                continue
            seen_in_batch.add(key)
            new_rows.append(
                Tick(
                    symbol_id=symbol_id,
                    timestamp=t.timestamp,
                    bid=bid,
                    ask=ask,
                    last=_numeric_8(t.last),
                    volume=_numeric_8(t.volume),
                    flags=t.flags,
                )
            )

        if new_rows:
            self._session.add_all(new_rows)
            self._session.flush()

        return len(new_rows)

    def get_last_timestamp(self, symbol_id: int) -> datetime | None:
        """Usado para preenchimento incremental: se houver tick conhecido,
        so buscamos do MetaTrader o que vier depois dele."""
        stmt = (
            select(Tick.timestamp)
            .where(Tick.symbol_id == symbol_id)
            .order_by(desc(Tick.timestamp))
            .limit(1)
        )
        row = self._session.execute(stmt).first()
        return as_aware_utc(row[0]) if row is not None else None

    def get_recent(self, symbol_id: int, limit: int) -> list[Tick]:
        """Retorna os `limit` ticks mais recentes, em ordem cronologica
        crescente (para que checagens de ordem/atraso facam sentido)."""
        stmt = (
            select(Tick)
            .where(Tick.symbol_id == symbol_id)
            .order_by(desc(Tick.timestamp))
            .limit(limit)
        )
        rows = list(self._session.execute(stmt).scalars())
        return list(reversed(rows))

    def get_range(self, symbol_id: int, *, start: datetime, end: datetime) -> list[Tick]:
        """Retorna todos os ticks no intervalo [start, end], em ordem
        cronologica crescente. Usado pelo backtest por tick (Fase 7), que
        precisa da sequencia real de ticks para simular fills — nao apenas
        os mais recentes."""
        stmt = (
            select(Tick)
            .where(Tick.symbol_id == symbol_id, Tick.timestamp >= start, Tick.timestamp <= end)
            .order_by(Tick.timestamp)
        )
        return list(self._session.execute(stmt).scalars())

    def delete_batch_older_than(
        self, *, cutoff: datetime, limit: int, symbol_id: int | None = None
    ) -> int:
        """Remove ATE `limit` ticks anteriores a `cutoff`. Devolve quantos.

        Um lote. Quem chama repete ate vir zero e e dono do commit — ver
        `app/maintenance/retention.py`, que e onde a transacao por lote faz
        sentido. Zero significa "nao ha mais nada anterior ao corte".

        Seleciona os ids e depois apaga por id, em vez de
        `DELETE ... LIMIT`: o SQLite so aceita `LIMIT` em DELETE se tiver
        sido compilado com `SQLITE_ENABLE_UPDATE_DELETE_LIMIT`, o que nao e
        garantido, e a suite roda em SQLite. As duas consultas usam o
        indice de `timestamp` e a chave primaria.
        """
        alvo = select(Tick.id).where(Tick.timestamp < cutoff)
        if symbol_id is not None:
            alvo = alvo.where(Tick.symbol_id == symbol_id)

        ids = list(self._session.execute(alvo.limit(limit)).scalars())
        if not ids:
            return 0

        result = cast(
            CursorResult, self._session.execute(delete(Tick).where(Tick.id.in_(ids)))
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def purge_older_than(
        self, retention_days: int, *, now: datetime, symbol_id: int | None = None
    ) -> int:
        """Remove ticks mais antigos que `retention_days`. Nao se aplica a
        candles (mantidas indefinidamente — sao muito menores em volume e
        sao a base do backtesting por candle).

        `symbol_id` e opcional: sem ele, a purga e global (todos os
        simbolos), que e o comportamento padrao da politica de retencao do
        sistema (`TICK_RETENTION_DAYS`). Passe um `symbol_id` para limitar a
        um unico simbolo (util para manutencao pontual sem afetar os
        demais).

        É UM `DELETE` SO. Serve para manutencao pontual e escopada, e para
        a suite. A retencao automatica NAO passa por aqui: ela usa
        `delete_batch_older_than` em lotes, porque um unico DELETE de
        milhoes de linhas trava a tabela de ticks justamente enquanto a
        coleta tenta escrever nela."""
        cutoff = now - timedelta(days=retention_days)
        stmt = delete(Tick).where(Tick.timestamp < cutoff)
        if symbol_id is not None:
            stmt = stmt.where(Tick.symbol_id == symbol_id)
        result = cast(CursorResult, self._session.execute(stmt))
        self._session.flush()
        return int(result.rowcount or 0)

    def summary(self) -> list[tuple[str, int, datetime, datetime]]:
        """Visao agregada por simbolo (quantidade de ticks, primeiro/
        ultimo timestamp) -- usada pela pagina `/dashboard/market-data`
        (Fase 16), nao pelo pipeline de coleta/backtest."""
        stmt = (
            select(
                Symbol.name,
                func.count(Tick.id),
                func.min(Tick.timestamp),
                func.max(Tick.timestamp),
            )
            .join(Symbol, Symbol.id == Tick.symbol_id)
            .group_by(Symbol.name)
            .order_by(Symbol.name)
        )
        return list(self._session.execute(stmt).tuples().all())
