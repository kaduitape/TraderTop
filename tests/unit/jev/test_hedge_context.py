"""Classificacao de contexto do Hedge: custo, cache, teto e silencio.

O snapshot chega a cada 1-2 segundos. Quase todo teste aqui conta
CHAMADAS, porque numa integracao paga "quantas vezes" e parte do
comportamento correto — este projeto ja esgotou a cota de uma API externa
com uma consulta por ciclo que nao virou nenhuma entrada.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.jev.cache import TTLCache
from app.jev.client import JevClient
from app.jev.hedge_context import (
    CONTEXT_CALM,
    CONTEXT_CRITERIA,
    CONTEXT_ELEVATED,
    CONTEXT_EVENT_RISK,
    CONTEXT_INSUFFICIENT,
    MINUTE_BUCKET,
    QUESTION_CONTEXT,
    QUESTION_REVIEW,
    ContextEvent,
    HedgeContextAssessment,
    HedgeContextAssessor,
    HedgeContextInput,
)

AGORA = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _limpa_orcamento(db_session):
    """O teto diario vive em `system_settings`, compartilhado pela suite.

    Sem limpar antes E depois, o consumo de um teste conta no do seguinte e
    os testes de orcamento passam a depender da ordem de execucao.
    """
    from app.database.models.system_setting import SystemSetting

    def apagar() -> None:
        db_session.query(SystemSetting).filter(
            SystemSetting.key.in_(("jev_daily_usage", "marketpulse_daily_usage"))
        ).delete(synchronize_session=False)
        db_session.commit()

    apagar()
    yield
    apagar()


def _evento(**overrides) -> ContextEvent:
    base = {"title": "CPI y/y", "impact": "HIGH", "minutes_until": 12, "currency": "USD"}
    base.update(overrides)
    return ContextEvent(**base)  # type: ignore[arg-type]


def _entrada(**overrides) -> HedgeContextInput:
    base = {
        "symbol": "MNQZ5",
        "currencies": ("USD",),
        "events": (_evento(),),
        "horizon_minutes": 60,
        "calendar_usable": True,
    }
    base.update(overrides)
    return HedgeContextInput(**base)  # type: ignore[arg-type]


def _corpo(categoria: str = CONTEXT_EVENT_RISK, confianca: float = 0.81, revisao: float = 0.7) -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            QUESTION_CONTEXT: {
                "type": "choice",
                "choice": categoria,
                "confidence": confianca,
                "probabilities": {categoria: confianca},
            },
            QUESTION_REVIEW: {"type": "noul", "noul": revisao},
        },
    }


class _Contador:
    def __init__(self, resposta=None) -> None:
        self.chamadas = 0
        self.corpos: list[dict] = []
        self._resposta = resposta or (lambda: httpx.Response(200, json=_corpo()))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.chamadas += 1
        self.corpos.append(json.loads(request.content))
        return self._resposta()


def _classificador(handler, *, ttl: float = 300.0, budget: int = 200) -> HedgeContextAssessor:
    return HedgeContextAssessor(
        JevClient(api_key="k", transport=httpx.MockTransport(handler)),
        cache=TTLCache[HedgeContextAssessment](ttl_seconds=ttl),
        cache_ttl_seconds=ttl,
        daily_budget=budget,
    )


# --- o atalho que torna o custo viavel -------------------------------------


def test_no_event_means_no_network_call(db_session) -> None:
    """A defesa mais importante: sem evento na janela nao ha o que
    classificar, e esse e o caso mais comum. Uma chamada aqui seria uma
    chamada a cada 1-2 segundos por grafico."""
    contador = _Contador()

    r = _classificador(contador).assess(_entrada(events=()), session=db_session, now=AGORA)

    assert contador.chamadas == 0
    assert r.category == CONTEXT_CALM
    assert r.available is False
    assert r.unavailable_reason == "SEM_EVENTO_NA_JANELA"


def test_local_calm_is_not_presented_as_a_model_verdict(db_session) -> None:
    """A categoria e verdadeira, mas nenhum modelo a emitiu. A tela precisa
    poder dizer a diferenca entre "o Jev classificou como calmo" e "nao
    havia o que classificar"."""
    r = HedgeContextAssessment.local_calm()

    assert r.category == CONTEXT_CALM
    assert r.available is False
    assert r.confidence is None
    assert r.model == ""


def test_an_unusable_calendar_never_calls(db_session) -> None:
    """Calendario ilegivel e ignorancia, nao informacao. Classificar sem
    ele seria pedir ao modelo um palpite sobre nada."""
    contador = _Contador()

    r = _classificador(contador).assess(
        _entrada(calendar_usable=False), session=db_session, now=AGORA
    )

    assert contador.chamadas == 0
    assert r.unavailable_reason == "CALENDARIO_INDISPONIVEL"


def test_a_symbol_without_currency_never_calls(db_session) -> None:
    contador = _Contador()

    r = _classificador(contador).assess(
        _entrada(currencies=()), session=db_session, now=AGORA
    )

    assert contador.chamadas == 0
    assert r.unavailable_reason == "SEM_MOEDA_IDENTIFICADA"


