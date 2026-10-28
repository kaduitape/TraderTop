"""Limite de taxa por chave, em memoria.

## Por que por chave e nao por IP

Todos os conectores de um cliente saem do mesmo IP residencial, e dois
operadores atras do mesmo NAT compartilhariam o limite. A chave identifica
quem de fato esta consumindo, e e ela que pode ser revogada se abusar.

## Por que token bucket

O conector manda snapshot a cada 1-2 segundos por grafico, e abrir cinco
graficos de uma vez produz uma rajada legitima. Uma janela fixa recusaria
essa rajada; o balde a absorve e so recusa quem mantem a taxa alta.

## Em memoria, e o que isso custa

O limite vale por processo. Com varios workers, o teto efetivo e o limite
vezes o numero de processos — aceito de proposito nesta versao: a
alternativa (Redis) acrescentaria uma dependencia de infraestrutura que
este projeto ainda nao tem, para proteger um endpoint que ja exige chave
valida. A conta muda se o endpoint virar publico.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

DEFAULT_RATE_PER_SECOND = 4.0
"""Quatro por segundo por chave. O conector manda um a cada 1-2s por
grafico, entao isto cobre varios graficos sem folga para laco em
descontrole."""

DEFAULT_BURST = 20.0
"""Rajada tolerada: abrir vários gráficos de uma vez é legítimo."""


@dataclass
class _Bucket:
    tokens: float
    last: float


class RateLimiter:
    def __init__(
        self,
        *,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: float = DEFAULT_BURST,
        max_keys: int = 4096,
    ) -> None:
        self._rate = rate_per_second
        self._burst = burst
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consome um token. False significa recusar com 429."""
        agora = now if now is not None else time.monotonic()
        with self._lock:
            balde = self._buckets.get(key)
            if balde is None:
                if len(self._buckets) >= self._max_keys:
                    # Teto de memoria: sem ele, chaves revogadas ficariam
                    # para sempre. Descarta a entrada mais antiga, o que no
                    # pior caso concede uma rajada extra — preferivel a
                    # crescer sem limite.
                    mais_antiga = min(self._buckets, key=lambda k: self._buckets[k].last)
                    del self._buckets[mais_antiga]
                self._buckets[key] = _Bucket(tokens=self._burst - 1, last=agora)
                return True

            decorrido = max(0.0, agora - balde.last)
            balde.tokens = min(self._burst, balde.tokens + decorrido * self._rate)
            balde.last = agora

            if balde.tokens < 1.0:
                return False
            balde.tokens -= 1.0
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


snapshot_limiter = RateLimiter()
"""Compartilhado pelo processo. O endpoint de snapshot e o unico de alta
frequencia; os outros sao consultados de tela."""
