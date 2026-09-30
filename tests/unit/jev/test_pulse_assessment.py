"""A pergunta enviada, o veredito devolvido e o cache entre os dois.

O contador de chamadas aparece em quase todo teste aqui de proposito.
Numa integracao paga, "quantas vezes" e parte do comportamento correto —
nao um detalhe de desempenho. Este projeto ja esgotou a cota de uma API
externa com uma consulta por ciclo que nao virou nenhuma entrada.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.jev.cache import TTLCache
from app.jev.client import JevClient, JevFailure
from app.jev.pulse_assessment import (
    NEEDS_REVIEW_THRESHOLD,
    PULSE_STATE_CRITERIA,
    QUESTION_REVIEW,
    QUESTION_STATE,
    STATE_CAUTION,
    STATE_INSUFFICIENT,
    STATE_STRONG,
    STATE_WAIT,
    PulseAssessment,
    PulseAssessor,
    as_payload,
)
from app.jev.snapshot import TechnicalSnapshot

AGORA = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


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
        "data_age_minutes": 1.0,
        "risk_reward": 2.1,
        "candle_open_time": "2026-09-30T11:45:00+00:00",
    }
    base.update(overrides)
    return TechnicalSnapshot(**base)  # type: ignore[arg-type]


def _corpo(estado: str = STATE_STRONG, confianca: float = 0.88, revisao: float = 0.1) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            QUESTION_STATE: {
                "type": "choice",
                "choice": estado,
                "confidence": confianca,
                "probabilities": {estado: confianca},
            },
            QUESTION_REVIEW: {"type": "noul", "noul": revisao},
        },
    }


class _Contador:
    """Conta chamadas e guarda o ultimo corpo enviado."""

    def __init__(self, resposta=None) -> None:
        self.chamadas = 0
        self.corpos: list[dict] = []
        self._resposta = resposta or (lambda: httpx.Response(200, json=_corpo()))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.chamadas += 1
        self.corpos.append(json.loads(request.content))
        return self._resposta()


def _assessor(handler, *, ttl: float = 300.0) -> PulseAssessor:
    cliente = JevClient(api_key="k", transport=httpx.MockTransport(handler))
    return PulseAssessor(
        cliente, cache=TTLCache[PulseAssessment](ttl_seconds=ttl), cache_ttl_seconds=ttl
    )


# --- o que e perguntado -----------------------------------------------------


def test_the_two_questions_are_typed_as_choice_and_noul() -> None:
    contador = _Contador()
    _assessor(contador).assess(_snapshot(), now=AGORA)

    perguntas = contador.corpos[0]["questions"]
    assert perguntas[QUESTION_STATE]["type"] == "choice"
    assert perguntas[QUESTION_REVIEW]["type"] == "noul"
    assert set(perguntas[QUESTION_STATE]["criteria"]) == {
        STATE_STRONG,
        STATE_CAUTION,
        STATE_WAIT,
        STATE_INSUFFICIENT,
    }


def test_the_instructions_forbid_inferring_price_and_demand_the_cautious_label() -> None:
    """As duas regras que separam "classificar o que ja foi calculado" de
    "opinar sobre o mercado". Sem elas, o modelo tenderia ao rotulo
    otimista na duvida — que e exatamente onde ele nao deve estar."""
    contador = _Contador()
    _assessor(contador).assess(_snapshot(), now=AGORA)

    instrucoes = contador.corpos[0]["questions"][QUESTION_STATE]["instructions"]
    assert "preco futuro" in instrucoes
    assert STATE_WAIT in instrucoes
    assert STATE_INSUFFICIENT in instrucoes
    assert f"Nunca escolha {STATE_STRONG}" in instrucoes


def test_the_state_carries_the_technical_summary_and_nothing_else() -> None:
    """A lista e fechada. Um campo novo so entra por decisao explicita, que
    aparece no diff — e nao porque alguem acrescentou algo a um objeto de
    dominio que era serializado inteiro."""
    contador = _Contador()
    _assessor(contador).assess(_snapshot(), now=AGORA)

    estado = contador.corpos[0]["state"]
    assert set(estado) == {
        "symbol",
        "timeframe",
        "technical_direction",
        "technical_score_0_100",
        "technical_recommendation",
        "technical_status",
        "trend",
        "trend_by_timeframe",
        "volume_score_0_100",
        "liquidity_score_0_100",
        "spread_ticks",
        "data_age_minutes",
        "data_is_stale",
        "has_blockers",
        "blocker_count",
        "entry_to_stop_pct",
        "entry_to_target_pct",
        "risk_reward",
    }


def test_the_state_never_carries_a_price_level() -> None:
    """Preco de entrada, stop e alvo pertencem ao motor local. Manda-los
    convidaria o modelo a comenta-los, e o contrato diz que ele nao opina
    sobre nivel."""
    contador = _Contador()
    _assessor(contador).assess(
        _snapshot(entry_to_stop_pct=0.35, entry_to_target_pct=0.7), now=AGORA
    )

    estado = contador.corpos[0]["state"]
    assert "entry" not in json.dumps(estado).replace("entry_to_stop_pct", "").replace(
        "entry_to_target_pct", ""
    )
    assert estado["entry_to_stop_pct"] == 0.35   # percentual, nunca o preco


def test_absent_measurements_stay_absent() -> None:
    """Sem tick nao ha spread. Mandar zero faria o modelo ler "spread
    minimo", que e uma medicao que ninguem fez."""
    contador = _Contador()
    _assessor(contador).assess(_snapshot(spread_ticks=None, volume_score=None), now=AGORA)

    estado = contador.corpos[0]["state"]
    assert estado["spread_ticks"] is None
    assert estado["volume_score_0_100"] is None