# --- cache ------------------------------------------------------------------


def test_the_same_context_is_classified_once(db_session) -> None:
    contador = _Contador()
    c = _classificador(contador)

    primeira = c.assess(_entrada(), session=db_session, now=AGORA)
    segunda = c.assess(_entrada(), session=db_session, now=AGORA + timedelta(seconds=2))

    assert contador.chamadas == 1
    assert segunda == primeira


def test_the_clock_ticking_does_not_invalidate_the_cache(db_session) -> None:
    """O tempo ate o evento entra em FAIXAS. Com o minuto exato, a chave
    mudaria a cada minuto e o cache perderia o sentido."""
    contador = _Contador()
    c = _classificador(contador)

    c.assess(_entrada(events=(_evento(minutes_until=12),)), session=db_session, now=AGORA)
    c.assess(_entrada(events=(_evento(minutes_until=11),)), session=db_session, now=AGORA)

    assert contador.chamadas == 1


def test_crossing_a_bucket_is_reclassified(db_session) -> None:
    """Um evento se aproximando muda de faixa algumas vezes antes de
    acontecer, e aí vale reavaliar."""
    contador = _Contador()
    c = _classificador(contador)

    c.assess(_entrada(events=(_evento(minutes_until=12),)), session=db_session, now=AGORA)
    c.assess(_entrada(events=(_evento(minutes_until=4),)), session=db_session, now=AGORA)

    assert contador.chamadas == 2


def test_a_new_event_is_reclassified(db_session) -> None:
    contador = _Contador()
    c = _classificador(contador)

    c.assess(_entrada(), session=db_session, now=AGORA)
    c.assess(
        _entrada(events=(_evento(), _evento(title="Fed decision", minutes_until=30))),
        session=db_session,
        now=AGORA,
    )

    assert contador.chamadas == 2


def test_two_symbols_do_not_share_a_verdict(db_session) -> None:
    contador = _Contador()
    c = _classificador(contador)

    c.assess(_entrada(symbol="MNQZ5"), session=db_session, now=AGORA)
    c.assess(_entrada(symbol="EURUSD"), session=db_session, now=AGORA)

    assert contador.chamadas == 2


def test_the_price_is_not_part_of_the_context_identity() -> None:
    """Nao ha campo de preco na entrada, e e por isso que o cache
    funciona: com ele, a chave mudaria a cada tick."""
    estado = _entrada().as_state()

    assert "price" not in estado
    assert "bid" not in estado
    assert "entry" not in estado


# --- teto diario ------------------------------------------------------------


def test_the_daily_budget_stops_the_calls(db_session) -> None:
    """O cache evita repeticao; o teto evita surpresa. Sao problemas
    diferentes: o cache nao protege contra muitos simbolos num dia."""
    contador = _Contador()
    c = _classificador(contador, budget=2)

    for i in range(5):
        c.assess(
            _entrada(events=(_evento(minutes_until=i * 30),)),
            session=db_session,
            now=AGORA,
        )

    assert contador.chamadas == 2


def test_the_budget_refusal_is_its_own_reason(db_session) -> None:
    c = _classificador(_Contador(), budget=1)
    c.assess(_entrada(events=(_evento(minutes_until=0),)), session=db_session, now=AGORA)

    r = c.assess(_entrada(events=(_evento(minutes_until=50),)), session=db_session, now=AGORA)

    assert r.available is False
    assert r.unavailable_reason == "ORCAMENTO_DIARIO_ATINGIDO"


def test_a_cache_hit_does_not_consume_budget(db_session) -> None:
    """So o que vira requisicao conta. Descontar no acerto de cache
    esgotaria o teto sem gasto real."""
    c = _classificador(_Contador(), budget=10)

    c.assess(_entrada(), session=db_session, now=AGORA)
    c.assess(_entrada(), session=db_session, now=AGORA)

    assert c.usage(db_session, now=AGORA).calls == 1


def test_a_failed_call_still_consumes_budget(db_session) -> None:
    """A chamada SAIU. Nao contar faria um endpoint quebrado ser tentado
    sem limite."""
    c = _classificador(
        _Contador(lambda: httpx.Response(500, json={})), budget=10
    )

    c.assess(_entrada(), session=db_session, now=AGORA)

    assert c.usage(db_session, now=AGORA).calls == 1


# --- a pergunta -------------------------------------------------------------


def test_the_four_categories_are_offered(db_session) -> None:
    contador = _Contador()
    _classificador(contador).assess(_entrada(), session=db_session, now=AGORA)

    perguntas = contador.corpos[0]["questions"]
    assert set(perguntas[QUESTION_CONTEXT]["criteria"]) == {
        CONTEXT_CALM,
        CONTEXT_EVENT_RISK,
        CONTEXT_ELEVATED,
        CONTEXT_INSUFFICIENT,
    }
    assert perguntas[QUESTION_REVIEW]["type"] == "noul"


