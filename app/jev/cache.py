"""Cache em memoria da classificacao do Jev.

## Por que a chave nao e so o simbolo

O indicador consulta `/api/pulso` a cada 15 segundos, por grafico aberto.
Uma chamada por consulta seria uma chamada a cada 15 segundos por grafico
— e o cenario analisado nao muda nesse ritmo: numa candle de M15 ele muda,
no maximo, a cada 15 minutos.

A chave e `simbolo | timeframe | abertura da candle | fingerprint`. Os dois
primeiros separam graficos; o terceiro garante que uma candle nova sempre
reavalia; o quarto captura mudanca DENTRO da candle que altere a decisao
(um bloqueio que apareceu, o score que cruzou uma faixa). O fingerprint
exclui o preco atual de proposito — ver `app/jev/snapshot.py`.

## O TTL e o teto, nao a regra

Quem decide reavaliar e a candle. O TTL so impede que uma candle longa
(H4, D1) congele o veredito por horas. Um TTL curto com candle longa
gastaria chamadas a toa; um TTL longo com candle curta nunca seria
alcancado. Os dois juntos cobrem as duas pontas.

## Falha nao entra no cache

Guardar um erro faria uma indisponibilidade de 2 segundos apagar a
classificacao pelos 5 minutos seguintes. A proxima consulta tenta de novo;
enquanto isso o grafico mostra o que o motor tecnico ja diz sozinho.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MAX_ENTRIES = 256
"""Teto de simbolos/timeframes lembrados ao mesmo tempo.

Sem teto, um processo de vida longa acumularia uma entrada por candle por
grafico para sempre. 256 cobre qualquer uso plausivel do painel; ao
estourar, a entrada mais antiga sai."""


@dataclass(frozen=True, slots=True)
class CacheKey:
    symbol: str
    timeframe: str
    candle_open_time: str
    fingerprint: str


class TTLCache[T]:
    """Dicionario com expiracao e teto de tamanho.

    Tem trava porque o FastAPI roda rotas sincronas num pool de threads:
    duas consultas simultaneas ao mesmo simbolo tocam este dicionario ao
    mesmo tempo, e dicionario de CPython nao garante consistencia entre
    leitura e escrita compostas.
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: dict[CacheKey, tuple[datetime, T]] = {}

    def get(self, key: CacheKey, *, now: datetime | None = None) -> T | None:
        agora = now or datetime.now(UTC)
        with self._lock:
            entrada = self._items.get(key)
            if entrada is None:
                return None
            expira_em, valor = entrada
            if agora >= expira_em:
                del self._items[key]
                return None
            return valor

    def set(self, key: CacheKey, value: T, *, now: datetime | None = None) -> None:
        if self._ttl <= 0:
            return   # TTL zero desliga o cache, sem virar caso especial no chamador
        agora = now or datetime.now(UTC)
        with self._lock:
            if len(self._items) >= self._max and key not in self._items:
                mais_antiga = min(self._items, key=lambda k: self._items[k][0])
                del self._items[mais_antiga]
            self._items[key] = (agora + timedelta(seconds=self._ttl), value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
