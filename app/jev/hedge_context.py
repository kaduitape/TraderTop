"""Classificacao de CONTEXTO para o Hedge AI — em modo observacao.

## O que o Jev faz aqui, e o que ele nao faz

Ele le o que ha de TEXTO em volta da operacao — titulos de eventos do
calendario economico — e devolve uma categoria entre quatro. E isso.

Nao calcula dinheiro, nao fornece cotacao, nao substitui o motor
estatistico, nao gera probabilidade de mercado e nao autoriza ignorar
limite nenhum. Essas contas tem dono, e o dono e local.

## Modo observacao, e por que isso e estrutural

A classificacao e gravada e exibida, mas NAO entra nas probabilidades.
Isso nao e uma configuracao que alguem pode virar: o veredito do Jev
simplesmente nao e passado ao `ForecastEngine` — ele chega ao servico
DEPOIS da previsao, e nada no caminho da previsao o le. Um teste compara
a previsao com e sem Jev e exige que sejam identicas.

Permitir influencia exigiria avaliacao fora da amostra que nao foi feita.
Ate lá, deixar um classificador de texto mexer num numero seria trocar um
modelo explicavel por um palpite — e o palpite chegaria a tela com a
mesma aparencia.

## O problema de custo, que aqui e o problema principal

O conector manda snapshot a cada 1-2 segundos, por grafico. Uma chamada
por snapshot seriam ~43 mil por dia por grafico. Tres defesas, de fora
para dentro, cada uma porque a de dentro custa mais:

1. **Nao ha o que classificar.** Sem evento na janela, a resposta sai
   `CALM` sem tocar a rede. Custo zero, e e o caso mais comum.
2. **Cache pela IMPRESSAO DIGITAL DO CONTEXTO**, nao pelo snapshot. A
   chave e o conjunto de eventos com o tempo em faixas de 5 minutos:
   enquanto os eventos e a faixa nao mudam, a resposta e reaproveitada.
   O preco nao entra — se entrasse, a chave mudaria a cada tick e o cache
   nao valeria nada.
3. **Teto diario** em `system_settings`, como o da MarketPulse. O cache
   evita repeticao; o teto protege contra um dia com muitos simbolos e
   muitos reinicios. Este projeto ja queimou a assinatura de uma API
   externa com uma consulta por ciclo que nao virou nenhuma entrada.

## Falhar nunca derruba nada

Toda falha vira `available=False` com um motivo. O motor financeiro e a
previsao nao sabem que o Jev existe, e o indicador desenha igual.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.jev.cache import CacheKey, TTLCache
from app.jev.client import JevClient, JevFailure, JevUnavailable, choice_question, noul_question
from app.news.budget import BudgetUsage, read_usage, record_call

logger = get_logger(__name__)

BUDGET_SETTING = "jev_daily_usage"
"""Chave propria, separada da MarketPulse: sao servicos distintos, e um
teto compartilhado faria o gasto de um apagar o do outro."""

CONTEXT_CALM = "CALM"
CONTEXT_EVENT_RISK = "EVENT_RISK_NEAR"
CONTEXT_ELEVATED = "ELEVATED_UNCERTAINTY"
CONTEXT_INSUFFICIENT = "INSUFFICIENT_CONTEXT"

CONTEXT_CRITERIA = {
    CONTEXT_CALM: (
        "Nenhum evento agendado relevante para as moedas deste instrumento "
        "dentro da janela analisada."
    ),
    CONTEXT_EVENT_RISK: (
        "Ha evento de alto impacto proximo para alguma moeda do instrumento. "
        "Spread tende a abrir e o preco a chicotear em volta da divulgacao."
    ),
    CONTEXT_ELEVATED: (
        "Varios eventos de impacto medio, ou eventos agrupados, elevando a "
        "incerteza sem um gatilho unico dominante."
    ),
    CONTEXT_INSUFFICIENT: (
        "Nao ha base para classificar: calendario indisponivel, sem moeda "
        "identificada para o instrumento, ou titulos ilegiveis."
    ),
}

_INSTRUCTIONS = (
    "Voce esta classificando o CONTEXTO DE NOTICIAS em volta de uma operacao, "
    "para exibicao em um painel. "
    "Use apenas os titulos e horarios de evento fornecidos. "
    "Nunca estime preco, direcao, probabilidade de mercado, tamanho de "
    "posicao ou resultado financeiro — nada disso e seu trabalho e tudo isso "
    "ja foi calculado por outro componente. "
    "Na duvida entre duas categorias, escolha a mais conservadora: "
    "ELEVATED_UNCERTAINTY quando ha eventos mas o peso nao esta claro, e "
    "INSUFFICIENT_CONTEXT quando os dados nao permitem julgar."
)

_REVIEW_INSTRUCTIONS = (
    "O contexto de noticias apresentado merece conferencia humana antes de "
    "agir — por exemplo, evento de alto impacto a minutos de distancia, ou "
    "titulos que sugerem decisao de politica monetaria."
)

QUESTION_CONTEXT = "market_context"
QUESTION_REVIEW = "needs_review"

NEEDS_REVIEW_THRESHOLD = 0.6
MINUTE_BUCKET = 5
"""Faixa de arredondamento do tempo ate o evento, em minutos.

