"""O resumo tecnico: o que sai, o que nunca sai, e o que faz o cache valer.

Os testes de `fingerprint` sao testes de CUSTO, nao de pureza: um
fingerprint sensivel demais gasta cota em cada tick; um insensivel demais
congela um veredito que devia ter mudado.
"""

from __future__ import annotations

import json

from app.jev.snapshot import (
    MAX_REASON_CODES,
    REASON_LABELS,
    TechnicalSnapshot,
    reason_codes,
)


def _snapshot(**overrides) -> TechnicalSnapshot:
    base = {
        "symbol": "MNQ",
        "timeframe": "M15",
        "direction": "LONG",
        "score": 78.0,
        "recommendation": "ENTER",
        "technical_status": "READY",
        "trend": "UP",
        "timeframe_trends": {"H1": "UP", "H4": "UP"},
        "volume_score": 71.0,
        "liquidity_score": 66.0,
        "spread_ticks": 3.0,
        "risk_reward": 2.1,
        "candle_open_time": "2026-09-30T11:45:00+00:00",
    }
    base.update(overrides)
    return TechnicalSnapshot(**base)  # type: ignore[arg-type]


# --- o que nunca sai --------------------------------------------------------


def test_no_secret_shaped_key_can_appear_in_the_state() -> None:
    """A lista de campos e fechada por construcao. Este teste e a rede
    embaixo dela: se alguem adicionar um campo com credencial, ele falha
    antes de o dado sair do processo."""
    serial = json.dumps(_snapshot().as_state()).lower()

    for proibido in (
        "password",
        "senha",
        "token",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "login",
        "account",
        "conta",
        "broker",
        "email",
        "user",
    ):
        assert proibido not in serial, proibido


def test_the_state_is_built_from_a_closed_list() -> None:
    """Se `as_state` passasse a serializar o objeto inteiro, um campo novo
    vazaria sozinho. Um numero fixo de chaves torna isso um diff."""
    assert len(_snapshot().as_state()) == 18


def test_absent_values_stay_none_and_never_become_zero() -> None:
    estado = _snapshot(volume_score=None, spread_ticks=None, risk_reward=None).as_state()

    assert estado["volume_score_0_100"] is None
    assert estado["spread_ticks"] is None
    assert estado["risk_reward"] is None


# --- fingerprint ------------------------------------------------------------


def test_the_current_price_is_not_part_of_the_identity() -> None:
    """Nao ha campo de preco atual no snapshot, e e por isso que o cache
    funciona: com ele, a identidade mudaria a cada tick."""
    assert "current_price" not in _snapshot().as_state()
    assert "price" not in _snapshot().as_state()


def test_noise_keeps_the_same_fingerprint() -> None:
    assert _snapshot(score=78.0).fingerprint() == _snapshot(score=78.4).fingerprint()


def test_a_decision_change_produces_a_new_fingerprint() -> None:
    base = _snapshot().fingerprint()

    assert _snapshot(technical_status="NO_SETUP").fingerprint() != base
    assert _snapshot(direction="SHORT").fingerprint() != base
    assert _snapshot(has_blockers=True, blocker_count=1).fingerprint() != base
    assert _snapshot(is_stale=True).fingerprint() != base
    assert _snapshot(candle_open_time="2026-09-30T12:00:00+00:00").fingerprint() != base


def test_the_fingerprint_is_stable_across_calls() -> None:
    """Fosse instavel (ordem de dicionario, hash aleatorio de string), o
    cache erraria sempre e a cota iria embora sem ninguem entender."""
    assert _snapshot().fingerprint() == _snapshot().fingerprint()
    assert (
        _snapshot(timeframe_trends={"H4": "UP", "H1": "UP"}).fingerprint()
        == _snapshot(timeframe_trends={"H1": "UP", "H4": "UP"}).fingerprint()
    )


# --- codigos de motivo ------------------------------------------------------


def test_every_code_emitted_has_a_short_label() -> None:
    """Um codigo sem rotulo chegaria cru ao grafico."""
    cenarios = [
        _snapshot(),
        _snapshot(is_stale=True, has_blockers=True, blocker_count=2),
        _snapshot(direction="NONE", volume_score=20.0, spread_ticks=50.0, risk_reward=0.4),
        _snapshot(timeframe_trends={"H1": "DOWN", "H4": "DOWN"}),
    ]
    for cenario in cenarios:
        for codigo in reason_codes(cenario):
            assert codigo in REASON_LABELS, codigo


def test_blockers_come_before_confirmations() -> None:
    """A legenda corta em tres. O operador precisa ver primeiro o que o
    faria desistir, nao o que o encorajaria."""
    codigos = reason_codes(_snapshot(is_stale=True, has_blockers=True, blocker_count=1))

    assert codigos[:3] == ["DATA_STALE", "BLOCKERS_PRESENT", "SPREAD_ACCEPTABLE"]
    assert codigos.index("DATA_STALE") < codigos.index("VOLUME_FAVORABLE")


def test_conflicting_timeframes_are_reported_as_conflict() -> None:
    codigos = reason_codes(_snapshot(timeframe_trends={"H1": "DOWN", "H4": "UP"}))

    assert "MTF_CONFLICT" in codigos
    assert "MTF_ALIGNED" not in codigos


def test_unknown_alignment_is_not_reported_as_conflict() -> None:
    """"Nao sei" e "conflito" sao afirmacoes diferentes, e a segunda e mais
    forte do que os dados sustentam."""
    sem_dados = reason_codes(_snapshot(timeframe_trends={"H1": "SEM_DADOS"}))
    sem_lado = reason_codes(_snapshot(direction="NONE"))

    assert "MTF_CONFLICT" not in sem_dados
    assert "MTF_ALIGNED" not in sem_dados
    assert "MTF_CONFLICT" not in sem_lado


def test_a_missing_measurement_produces_no_code_either_way() -> None:
    """Sem spread medido nao ha "spread aceitavel" nem "spread alargado" —
    ha ausencia, e ausencia nao vira confirmacao."""
    codigos = reason_codes(_snapshot(spread_ticks=None, volume_score=None))

    assert "SPREAD_ACCEPTABLE" not in codigos
    assert "SPREAD_WIDE" not in codigos
    assert "VOLUME_FAVORABLE" not in codigos
    assert "VOLUME_WEAK" not in codigos


def test_the_codes_are_deterministic() -> None:
    """Derivados da analise local, nunca de texto do Jev: o mesmo cenario
    sempre produz a mesma lista, e cada codigo pode ser conferido contra o
    relatorio."""
    assert reason_codes(_snapshot()) == reason_codes(_snapshot())


def test_three_codes_is_the_chart_budget() -> None:
    assert MAX_REASON_CODES == 3
