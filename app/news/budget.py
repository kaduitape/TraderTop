"""Orcamento diario de chamadas a MarketPulse.

O cache evita repeticao; este modulo garante um TETO. Sao problemas
diferentes: o cache nao protege contra um dia com muitos simbolos, muitos
reinicios do worker (cada um comeca com cache vazio) ou uma tela aberta em
varias abas. O que protege contra "a cota acabou de novo" e um limite duro.

Contado por dia UTC e persistido em `system_settings`, e nao em memoria, por
dois motivos: sobrevive a reinicio do processo e e COMPARTILHADO entre o
servidor web e o conector Windows — que sao processos distintos e, sem isso,
teriam cada um o seu orcamento, dobrando o gasto real.

Quando o limite e atingido, a consulta simplesmente nao acontece e o fator
sai do calculo com `SKIPPED`. Nunca vira erro, nunca vira dado inventado, e
o operador ve no painel quanto ja gastou.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy.orm import Session

from app.database.repositories.system_setting_repository import SystemSettingRepository

logger = logging.getLogger(__name__)

BUDGET_SETTING = "marketpulse_daily_usage"
"""Chave padrao: o consumo da MarketPulse.

O contador e parametrizavel (`key`) porque outras integracoes pagas usam a
mesma mecanica com teto PROPRIO. Compartilhar a chave faria o gasto de uma
API esgotar o orcamento da outra, e o sintoma seria uma integracao se
calando por culpa de outra."""


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    day: str
    """Dia UTC (YYYY-MM-DD) a que a contagem se refere."""

    calls: int
    limit: int
    """Zero significa sem limite."""

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.calls >= self.limit

    @property
    def remaining(self) -> int | None:
        if self.limit <= 0:
            return None
        return max(0, self.limit - self.calls)


def _today(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d")


def read_usage(
    session: Session,
    *,
    limit: int,
    now: datetime | None = None,
    key: str = BUDGET_SETTING,
) -> BudgetUsage:
    """Consumo de hoje. Um registro de ontem conta como zero — a virada do
    dia UTC zera sozinha, sem tarefa agendada para limpar nada."""
    day = _today(now)
    raw = SystemSettingRepository(session).get(key)
    calls = 0
    if raw:
        try:
            data = json.loads(raw)
            if str(data.get("day")) == day:
                calls = int(data.get("calls", 0))
        except (ValueError, TypeError):
            # Registro corrompido nao pode liberar gasto ilimitado nem
            # travar o sistema: recomeca a contagem do dia.
            calls = 0
    return BudgetUsage(day=day, calls=calls, limit=max(0, limit))


def record_call(
    session: Session,
    *,
    limit: int,
    now: datetime | None = None,
    key: str = BUDGET_SETTING,
    description: str = "Consumo diario da API MarketPulse (reiniciado a cada dia UTC).",
) -> BudgetUsage:
    """Soma uma chamada ao dia corrente e devolve o consumo atualizado."""
    usage = read_usage(session, limit=limit, now=now, key=key)
    updated = BudgetUsage(day=usage.day, calls=usage.calls + 1, limit=usage.limit)
    SystemSettingRepository(session).set(
        key,
        json.dumps({"day": updated.day, "calls": updated.calls}),
        description=description,
    )
    return updated


class BudgetedProvider:
    """Aplica o teto diario a qualquer provedor de avaliacao.

    Fica ENTRE o cache e o cliente HTTP: acerto de cache nao consome
    orcamento (nao houve chamada), e so o que realmente vira requisicao e
    contabilizado.

    A contagem usa uma sessao propria e faz commit sozinha, porque precisa
    persistir mesmo em requisicoes de leitura (abrir a tela de analise nao
    faz commit de nada) e porque o consumo e um fato independente do
    trabalho do chamador — nao pode ser desfeito por um rollback dele.
    """

    def __init__(self, inner, *, limit: int, skipped_factory, kind: str) -> None:
        self._inner = inner
        self._limit = limit
        self._skipped = skipped_factory
        self._kind = kind

    def fetch_assessment(self, symbol: str, *, now: datetime):
        from app.database.session import get_session_factory

        if self._limit > 0:
            session = get_session_factory()()
            try:
                usage = read_usage(session, limit=self._limit, now=now)
                if usage.exhausted:
                    return self._skipped(
                        f"Orcamento diario da MarketPulse esgotado "
                        f"({usage.calls}/{usage.limit} chamadas hoje) — "
                        f"{self._kind} fora do calculo ate a virada do dia UTC."
                    )
            finally:
                session.close()

        inicio = perf_counter()
        try:
            assessment = self._inner.fetch_assessment(symbol, now=now)
        except Exception:
            # Falha inesperada tambem consumiu a chamada: registrar so o que
            # deu certo faria o log mentir justamente no dia problematico.
            self._register(symbol, outcome="ERRO", inicio=inicio, now=now, contar=False)
            raise

        self._register(
            symbol,
            outcome=str(getattr(assessment, "status", "")) or "OK",
            inicio=inicio,
            now=now,
            contar=self._limit > 0,
        )
        return assessment

    def _register(
        self, symbol: str, *, outcome: str, inicio: float, now: datetime, contar: bool
    ) -> None:
        """Contabiliza a chamada e grava o registro dela.

        Sessao propria com commit proprio, pelo mesmo motivo da contagem: e
        um fato independente do trabalho do chamador e nao pode ser desfeito
        por um rollback dele. Falha ao registrar nunca derruba a analise — o
        dado ja foi buscado, e perder a analise por causa do diario seria
        trocar o essencial pelo acessorio.
        """
        from app.database.session import get_session_factory
        from app.news.call_log import record_api_call

        session = get_session_factory()()
        try:
            if contar:
                record_call(session, limit=self._limit, now=now)
            record_api_call(
                session,
                kind=self._kind,
                symbol=symbol,
                outcome=outcome,
                duration_ms=int((perf_counter() - inicio) * 1000),
                now=now,
            )
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("marketpulse_call_log_failed")
        finally:
            session.close()
