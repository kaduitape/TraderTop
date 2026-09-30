"""Cliente HTTP do Jev (TypeSafe System One).

## O contrato, em uma tela

    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <chave>

    {"model": "jev-latest",
     "state": {...},
     "questions": {"nome": {"type": "choice",
                            "instructions": "...",
                            "criteria": {"ROTULO": "quando escolher"}}}}

    -> {"model": "jev-1.13.0",
        "answers": {"nome": {"type": "choice", "choice": "ROTULO",
                             "confidence": 0.88,
                             "probabilities": {"ROTULO": 0.88, ...}}},
        "usage": {...}}

Duas assimetrias do protocolo que o codigo abaixo respeita e que sao facil
de errar:

- **Noul nao tem `confidence`.** A resposta e `{"type": "noul", "noul":
  0.87}`, e esse numero JA E a probabilidade do "sim". Inventar um
  `confidence` aqui, ou tratar `noul` como booleano, jogaria fora a
  calibragem que e a razao de usar este modelo.
- **Choice nunca devolve rotulo fora do conjunto.** O modelo escolhe entre
  as chaves de `criteria`; nao ha texto livre. Mesmo assim validamos o
  rotulo recebido, porque confiar em invariante de terceiro e como o
  sistema aprende da pior forma que a API mudou.

## Falhar nao e excecao de verdade aqui

Toda falha vira `JevUnavailable`, com um `reason` que diz o que aconteceu.
Este passo e acessorio: o Pulso ja tem resposta completa sem ele. Deixar
uma `httpx.TimeoutException` subir ate a rota transformaria uma melhoria
visual em erro 500 numa tela que nao precisava do Jev para nada.

## O que nunca entra no log

Nem a chave, nem os headers, nem o corpo bruto da resposta. O `state`
tambem nao: ele e pequeno e sem segredo por construcao (ver
`app/jev/snapshot.py`), mas registrar payload de integracao e o habito que
transforma o proximo campo adicionado num vazamento. O log carrega o
motivo e o codigo HTTP, que e o que serve para consertar.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


class JevFailure(enum.StrEnum):
    """Por que nao houve veredito. Cada valor exige uma acao diferente."""

    DISABLED = "DISABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    TIMEOUT = "TIMEOUT"
    UNAUTHORIZED = "UNAUTHORIZED"
    """401/403 — chave errada, revogada ou sem permissao."""

    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    BAD_RESPONSE = "BAD_RESPONSE"
    """Respondeu 2xx com algo que nao da para usar. Inclui o caso real de
    um WAF devolver HTML com status 200."""

    NETWORK = "NETWORK"


_HTTP_HINTS = {
    400: "a API recusou o formato do pedido.",
    401: "chave do Jev nao aceita — confira JEV_API_KEY.",
    403: "chave valida, mas sem permissao para este endpoint/modelo.",
    404: "endpoint inexistente — confira JEV_API_BASE_URL.",
    422: "o pedido foi entendido mas as perguntas foram recusadas.",
    429: "limite de requisicoes do Jev atingido.",
}


class JevUnavailable(Exception):
    """Sem veredito desta vez. Nunca e motivo para bloquear ou executar."""

    def __init__(self, reason: JevFailure, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    probability: float
    """Probabilidade do "sim", entre 0 e 1. Nao ha `confidence` em Noul."""

    def at_least(self, threshold: float) -> bool:
        return self.probability >= threshold


@dataclass(frozen=True, slots=True)
class SystemOneResult:
    model: str
    choices: dict[str, ChoiceAnswer] = field(default_factory=dict)
    nouls: dict[str, NoulAnswer] = field(default_factory=dict)


def choice_question(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def noul_question(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


class JevClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = 2.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise JevUnavailable(JevFailure.NOT_CONFIGURED, "JEV_API_KEY vazia")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        # Ponto de injecao do teste: um MockTransport exercita o cliente
        # inteiro (montagem, headers, parsing, erros) sem rede.
        self._transport = transport

    def system_one(
        self, *, state: dict[str, Any], questions: dict[str, dict[str, Any]]
    ) -> SystemOneResult:
        if not questions:
            raise JevUnavailable(JevFailure.BAD_RESPONSE, "nenhuma pergunta enviada")

        corpo = {"model": self._model, "state": state, "questions": questions}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resposta = client.post(
                    self._base_url,
                    json=corpo,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
        except httpx.TimeoutException as exc:
            raise JevUnavailable(JevFailure.TIMEOUT, f"{self._timeout:.1f}s") from exc
        except httpx.HTTPError as exc:
            raise JevUnavailable(JevFailure.NETWORK, type(exc).__name__) from exc

        self._raise_for_status(resposta)
        return _parse(resposta, expected=questions)

    def _raise_for_status(self, resposta: httpx.Response) -> None:
        codigo = resposta.status_code
        if codigo < 400:
            return

        dica = _HTTP_HINTS.get(codigo, "")
        if codigo in (401, 403):
            motivo = JevFailure.UNAUTHORIZED
        elif codigo == 429:
            motivo = JevFailure.RATE_LIMITED
        elif codigo >= 500:
            motivo = JevFailure.SERVER_ERROR
        else:
            motivo = JevFailure.BAD_RESPONSE

        # So o codigo e a dica. O corpo pode carregar de volta trechos do
        # que enviamos, e um erro nao e lugar para reabrir esse caminho.
        logger.warning("jev_http_error", extra={"status_code": codigo, "jev_reason": motivo.value})
        raise JevUnavailable(motivo, dica or f"HTTP {codigo}")


def _parse(resposta: httpx.Response, *, expected: dict[str, dict[str, Any]]) -> SystemOneResult:
    """A resposta, ou `BAD_RESPONSE`.

    O cuidado com o corpo nao-JSON nao e teorico: o WAF na frente desta API
    devolve HTML quando o texto do `state` parece um comando de shell. Um
    `.json()` cru estouraria `JSONDecodeError` no meio da rota.
    """
    try:
        payload = resposta.json()
    except ValueError as exc:
        tipo = resposta.headers.get("content-type", "?")
        raise JevUnavailable(JevFailure.BAD_RESPONSE, f"corpo nao e JSON ({tipo})") from exc

    if not isinstance(payload, dict):
        raise JevUnavailable(JevFailure.BAD_RESPONSE, "corpo nao e um objeto")

    respostas = payload.get("answers")
    if not isinstance(respostas, dict):
        raise JevUnavailable(JevFailure.BAD_RESPONSE, "sem 'answers'")

    escolhas: dict[str, ChoiceAnswer] = {}
    nouls: dict[str, NoulAnswer] = {}

    for nome, pergunta in expected.items():
        bruto = respostas.get(nome)
        if not isinstance(bruto, dict):
            raise JevUnavailable(JevFailure.BAD_RESPONSE, f"resposta ausente para '{nome}'")

        if pergunta.get("type") == "choice":
            escolhas[nome] = _parse_choice(nome, bruto, criteria=pergunta.get("criteria") or {})
        elif pergunta.get("type") == "noul":
            nouls[nome] = _parse_noul(nome, bruto)

    modelo = payload.get("model")
    return SystemOneResult(
        model=str(modelo) if isinstance(modelo, str) else "",
        choices=escolhas,
        nouls=nouls,
    )


def _parse_choice(nome: str, bruto: dict, *, criteria: dict) -> ChoiceAnswer:
    rotulo = bruto.get("choice")
    if not isinstance(rotulo, str) or (criteria and rotulo not in criteria):
        raise JevUnavailable(JevFailure.BAD_RESPONSE, f"'{nome}' devolveu rotulo desconhecido")

    probabilidades = {
        str(chave): float(valor)
        for chave, valor in (bruto.get("probabilities") or {}).items()
        if isinstance(valor, (int, float))
    }
    return ChoiceAnswer(
        choice=rotulo,
        confidence=_unit(bruto.get("confidence"), nome),
        probabilities=probabilidades,
    )


def _parse_noul(nome: str, bruto: dict) -> NoulAnswer:
    return NoulAnswer(probability=_unit(bruto.get("noul"), nome))


def _unit(valor: Any, nome: str) -> float:
    """Um numero entre 0 e 1, ou falha.

    Sem faixa valida nao existe "valor parcialmente util": 1.4 de confianca
    viraria 140% na tela. Recusar e melhor que normalizar em silencio, que
    esconderia uma mudanca de contrato.
    """
    if not isinstance(valor, (int, float)) or isinstance(valor, bool):
        raise JevUnavailable(JevFailure.BAD_RESPONSE, f"'{nome}' sem valor numerico")
    numero = float(valor)
    if not 0.0 <= numero <= 1.0:
        raise JevUnavailable(JevFailure.BAD_RESPONSE, f"'{nome}' fora de 0..1")
    return numero
