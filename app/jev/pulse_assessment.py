"""A pergunta que fazemos ao Jev, e o que fazemos com a resposta.

## O escopo, dito uma vez

O Jev escolhe entre QUATRO rotulos e responde UMA pergunta de sim/nao.
Nada alem. Ele nao produz preco, zona, entrada, stop, alvo, direcao nem
ordem — esses valores ja existem quando este modulo e chamado, vieram do
motor tecnico local, e nao sao nem enviados de volta modificados.

Isso e uma escolha de desenho, nao uma limitacao temporaria. O sistema tem
um motor deterministico e testavel; trocar qualquer parte dele por um
julgamento externo exigiria validacao que este projeto nao tem — nao ha
backtest com custos aqui que sustente essa troca. O que o Jev acrescenta e
uma LEITURA do conjunto, para a tela, onde hoje o operador tem que cruzar
sete numeros na cabeca.

## Os quatro rotulos nao sao uma escala

`STRONG_SETUP` e `CAUTION` falam da qualidade do cenario. `WAIT` fala do
momento. `INSUFFICIENT_DATA` fala da ausencia de base para dizer qualquer
coisa. Sao respostas a perguntas diferentes, de proposito: sem
`INSUFFICIENT_DATA` como opcao legitima, um cenario sem dados seria
espremido em `CAUTION`, que parece um julgamento e nao e.

## Sobre `confidence`

E o quao concentrada ficou a distribuicao entre os quatro rotulos —
"quanto o modelo hesitou entre as opcoes". NAO e probabilidade de o trade
dar lucro, e em nenhum lugar deste sistema pode ser apresentada assim (ver
`CLAUDE.md`; um teste trava o vocabulario). Confianca alta num `CAUTION`
significa "tenho certeza de que e para ter cautela".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger
from app.jev.cache import CacheKey, TTLCache
from app.jev.client import JevClient, JevFailure, JevUnavailable, choice_question, noul_question
from app.jev.snapshot import MAX_REASON_CODES, TechnicalSnapshot, reason_codes

logger = get_logger(__name__)

STATE_STRONG = "STRONG_SETUP"
STATE_CAUTION = "CAUTION"
STATE_WAIT = "WAIT"
STATE_INSUFFICIENT = "INSUFFICIENT_DATA"

PULSE_STATE_CRITERIA = {
    STATE_STRONG: (
        "Os sinais tecnicos ja calculados apontam na mesma direcao, os dados "
        "estao atuais e nao ha bloqueios. O cenario esta coerente."
    ),
    STATE_CAUTION: (
        "Ha um cenario legivel, mas com sinais conflitantes, spread alargado, "
        "volume fraco ou retorno/risco desfavoravel."
    ),
    STATE_WAIT: (
        "O cenario pode se tornar valido, mas ainda nao esta: falta "
        "confirmacao, o preco esta longe da zona, ou ha bloqueio ativo."
    ),
    STATE_INSUFFICIENT: (
        "Nao ha base para classificar: dados atrasados ou ausentes, sem "
        "direcao definida, ou cobertura insuficiente."
    ),
}

_PULSE_INSTRUCTIONS = (
    "Voce esta classificando um resumo tecnico JA CALCULADO por um motor "
    "deterministico, para exibicao em um grafico. "
    "Nunca infira, estime ou sugira preco futuro, direcao, ponto de entrada, "
    "stop ou alvo — esses valores ja existem e nao sao seu trabalho. "
    "Classifique apenas a coerencia do conjunto apresentado. "
    "Na duvida entre dois rotulos, escolha o mais conservador: WAIT quando o "
    "cenario e legivel mas incerto, INSUFFICIENT_DATA quando faltam dados "
    "para julgar. Nunca escolha STRONG_SETUP em caso de ambiguidade."
)

_REVIEW_INSTRUCTIONS = (
    "O resumo tecnico apresenta alguma inconsistencia interna que mereca "
    "conferencia humana antes de agir — por exemplo, um score alto junto de "
    "bloqueios ativos, ou timeframes em conflito com a direcao escolhida."
)

NEEDS_REVIEW_THRESHOLD = 0.6
"""Acima disto o aviso de revisao aparece no grafico.

