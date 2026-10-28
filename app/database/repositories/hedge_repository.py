"""Acesso aos setups, sessoes e analises do Hedge AI.

## O isolamento vive aqui, nao na rota

Todo metodo que le ou escreve um setup recebe `user_id` e filtra por ele
no SQL. A alternativa — buscar por id e conferir o dono depois — funciona
ate alguem escrever uma rota nova e esquecer a conferencia. Com o filtro
no repositorio, esquecer significa nao achar o registro, que falha fechado.

## Versao nova em vez de UPDATE

`update_setup` nao altera a versao vigente: cria a proxima e move o
ponteiro. O historico de analises aponta para a versao sob a qual foi
calculado, entao mexer na linha antiga faria o passado descrever uma
operacao que nunca existiu.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database.models.hedge import (
    HedgeAnalysis,
    HedgeSession,
    HedgeSetup,
    HedgeSetupVersion,
)


class HedgeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- setups ------------------------------------------------------------

    def list_setups(self, *, user_id: int, include_closed: bool = True) -> list[HedgeSetup]:
        stmt = select(HedgeSetup).where(HedgeSetup.user_id == user_id)
        if not include_closed:
            stmt = stmt.where(HedgeSetup.closed_at.is_(None))
        return list(self._session.execute(stmt.order_by(desc(HedgeSetup.id))).scalars())

    def get_setup(self, setup_id: int, *, user_id: int) -> HedgeSetup | None:
        """O setup, se e DESTE usuario.

        `user_id` nao e opcional de proposito: a chave de API de um cliente
        nunca pode alcancar o setup de outro, e a forma de garantir isso e
        nao oferecer um caminho que dispense o filtro.
        """
        stmt = select(HedgeSetup).where(
            HedgeSetup.id == setup_id, HedgeSetup.user_id == user_id
        )
        return self._session.execute(stmt).scalars().first()

    def get_version(self, setup_id: int, version: int) -> HedgeSetupVersion | None:
        stmt = select(HedgeSetupVersion).where(
            HedgeSetupVersion.setup_id == setup_id,
            HedgeSetupVersion.version == version,
        )
        return self._session.execute(stmt).scalars().first()

    def current_version(self, setup: HedgeSetup) -> HedgeSetupVersion | None:
        return self.get_version(setup.id, setup.current_version)

    def list_versions(self, setup_id: int) -> list[HedgeSetupVersion]:
        stmt = (
            select(HedgeSetupVersion)
            .where(HedgeSetupVersion.setup_id == setup_id)
            .order_by(desc(HedgeSetupVersion.version))
        )
        return list(self._session.execute(stmt).scalars())

    def create_setup(
        self, *, user_id: int, name: str, mode: str, objective: str, params: dict
    ) -> tuple[HedgeSetup, HedgeSetupVersion]:
        setup = HedgeSetup(
            user_id=user_id,
            name=name.strip()[:120] or "sem nome",
            mode=mode,
            objective=objective,
            current_version=1,
        )
        self._session.add(setup)
        self._session.flush()

        versao = HedgeSetupVersion(setup_id=setup.id, version=1, **params)
        self._session.add(versao)
        self._session.flush()
        return setup, versao

    def update_setup(
        self,
        setup: HedgeSetup,
        *,
        params: dict,
        note: str,
        user_id: int,
        mode: str | None = None,
        objective: str | None = None,
    ) -> HedgeSetupVersion:
        """Grava uma versao nova e aponta o setup para ela."""
        proxima = setup.current_version + 1
        versao = HedgeSetupVersion(
            setup_id=setup.id,
            version=proxima,
            change_note=note,
            created_by_user_id=user_id,
            **params,
        )
        self._session.add(versao)
        setup.current_version = proxima
        if mode is not None:
            setup.mode = mode
        if objective is not None:
            setup.objective = objective
        self._session.flush()
        return versao

    def close_setup(self, setup: HedgeSetup, *, reason: str) -> HedgeSetup:
        """Encerra. Idempotente: o primeiro motivo registrado prevalece.

        Reescrever o motivo faria "tocou o take" virar "encerrado a mao"
        por causa de um clique posterior, e o historico perderia o
        desfecho real."""
        if setup.closed_at is None:
            setup.closed_at = datetime.now(UTC)
            setup.closed_reason = reason
        return setup

    # --- sessoes -----------------------------------------------------------

    def get_or_create_session(
        self,
        *,
        session_key: str,
        setup_id: int,
        terminal_id: str,
        chart_id: str,
        symbol: str,
        api_token_id: int | None,
        connector_version: str,
    ) -> HedgeSession:
        stmt = select(HedgeSession).where(HedgeSession.session_key == session_key)
        existente = self._session.execute(stmt).scalars().first()
        if existente is not None:
            existente.connector_version = connector_version
            existente.api_token_id = api_token_id
            return existente

        nova = HedgeSession(
            session_key=session_key,
            setup_id=setup_id,
            terminal_id=terminal_id[:64],
            chart_id=chart_id[:64],
            symbol=symbol[:40],
            api_token_id=api_token_id,
            connector_version=connector_version[:32],
        )
        self._session.add(nova)
        self._session.flush()
        return nova

    def list_sessions(self, *, setup_ids: list[int]) -> list[HedgeSession]:
        if not setup_ids:
            return []
        stmt = (
            select(HedgeSession)
            .where(HedgeSession.setup_id.in_(setup_ids))
            .order_by(desc(HedgeSession.last_seen_at))
        )
        return list(self._session.execute(stmt).scalars())

    # --- analises ----------------------------------------------------------

    def record_analysis(self, analysis: HedgeAnalysis) -> HedgeAnalysis:
        self._session.add(analysis)
        self._session.flush()
        return analysis

    def latest_analysis(self, setup_id: int) -> HedgeAnalysis | None:
        stmt = (
            select(HedgeAnalysis)
            .where(HedgeAnalysis.setup_id == setup_id)
            .order_by(desc(HedgeAnalysis.id))
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def history(self, *, setup_id: int, limit: int = 100) -> list[HedgeAnalysis]:
        stmt = (
            select(HedgeAnalysis)
            .where(HedgeAnalysis.setup_id == setup_id)
            .order_by(desc(HedgeAnalysis.id))
            .limit(max(1, min(limit, 500)))
        )
        return list(self._session.execute(stmt).scalars())
