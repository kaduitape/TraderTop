"""Identidade de sessao e os estados visiveis na tela.

## Por que a sessao tem quatro partes

`terminal + grafico + simbolo + setup`. Tirar qualquer uma mistura dados:

- sem **terminal**, dois computadores do mesmo operador escrevem um sobre
  o outro;
- sem **grafico**, dois graficos do mesmo ativo no mesmo terminal
  disputam a mesma sessao — e o conector pode estar num grafico separado
  justamente para nao brigar com outro EA, o que torna esse caso o
  normal, nao a excecao;
- sem **simbolo**, trocar o ativo do grafico herdaria a analise do
  anterior;
- sem **setup**, duas operacoes cobertas no mesmo ativo se confundiriam.

A chave e um hash das quatro: cabe em `String(128)` com folga e nao expoe
nome de maquina na URL nem no log.

## Os estados nao sao graus do mesmo eixo

"Aguardando dados" e esperanca; "desconectado" e ausencia; "modelo
indisponivel" e uma resposta que chegou dizendo que nao sabe. As tres
desenham coisas diferentes no grafico e pedem acoes diferentes de quem
olha. Um enum unico com ordem de severidade esconderia isso.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class SessionState(enum.StrEnum):
    CONNECTED = "CONNECTED"
    """Snapshot recente aceito e analise entregue."""

    WAITING_DATA = "WAITING_DATA"
    """Sessao aberta, nenhum snapshot utilizavel ainda."""

    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    """O servidor respondeu; a previsao e que nao existe. O motor
    financeiro pode continuar valendo — sao camadas separadas."""

    STALE_DATA = "STALE_DATA"
    """Passou da validade. O ultimo numero pode ficar na tela, mas
    identificado como historico — nunca com aparencia de sinal atual."""

    DISCONNECTED = "DISCONNECTED"
    CLOSED = "CLOSED"
    """Barreira atingida ou prazo esgotado. Fim da previsao, nao falha."""

    INVALID_KEY = "INVALID_KEY"


LIVE_STATES = frozenset({SessionState.CONNECTED})
"""Apenas um estado autoriza a tela a parecer um sinal atual."""

DISCONNECTED_AFTER = timedelta(seconds=90)
"""Sem snapshot por este tempo, a sessao esta desconectada.

Generoso em relacao ao intervalo de 1-2s do conector: uma rede ruim nao
pode apagar o desenho a cada engasgo, e o estado STALE_DATA ja cobre a
faixa intermediaria."""


def session_key(
    *, terminal_id: str, chart_id: str, symbol: str, setup_id: int
) -> str:
    """Chave estavel das quatro partes.

    Normaliza antes de hashear: o terminal manda o simbolo como esta no
    grafico, e `BTCUSDm` contra `BTCUSDM` criaria duas sessoes para o
    mesmo par olho-no-olho.
    """
    material = "|".join(
        (
            terminal_id.strip(),
            chart_id.strip(),
            symbol.strip().upper(),
            str(setup_id),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    state: SessionState
    age_seconds: float
    is_live: bool
    reason: str = ""


def evaluate_freshness(
    *,
    last_seen_at: datetime | None,
    now: datetime,
    max_age_seconds: int,
) -> FreshnessVerdict:
    """Traduz "quando chegou o ultimo dado" em estado de tela.

    Tres faixas, nao duas: dentro da validade, passou da validade, e
    silencio longo o bastante para ser desconexao. A faixa do meio e a
    que impede o grafico de piscar entre conectado e desconectado a cada
    atraso de rede.
    """
    if last_seen_at is None:
        return FreshnessVerdict(
            state=SessionState.WAITING_DATA,
            age_seconds=0.0,
            is_live=False,
            reason="Nenhum snapshot recebido ainda.",
        )

    # O banco devolve datetime sem fuso (SQLite sempre; MySQL conforme a
    # coluna). Subtrair ingenuo de consciente estoura, e o sintoma aparece
    # no caminho menos testado — o do snapshot fora de ordem. Gravamos
    # sempre em UTC, entao assumir UTC aqui e a leitura correta, nao um
    # chute.
    marca = last_seen_at if last_seen_at.tzinfo is not None else last_seen_at.replace(tzinfo=UTC)
    idade = (now - marca).total_seconds()

    if idade > DISCONNECTED_AFTER.total_seconds():
        return FreshnessVerdict(
            state=SessionState.DISCONNECTED,
            age_seconds=idade,
            is_live=False,
            reason=f"Sem dados do terminal ha {idade:.0f}s.",
        )

    if idade > max_age_seconds:
        return FreshnessVerdict(
            state=SessionState.STALE_DATA,
            age_seconds=idade,
            is_live=False,
            reason=(
                f"Ultimo dado tem {idade:.0f}s, acima do limite de "
                f"{max_age_seconds}s deste setup."
            ),
        )

    return FreshnessVerdict(
        state=SessionState.CONNECTED, age_seconds=idade, is_live=True
    )


def accepts_sequence(*, incoming: int, last_accepted: int) -> bool:
    """Snapshot fora de ordem e descartado.

    A rede nao garante ordem de chegada. Aplicar um estado antigo sobre um
    novo faria o grafico voltar no tempo — e sem erro nenhum, porque o
    pacote velho e perfeitamente valido em si.

    Igual tambem e rejeitado: e reenvio, e reprocessar gravaria a mesma
    observacao duas vezes no historico, inflando a base de treino com
    duplicatas que pareceriam independentes.
    """
    return incoming > last_accepted
