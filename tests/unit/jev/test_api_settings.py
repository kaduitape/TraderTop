"""A chave do Jev cadastrada pelo painel.

Este modulo existe porque o dono pediu o campo na tela, e um campo de
segredo tem tres modos de falha que nao dao erro nenhum:

1. gravar em texto puro e descobrir no dump do banco;
2. devolver o valor para a tela "so para conferir";
3. apagar a chave sem querer ao salvar outro ajuste.

Os tres estao cobertos aqui.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.database.repositories.system_setting_repository import SystemSettingRepository
from app.jev.api_settings import (
    JEV_API_KEY_SETTING,
    MIN_KEY_LENGTH,
    clear_api_key,
    load_jev_settings,
    save_api_key,
    set_enabled,
)

CHAVE = "tsk_exemplo_de_chave_do_jev_1234567890"


@pytest.fixture(autouse=True)
def _limpa(db_session):
    """`system_settings` e compartilhado pela suite inteira."""
    from app.database.models.system_setting import SystemSetting

    def apagar() -> None:
        db_session.query(SystemSetting).filter(
            SystemSetting.key.in_(("jev_api_key_encrypted", "jev_enabled"))
        ).delete(synchronize_session=False)
        db_session.commit()

    apagar()
    yield
    apagar()


def _settings():
    return get_settings()


# --- a chave nao fica legivel ----------------------------------------------


def test_the_stored_value_is_not_the_key(db_session) -> None:
    """O teste que justifica o modulo: quem abrir a tabela nao le a
    chave. Protege contra vazamento do BANCO — dump, backup, replica."""
    save_api_key(db_session, CHAVE)
    db_session.commit()

    gravado = SystemSettingRepository(db_session).get(JEV_API_KEY_SETTING)

    assert gravado
    assert CHAVE not in gravado
    assert gravado != CHAVE
    assert decrypt_secret(gravado) == CHAVE


def test_the_setting_name_warns_it_is_encrypted(db_session) -> None:
    """Quem abrir a tabela precisa saber antes de tentar usar o campo."""
    assert JEV_API_KEY_SETTING.endswith("_encrypted")


def test_an_unreadable_key_is_treated_as_absent(db_session) -> None:
    """Material de cifra trocado, ou registro adulterado. Deixar a
    excecao subir derrubaria o snapshot — e ainda assim nao haveria
    chave."""
    SystemSettingRepository(db_session).set(JEV_API_KEY_SETTING, "lixo-nao-cifrado")
    db_session.commit()

    vigente = load_jev_settings(db_session, _settings())

    assert vigente.api_key is None
    assert vigente.configured is False


# --- precedencia ------------------------------------------------------------


def test_the_dashboard_key_overrides_the_env(db_session, monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(settings, "jev_api_key", "do-env-1234567890", raising=False)

    save_api_key(db_session, CHAVE)
    db_session.commit()

    vigente = load_jev_settings(db_session, settings)

    assert vigente.api_key == CHAVE
    assert vigente.key_source == "dashboard"


def test_without_a_dashboard_key_the_env_still_works(db_session, monkeypatch) -> None:
    """Quem ja tem o `.env` ajustado nao ve mudanca ate mexer no painel."""
    settings = _settings()
    monkeypatch.setattr(settings, "jev_api_key", "do-env-1234567890", raising=False)

    vigente = load_jev_settings(db_session, settings)

    assert vigente.api_key == "do-env-1234567890"
    assert vigente.key_source == "env"


def test_removing_falls_back_to_the_env(db_session, monkeypatch) -> None:
    """"Removi o que eu tinha posto no painel" volta ao `.env`, nao a
    lugar nenhum."""
    settings = _settings()
    monkeypatch.setattr(settings, "jev_api_key", "do-env-1234567890", raising=False)
    save_api_key(db_session, CHAVE)
    db_session.commit()

    clear_api_key(db_session)
    db_session.commit()

    vigente = load_jev_settings(db_session, settings)
    assert vigente.api_key == "do-env-1234567890"
    assert vigente.key_source == "env"


def test_the_toggle_also_overrides_the_env(db_session, monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(settings, "jev_enabled", False, raising=False)

    set_enabled(db_session, True)
    db_session.commit()

    vigente = load_jev_settings(db_session, settings)
    assert vigente.enabled is True
    assert vigente.enabled_source == "dashboard"


# --- ligado e configurado sao coisas diferentes -----------------------------


def test_enabled_without_a_key_is_not_active(db_session, monkeypatch) -> None:
    """A tela precisa poder dizer QUAL dos dois falta: ligado sem chave
    nao classifica nada, e "desligado" seria uma explicacao errada."""
    settings = _settings()
    monkeypatch.setattr(settings, "jev_api_key", None, raising=False)
    set_enabled(db_session, True)
    db_session.commit()

    vigente = load_jev_settings(db_session, settings)

    assert vigente.enabled is True
    assert vigente.configured is False
    assert vigente.active is False


def test_a_key_without_the_toggle_is_not_active(db_session, monkeypatch) -> None:
    settings = _settings()
    monkeypatch.setattr(settings, "jev_enabled", False, raising=False)
    save_api_key(db_session, CHAVE)
    db_session.commit()

    vigente = load_jev_settings(db_session, settings)

    assert vigente.configured is True
    assert vigente.active is False


# --- validacao e dica -------------------------------------------------------


def test_a_truncated_key_is_refused_at_the_form(db_session) -> None:
    """Colagem incompleta so falharia na proxima chamada, com 401, longe
    do campo onde o erro foi cometido."""
    with pytest.raises(ValueError, match="incompleta"):
        save_api_key(db_session, "curta")


def test_the_minimum_is_short_enough_to_not_reject_real_keys() -> None:
    assert MIN_KEY_LENGTH <= 20


def test_only_the_last_four_characters_are_exposed(db_session) -> None:
    """Identifica QUAL chave esta em uso quando ha mais de uma conta, sem
    revelar o resto."""
    save_api_key(db_session, CHAVE)
    db_session.commit()

    vigente = load_jev_settings(db_session, _settings())

    assert vigente.key_hint == CHAVE[-4:]
    assert len(vigente.key_hint) == 4
    assert CHAVE[:-4] not in vigente.key_hint


def test_the_fingerprint_changes_with_the_key_and_hides_it(db_session) -> None:
    """Entra na assinatura do cache do factory: sem ela, trocar a chave
    manteria o cliente antigo e o operador veria 401 com a chave nova ja
    salva. Hash, e nao a chave, porque a tupla vive na memoria do
    processo e um `repr` de depuracao a imprimiria inteira."""
    settings = _settings()

    save_api_key(db_session, CHAVE)
    db_session.commit()
    primeira = load_jev_settings(db_session, settings).fingerprint()

    save_api_key(db_session, CHAVE + "XYZ")
    db_session.commit()
    segunda = load_jev_settings(db_session, settings).fingerprint()

    assert primeira != segunda
    assert CHAVE not in primeira
    assert CHAVE not in segunda


def test_the_fingerprint_also_tracks_the_toggle(db_session) -> None:
    settings = _settings()
    save_api_key(db_session, CHAVE)
    set_enabled(db_session, True)
    db_session.commit()
    ligado = load_jev_settings(db_session, settings).fingerprint()

    set_enabled(db_session, False)
    db_session.commit()
    desligado = load_jev_settings(db_session, settings).fingerprint()

    assert ligado != desligado