def test_the_instructions_forbid_the_four_misuses(db_session) -> None:
    """Preco, direcao, probabilidade de mercado e tamanho de posicao. As
    quatro coisas que o Jev nao pode fazer aqui."""
    contador = _Contador()
    _classificador(contador).assess(_entrada(), session=db_session, now=AGORA)

    instrucoes = contador.corpos[0]["questions"][QUESTION_CONTEXT]["instructions"]
    for proibido in ("preco", "direcao", "probabilidade de mercado", "tamanho de"):
        assert proibido in instrucoes, proibido


def test_only_text_and_schedule_reach_the_model(db_session) -> None:
    """A lista e fechada: nenhum preco, nivel, lote ou dado de conta."""
    contador = _Contador()
    _classificador(contador).assess(_entrada(), session=db_session, now=AGORA)

    estado = contador.corpos[0]["state"]
    assert set(estado) == {
        "symbol",
        "currencies",
        "horizon_minutes",
        "calendar_available",
        "scheduled_events",
    }
    serial = json.dumps(estado).lower()
    for proibido in ("password", "token", "api_key", "lot", "stop", "take", "tick_value"):
        assert proibido not in serial, proibido


def test_the_criteria_describe_when_to_pick_each_one() -> None:
    assert len(CONTEXT_CRITERIA) == 4
    assert all(len(t) > 40 for t in CONTEXT_CRITERIA.values())


# --- o veredito -------------------------------------------------------------


def test_a_good_verdict_keeps_category_confidence_and_model(db_session) -> None:
    r = _classificador(_Contador()).assess(_entrada(), session=db_session, now=AGORA)

    assert r.available is True
    assert r.category == CONTEXT_EVENT_RISK
    assert r.confidence == 0.81
    assert r.model == "jev-1.13.0"
    assert r.observation_only is True


def test_needs_review_comes_from_the_noul(db_session) -> None:
    alto = _Contador(lambda: httpx.Response(200, json=_corpo(revisao=0.9)))
    baixo = _Contador(lambda: httpx.Response(200, json=_corpo(revisao=0.1)))

    assert _classificador(alto).assess(_entrada(), session=db_session, now=AGORA).needs_review
    assert not _classificador(baixo).assess(_entrada(), session=db_session, now=AGORA).needs_review


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
def test_a_failure_never_raises_and_invents_no_confidence(db_session, handler) -> None:
    r = _classificador(handler).assess(_entrada(), session=db_session, now=AGORA)

    assert r.available is False
    assert r.confidence is None
    assert r.category == CONTEXT_INSUFFICIENT


def test_a_failure_is_not_cached(db_session) -> None:
    contador = _Contador(lambda: httpx.Response(500, json={}))
    c = _classificador(contador, budget=10)

    c.assess(_entrada(), session=db_session, now=AGORA)
    c.assess(_entrada(), session=db_session, now=AGORA)

    assert contador.chamadas == 2


def test_observation_only_is_always_true_in_this_version(db_session) -> None:
    """Se um dia deixar de ser, este teste falha e quem o mudar tera de
    olhar os criterios de avaliacao fora da amostra."""
    for r in (
        _classificador(_Contador()).assess(_entrada(), session=db_session, now=AGORA),
        HedgeContextAssessment.local_calm(),
        HedgeContextAssessment.unavailable("x"),
    ):
        assert r.observation_only is True


def test_the_bucket_is_five_minutes() -> None:
    assert MINUTE_BUCKET == 5


def test_the_jev_budget_does_not_share_the_marketpulse_counter(db_session) -> None:
    """Chave propria. Compartilhada, o gasto de uma API esgotaria o
    orcamento da outra, e o sintoma seria uma integracao se calando por
    culpa de outra."""
    from app.jev.hedge_context import BUDGET_SETTING as JEV_KEY
    from app.news.budget import BUDGET_SETTING as MARKETPULSE_KEY
    from app.news.budget import read_usage, record_call

    assert JEV_KEY != MARKETPULSE_KEY

    # Gasta 3 na MarketPulse.
    for _ in range(3):
        record_call(db_session, limit=100, now=AGORA, key=MARKETPULSE_KEY)
    db_session.commit()

    c = _classificador(_Contador(), budget=100)
    c.assess(_entrada(), session=db_session, now=AGORA)

    assert c.usage(db_session, now=AGORA).calls == 1
    assert read_usage(db_session, limit=100, now=AGORA, key=MARKETPULSE_KEY).calls == 3


def test_the_budget_resets_on_the_utc_day_change(db_session) -> None:
    """A virada zera sozinha, sem tarefa agendada para limpar nada."""
    c = _classificador(_Contador(), budget=1)
    c.assess(_entrada(), session=db_session, now=AGORA)
    db_session.commit()

    assert c.usage(db_session, now=AGORA).exhausted is True
    assert c.usage(db_session, now=AGORA + timedelta(days=1)).calls == 0
