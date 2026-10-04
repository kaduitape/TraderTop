"""Catalogo curado de Forex, metais e índices para descoberta no dashboard.

O catalogo nao cria simbolos negociaveis nem inventa especificacoes. Ele
apenas reconcilia nomes conhecidos com os simbolos reais sincronizados do
MetaTrader, inclusive sufixos comuns de corretora (ex.: EURUSD.a).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.database.models.symbol import Symbol

MarketGroup = Literal["MAJORS", "CROSSES", "EXOTICS", "METALS", "INDICES", "CRYPTO"]


@dataclass(frozen=True, slots=True)
class MarketInstrument:
    code: str
    label: str
    group: MarketGroup
    base: str
    quote: str
    icon: str = "bi-currency-exchange"


@dataclass(frozen=True, slots=True)
class MarketAvailability:
    instrument: MarketInstrument
    synced_symbol: str | None

    @property
    def is_available(self) -> bool:
        return self.synced_symbol is not None


MARKET_CATALOG: tuple[MarketInstrument, ...] = (
    MarketInstrument("EURUSD", "Euro / Dolar", "MAJORS", "EUR", "USD"),
    MarketInstrument("GBPUSD", "Libra / Dolar", "MAJORS", "GBP", "USD"),
    MarketInstrument("USDJPY", "Dolar / Iene", "MAJORS", "USD", "JPY"),
    MarketInstrument("USDCHF", "Dolar / Franco", "MAJORS", "USD", "CHF"),
    MarketInstrument("AUDUSD", "Dolar Australiano / Dolar", "MAJORS", "AUD", "USD"),
    MarketInstrument("USDCAD", "Dolar / Dolar Canadense", "MAJORS", "USD", "CAD"),
    MarketInstrument("NZDUSD", "Dolar Neozelandes / Dolar", "MAJORS", "NZD", "USD"),
    MarketInstrument("EURGBP", "Euro / Libra", "CROSSES", "EUR", "GBP"),
    MarketInstrument("EURJPY", "Euro / Iene", "CROSSES", "EUR", "JPY"),
    MarketInstrument("EURCHF", "Euro / Franco", "CROSSES", "EUR", "CHF"),
    MarketInstrument("EURAUD", "Euro / Dolar Australiano", "CROSSES", "EUR", "AUD"),
    MarketInstrument("EURCAD", "Euro / Dolar Canadense", "CROSSES", "EUR", "CAD"),
    MarketInstrument("EURNZD", "Euro / Dolar Neozelandes", "CROSSES", "EUR", "NZD"),
    MarketInstrument("GBPJPY", "Libra / Iene", "CROSSES", "GBP", "JPY"),
    MarketInstrument("GBPCHF", "Libra / Franco", "CROSSES", "GBP", "CHF"),
    MarketInstrument("GBPAUD", "Libra / Dolar Australiano", "CROSSES", "GBP", "AUD"),
    MarketInstrument("GBPCAD", "Libra / Dolar Canadense", "CROSSES", "GBP", "CAD"),
    MarketInstrument("GBPNZD", "Libra / Dolar Neozelandes", "CROSSES", "GBP", "NZD"),
    MarketInstrument("AUDJPY", "Dolar Australiano / Iene", "CROSSES", "AUD", "JPY"),
    MarketInstrument("AUDNZD", "Dolar Australiano / Dolar Neozelandes", "CROSSES", "AUD", "NZD"),
    MarketInstrument("AUDCAD", "Dolar Australiano / Dolar Canadense", "CROSSES", "AUD", "CAD"),
    MarketInstrument("AUDCHF", "Dolar Australiano / Franco", "CROSSES", "AUD", "CHF"),
    MarketInstrument("CADJPY", "Dolar Canadense / Iene", "CROSSES", "CAD", "JPY"),
    MarketInstrument("CADCHF", "Dolar Canadense / Franco", "CROSSES", "CAD", "CHF"),
    MarketInstrument("NZDJPY", "Dolar Neozelandes / Iene", "CROSSES", "NZD", "JPY"),
    MarketInstrument("NZDCHF", "Dolar Neozelandes / Franco", "CROSSES", "NZD", "CHF"),
    MarketInstrument("CHFJPY", "Franco / Iene", "CROSSES", "CHF", "JPY"),
    MarketInstrument("USDZAR", "Dolar / Rand", "EXOTICS", "USD", "ZAR"),
    MarketInstrument("USDTRY", "Dolar / Lira Turca", "EXOTICS", "USD", "TRY"),
    MarketInstrument("USDMXN", "Dolar / Peso Mexicano", "EXOTICS", "USD", "MXN"),
    MarketInstrument("XAUUSD", "Ouro / Dolar", "METALS", "XAU", "USD", "bi-gem"),
    MarketInstrument("XAGUSD", "Prata / Dolar", "METALS", "XAG", "USD", "bi-gem"),
    # USTEC é o nome oferecido pelo terminal conectado à produção. O índice
    # é cotado em USD, mas não é um par de moedas; o calendário considera
    # somente os eventos americanos para este ativo.
    MarketInstrument(
        "USTEC", "US Tech 100 Index", "INDICES", "USTEC", "USD", "bi-graph-up-arrow"
    ),
    # Cripto. Nao estar aqui nao impedia o simbolo de existir no banco — o
    # catalogo da corretora e sincronizado inteiro e BTCUSDm aparecia no
    # seletor da FotoAnalise normalmente. Impedia outra coisa, e em
    # silencio: `load_sync_config` descarta todo codigo fora deste catalogo,
    # entao o worker nunca coletava o ativo. O painel oferecia um simbolo
    # que ele proprio nunca manteria atualizado.
    #
    # Como o indice, estas nao sao moedas: o calendario economico considera
    # apenas os eventos do dolar (ver `_NON_MONETARY_BASES`). Nao existe
    # agenda de indicadores do Bitcoin.
    MarketInstrument("BTCUSD", "Bitcoin / Dolar", "CRYPTO", "BTC", "USD", "bi-currency-bitcoin"),
    MarketInstrument("ETHUSD", "Ethereum / Dolar", "CRYPTO", "ETH", "USD", "bi-currency-bitcoin"),
)

GROUP_LABELS: dict[MarketGroup, str] = {
    "METALS": "Metais",
    "MAJORS": "Pares principais",
    "CROSSES": "Pares cruzados",
    "EXOTICS": "Pares exoticos",
    "INDICES": "Índices",
    "CRYPTO": "Cripto",
}


def _matches_catalog_code(symbol_name: str, code: str) -> bool:
    normalized = symbol_name.upper().replace("/", "")
    return normalized == code or normalized.startswith(code) or normalized.endswith(code)


def resolve_broker_symbol(code: str, broker_names: list[str]) -> str | None:
    """Resolve um codigo canonico para o nome real oferecido pela corretora.

    Correspondencia exata sempre vence; depois, o nome mais curto entre os
    candidatos com prefixo/sufixo (ex.: ``XAUUSD.a``). Isso evita depender
    de um sufixo especifico de corretora.
    """
    normalized_code = code.strip().upper().replace("/", "")
    exact = next(
        (
            name
            for name in broker_names
            if name.upper().replace("/", "") == normalized_code
        ),
        None,
    )
    if exact is not None:
        return exact
    matches = [
        name for name in broker_names if _matches_catalog_code(name, normalized_code)
    ]
    return min(matches, key=lambda name: (len(name), name)) if matches else None


def catalog_availability(symbols: list[Symbol]) -> list[MarketAvailability]:
    active_names = [symbol.name for symbol in symbols if symbol.is_active]
    result: list[MarketAvailability] = []
    for instrument in MARKET_CATALOG:
        match = resolve_broker_symbol(instrument.code, active_names)
        result.append(MarketAvailability(instrument=instrument, synced_symbol=match))
    return result


def grouped_availability(
    symbols: list[Symbol],
) -> dict[MarketGroup, list[MarketAvailability]]:
    grouped: dict[MarketGroup, list[MarketAvailability]] = {
        "METALS": [],
        "MAJORS": [],
        "CROSSES": [],
        "EXOTICS": [],
        "INDICES": [],
        "CRYPTO": [],
    }
    for availability in catalog_availability(symbols):
        grouped[availability.instrument.group].append(availability)
    return grouped