Sem ela a impressao digital mudaria a cada minuto e o cache perderia o
sentido. Cinco minutos e fino o bastante para um evento se aproximando
mudar de faixa algumas vezes antes de acontecer."""


@dataclass(frozen=True, slots=True)
class ContextEvent:
    """Um evento, reduzido ao que o Jev precisa ler."""

    title: str
    impact: str
    minutes_until: int
    """Negativo significa que ja aconteceu — o perigo nao termina na
    divulgacao, e o modelo precisa poder distinguir antes de depois."""

    currency: str = ""

    def scheduled_bucket(self) -> int:
        """O tempo ate o evento, arredondado em faixas.

        Entra na impressao digital do cache no lugar do minuto exato: sem
        isso a chave mudaria a cada minuto e o cache perderia o sentido
        num endpoint chamado a cada dois segundos.
        """
        return int(self.minutes_until // MINUTE_BUCKET) * MINUTE_BUCKET


@dataclass(frozen=True, slots=True)
class HedgeContextInput:
    symbol: str
    currencies: tuple[str, ...]
    events: tuple[ContextEvent, ...] = ()
    horizon_minutes: int = 60
    calendar_usable: bool = True
    """False quando o calendario nao pode ser lido. Diferente de "nenhum
    evento": a primeira e ignorancia, a segunda e informacao."""

    def fingerprint(self) -> str:
        """Identidade do CONTEXTO, nao do snapshot.

        Preco e sequencia nao entram de proposito. Entram os eventos, com
        o tempo em faixas: e isso que faz o cache valer alguma coisa num
        endpoint chamado a cada dois segundos.
        """
        material = [
            self.symbol.upper(),
            ",".join(sorted(self.currencies)),
            str(self.calendar_usable),
        ]
        for evento in sorted(self.events, key=lambda e: (e.scheduled_bucket(), e.title)):
            material.append(
                f"{evento.title}|{evento.impact}|{evento.scheduled_bucket()}|{evento.currency}"
            )
        serial = "||".join(material)
        return hashlib.sha256(serial.encode("utf-8")).hexdigest()[:32]

    def as_state(self) -> dict[str, object]:
        """O `state` enviado. Lista fechada, como no Pulso.

        Nenhum preco, nenhum nivel, nenhum valor financeiro, nenhum dado
        de conta. O Jev nao tem por que ver isso para classificar texto, e
        mandar o que ele nao precisa e como um campo novo vaza.
        """
        return {
            "symbol": self.symbol,
            "currencies": list(self.currencies),
            "horizon_minutes": self.horizon_minutes,
            "calendar_available": self.calendar_usable,
            "scheduled_events": [
                {
                    "title": evento.title,
                    "impact": evento.impact,
                    "minutes_until": evento.minutes_until,
                    "currency": evento.currency,
                }
                for evento in self.events
            ],
        }


@dataclass(frozen=True, slots=True)
class HedgeContextAssessment:
    available: bool
    category: str
    confidence: float | None = None
    needs_review: bool = False
    needs_review_probability: float | None = None
    model: str = ""
    evaluated_at: datetime | None = None
    expires_at: datetime | None = None
    unavailable_reason: str = ""
    observation_only: bool = True
    """Sempre True nesta versao. O campo existe para que a tela diga, e
    para que mudar isso seja uma alteracao visivel no diff."""

    probabilities: dict[str, float] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, reason: str, *, category: str = CONTEXT_INSUFFICIENT) -> HedgeContextAssessment:
        """Sem classificacao, e sem inventar uma.

        `confidence` fica em `None`, nao em zero: zero seria uma medida, e
        aqui nao houve medida nenhuma.
        """
        return cls(
            available=False,
            category=category,
            confidence=None,
            unavailable_reason=reason,
        )

    @classmethod
    def local_calm(cls) -> HedgeContextAssessment:
        """Sem evento na janela: `CALM` decidido LOCALMENTE, sem chamada.

        Marcado como indisponivel de proposito — a categoria e verdadeira,
        mas nenhum modelo a emitiu, e a tela precisa poder dizer a
        diferenca entre "o Jev classificou como calmo" e "nao havia o que
        classificar".
        """
        return cls(
            available=False,
            category=CONTEXT_CALM,
            confidence=None,
            unavailable_reason="SEM_EVENTO_NA_JANELA",
        )


