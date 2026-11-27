"""Repositorio de ocorrencias de qualidade de dados. Cada chamada de
`bulk_insert` grava um evento por `DataQualityIssue` — e um log de
ocorrencias, nao um snapshot deduplicado (o mesmo problema pode, e deve,
ser registrado de novo se persistir em coletas futuras)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session

from app.database.models.data_quality_event import DataQualityEvent
from app.market.data_quality import DataQualityIssue


class DataQualityEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def bulk_insert(
        self, symbol_id: int, timeframe: str | None, issues: Sequence[DataQualityIssue]
    ) -> int:
        if not issues:
            return 0

        rows = [
            DataQualityEvent(
                symbol_id=symbol_id,
                timeframe=timeframe,
                check_name=issue.check,
                severity=issue.severity.value,
                message=issue.message,
            )
            for issue in issues
        ]
        self._session.add_all(rows)
        self._session.flush()
        return len(rows)

    def delete_batch_older_than(
        self,
        *,
        cutoff: datetime,
        limit: int,
        severities: Sequence[str] | None = None,
        exclude_severities: Sequence[str] | None = None,
    ) -> int:
        """Remove ATE `limit` eventos anteriores a `cutoff`. Devolve quantos.

        Um lote; quem chama repete e e dono do commit (mesmo contrato de
        `TickRepository.delete_batch_older_than`, e pelo mesmo motivo).

        `severities` restringe a essas severidades; `exclude_severities`
        remove todas MENOS essas. A politica usa a segunda forma para o
        prazo curto, de proposito: uma severidade nova que alguem
        acrescentar amanha cai na retencao curta sozinha, em vez de ficar
        para sempre por nao constar de uma lista."""
        alvo = select(DataQualityEvent.id).where(DataQualityEvent.detected_at < cutoff)
        if severities is not None:
            alvo = alvo.where(DataQualityEvent.severity.in_(list(severities)))
        if exclude_severities is not None:
            alvo = alvo.where(DataQualityEvent.severity.notin_(list(exclude_severities)))

        ids = list(self._session.execute(alvo.limit(limit)).scalars())
        if not ids:
            return 0

        result = cast(
            CursorResult,
            self._session.execute(
                delete(DataQualityEvent).where(DataQualityEvent.id.in_(ids))
            ),
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def count_by_severity(self) -> dict[str, int]:
        """Quantos eventos por severidade. Existe para a tela e o CLI
        poderem dizer o tamanho do problema ANTES de purgar — "removi
        4 milhoes de linhas" sem numero de partida nao informa nada."""
        stmt = select(DataQualityEvent.severity, func.count(DataQualityEvent.id)).group_by(
            DataQualityEvent.severity
        )
        return dict(self._session.execute(stmt).all())
