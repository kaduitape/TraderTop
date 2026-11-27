"""O agendador: quando roda, quando NAO roda, e o que nunca pode fazer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.maintenance.scheduler import (
    LAST_RUN_KEY,
    RetentionScheduler,
    due,
    read_last_run,
    write_last_run,
)

AGORA = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)


def _settings(**extra) -> Settings:
    base = {
        "app_env": "production",
        "app_secret_key": "x" * 32,
        "retention_enabled": True,
        "retention_interval_hours": 24,
    }
    return Settings(**{**base, **extra})


# --- quando esta na hora ----------------------------------------------------


def test_never_run_is_due() -> None:
    assert due(None, now=AGORA, interval_hours=24) is True


def test_inside_the_interval_is_not_due() -> None:
    assert due(AGORA - timedelta(hours=5), now=AGORA, interval_hours=24) is False


def test_past_the_interval_is_due() -> None:
    assert due(AGORA - timedelta(hours=25), now=AGORA, interval_hours=24) is True


def test_a_clock_that_went_backwards_does_not_freeze_retention() -> None:
    """Correcao de NTP ou restore de banco pode deixar um horario FUTURO
    gravado. Sem este caso, a retencao ficaria parada ate o futuro chegar —
    e e justamente quando o disco ja esta apertado que ninguem percebe."""
    assert due(AGORA + timedelta(days=3), now=AGORA, interval_hours=24) is True


# --- o registro em banco ----------------------------------------------------


@pytest.fixture
def _limpa_marcador(db_session):
    from app.database.models.system_setting import SystemSetting

    def apagar() -> None:
        db_session.query(SystemSetting).filter(SystemSetting.key == LAST_RUN_KEY).delete(
            synchronize_session=False
        )
        db_session.commit()

    apagar()
    yield
    apagar()


def test_the_marker_survives_a_restart(db_session, _limpa_marcador) -> None:
    """Em memoria, um container que reinicia de hora em hora dispararia uma
    purga por reinicio."""
    write_last_run(db_session, AGORA)
    db_session.commit()

    assert read_last_run(db_session) == AGORA


def test_a_naive_marker_is_read_as_utc(db_session, _limpa_marcador) -> None:
    from app.database.repositories.system_setting_repository import SystemSettingRepository

    SystemSettingRepository(db_session).set(LAST_RUN_KEY, "2026-11-27T03:00:00")
    db_session.commit()

    lido = read_last_run(db_session)
    assert lido is not None and lido.tzinfo is not None
    assert lido == AGORA


def test_a_corrupted_marker_counts_as_never_run(db_session, _limpa_marcador) -> None:
    """Melhor uma purga a mais do que uma tabela que nunca mais e limpa
    porque alguem editou a linha a mao."""
    from app.database.repositories.system_setting_repository import SystemSettingRepository

    SystemSettingRepository(db_session).set(LAST_RUN_KEY, "ontem de tarde")
    db_session.commit()

    assert read_last_run(db_session) is None


# --- o que o agendador recusa a fazer ---------------------------------------


def test_it_never_starts_in_the_test_suite() -> None:
    """O lifespan dispara no `TestClient`. Uma thread apagando linhas em
    paralelo com os testes produziria falha intermitente — a pior especie."""
    pode, motivo = RetentionScheduler(_settings(app_env="test")).should_start()

    assert pode is False
    assert motivo == "APP_ENV=test"


def test_the_switch_turns_it_off() -> None:
    pode, motivo = RetentionScheduler(_settings(retention_enabled=False)).should_start()

    assert pode is False
    assert motivo == "RETENTION_ENABLED=false"


def test_an_invalid_policy_disables_it_instead_of_crashing_the_api() -> None:
    """Configuracao errada nao pode derrubar o servidor web: a retencao e
    manutencao, a API e producao."""
    agendador = RetentionScheduler(
        _settings(quality_event_retention_days=30, quality_event_critical_retention_days=7)
    )
    pode, motivo = agendador.should_start()

    assert pode is False
    assert "nao pode ser menor" in motivo
    assert agendador.start() is False
    assert agendador.running is False


def test_a_sane_production_config_does_start() -> None:
    agendador = RetentionScheduler(_settings())
    pode, motivo = agendador.should_start()

    assert pode is True
    assert motivo == ""


def test_the_default_configuration_is_startable() -> None:
    """O default do `.env.example` e do compose tem de ser uma politica
    valida: se nao for, a retencao nasce desligada em producao e ninguem
    descobre ate o disco encher."""
    pode, _motivo = RetentionScheduler(_settings()).should_start()

    assert pode is True
