"""Por que um simbolo fica sem candles novas.

Este modulo nasceu de um caso real: BTCUSDm apareceu na FotoAnalise com
27 dias de atraso enquanto o conector estava online, verde e coletando.
A tela dizia "o coletor MT5 provavelmente esta parado", o operador foi
conferir o conector, encontrou tudo certo — e ficou sem proximo passo.

A causa era outra: BTCUSD nao estava no catalogo de ativos
sincronizaveis, entao `load_sync_config` o descartava da lista e o worker
nunca o coletava. Duas listas diferentes, nenhuma tela dizendo isso.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.mt5.sync_settings import load_sync_config, save_sync_config, stale_data_reason


@pytest.fixture(autouse=True)
def _limpa(db_session):
    """A configuracao vive em `system_settings`, compartilhado pela suite."""
    from app.database.models.system_setting import SystemSetting

    def apaga():
        db_session.query(SystemSetting).delete()
        db_session.commit()

    apaga()
    yield
    apaga()


def _configura(db_session, *, symbols: tuple[str, ...], enabled: bool = True) -> None:
    save_sync_config(
        db_session, replace(load_sync_config(db_session), symbols=symbols, enabled=enabled)
    )
    db_session.commit()


def test_a_covered_symbol_gives_no_special_reason(db_session) -> None:
    """Coberto e ligado: o diagnostico generico ("o coletor parou") e mesmo
    o mais provavel, e a frase especifica so atrapalharia."""
    _configura(db_session, symbols=("EURUSD", "XAUUSD"))

    assert stale_data_reason(db_session, "EURUSD") == ""


def test_the_broker_suffix_still_counts_as_covered(db_session) -> None:
    """O worker sincroniza `EURUSD` e grava `EURUSDm`. Comparar os dois
    nomes crus diria "nao coberto" para todo ativo de corretora com
    sufixo — ou seja, para quase todos."""
    _configura(db_session, symbols=("EURUSD",))

    assert stale_data_reason(db_session, "EURUSDM") == ""
    assert stale_data_reason(db_session, "EURUSD.raw") == ""


def test_a_symbol_outside_the_sync_list_says_so(db_session) -> None:
    """A causa mais comum, e a que a mensagem antiga nunca mencionava."""
    _configura(db_session, symbols=("EURUSD",))

    motivo = stale_data_reason(db_session, "XAUUSD")

    assert "nao esta na lista" in motivo
    assert "Conexao MT5" in motivo
    assert "coletor" not in motivo.lower(), "nao pode culpar o coletor"


def test_paused_sync_is_named_as_paused(db_session) -> None:
    """Pausado e diferente de quebrado, e a acao tambem."""
    _configura(db_session, symbols=("EURUSD",), enabled=False)

    motivo = stale_data_reason(db_session, "EURUSD")

    assert "pausada" in motivo


def test_a_symbol_outside_the_catalog_is_its_own_case(db_session) -> None:
    """O caso que gerou tudo isto. Antes de BTCUSD entrar no catalogo,
    marca-lo em Conexao MT5 nem era possivel — a mensagem precisa dizer
    isso em vez de mandar marcar uma caixa que nao existe."""
    _configura(db_session, symbols=("EURUSD",))

    motivo = stale_data_reason(db_session, "DOGECOINXYZ")

    assert "catalogo" in motivo
    assert "nunca o coleta" in motivo


def test_bitcoin_is_now_selectable(db_session) -> None:
    """Regressao direta do caso relatado: `load_sync_config` filtra contra
    o catalogo, entao um codigo ausente dali era descartado em silencio e
    o ativo nunca era coletado."""
    _configura(db_session, symbols=("BTCUSD",))

    assert load_sync_config(db_session).symbols == ("BTCUSD",)
    assert stale_data_reason(db_session, "BTCUSDm") == ""