class HedgeContextAssessor:
    """Classifica contexto, com cache, teto diario e atalho local."""

    def __init__(
        self,
        client: JevClient,
        *,
        cache: TTLCache[HedgeContextAssessment] | None = None,
        cache_ttl_seconds: float = 300.0,
        daily_budget: int = 200,
    ) -> None:
        self._client = client
        self._cache = cache if cache is not None else TTLCache(ttl_seconds=cache_ttl_seconds)
        self._ttl = cache_ttl_seconds
        self._budget = daily_budget

    def assess(
        self,
        entrada: HedgeContextInput,
        *,
        session: Session,
        now: datetime | None = None,
    ) -> HedgeContextAssessment:
        """A classificacao. Nunca levanta excecao."""
        agora = now or datetime.now(UTC)

        if not entrada.calendar_usable:
            return HedgeContextAssessment.unavailable("CALENDARIO_INDISPONIVEL")

        if not entrada.currencies:
            return HedgeContextAssessment.unavailable("SEM_MOEDA_IDENTIFICADA")

        # Defesa 1: nao ha o que classificar. O caso mais comum, e o mais
        # barato — nenhuma requisicao.
        if not entrada.events:
            return HedgeContextAssessment.local_calm()

        chave = CacheKey(
            symbol=entrada.symbol.upper(),
            timeframe="CONTEXT",
            candle_open_time="",
            fingerprint=entrada.fingerprint(),
        )

        # Defesa 2: cache pelo contexto.
        guardado = self._cache.get(chave, now=agora)
        if guardado is not None:
            return guardado

        # Defesa 3: teto diario, contado no banco e compartilhado entre
        # processos.
        uso = read_usage(session, limit=self._budget, now=agora, key=BUDGET_SETTING)
        if uso.exhausted:
            logger.info(
                "jev_contexto_sem_orcamento",
                extra={"calls": uso.calls, "limit": uso.limit},
            )
            return HedgeContextAssessment.unavailable("ORCAMENTO_DIARIO_ATINGIDO")

        try:
            resultado = self._client.system_one(
                state=entrada.as_state(),
                questions={
                    QUESTION_CONTEXT: choice_question(_INSTRUCTIONS, CONTEXT_CRITERIA),
                    QUESTION_REVIEW: noul_question(_REVIEW_INSTRUCTIONS),
                },
            )
        except JevUnavailable as exc:
            # A chamada SAIU: conta no orcamento mesmo tendo falhado, senao
            # um endpoint quebrado seria tentado sem limite.
            self._record(session, agora)
            logger.info("jev_contexto_indisponivel", extra={"jev_reason": exc.reason.value})
            return HedgeContextAssessment.unavailable(exc.reason.value)

        self._record(session, agora)

        escolha = resultado.choices.get(QUESTION_CONTEXT)
        if escolha is None:
            return HedgeContextAssessment.unavailable(JevFailure.BAD_RESPONSE.value)

        revisao = resultado.nouls.get(QUESTION_REVIEW)
        probabilidade = revisao.probability if revisao else None

        avaliacao = HedgeContextAssessment(
            available=True,
            category=escolha.choice,
            confidence=escolha.confidence,
            needs_review=bool(
                probabilidade is not None and probabilidade >= NEEDS_REVIEW_THRESHOLD
            ),
            needs_review_probability=probabilidade,
            model=resultado.model,
            evaluated_at=agora,
            expires_at=agora + timedelta(seconds=self._ttl) if self._ttl > 0 else agora,
            probabilities=dict(escolha.probabilities),
        )
        # Falha nao entra no cache; sucesso entra.
        self._cache.set(chave, avaliacao, now=agora)
        return avaliacao

    def _record(self, session: Session, agora: datetime) -> None:
        record_call(
            session,
            limit=self._budget,
            now=agora,
            key=BUDGET_SETTING,
            description="Consumo diario da API Jev (reiniciado a cada dia UTC).",
        )

    def usage(self, session: Session, *, now: datetime | None = None) -> BudgetUsage:
        return read_usage(session, limit=self._budget, now=now, key=BUDGET_SETTING)