# --- o veredito -------------------------------------------------------------


def test_a_good_verdict_preserves_confidence_and_model() -> None:
    avaliacao = _assessor(_Contador()).assess(_snapshot(), now=AGORA)

    assert avaliacao.available is True
    assert avaliacao.state == STATE_STRONG
    assert avaliacao.confidence == 0.88
    assert avaliacao.model == "jev-1.13.0"
    assert avaliacao.evaluated_at == AGORA
    assert avaliacao.expires_at == AGORA + timedelta(seconds=300)


def test_needs_review_comes_from_the_noul_probability() -> None:
    """O booleano e derivado com um corte explicito, e a probabilidade
    continua disponivel ao lado dele."""
    acima = _Contador(lambda: httpx.Response(200, json=_corpo(revisao=0.9)))
    abaixo = _Contador(lambda: httpx.Response(200, json=_corpo(revisao=0.2)))

    alto = _assessor(acima).assess(_snapshot(), now=AGORA)
    baixo = _assessor(abaixo).assess(_snapshot(), now=AGORA)

    assert alto.needs_review is True
    assert alto.needs_review_probability == 0.9
    assert baixo.needs_review is False
    assert baixo.needs_review_probability == 0.2


def test_the_review_threshold_is_above_the_coin_flip() -> None:
    """Um aviso disparado na duvida (0.5) aparece sempre e deixa de ser
    lido. O corte precisa estar acima do ponto de indecisao."""
    assert NEEDS_REVIEW_THRESHOLD > 0.5


# --- indisponibilidade ------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(500, json={}),
        lambda r: httpx.Response(401, json={}),
        lambda r: httpx.Response(429, json={}),
        lambda r: httpx.Response(200, text="<html/>", headers={"content-type": "text/html"}),
    ],
    ids=["5xx", "401", "429", "html"],
)
def test_a_failure_never_raises_and_never_invents_confidence(handler) -> None:
    """Esta camada e acessoria: o Pulso ja esta completo sem ela. Uma
    excecao daqui viraria erro 500 numa tela que nao precisava do Jev."""
    avaliacao = _assessor(handler).assess(_snapshot(), now=AGORA)

    assert avaliacao.available is False
    assert avaliacao.confidence == 0.0
    assert avaliacao.state == STATE_INSUFFICIENT


def test_an_unavailable_verdict_is_not_a_cautious_verdict() -> None:
    """`CAUTION` seria um julgamento que ninguem emitiu, e `WAIT` sugeriria
    um cenario se formando. "Nao sei" tem rotulo proprio."""
    avaliacao = _assessor(lambda r: httpx.Response(500, json={})).assess(_snapshot(), now=AGORA)

    assert avaliacao.state == STATE_INSUFFICIENT
    assert avaliacao.state not in (STATE_CAUTION, STATE_WAIT, STATE_STRONG)


def test_local_reason_codes_survive_the_outage() -> None:
    """Os codigos vem da analise local, que continua disponivel. O Jev cair
    nao e motivo para esconder o que o motor tecnico ja sabe."""
    avaliacao = _assessor(lambda r: httpx.Response(503, json={})).assess(_snapshot(), now=AGORA)

    assert avaliacao.available is False
    assert "MTF_ALIGNED" in avaliacao.reason_codes


