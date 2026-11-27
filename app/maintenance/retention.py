"""Politica de retencao: o que sai do banco, e por que o resto fica.

## O problema

Duas tabelas crescem sem teto:

- **`ticks`** e, de longe, a maior. Varias linhas por segundo por simbolo,
  cada uma com quatro `Numeric(18,8)`, dois indices e uma unique de quatro
  colunas — na pratica ~200 bytes por tick somando indices. Um mes de
  coleta em tres simbolos passa de dois gigabytes.
- **`data_quality_events`** cresce devagar mas para sempre, e **nenhum
  codigo do sistema le essa tabela**. Ela so e consultada a mao, quando
  alguem investiga um feed ruim.

Havia purga para a primeira (`data purge-ticks`) e nenhuma para a segunda,
e a purga existente so rodava quando alguem a chamava. "Politica" que
depende de alguem lembrar nao e politica.

## O que a retencao NAO pode quebrar

A analise ao vivo le, no maximo:

- **1 hora** de ticks (`tick_window_seconds`, limitado a 3600 no painel);
- **1 tick** na FotoAnalise, so para saber se o feed esta vivo;
- **candles**, que nao sao tocadas aqui — elas ficam indefinidamente, sao
  ordens de grandeza menores e sao a base do backtest por candle.

Entao qualquer retencao de ticks acima de algumas horas e invisivel para o
sistema em operacao. O que a janela de fato limita e o **backtest por
tick**, que precisa da sequencia real para simular fills.

## Por que nao afinar em vez de apagar

A alternativa obvia — guardar 1 tick por segundo depois de N dias em vez
de apagar — foi rejeitada. O backtest por tick existe justamente para
simular preenchimento contra a sequencia real; rodado sobre ticks
afinados ele continua produzindo numero, e o numero fica otimista sem
avisar. Um backtest ausente e um problema visivel; um backtest que mente
nao e.

## Lotes, e por que isso importa

Todo apagamento aqui e em lotes com commit por lote. Um `DELETE` unico de
milhoes de linhas no InnoDB segura uma transacao gigante: o undo log
cresce, a tabela de ticks trava para a coleta que tenta escrever nela, e
um timeout desfaz tudo — a execucao seguinte encara a mesma montanha e
falha igual. Em lotes, o trabalho feito fica feito.

## Espaco em disco nao volta sozinho

`DELETE` no InnoDB **nao devolve espaco ao sistema de arquivos**: libera
paginas para reuso dentro do mesmo `.ibd`. Depois da primeira purga
grande, `df` nao se mexe. Para encolher o arquivo e preciso `OPTIMIZE
TABLE`, que reconstroi a tabela — ver `optimize_tables` e o aviso la.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database.repositories.data_quality_repository import DataQualityEventRepository
from app.database.repositories.tick_repository import TickRepository

logger = logging.getLogger(__name__)

CRITICAL = "CRITICAL"
"""A severidade que recebe o prazo longo. Literal e nao
`Severity.CRITICAL.value` para nao arrastar `app.market` para dentro de
`app.maintenance` por um unico texto."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Quantos dias cada coisa fica, e em que ritmo apagar."""

    tick_days: int
    quality_days: int
    quality_critical_days: int
    chunk_size: int
    max_chunks_per_run: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> RetentionPolicy:
        s = settings or get_settings()
        return cls(
            tick_days=s.tick_retention_days,
            quality_days=s.quality_event_retention_days,
            quality_critical_days=s.quality_event_critical_retention_days,
            chunk_size=s.retention_chunk_size,
            max_chunks_per_run=s.retention_max_chunks_per_run,
        )

    def validate(self) -> list[str]:
        """O que torna esta politica inutilizavel.

        Devolve lista em vez de levantar: a inicializacao precisa poder
        reclamar de tudo de uma vez, e uma politica invalida desliga a
        retencao em vez de derrubar a aplicacao."""
        problemas: list[str] = []
        if self.tick_days < 1:
            problemas.append("TICK_RETENTION_DAYS precisa ser >= 1")
        if self.quality_days < 1:
            problemas.append("QUALITY_EVENT_RETENTION_DAYS precisa ser >= 1")
        if self.quality_critical_days < self.quality_days:
            # Nao e questao de gosto: com o prazo de CRITICAL menor que o
            # dos demais, os eventos graves sumiriam ANTES dos triviais —
            # o oposto do que a separacao existe para fazer.
            problemas.append(
                "QUALITY_EVENT_CRITICAL_RETENTION_DAYS nao pode ser menor que "
                "QUALITY_EVENT_RETENTION_DAYS"
            )
        if self.chunk_size < 1:
            problemas.append("RETENTION_CHUNK_SIZE precisa ser >= 1")
        if self.max_chunks_per_run < 1:
            problemas.append("RETENTION_MAX_CHUNKS_PER_RUN precisa ser >= 1")
        return problemas


@dataclass(frozen=True, slots=True)
class RetentionResult:
    ticks_deleted: int = 0
    quality_deleted: int = 0
    chunks: int = 0
    hit_chunk_limit: bool = False
    """Parou por teto, nao por ter acabado. A proxima execucao continua de
    onde esta — e se isso aparecer varias vezes seguidas, o acumulo esta
    entrando mais rapido do que sai e a janela precisa encolher."""

    duration_seconds: float = 0.0
    skipped_reason: str = ""

    @property
    def total_deleted(self) -> int:
        return self.ticks_deleted + self.quality_deleted

    def describe(self) -> str:
        if self.skipped_reason:
            return f"retencao nao executada: {self.skipped_reason}"
        partes = [
            f"{self.ticks_deleted} tick(s)",
            f"{self.quality_deleted} evento(s) de qualidade",
            f"em {self.chunks} lote(s), {self.duration_seconds:.1f}s",
        ]
        if self.hit_chunk_limit:
            partes.append("teto de lotes atingido — resta trabalho para a proxima")
        return "removidos: " + ", ".join(partes)


