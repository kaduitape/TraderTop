from app.database.repositories.symbol_repository import SymbolRepository
from app.market.catalog import (
    MARKET_CATALOG,
    catalog_availability,
    grouped_availability,
    resolve_broker_symbol,
)
from app.mt5.symbol_mapper import SymbolSpecification


def _add_symbol(db_session, name: str, *, active: bool = True):
    return SymbolRepository(db_session).upsert_from_specification(
        SymbolSpecification(
            name=name,
            description="Broker symbol",
            digits=2 if name.startswith("XAU") else 5,
            point=0.01 if name.startswith("XAU") else 0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_contract_size=100_000.0,
            spread=2,
            trade_mode=4,
            visible=active,
        )
    )


def test_catalog_contains_xauusd_and_broad_forex_coverage() -> None:
    codes = {instrument.code for instrument in MARKET_CATALOG}

    assert "XAUUSD" in codes
    assert "USTEC" in codes
    assert "EURUSD" in codes
    assert "GBPJPY" in codes
    assert len(codes) >= 30


def test_catalog_matches_broker_suffix_without_inventing_symbol(db_session) -> None:
    synced = _add_symbol(db_session, "XAUUSD.a")

    availability = catalog_availability([synced])
    gold = next(item for item in availability if item.instrument.code == "XAUUSD")

    assert gold.is_available is True
    assert gold.synced_symbol == "XAUUSD.a"


def test_catalog_does_not_mark_inactive_symbol_as_available(db_session) -> None:
    inactive = _add_symbol(db_session, "EURUSD", active=False)

    availability = catalog_availability([inactive])
    euro = next(item for item in availability if item.instrument.code == "EURUSD")

    assert euro.is_available is False
    assert euro.synced_symbol is None


def test_grouped_availability_has_all_market_sections() -> None:
    grouped = grouped_availability([])

    assert set(grouped) == {"MAJORS", "CROSSES", "EXOTICS", "METALS", "INDICES", "CRYPTO"}
    assert any(item.instrument.code == "XAGUSD" for item in grouped["METALS"])
    assert any(item.instrument.code == "USTEC" for item in grouped["INDICES"])
    assert any(item.instrument.code == "BTCUSD" for item in grouped["CRYPTO"])


def test_catalog_matches_ustec_broker_symbol(db_session) -> None:
    synced = _add_symbol(db_session, "USTEC")

    availability = catalog_availability([synced])
    ustech = next(item for item in availability if item.instrument.code == "USTEC")

    assert ustech.is_available is True
    assert ustech.synced_symbol == "USTEC"


def test_resolve_broker_symbol_prefers_exact_then_shortest_suffix() -> None:
    names = ["XAUUSD.raw", "XAUUSD.a", "XAUUSD", "EURUSD"]

    assert resolve_broker_symbol("XAUUSD", names) == "XAUUSD"
    assert resolve_broker_symbol("XAUUSD", names[:2]) == "XAUUSD.a"


# --- cripto, e a lista que o worker enxerga ---------------------------------


def test_bitcoin_is_in_the_catalog() -> None:
    """Nao e cosmetico: `load_sync_config` filtra a selecao contra este
    catalogo, entao um codigo ausente aqui nunca chega ao worker — o ativo
    aparece no seletor da FotoAnalise e fica eternamente desatualizado."""
    from app.market.catalog import MARKET_CATALOG

    codigos = {instrumento.code for instrumento in MARKET_CATALOG}

    assert "BTCUSD" in codigos
    assert "ETHUSD" in codigos


def test_every_group_has_a_label_and_a_bucket() -> None:
    """Um grupo novo sem rotulo quebra a tela de Mercados, e sem balde
    `grouped_availability` levanta KeyError na primeira sincronizacao."""
    from app.market.catalog import GROUP_LABELS, MARKET_CATALOG, grouped_availability

    grupos = {instrumento.group for instrumento in MARKET_CATALOG}
    baldes = set(grouped_availability([]))

    for grupo in grupos:
        assert grupo in GROUP_LABELS, grupo
        assert grupo in baldes, grupo


def test_crypto_is_resolved_through_a_broker_suffix() -> None:
    """O caso real: a corretora publica BTCUSDm, nao BTCUSD."""
    from app.market.catalog import resolve_broker_symbol

    assert resolve_broker_symbol("BTCUSD", ["BTCUSDm", "EURUSDm"]) == "BTCUSDm"


def test_crypto_has_no_economic_calendar_of_its_own() -> None:
    """Bitcoin nao divulga PIB. Como o indice, so os eventos do dolar
    importam — e "BTC" como moeda faria o filtro descartar eventos
    legitimos com base numa sigla que nao existe no calendario."""
    from app.calendar_feed.blackout import currencies_for_symbol

    assert currencies_for_symbol("BTCUSD") == frozenset({"USD"})
    assert currencies_for_symbol("BTCUSDm") == frozenset({"USD"})
    assert currencies_for_symbol("ETHUSD") == frozenset({"USD"})