def test_a_failure_is_not_cached() -> None:
    """Guardar o erro faria uma falha de 2 segundos apagar a classificacao
    pelos 5 minutos seguintes."""
    contador = _Contador(lambda: httpx.Response(500, json={}))
    avaliador = _assessor(contador)

    avaliador.assess(_snapshot(), now=AGORA)
    avaliador.assess(_snapshot(), now=AGORA)

    assert contador.chamadas == 2


# --- cache ------------------------------------------------------------------


def test_the_same_snapshot_is_evaluated_once() -> None:
    """O indicador consulta a cada 15 segundos. Uma chamada por consulta
    seria uma chamada a cada 15 segundos, por grafico aberto."""
    contador = _Contador()
    avaliador = _assessor(contador)

    primeira = avaliador.assess(_snapshot(), now=AGORA)
    segunda = avaliador.assess(_snapshot(), now=AGORA + timedelta(seconds=15))

    assert contador.chamadas == 1
    assert segunda == primeira


def test_a_new_candle_is_evaluated_again() -> None:
    contador = _Contador()
    avaliador = _assessor(contador)

    avaliador.assess(_snapshot(), now=AGORA)
    avaliador.assess(_snapshot(candle_open_time="2026-09-30T12:00:00+00:00"), now=AGORA)

    assert contador.chamadas == 2


def test_a_changed_scenario_inside_the_same_candle_is_evaluated_again() -> None:
    """Um bloqueio que aparece no meio da candle muda a decisao, e a candle
    sozinha nao capturaria isso."""
    contador = _Contador()
    avaliador = _assessor(contador)

    avaliador.assess(_snapshot(), now=AGORA)
    avaliador.assess(_snapshot(has_blockers=True, blocker_count=1), now=AGORA)

    assert contador.chamadas == 2


def test_noise_that_does_not_change_the_decision_reuses_the_verdict() -> None:
    """Se o fingerprint acompanhasse cada oscilacao, o cache nunca
    acertaria e cada refresh viraria chamada paga."""
    contador = _Contador()
    avaliador = _assessor(contador)

    avaliador.assess(_snapshot(score=78.0, spread_ticks=3.0), now=AGORA)
    avaliador.assess(_snapshot(score=78.2, spread_ticks=3.04), now=AGORA)

    assert contador.chamadas == 1


def test_an_expired_entry_is_evaluated_again() -> None:
    contador = _Contador()
    avaliador = _assessor(contador, ttl=60.0)

    avaliador.assess(_snapshot(), now=AGORA)
    avaliador.assess(_snapshot(), now=AGORA + timedelta(seconds=61))

    assert contador.chamadas == 2


def test_two_symbols_do_not_share_a_verdict() -> None:
    contador = _Contador()
    avaliador = _assessor(contador)

    avaliador.assess(_snapshot(symbol="MNQ"), now=AGORA)
    avaliador.assess(_snapshot(symbol="ES"), now=AGORA)

    assert contador.chamadas == 2


# --- o bloco da API ---------------------------------------------------------


def test_the_payload_never_contains_null() -> None:
    """Regra do contrato do Pulso: o indicador le por busca de string, e um
    `null` o obrigaria a distinguir ausencia de zero dentro de um texto."""
    for avaliacao in (
        _assessor(_Contador()).assess(_snapshot(), now=AGORA),
        PulseAssessment.unavailable(JevFailure.DISABLED),
    ):
        assert None not in as_payload(avaliacao).values()


def test_the_payload_states_it_is_not_a_profit_probability() -> None:
    """O vocabulario e travado em todo o sistema: score e confluencia,
    confianca e concentracao da distribuicao. Nenhum dos dois e chance de
    lucro (ver CLAUDE.md)."""
    nota = as_payload(_assessor(_Contador()).assess(_snapshot(), now=AGORA))["note"]

    assert "probabilidade de lucro" in nota
    assert "nao envia ordens" in nota


def test_the_criteria_describe_when_to_pick_each_label() -> None:
    """Rotulo sem criterio faria o modelo inventar a propria escala."""
    assert set(PULSE_STATE_CRITERIA) == {
        STATE_STRONG,
        STATE_CAUTION,
        STATE_WAIT,
        STATE_INSUFFICIENT,
    }
    assert all(len(texto) > 40 for texto in PULSE_STATE_CRITERIA.values())