class _ChunkBudget:
    """Teto compartilhado de lotes entre as tabelas de uma execucao.

    Compartilhado de proposito: o custo que o teto limita e IO no mesmo
    disco, e dar um orcamento proprio a cada tabela dobraria o impacto
    exatamente na execucao em que ele ja esta no limite.
    """

    def __init__(self, max_chunks: int) -> None:
        self._restantes = max_chunks
        self.usados = 0

    @property
    def esgotado(self) -> bool:
        return self._restantes <= 0

    def consumir(self) -> None:
        self._restantes -= 1
        self.usados += 1


def apply_retention(
    session: Session,
    policy: RetentionPolicy,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    """Aplica a politica. Commita por lote; nunca levanta por dado.

    A sessao chega de fora para que o CLI e o agendador usem o mesmo
    caminho, e para que o teste possa inspecionar o que sobrou.
    """
    problemas = policy.validate()
    if problemas:
        return RetentionResult(skipped_reason="; ".join(problemas))

    agora = now or datetime.now(UTC)
    inicio = time.monotonic()
    orcamento = _ChunkBudget(policy.max_chunks_per_run)

    ticks = _purge_ticks(session, policy, agora, orcamento)
    qualidade = _purge_quality(session, policy, agora, orcamento)

    return RetentionResult(
        ticks_deleted=ticks,
        quality_deleted=qualidade,
        chunks=orcamento.usados,
        hit_chunk_limit=orcamento.esgotado,
        duration_seconds=time.monotonic() - inicio,
    )


def _purge_ticks(
    session: Session, policy: RetentionPolicy, now: datetime, orcamento: _ChunkBudget
) -> int:
    repo = TickRepository(session)
    cutoff = now - timedelta(days=policy.tick_days)
    total = 0

    while not orcamento.esgotado:
        removidos = repo.delete_batch_older_than(cutoff=cutoff, limit=policy.chunk_size)
        if removidos == 0:
            break
        session.commit()
        orcamento.consumir()
        total += removidos

    return total


def _purge_quality(
    session: Session, policy: RetentionPolicy, now: datetime, orcamento: _ChunkBudget
) -> int:
    repo = DataQualityEventRepository(session)
    total = 0

    # Duas passadas, prazos diferentes. A primeira exclui CRITICAL em vez de
    # listar INFO/WARNING: severidade e coluna de texto livre, e um valor
    # novo tem de cair no prazo curto por omissao.
    passadas = (
        (now - timedelta(days=policy.quality_days), {"exclude_severities": (CRITICAL,)}),
        (now - timedelta(days=policy.quality_critical_days), {"severities": (CRITICAL,)}),
    )

    for cutoff, filtro in passadas:
        while not orcamento.esgotado:
            removidos = repo.delete_batch_older_than(
                cutoff=cutoff, limit=policy.chunk_size, **filtro
            )
            if removidos == 0:
                break
            session.commit()
            orcamento.consumir()
            total += removidos

    return total


# --- Recuperacao de espaco -------------------------------------------------

OPTIMIZABLE_TABLES = ("ticks", "data_quality_events")


def optimize_tables(
    session: Session, tables: tuple[str, ...] = OPTIMIZABLE_TABLES
) -> dict[str, str]:
    """Devolve o espaco das linhas apagadas ao sistema de arquivos.

    **Isto nao e parte da rotina automatica, e nao deve ser.** `OPTIMIZE
    TABLE` no InnoDB reconstroi a tabela inteira: precisa de espaco livre
    da ordem do tamanho dela para a copia, e bloqueia escrita enquanto
    roda. Numa tabela de ticks de gigabytes isso e minutos com a coleta
    parada — aceitavel uma vez, com o mercado fechado, decidido por uma
    pessoa; inaceitavel toda madrugada sem ninguem olhando.

    Rodar isto so faz sentido DEPOIS da primeira purga grande. Em regime,
    as paginas liberadas sao reaproveitadas pelas linhas novas e o arquivo
    para de crescer sozinho.

    Em SQLite nao ha equivalente por tabela (`VACUUM` e do arquivo todo) e
    a funcao devolve `nao suportado` em vez de fingir que fez — e o caso
    da suite, e um "ok" mentiroso ali esconderia a diferenca real entre os
    dois bancos.
    """
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect != "mysql":
        return dict.fromkeys(tables, f"nao suportado em {dialect or 'dialeto desconhecido'}")

    resultado: dict[str, str] = {}
    for tabela in tables:
        if tabela not in OPTIMIZABLE_TABLES:
            # Nome de tabela entra em SQL por interpolacao: nao ha bind
            # parameter para identificador. A allowlist e o que impede
            # isso de virar injecao.
            resultado[tabela] = "tabela fora da lista permitida"
            continue
        try:
            session.execute(text(f"OPTIMIZE TABLE {tabela}"))
            session.commit()
            resultado[tabela] = "ok"
        except Exception as exc:  # pragma: no cover - depende do servidor
            session.rollback()
            logger.warning("OPTIMIZE TABLE %s falhou: %s", tabela, exc)
            resultado[tabela] = f"falhou: {exc}"
    return resultado
