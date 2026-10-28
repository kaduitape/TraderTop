"""A pergunta que este modulo responde — e as quatro que ele NAO responde.

## O evento previsto

    "Dado o estado atual do mercado e o prazo restante, qual barreira
     desta operacao sera atingida primeiro?"

Tres desfechos mutuamente exclusivos e exaustivos: o take da mesa
primeiro, o stop da mesa primeiro, ou nenhuma barreira ate o prazo. Eles
somam 1 por construcao.

## Quatro coisas que parecem a mesma e nao sao

1. **Probabilidade de TOCAR um nivel** — ignora o outro nivel. Quase
   sempre maior que a que interessa aqui.
2. **Probabilidade de tocar ANTES do outro** — e esta. A ordem importa:
   um nivel tocado depois do stop nao paga nada.
3. **Pontuacao de tendencia** — ordena cenarios entre si. Nao e
   probabilidade, nao soma 1, e nao tem unidade.
4. **Confianca do modelo** — fala do modelo, nao do mercado. Confianca
   alta numa probabilidade de 50% significa "tenho certeza de que esta
   equilibrado".

Misturar (1) com (2) infla toda estimativa de alvo. Apresentar (3) ou (4)
como (2) e o erro que este projeto ja trava em teste para o score da
FotoAnalise, e vale igual aqui.

## Feed: qual preco define o evento

O preco da mesa e o preco do MT5 nao sao o mesmo numero. O evento
previsto e definido pelo feed declarado em `Forecast.feed`, e a tela
precisa dizer qual e — prever sobre um feed e executar em outro e uma
diferenca que aparece justamente nos toques de barreira, que e onde
tudo se decide.

## Sem modelo validado, nao se inventa numero

`ModelKind` existe para que a origem viaje junto com o numero ate a tela.
Um baseline teorico e util e honesto; o mesmo numero apresentado como
"modelo treinado" seria falso. Nao ha, neste projeto, backtest com custos
que valide uma previsao — entao nada aqui pode se apresentar como tal.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

PROBABILITY_FIELDS = ("take_first", "stop_first", "no_barrier")


class ModelKind(enum.StrEnum):
    THEORETICAL_BASELINE = "THEORETICAL_BASELINE"
    """Derivado de hipoteses declaradas sobre o processo de preco, nao de
    dados historicos deste ativo. Util como referencia; nao e evidencia de
    que o mercado se comporta assim."""

    TREND_SCORE = "TREND_SCORE"
    """Ordenacao, nao probabilidade. Usado quando ha sinal mas nao ha
    base para quantificar."""

    TRAINED = "TRAINED"
    """Ajustado e validado fora da amostra, com calibracao medida. Nenhum
    modelo deste tipo acompanha esta versao."""


class Unavailable(enum.StrEnum):
    """Por que nao ha previsao. Cada motivo tem uma correcao diferente."""

    NO_MODEL = "NO_MODEL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE_DATA = "STALE_DATA"
    EXPIRED = "EXPIRED"
    INVALID_SETUP = "INVALID_SETUP"
    BARRIER_HIT = "BARRIER_HIT"
    """Nao e falha: a operacao acabou. Continuar recalculando como se
    estivesse aberta produziria uma previsao sobre um evento que ja
    aconteceu."""


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    """O estado do mercado no instante da previsao.

    Campos opcionais sao genuinamente opcionais: `None` significa "o
    terminal nao mandou", e cada motor decide se consegue trabalhar sem.
    Nenhum deles vira zero no caminho."""

    symbol: str
    is_long: bool
    entry: float
    take: float
    stop: float
    current_price: float
    seconds_remaining: float
    feed: str = "MT5"

    atr: float | None = None
    atr_period_seconds: float | None = None
    """A que intervalo o ATR se refere. Sem isso o ATR e um numero sem
    escala temporal e nao da para projetar para o horizonte."""

    recent_returns: tuple[float, ...] = ()
    spread: float | None = None
    tick_volume: int | None = None
    real_volume: int | None = None
    """Distinto de `tick_volume` de proposito: em forex o segundo e
    contagem de cotacoes, nao quantidade negociada, e trata-los como a
    mesma coisa produziria uma feature que mede outra coisa."""

    server_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class Forecast:
    """As tres probabilidades, ou a ausencia delas."""

    available: bool
    model_kind: ModelKind
    model_version: str
    feed: str = "MT5"

    take_first: float | None = None
    stop_first: float | None = None
    no_barrier: float | None = None
    """`None` quando nao ha previsao. NUNCA zero: zero e uma estimativa
    de que o evento nao acontece, e seria lido como tal na tela."""

    confidence: float | None = None
    """Do MODELO, nao do mercado. Mantida separada das tres acima."""

    valid_until: datetime | None = None
    unavailable_reason: Unavailable | None = None
    assumptions: tuple[str, ...] = ()
    """As hipoteses que sustentam o numero. Um baseline sem elas e um
    palpite com aparencia de conta."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.available:
            return
        valores = [self.take_first, self.stop_first, self.no_barrier]
        if any(v is None for v in valores):
            raise ValueError("previsao disponivel precisa das tres probabilidades")
        total = sum(valores)  # type: ignore[arg-type]
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"as tres probabilidades somam {total}, nao 1")
        if any(v < 0 or v > 1 for v in valores):  # type: ignore[operator]
            raise ValueError("probabilidade fora de 0..1")

    @classmethod
    def unavailable(
        cls,
        reason: Unavailable,
        *,
        model_kind: ModelKind = ModelKind.THEORETICAL_BASELINE,
        model_version: str = "",
        notes: tuple[str, ...] = (),
    ) -> Forecast:
        return cls(
            available=False,
            model_kind=model_kind,
            model_version=model_version,
            unavailable_reason=reason,
            notes=notes,
        )

    def percentages(self) -> tuple[int, int, int] | None:
        """Inteiros que somam exatamente 100.

        Arredondar cada um por conta propria produz 33/33/33 ou 34/33/34 —
        uma tela que nao fecha em 100 faz o operador desconfiar do numero
        inteiro, com razao. Maior resto resolve sem favorecer ninguem.
        """
        if not self.available:
            return None

        brutos = [
            (self.take_first or 0.0) * 100,
            (self.stop_first or 0.0) * 100,
            (self.no_barrier or 0.0) * 100,
        ]
        chao = [int(v) for v in brutos]
        sobra = 100 - sum(chao)
        restos = sorted(range(3), key=lambda i: brutos[i] - chao[i], reverse=True)
        for i in range(sobra):
            chao[restos[i % 3]] += 1
        return (chao[0], chao[1], chao[2])


class ForecastEngine(Protocol):
    """Interface versionada. Trocar o motor nao pode exigir mudar a rota,
    o painel nem o indicador."""

    @property
    def version(self) -> str: ...

    @property
    def kind(self) -> ModelKind: ...

    def predict(self, inputs: ForecastInputs) -> Forecast: ...