Nao e um numero otimizado — nao ha aqui dado que o otimizasse. E o
reconhecimento de que 0.5 e o ponto onde o modelo esta EM DUVIDA, e um
aviso disparado na duvida aparece sempre e deixa de ser lido."""

QUESTION_STATE = "pulse_state"
QUESTION_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class PulseAssessment:
    """O veredito pronto para a resposta da API."""

    available: bool
    state: str
    confidence: float = 0.0
    needs_review: bool = False
    needs_review_probability: float = 0.0
    model: str = ""
    evaluated_at: datetime | None = None
    expires_at: datetime | None = None
    reason_codes: list[str] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)
    unavailable_reason: str = ""

    @classmethod
    def unavailable(
        cls, reason: JevFailure, *, snapshot: TechnicalSnapshot | None = None
    ) -> PulseAssessment:
        """Sem veredito — e sem inventar um.

        `confidence` fica em 0 e `state` em `INSUFFICIENT_DATA`, que e o
        rotulo honesto para "ninguem classificou". Escolher `CAUTION` aqui
        seria emitir um julgamento que nenhum modelo emitiu, e `WAIT`
        sugeriria que ha um cenario se formando.

        Os `reason_codes` CONTINUAM saindo: eles vem da analise local, que
        esta disponivel de qualquer jeito. A indisponibilidade do Jev nao
        e motivo para esconder o que o motor tecnico ja sabe.
        """
        return cls(
            available=False,
            state=STATE_INSUFFICIENT,
            reason_codes=reason_codes(snapshot)[:MAX_REASON_CODES] if snapshot else [],
            unavailable_reason=reason.value,
        )


class PulseAssessor:
    """Classifica um snapshot tecnico, com cache."""

    def __init__(
        self,
        client: JevClient,
        *,
        cache: TTLCache[PulseAssessment] | None = None,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._client = client
        self._cache = cache if cache is not None else TTLCache(ttl_seconds=cache_ttl_seconds)
        self._ttl = cache_ttl_seconds

    def assess(
        self, snapshot: TechnicalSnapshot, *, now: datetime | None = None
    ) -> PulseAssessment:
        """O veredito. Nunca levanta excecao."""
        agora = now or datetime.now(UTC)
        chave = CacheKey(
            symbol=snapshot.symbol,
            timeframe=snapshot.timeframe,
            candle_open_time=snapshot.candle_open_time,
            fingerprint=snapshot.fingerprint(),
        )

        guardado = self._cache.get(chave, now=agora)
        if guardado is not None:
            return guardado

        codigos = reason_codes(snapshot)[:MAX_REASON_CODES]
        try:
            resultado = self._client.system_one(
                state=snapshot.as_state(),
                questions={
                    QUESTION_STATE: choice_question(_PULSE_INSTRUCTIONS, PULSE_STATE_CRITERIA),
                    QUESTION_REVIEW: noul_question(_REVIEW_INSTRUCTIONS),
                },
            )
        except JevUnavailable as exc:
            # Nao vai para o cache: uma falha de 2s nao pode apagar a
            # classificacao pelos 5 minutos seguintes.
            logger.info(
                "jev_indisponivel",
                extra={"jev_reason": exc.reason.value, "symbol": snapshot.symbol},
            )
            return PulseAssessment.unavailable(exc.reason, snapshot=snapshot)

        escolha = resultado.choices.get(QUESTION_STATE)
        if escolha is None:
            return PulseAssessment.unavailable(JevFailure.BAD_RESPONSE, snapshot=snapshot)

        revisao = resultado.nouls.get(QUESTION_REVIEW)
        probabilidade = revisao.probability if revisao else 0.0

        avaliacao = PulseAssessment(
            available=True,
            state=escolha.choice,
            confidence=escolha.confidence,
            needs_review=probabilidade >= NEEDS_REVIEW_THRESHOLD,
            needs_review_probability=probabilidade,
            model=resultado.model,
            evaluated_at=agora,
            expires_at=agora + timedelta(seconds=self._ttl) if self._ttl > 0 else agora,
            reason_codes=codigos,
            probabilities=dict(escolha.probabilities),
        )
        self._cache.set(chave, avaliacao, now=agora)
        return avaliacao


def as_payload(assessment: PulseAssessment) -> dict:
    """O bloco `signal_ai` da resposta da API.

    Segue as regras do contrato do Pulso (ver `pulso_api`): objeto raso,
    nunca `null`, numeros sem aspas. O indicador le com busca de string, e
    um `null` aqui o obrigaria a distinguir ausencia de zero dentro de um
    texto.
    """
    return {
        "available": assessment.available,
        "state": assessment.state,
        "confidence": round(assessment.confidence, 4),
        "needs_review": assessment.needs_review,
        "needs_review_probability": round(assessment.needs_review_probability, 4),
        "model": assessment.model,
        "evaluated_at": assessment.evaluated_at.isoformat() if assessment.evaluated_at else "",
        "expires_at": assessment.expires_at.isoformat() if assessment.expires_at else "",
        "reason_codes": list(assessment.reason_codes),
        "unavailable_reason": assessment.unavailable_reason,
        "note": (
            "Classificacao visual do cenario ja calculado. Nao e previsao, "
            "nao e probabilidade de lucro e nao envia ordens."
        ),
    }
