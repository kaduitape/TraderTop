"""A politica de retencao: o que ela apaga, o que ela recusa a apagar, e o
ritmo em que faz isso."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.database.models.data_quality_event import DataQualityEvent
from app.database.models.tick import Tick
from app.database.repositories.symbol_repository import SymbolRepository
from app.maintenance.retention import (
    OPTIMIZABLE_TABLES,
    RetentionPolicy,
    apply_retention,
    optimize_tables,
)
from app.mt5.symbol_mapper import SymbolSpecification

AGORA = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


def _politica(**extra) -> RetentionPolicy:
    base = {
        "tick_days": 7,
        "quality_days": 14,
        "quality_critical_days": 90,
        "chunk_size": 100,
        "max_chunks_per_run": 100,
    }
    return RetentionPolicy(**{**base, **extra})


def _spec(name: str) -> SymbolSpecification:
    return SymbolSpecification(  # type: ignore[arg-type]
        name=name,
        description="Test symbol",
        digits=5,
        point=0.00001,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        trade_contract_size=100_000.0,
        spread=2,
        trade_mode=4,
        visible=True,
    )


@pytest.fixture
def simbolo(db_session):
    """Limpa antes E depois, inclusive ticks e eventos: o SQLite da suite e
    compartilhado e nao aplica ON DELETE CASCADE por padrao."""

    def limpar() -> None:
        db_session.query(Tick).delete()
        db_session.query(DataQualityEvent).delete()
        db_session.commit()

    limpar()
    row = SymbolRepository(db_session).upsert_from_specification(_spec("RETEN_SYM"))
    db_session.commit()
    yield row
    limpar()


def _tick(db_session, symbol_id: int, *, idade_dias: float, bid: str = "1.10000") -> None:
    db_session.add(
        Tick(
            symbol_id=symbol_id,
            timestamp=AGORA - timedelta(days=idade_dias),
            bid=Decimal(bid),
            ask=Decimal(bid),
            last=Decimal(bid),
            volume=Decimal("1"),
            flags=0,
        )
    )


def _evento(db_session, symbol_id: int, *, idade_dias: float, severity: str) -> None:
    db_session.add(
        DataQualityEvent(
            symbol_id=symbol_id,
            timeframe=None,
            check_name="feed_delay",
            severity=severity,
            message="x",
            detected_at=AGORA - timedelta(days=idade_dias),
        )
    )


# --- ticks -----------------------------------------------------------------


def test_ticks_inside_the_window_survive(db_session, simbolo) -> None:
    _tick(db_session, simbolo.id, idade_dias=1)
    _tick(db_session, simbolo.id, idade_dias=6.9)
    _tick(db_session, simbolo.id, idade_dias=8, bid="1.20000")
    db_session.commit()

    resultado = apply_retention(db_session, _politica(), now=AGORA)

    assert resultado.ticks_deleted == 1
    assert db_session.query(Tick).count() == 2


def test_the_live_analysis_window_is_never_at_risk(db_session, simbolo) -> None:
    """A analise ao vivo le no maximo 1 hora de ticks (`tick_window_seconds`
    e limitado a 3600 no painel) e a FotoAnalise le 1 tick. Com a retencao
    minima aceita pela politica — 1 dia — essa hora continua inteira. E a
    razao de a retencao de ticks nao poder afetar o painel."""
    for minutos in (1, 10, 30, 59):
        _tick(db_session, simbolo.id, idade_dias=minutos / 1440, bid=f"1.1000{minutos % 10}")
    db_session.commit()

    resultado = apply_retention(db_session, _politica(tick_days=1), now=AGORA)

    assert resultado.ticks_deleted == 0
    assert db_session.query(Tick).count() == 4


# --- eventos de qualidade ---------------------------------------------------


def test_critical_events_outlive_the_rest(db_session, simbolo) -> None:
    _evento(db_session, simbolo.id, idade_dias=20, severity="INFO")
    _evento(db_session, simbolo.id, idade_dias=20, severity="WARNING")
    _evento(db_session, simbolo.id, idade_dias=20, severity="CRITICAL")
    db_session.commit()

    resultado = apply_retention(db_session, _politica(), now=AGORA)

    assert resultado.quality_deleted == 2
    restantes = db_session.query(DataQualityEvent).all()
    assert [e.severity for e in restantes] == ["CRITICAL"]


def test_critical_goes_too_once_past_its_own_window(db_session, simbolo) -> None:
    _evento(db_session, simbolo.id, idade_dias=100, severity="CRITICAL")
    db_session.commit()

    assert apply_retention(db_session, _politica(), now=AGORA).quality_deleted == 1
    assert db_session.query(DataQualityEvent).count() == 0


def test_an_unknown_severity_falls_into_the_short_window(db_session, simbolo) -> None:
    """A passada curta EXCLUI critical em vez de listar as demais. Com uma
    lista de permitidos, uma severidade nova ficaria no banco para sempre
    sem ninguem notar."""
    _evento(db_session, simbolo.id, idade_dias=20, severity="DEBUG")
    db_session.commit()

    assert apply_retention(db_session, _politica(), now=AGORA).quality_deleted == 1
    assert db_session.query(DataQualityEvent).count() == 0


# --- ritmo ------------------------------------------------------------------


def test_deletion_happens_in_chunks(db_session, simbolo) -> None:
    for i in range(25):
        _tick(db_session, simbolo.id, idade_dias=30, bid=f"1.{i:05d}")
    db_session.commit()

    resultado = apply_retention(db_session, _politica(chunk_size=10), now=AGORA)

    assert resultado.ticks_deleted == 25
    # 10 + 10 + 5: o ultimo lote volta incompleto e encerra o laco.
    assert resultado.chunks == 3


def test_the_chunk_ceiling_stops_the_run_and_says_so(db_session, simbolo) -> None:
    """A primeira execucao contra um acumulo de meses nao pode rodar por
    horas disputando IO com a coleta. Ela limpa o teto, avisa, e a proxima
    continua de onde parou."""
    for i in range(25):
        _tick(db_session, simbolo.id, idade_dias=30, bid=f"1.{i:05d}")
    db_session.commit()

    resultado = apply_retention(
        db_session, _politica(chunk_size=10, max_chunks_per_run=2), now=AGORA
    )

    assert resultado.ticks_deleted == 20
    assert resultado.hit_chunk_limit is True
    assert "resta trabalho" in resultado.describe()
    assert db_session.query(Tick).count() == 5


def test_the_chunk_budget_is_shared_between_the_two_tables(db_session, simbolo) -> None:
    """O teto limita IO no mesmo disco. Um orcamento por tabela dobraria o
    impacto exatamente na execucao em que ele ja esta no limite."""
    for i in range(10):
        _tick(db_session, simbolo.id, idade_dias=30, bid=f"1.{i:05d}")
    for _ in range(10):
        _evento(db_session, simbolo.id, idade_dias=30, severity="INFO")
    db_session.commit()

    resultado = apply_retention(
        db_session, _politica(chunk_size=5, max_chunks_per_run=3), now=AGORA
    )

    assert resultado.chunks == 3
    assert resultado.total_deleted == 15


# --- configuracao invalida ---------------------------------------------------


def test_critical_retention_below_the_others_is_refused(db_session, simbolo) -> None:
    """Com CRITICAL mais curto que o resto, os eventos graves sumiriam ANTES
    dos triviais — o oposto do que a separacao existe para fazer. A politica
    nao roda, e nada e apagado."""
    _evento(db_session, simbolo.id, idade_dias=100, severity="CRITICAL")
    db_session.commit()

    resultado = apply_retention(
        db_session, _politica(quality_days=30, quality_critical_days=7), now=AGORA
    )

    assert resultado.total_deleted == 0
    assert "nao pode ser menor" in resultado.skipped_reason
    assert db_session.query(DataQualityEvent).count() == 1


@pytest.mark.parametrize(
    ("campo", "valor"),
    [
        ("tick_days", 0),
        ("quality_days", 0),
        ("chunk_size", 0),
        ("max_chunks_per_run", 0),
    ],
)
def test_a_policy_that_would_delete_everything_is_refused(
    db_session, simbolo, campo, valor
) -> None:
    """`tick_days=0` apagaria ate o tick que acabou de chegar. Zero em
    qualquer destes campos e erro de configuracao, nao instrucao."""
    _tick(db_session, simbolo.id, idade_dias=0.001)
    db_session.commit()

    resultado = apply_retention(db_session, _politica(**{campo: valor}), now=AGORA)

    assert resultado.skipped_reason != ""
    assert db_session.query(Tick).count() == 1


# --- recuperacao de espaco ---------------------------------------------------


def test_optimize_says_it_cannot_instead_of_pretending(db_session) -> None:
    """Em SQLite nao ha OPTIMIZE por tabela. Um "ok" ali esconderia que o
    espaco nao voltou — e o ponto inteiro do comando e devolver espaco."""
    resultado = optimize_tables(db_session)

    assert set(resultado) == set(OPTIMIZABLE_TABLES)
    assert all("nao suportado" in estado for estado in resultado.values())


def test_optimize_refuses_a_table_outside_the_allowlist(db_session) -> None:
    """Nome de tabela entra em SQL por interpolacao — nao existe bind
    parameter para identificador. A allowlist e o que impede injecao."""
    resultado = optimize_tables(db_session, ("users; DROP TABLE users",))

    assert "nao suportado" in next(iter(resultado.values())) or "fora da lista" in next(
        iter(resultado.values())
    )
