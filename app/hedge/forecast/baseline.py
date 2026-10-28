"""Baseline teorico: duas barreiras, um prazo, nenhum dado historico.

## O modelo, dito inteiro

Supoe que o preco se move como um passeio aleatorio sem tendencia
(movimento browniano aritmetico) com volatilidade constante estimada do
ATR ou dos retornos recentes. Com isso, duas contas classicas:

1. **Sobreviver ao prazo** — a chance de o preco ficar entre as duas
   barreiras ate T e a solucao da equacao do calor com fronteiras
   absorventes:

       P(T) = (4/pi) * sum_{n impar} (1/n) sin(n*pi*x0/L) exp(-n^2 pi^2 s^2 T / (2 L^2))

   onde L e a distancia entre barreiras e x0 a posicao atual medida da
   barreira de baixo.

2. **Quem vem primeiro** — sem tendencia, a chance de tocar a barreira de
   cima antes da de baixo e `x0/L` (a distancia relativa ate cada uma).

A massa absorvida ate T, `1 - P(T)`, e dividida nessa proporcao.

## As hipoteses, e onde elas quebram

- **Sem tendencia.** Um mercado em tendencia clara viola isto, e o
  baseline vai subestimar a barreira a favor.
- **Volatilidade constante.** Nao ha cluster de volatilidade aqui; perto
  de noticia, o numero fica baixo demais.
- **Movimento continuo.** Gap pula barreira sem tocar nela.
- **A divisao da massa absorvida usa a proporcao de horizonte INFINITO.**
  E uma aproximacao: no horizonte finito a barreira mais proxima e
  favorecida um pouco mais do que `x0/L` indica. O erro cresce com
  prazos curtos em relacao a volatilidade.

Nada disso e defeito a corrigir: e o preco de nao ter dado. O numero sai
marcado como `THEORETICAL_BASELINE` justamente para que ninguem o
confunda com uma frequencia medida neste ativo.

## Por que ele existe

Para ser o piso de comparacao. Qualquer modelo treinado que venha depois
precisa bater isto em Brier score fora da amostra — senao a complexidade
nao se pagou. E porque "sem numero nenhum" e pior: a distancia relativa
ate cada barreira E informacao, e esconde-la nao torna o operador mais
cuidadoso, so menos informado.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from app.hedge.forecast.base import (
    Forecast,
    ForecastInputs,
    ModelKind,
    Unavailable,
)

VERSION = "baseline-2barrier-1.0.0"

MAX_TERMS = 64
"""Termos da serie. Converge rapido (cada termo decai com n^2 no
expoente); 64 e folga larga para prazos curtos, onde a convergencia e
mais lenta."""

MIN_SECONDS = 1.0
MIN_VOLATILITY = 1e-12

ASSUMPTIONS = (
    "Passeio aleatorio sem tendencia (nao incorpora direcao do mercado).",
    "Volatilidade constante no horizonte, estimada do ATR ou dos retornos.",
    "Movimento continuo: gaps podem pular uma barreira sem toca-la.",
    "A divisao entre take e stop usa a proporcao de horizonte infinito.",
    "Derivado de hipoteses, nao de frequencias medidas neste ativo.",
)


def _volatility_per_second(inputs: ForecastInputs) -> float | None:
    """Volatilidade em unidades de PRECO por raiz de segundo.

    O ATR vem primeiro porque e o que o terminal ja calcula e manda. Ele
    precisa da propria escala temporal: ATR(14) em M15 e ATR(14) em M1
    sao numeros diferentes para o mesmo mercado, e sem saber de qual se
    trata nao da para projetar para o horizonte.
    """
    if inputs.atr and inputs.atr > 0 and inputs.atr_period_seconds:
        if inputs.atr_period_seconds <= 0:
            return None
        # ATR ~ amplitude tipica de uma barra. Como proxy de desvio padrao
        # por barra e grosseiro mas conservador; dividir pela raiz do
        # tempo da barra converte para "por raiz de segundo".
        return inputs.atr / math.sqrt(inputs.atr_period_seconds)

    retornos = [r for r in inputs.recent_returns if r is not None]
    if len(retornos) >= 10 and inputs.atr_period_seconds:
        media = sum(retornos) / len(retornos)
        variancia = sum((r - media) ** 2 for r in retornos) / (len(retornos) - 1)
        desvio_por_barra = math.sqrt(variancia) * abs(inputs.current_price)
        if desvio_por_barra > 0 and inputs.atr_period_seconds > 0:
            return desvio_por_barra / math.sqrt(inputs.atr_period_seconds)

    return None


def survival_probability(*, up: float, down: float, sigma: float, seconds: float) -> float:
    """Chance de NAO tocar nenhuma barreira ate `seconds`.

    `up` e `down` sao distancias positivas do preco atual ate cada
    barreira. A serie e a solucao padrao com duas fronteiras absorventes.
    """
    largura = up + down
    if largura <= 0 or sigma <= MIN_VOLATILITY or seconds <= 0:
        return 0.0

    x0 = down  # posicao medida a partir da barreira de baixo
    escala = (sigma**2) * seconds / (2 * largura**2)

    total = 0.0
    for n in range(1, MAX_TERMS * 2, 2):
        expoente = -(n**2) * (math.pi**2) * escala
        if expoente < -700:  # underflow de exp; o resto da serie e zero
            break
        total += (1.0 / n) * math.sin(n * math.pi * x0 / largura) * math.exp(expoente)

    return max(0.0, min(1.0, (4.0 / math.pi) * total))


class BaselineForecastEngine:
    """Duas barreiras, sem tendencia, com as hipoteses na propria saida."""

    version = VERSION
    kind = ModelKind.THEORETICAL_BASELINE

    def __init__(self, *, validity_seconds: float = 10.0) -> None:
        self._validity = validity_seconds

    def predict(self, inputs: ForecastInputs) -> Forecast:
        agora = datetime.now(UTC)

        if inputs.seconds_remaining <= 0:
            return Forecast.unavailable(
                Unavailable.EXPIRED, model_kind=self.kind, model_version=self.version
            )

        take, stop, preco = inputs.take, inputs.stop, inputs.current_price

        # Setup incoerente: take e stop precisam cercar a ENTRADA, cada um
        # do seu lado. Sem esta checagem o motor aceitaria os dois niveis
        # acima da entrada numa compra — a geometria entre eles fecha, a
        # serie converge, e sai um numero perfeitamente formado sobre uma
        # operacao que ninguem conseguiria montar.
        #
        # A mesma regra vale em `engine.calculate`, de proposito: as duas
        # camadas sao alcancaveis de forma independente, e uma previsao
        # sobre setup invalido nao fica menos errada por o financeiro ter
        # recusado em silencio.
        lado_ok = (
            (take > inputs.entry > stop)
            if inputs.is_long
            else (take < inputs.entry < stop)
        )
        if not lado_ok:
            return Forecast.unavailable(
                Unavailable.INVALID_SETUP,
                model_kind=self.kind,
                model_version=self.version,
                notes=(
                    "Take e stop precisam ficar em lados opostos da entrada: "
                    + (
                        "take acima e stop abaixo numa compra."
                        if inputs.is_long
                        else "take abaixo e stop acima numa venda."
                    ),
                ),
            )

        # Barreira ja tocada: a previsao acabou. Recalcular aqui produziria
        # um numero sobre um evento que ja se decidiu.
        if inputs.is_long:
            if preco >= take or preco <= stop:
                return self._terminal()
            subida, descida = take - preco, preco - stop
        else:
            if preco <= take or preco >= stop:
                return self._terminal()
            subida, descida = stop - preco, preco - take

        if subida <= 0 or descida <= 0:
            return Forecast.unavailable(
                Unavailable.INVALID_SETUP,
                model_kind=self.kind,
                model_version=self.version,
                notes=("Take e stop precisam ficar em lados opostos da entrada.",),
            )

        sigma = _volatility_per_second(inputs)
        if sigma is None or sigma <= MIN_VOLATILITY:
            return Forecast.unavailable(
                Unavailable.INSUFFICIENT_DATA,
                model_kind=self.kind,
                model_version=self.version,
                notes=(
                    "Sem ATR com escala temporal nem retornos suficientes para "
                    "estimar volatilidade.",
                ),
            )

        segundos = max(MIN_SECONDS, inputs.seconds_remaining)
        sobrevive = survival_probability(
            up=subida, down=descida, sigma=sigma, seconds=segundos
        )
        absorvida = 1.0 - sobrevive

        # Sem tendencia, tocar a de cima antes da de baixo tem chance igual
        # a distancia RELATIVA ate a de baixo.
        p_cima = descida / (subida + descida)
        if inputs.is_long:
            p_take, p_stop = absorvida * p_cima, absorvida * (1 - p_cima)
        else:
            # Vendido: o take esta embaixo e o stop em cima.
            p_take, p_stop = absorvida * (1 - p_cima), absorvida * p_cima

        # Reparo de arredondamento: a soma precisa fechar em 1 exatamente,
        # e o excedente cabe no "nenhuma barreira", que e o residual.
        resto = max(0.0, 1.0 - p_take - p_stop)

        return Forecast(
            available=True,
            model_kind=self.kind,
            model_version=self.version,
            feed=inputs.feed,
            take_first=p_take,
            stop_first=p_stop,
            no_barrier=resto,
            confidence=None,  # um baseline teorico nao tem calibracao medida
            valid_until=agora + timedelta(seconds=self._validity),
            assumptions=ASSUMPTIONS,
        )

    def _terminal(self) -> Forecast:
        return Forecast.unavailable(
            Unavailable.BARRIER_HIT,
            model_kind=self.kind,
            model_version=self.version,
            notes=("Uma barreira ja foi atingida; esta previsao esta encerrada.",),
        )
