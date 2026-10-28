"""Identidade de sessao, frescor e ordem de chegada."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.hedge.ratelimit import RateLimiter
from app.hedge.sessions import (
    SessionState,
    accepts_sequence,
    evaluate_freshness,
    session_key,
)

AGORA = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


# --- identidade -------------------------------------------------------------


def _chave(**overrides) -> str:
    base = {"terminal_id": "T1", "chart_id": "C1", "symbol": "BTCUSDm", "setup_id": 7}
    base.update(overrides)
    return session_key(**base)  # type: ignore[arg-type]


def test_each_part_changes_the_identity() -> None:
    """Tirar qualquer uma das quatro mistura dados de sessoes distintas."""
    base = _chave()

    assert _chave(terminal_id="T2") != base
    assert _chave(chart_id="C2") != base
    assert _chave(symbol="ETHUSDm") != base
    assert _chave(setup_id=8) != base


def test_two_charts_of_the_same_symbol_are_two_sessions() -> None:
    """O caso NORMAL, nao a excecao: o conector pode estar num grafico
    separado justamente para nao brigar com outro EA."""
    assert _chave(chart_id="C1") != _chave(chart_id="C2")


def test_the_symbol_case_does_not_split_a_session() -> None:
    """O terminal manda o simbolo como esta no grafico. `BTCUSDm` contra
    `BTCUSDM` criaria duas sessoes para o mesmo par."""
    assert _chave(symbol="BTCUSDm") == _chave(symbol="BTCUSDM")


def test_the_key_leaks_no_machine_name() -> None:
    """A chave vai para log e banco. Nome de maquina ali seria dado do
    cliente onde ninguem procuraria."""
    assert "T1" not in _chave()
    assert len(_chave()) == 64


def test_the_key_is_stable_across_calls() -> None:
    assert _chave() == _chave()


# --- frescor ----------------------------------------------------------------


def test_no_snapshot_yet_is_waiting_not_disconnected() -> None:
    """Esperanca e ausencia desenham coisas diferentes na tela."""
    v = evaluate_freshness(last_seen_at=None, now=AGORA, max_age_seconds=30)

    assert v.state is SessionState.WAITING_DATA
    assert v.is_live is False


def test_inside_the_limit_is_live() -> None:
    v = evaluate_freshness(
        last_seen_at=AGORA - timedelta(seconds=5), now=AGORA, max_age_seconds=30
    )

    assert v.state is SessionState.CONNECTED
    assert v.is_live is True


def test_past_the_limit_is_stale_but_not_disconnected() -> None:
    """A faixa do meio existe para o grafico nao piscar entre conectado e
    desconectado a cada atraso de rede."""
    v = evaluate_freshness(
        last_seen_at=AGORA - timedelta(seconds=45), now=AGORA, max_age_seconds=30
    )

    assert v.state is SessionState.STALE_DATA
    assert v.is_live is False
    assert "45s" in v.reason


def test_long_silence_is_disconnected() -> None:
    v = evaluate_freshness(
        last_seen_at=AGORA - timedelta(seconds=200), now=AGORA, max_age_seconds=30
    )

    assert v.state is SessionState.DISCONNECTED


def test_only_connected_may_look_like_a_live_signal() -> None:
    """Depois da validade o ultimo numero pode ficar na tela, mas nunca
    com aparencia de sinal atual."""
    for idade in (45, 200):
        v = evaluate_freshness(
            last_seen_at=AGORA - timedelta(seconds=idade),
            now=AGORA,
            max_age_seconds=30,
        )
        assert v.is_live is False


# --- ordem ------------------------------------------------------------------


def test_an_out_of_order_snapshot_is_rejected() -> None:
    """A rede nao garante ordem. Aplicar um estado antigo sobre um novo
    faria o grafico voltar no tempo — sem erro, porque o pacote velho e
    valido em si."""
    assert accepts_sequence(incoming=5, last_accepted=9) is False


def test_a_repeat_is_rejected_too() -> None:
    """Reprocessar gravaria a mesma observacao duas vezes, inflando a base
    de treino com duplicatas que pareceriam independentes."""
    assert accepts_sequence(incoming=9, last_accepted=9) is False


def test_the_next_snapshot_is_accepted() -> None:
    assert accepts_sequence(incoming=10, last_accepted=9) is True
    assert accepts_sequence(incoming=1, last_accepted=0) is True


# --- limite de taxa ---------------------------------------------------------


def test_a_burst_is_allowed_then_throttled() -> None:
    """Abrir varios graficos de uma vez e uma rajada legitima; manter a
    taxa alta nao e."""
    limitador = RateLimiter(rate_per_second=2.0, burst=5.0)

    assert all(limitador.allow("k", now=0.0) for _ in range(5))
    assert limitador.allow("k", now=0.0) is False


def test_the_bucket_refills_with_time() -> None:
    limitador = RateLimiter(rate_per_second=2.0, burst=5.0)
    for _ in range(5):
        limitador.allow("k", now=0.0)

    assert limitador.allow("k", now=0.0) is False
    assert limitador.allow("k", now=1.0) is True


def test_two_keys_do_not_share_the_limit() -> None:
    """Dois operadores atras do mesmo NAT nao podem se limitar um ao
    outro — e por isso o limite e por chave, nao por IP."""
    limitador = RateLimiter(rate_per_second=1.0, burst=2.0)
    limitador.allow("a", now=0.0)
    limitador.allow("a", now=0.0)

    assert limitador.allow("a", now=0.0) is False
    assert limitador.allow("b", now=0.0) is True


def test_the_key_table_has_a_ceiling() -> None:
    """Sem teto, chaves revogadas ficariam na memoria para sempre."""
    limitador = RateLimiter(rate_per_second=1.0, burst=1.0, max_keys=4)
    for i in range(20):
        limitador.allow(f"k{i}", now=float(i))

    assert len(limitador._buckets) <= 4  # noqa: SLF001
