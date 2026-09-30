"""O cache em si: expiracao, teto e o desligamento por TTL zero."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.jev.cache import CacheKey, TTLCache

AGORA = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _chave(n: int = 0) -> CacheKey:
    return CacheKey(symbol=f"S{n}", timeframe="M15", candle_open_time="t", fingerprint="f")


def test_a_stored_value_comes_back() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set(_chave(), "veredito", now=AGORA)

    assert cache.get(_chave(), now=AGORA + timedelta(seconds=59)) == "veredito"


def test_an_expired_value_is_gone_and_stops_occupying_space() -> None:
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set(_chave(), "veredito", now=AGORA)

    assert cache.get(_chave(), now=AGORA + timedelta(seconds=60)) is None
    assert len(cache) == 0


def test_ttl_zero_stores_nothing() -> None:
    """Desligar o cache e uma configuracao valida (JEV_CACHE_TTL_SECONDS=0).
    Trata-la aqui evita um `if` no chamador para cada uso."""
    cache: TTLCache[str] = TTLCache(ttl_seconds=0)
    cache.set(_chave(), "veredito", now=AGORA)

    assert cache.get(_chave(), now=AGORA) is None


def test_the_oldest_entry_leaves_when_the_cap_is_reached() -> None:
    """Sem teto, um processo de vida longa acumularia uma entrada por
    candle por grafico, para sempre."""
    cache: TTLCache[str] = TTLCache(ttl_seconds=600, max_entries=3)

    for i in range(4):
        cache.set(_chave(i), f"v{i}", now=AGORA + timedelta(seconds=i))

    assert len(cache) == 3
    assert cache.get(_chave(0), now=AGORA) is None
    assert cache.get(_chave(3), now=AGORA) == "v3"


def test_rewriting_a_key_does_not_evict_anyone() -> None:
    """Atualizar o veredito do mesmo cenario nao e uma entrada nova."""
    cache: TTLCache[str] = TTLCache(ttl_seconds=600, max_entries=2)
    cache.set(_chave(0), "a", now=AGORA)
    cache.set(_chave(1), "b", now=AGORA)
    cache.set(_chave(0), "a2", now=AGORA)

    assert len(cache) == 2
    assert cache.get(_chave(1), now=AGORA) == "b"


def test_concurrent_writes_do_not_corrupt_the_cache() -> None:
    """As rotas sincronas do FastAPI rodam num pool de threads: duas
    consultas ao mesmo simbolo tocam este dicionario ao mesmo tempo."""
    import threading

    cache: TTLCache[int] = TTLCache(ttl_seconds=600, max_entries=50)
    barreira = threading.Barrier(8)

    def escreve(n: int) -> None:
        barreira.wait()
        for i in range(50):
            cache.set(_chave(n * 50 + i), i, now=AGORA)

    linhas = [threading.Thread(target=escreve, args=(n,)) for n in range(8)]
    for linha in linhas:
        linha.start()
    for linha in linhas:
        linha.join()

    assert len(cache) <= 50
