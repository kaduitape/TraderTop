"""O cliente do Jev, exercitado inteiro sem rede.

`httpx.MockTransport` deixa o caminho real rodar — montagem do corpo,
headers, status, parsing — e so troca o socket. Testar com um duble do
`JevClient` verificaria o duble.

O valor destes testes nao esta em "detecta falha": esta em nao MISTURAR
falhas. Chave errada, cota estourada e API fora do ar produzem a mesma
ausencia de veredito na tela e exigem tres acoes diferentes de quem
mantem o sistema.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.jev.client import (
    JevClient,
    JevFailure,
    JevUnavailable,
    choice_question,
    noul_question,
)

CRITERIOS = {"A": "quando A", "B": "quando B"}
PERGUNTAS = {
    "estado": choice_question("classifique", CRITERIOS),
    "revisar": noul_question("precisa de revisao?"),
}


def _cliente(handler, **kwargs) -> JevClient:
    return JevClient(
        api_key="chave-de-teste",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _resposta_ok(**overrides) -> dict:
    corpo = {
        "model": "jev-1.13.0",
        "answers": {
            "estado": {
                "type": "choice",
                "choice": "A",
                "confidence": 0.88,
                "probabilities": {"A": 0.88, "B": 0.12},
            },
            "revisar": {"type": "noul", "noul": 0.21},
        },
        "usage": {"input": 120, "output": 8},
    }
    corpo.update(overrides)
    return corpo


# --- o pedido ---------------------------------------------------------------


def test_the_request_matches_the_documented_contract() -> None:
    """URL, header e as tres chaves do corpo. Errar qualquer uma vira 4xx
    em producao e "nao funciona" para quem instalou."""
    capturado: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado["url"] = str(request.url)
        capturado["auth"] = request.headers.get("authorization")
        capturado["corpo"] = json.loads(request.content)
        return httpx.Response(200, json=_resposta_ok())

    _cliente(handler, model="jev-latest").system_one(
        state={"symbol": "MNQ"}, questions=PERGUNTAS
    )

    assert capturado["url"] == "https://api.typesafe.ai/v1/systemone"
    assert capturado["auth"] == "Bearer chave-de-teste"

    corpo = capturado["corpo"]
    assert corpo["model"] == "jev-latest"
    assert corpo["state"] == {"symbol": "MNQ"}
    assert corpo["questions"]["estado"]["type"] == "choice"
    assert corpo["questions"]["estado"]["criteria"] == CRITERIOS
    assert corpo["questions"]["revisar"]["type"] == "noul"


def test_an_empty_question_set_never_reaches_the_network() -> None:
    """Uma chamada sem pergunta seria paga e inutil."""

    def handler(request):  # pragma: no cover - so dispara em regressao
        raise AssertionError("nao deveria chamar a API")

    with pytest.raises(JevUnavailable) as erro:
        _cliente(handler).system_one(state={}, questions={})

    assert erro.value.reason is JevFailure.BAD_RESPONSE


def test_a_missing_key_fails_at_construction() -> None:
    """Falha ao montar, nao no meio de uma requisicao do operador."""
    with pytest.raises(JevUnavailable) as erro:
        JevClient(api_key="")

    assert erro.value.reason is JevFailure.NOT_CONFIGURED


# --- a resposta boa ---------------------------------------------------------


def test_a_choice_keeps_confidence_and_probabilities() -> None:
    """A distribuicao inteira e preservada. Guardar so o rotulo jogaria
    fora a calibragem, que e a razao de usar este modelo."""
    resultado = _cliente(lambda r: httpx.Response(200, json=_resposta_ok())).system_one(
        state={}, questions=PERGUNTAS
    )

    escolha = resultado.choices["estado"]
    assert escolha.choice == "A"
    assert escolha.confidence == 0.88
    assert escolha.probabilities == {"A": 0.88, "B": 0.12}
    assert resultado.model == "jev-1.13.0"


def test_a_noul_is_a_probability_and_has_no_confidence() -> None:
    """Assimetria real do protocolo: Noul devolve `noul`, que JA E a
    probabilidade do "sim", e nao acompanha `confidence`. Tratar como
    booleano perderia a calibragem; inventar uma confianca seria dado
    fabricado."""
    resultado = _cliente(lambda r: httpx.Response(200, json=_resposta_ok())).system_one(
        state={}, questions=PERGUNTAS
    )

    noul = resultado.nouls["revisar"]
    assert noul.probability == 0.21
    assert noul.at_least(0.6) is False
    assert noul.at_least(0.2) is True
    assert not hasattr(noul, "confidence")


# --- as falhas, uma a uma ---------------------------------------------------


@pytest.mark.parametrize(
    ("status", "esperado"),
    [
        (401, JevFailure.UNAUTHORIZED),
        (403, JevFailure.UNAUTHORIZED),
        (429, JevFailure.RATE_LIMITED),
        (500, JevFailure.SERVER_ERROR),
        (503, JevFailure.SERVER_ERROR),
        (400, JevFailure.BAD_RESPONSE),
    ],
)
def test_each_status_keeps_its_own_reason(status: int, esperado: JevFailure) -> None:
    """Sem isto, "sem classificacao" cobriria chave errada, cota estourada
    e API caida — tres problemas com tres correcoes diferentes."""
    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: httpx.Response(status, json={"erro": "x"})).system_one(
            state={}, questions=PERGUNTAS
        )

    assert erro.value.reason is esperado


def test_a_timeout_is_its_own_reason() -> None:
    def expira(request):
        raise httpx.ReadTimeout("sem resposta", request=request)

    with pytest.raises(JevUnavailable) as erro:
        _cliente(expira, timeout_seconds=2.5).system_one(state={}, questions=PERGUNTAS)

    assert erro.value.reason is JevFailure.TIMEOUT


def test_a_connection_failure_is_not_a_timeout() -> None:
    def sem_rota(request):
        raise httpx.ConnectError("sem rota", request=request)

    with pytest.raises(JevUnavailable) as erro:
        _cliente(sem_rota).system_one(state={}, questions=PERGUNTAS)

    assert erro.value.reason is JevFailure.NETWORK


def test_html_with_status_200_is_a_bad_response() -> None:
    """Caso real, nao hipotetico: o WAF na frente desta API devolve uma
    pagina HTML quando o texto do state parece um comando de shell. Um
    `.json()` cru estouraria JSONDecodeError no meio da rota."""
    html = httpx.Response(200, text="<html>blocked</html>", headers={"content-type": "text/html"})

    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: html).system_one(state={}, questions=PERGUNTAS)

    assert erro.value.reason is JevFailure.BAD_RESPONSE


@pytest.mark.parametrize(
    "corpo",
    [
        {"model": "x"},                                  # sem 'answers'
        {"answers": []},                                 # 'answers' nao e objeto
        {"answers": {"estado": {"type": "choice"}}},     # sem 'choice'
        {"answers": {"estado": {"choice": "A", "confidence": 0.5}}},  # sem 'revisar'
    ],
    ids=["sem answers", "answers e lista", "choice incompleto", "pergunta faltando"],
)
def test_a_malformed_payload_never_becomes_a_partial_verdict(corpo: dict) -> None:
    """Meio veredito e pior que nenhum: ele aparece no grafico com a mesma
    aparencia de um completo."""
    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: httpx.Response(200, json=corpo)).system_one(
            state={}, questions=PERGUNTAS
        )

    assert erro.value.reason is JevFailure.BAD_RESPONSE


def test_a_label_outside_the_criteria_is_refused() -> None:
    """O modelo nao devolve rotulo invalido por construcao — mas confiar em
    invariante de terceiro e como se descobre tarde que a API mudou."""
    corpo = _resposta_ok()
    corpo["answers"]["estado"]["choice"] = "INVENTADO"

    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: httpx.Response(200, json=corpo)).system_one(
            state={}, questions=PERGUNTAS
        )

    assert erro.value.reason is JevFailure.BAD_RESPONSE


@pytest.mark.parametrize("valor", [1.4, -0.1, "0.8", True, None])
def test_a_probability_outside_zero_to_one_is_refused(valor) -> None:
    """1.4 de confianca viraria "140%" na tela. Normalizar em silencio
    esconderia uma mudanca de contrato; recusar a torna visivel."""
    corpo = _resposta_ok()
    corpo["answers"]["estado"]["confidence"] = valor

    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: httpx.Response(200, json=corpo)).system_one(
            state={}, questions=PERGUNTAS
        )

    assert erro.value.reason is JevFailure.BAD_RESPONSE


# --- segredos ---------------------------------------------------------------


def test_the_api_key_never_reaches_the_logs(caplog) -> None:
    """A chave viaja no header e em lugar nenhum mais."""
    caplog.set_level("DEBUG")

    with pytest.raises(JevUnavailable):
        _cliente(lambda r: httpx.Response(401, json={"error": "bad key"})).system_one(
            state={"symbol": "MNQ"}, questions=PERGUNTAS
        )

    assert "chave-de-teste" not in caplog.text


def test_the_error_message_does_not_carry_the_response_body() -> None:
    """O corpo de um erro pode devolver trechos do que enviamos. A mensagem
    leva o codigo e a acao; o corpo fica de fora."""
    segredo = "eyJhbGciOiJIUzI1NiJ9.payload-que-nao-pode-vazar"

    with pytest.raises(JevUnavailable) as erro:
        _cliente(lambda r: httpx.Response(401, text=segredo)).system_one(
            state={}, questions=PERGUNTAS
        )

    assert segredo not in str(erro.value)
    assert "JEV_API_KEY" in erro.value.detail
