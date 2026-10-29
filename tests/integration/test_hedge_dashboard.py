"""O painel do Hedge AI renderizado de verdade.

Erro de template so aparece renderizando: testar o servico sozinho
deixaria a tela quebrada passar. Por isso cada teste aqui faz um GET real
e olha o HTML.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.database.repositories.hedge_repository import HedgeRepository


@pytest.fixture(autouse=True)
def _limpa(engine):
    del engine

    def apagar() -> None:
        from app.database.models.api_token import ApiToken
        from app.database.models.audit_log import AuditLog
        from app.database.models.hedge import (
            HedgeAnalysis,
            HedgeSession,
            HedgeSetup,
            HedgeSetupVersion,
        )
        from app.database.session import get_session_factory

        sessao = get_session_factory()()
        try:
            sessao.query(HedgeAnalysis).delete()
            sessao.query(HedgeSession).delete()
            sessao.query(HedgeSetupVersion).delete()
            sessao.query(HedgeSetup).delete()
            sessao.query(ApiToken).delete()
            sessao.query(AuditLog).filter(
                AuditLog.action.like("hedge_%")
            ).delete(synchronize_session=False)
            sessao.commit()
        finally:
            sessao.close()

    apagar()
    yield
    apagar()


_PARAMS_MIN = {
    "desk_symbol": "MNQ",
    "desk_is_long": True,
    "desk_entry": Decimal("20000"),
    "desk_take": Decimal("20100"),
    "desk_stop": Decimal("19950"),
    "desk_contracts": Decimal("1"),
    "level_unit": "PRICE",
    "desk_tick_size": Decimal("0.25"),
    "desk_tick_value": Decimal("0.50"),
    "desk_point": Decimal("0.01"),
    "broker_symbol": "MNQ",
    "account_currency": "USD",
}


def _form(**overrides) -> dict:
    base = {
        "name": "Cobertura MNQ",
        "desk_symbol": "MNQ",
        "desk_direction": "LONG",
        "desk_entry": "20000",
        "desk_take": "20100",
        "desk_stop": "19950",
        "desk_contracts": "1",
        "level_unit": "PRICE",
        "desk_tick_size": "0.25",
        "desk_tick_value": "0.50",
        "desk_point": "0.01",
        "broker_symbol": "MNQ",
        "coverage_ratio": "1",
        "account_currency": "USD",
        "horizon_minutes": "60",
        "objective": "FAVOR_DESK_TAKE",
        "mode": "SIMULATION",
    }
    base.update(overrides)
    return base


@pytest.fixture
def hedge_user(db_session, request) -> int:
    """Um usuario por teste, para que setups de um nao vazem no outro."""
    from app.core.security import hash_password
    from app.database.repositories.user_repository import UserRepository

    nome = f"hedge_ui_{abs(hash(request.node.name)) % 10**8}"
    repo = UserRepository(db_session)
    papel = repo.get_or_create_role("ADMIN")
    user = repo.create_user(
        username=nome,
        email=f"{nome}@example.com",
        password_hash=hash_password("Sup3rSecret!"),
        roles=[papel],
    )
    db_session.commit()
    request.node.stash_username = nome  # type: ignore[attr-defined]
    return user.id


@pytest.fixture
def logged_in(client, db_session, hedge_user, request):
    nome = request.node.stash_username  # type: ignore[attr-defined]
    client.post(
        "/login",
        data={"username": nome, "password": "Sup3rSecret!"},
        follow_redirects=False,
    )
    return client


def _usuario_id(db_session, user_id: int | None = None) -> int:
    assert user_id is not None, "passe hedge_user"
    return user_id


# --- render -----------------------------------------------------------------


def test_the_page_renders_with_no_setups(logged_in) -> None:
    """Tela vazia e o primeiro estado que todo mundo ve."""
    resposta = logged_in.get("/dashboard/hedge")

    assert resposta.status_code == 200
    assert "Hedge AI" in resposta.text
    assert "Nenhum setup ainda" in resposta.text


def test_the_six_areas_are_present(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    for aba in ("tab-visao", "tab-setups", "tab-chaves", "tab-vivo", "tab-hist", "tab-jev"):
        assert aba in html, aba


def test_the_page_says_it_sends_no_orders(logged_in) -> None:
    """O operador precisa ler isso na tela, nao so na documentacao."""
    html = logged_in.get("/dashboard/hedge").text

    assert "não envia" in html.lower() or "nao envia" in html.lower()


def test_the_page_calls_the_model_theoretical(logged_in) -> None:
    """O vocabulario tem de chegar a tela: um baseline apresentado como
    previsao validada seria falso."""
    html = logged_in.get("/dashboard/hedge").text

    assert "THEORETICAL_BASELINE" in html
    assert "teóricas" in html or "teoricas" in html


# --- setups -----------------------------------------------------------------


def test_creating_a_setup_shows_it_on_the_page(logged_in) -> None:
    logged_in.post("/dashboard/hedge/setup", data=_form(), follow_redirects=True)

    html = logged_in.get("/dashboard/hedge").text

    assert "Cobertura MNQ" in html
    assert "20100" in html


def test_levels_in_ticks_become_prices(logged_in, db_session, hedge_user) -> None:
    """A conversao acontece UMA vez, no painel, e o preco resultante fica
    gravado. 20 ticks de 0.25 sao 5 pontos, nao 20."""
    logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(level_unit="TICKS", desk_take="400", desk_stop="200"),
        follow_redirects=True,
    )

    repo = HedgeRepository(db_session)
    setup = repo.list_setups(user_id=_usuario_id(db_session, hedge_user))[0]
    versao = repo.current_version(setup)

    assert versao.desk_take == Decimal("20100.00")   # 400 * 0.25 acima
    assert versao.desk_stop == Decimal("19950.00")   # 200 * 0.25 abaixo
    assert versao.level_unit == "TICKS"              # a unidade original fica


def test_a_short_setup_puts_the_take_below_the_entry(logged_in, db_session, hedge_user) -> None:
    logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(desk_direction="SHORT", level_unit="TICKS", desk_take="400", desk_stop="200"),
        follow_redirects=True,
    )

    repo = HedgeRepository(db_session)
    versao = repo.current_version(repo.list_setups(user_id=_usuario_id(db_session, hedge_user))[0])

    assert versao.desk_take < versao.desk_entry
    assert versao.desk_stop > versao.desk_entry


def test_editing_creates_a_new_version_instead_of_overwriting(logged_in, db_session, hedge_user) -> None:
    """O historico de analises aponta para a versao sob a qual foi
    calculado. Reescrever faria o passado descrever outra operacao."""
    logged_in.post("/dashboard/hedge/setup", data=_form(), follow_redirects=True)

    repo = HedgeRepository(db_session)
    setup = repo.list_setups(user_id=_usuario_id(db_session, hedge_user))[0]

    logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(setup_id=str(setup.id), desk_stop="19900", change_note="stop afastado"),
        follow_redirects=True,
    )

    db_session.expire_all()
    versoes = repo.list_versions(setup.id)

    assert len(versoes) == 2
    assert repo.get_setup(setup.id, user_id=_usuario_id(db_session, hedge_user)).current_version == 2
    assert versoes[0].change_note == "stop afastado"
    # A versao 1 permanece intacta.
    assert versoes[1].desk_stop == Decimal("19950")


def test_a_closed_setup_refuses_new_versions(logged_in, db_session, hedge_user) -> None:
    logged_in.post("/dashboard/hedge/setup", data=_form(), follow_redirects=True)
    repo = HedgeRepository(db_session)
    setup = repo.list_setups(user_id=_usuario_id(db_session, hedge_user))[0]

    logged_in.post(
        "/dashboard/hedge/setup/close", data={"setup_id": setup.id}, follow_redirects=True
    )
    resposta = logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(setup_id=str(setup.id), desk_stop="19900"),
        follow_redirects=True,
    )

    assert "encerrado" in resposta.text


def test_inverted_levels_are_rejected_before_saving(logged_in, db_session, hedge_user) -> None:
    """Em PRICE nada impede trocar take com stop. O painel recusa antes de
    gravar — nao adianta o motor recusar depois, com o setup invalido ja no
    banco, visivel na lista e pronto para ser anexado a um grafico."""
    resposta = logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(level_unit="PRICE", desk_take="19950", desk_stop="20100"),
        follow_redirects=True,
    )

    assert "invertidos" in resposta.text
    assert HedgeRepository(db_session).list_setups(user_id=hedge_user) == []


# --- chaves -----------------------------------------------------------------


def test_a_new_key_is_shown_once_and_has_hedge_scopes(logged_in, db_session) -> None:
    resposta = logged_in.post(
        "/dashboard/hedge/key", data={"name": "notebook"}, follow_redirects=True
    )

    assert "Guarde esta chave agora" in resposta.text

    from app.database.repositories.api_token_repository import ApiTokenRepository

    chave = ApiTokenRepository(db_session).list_all()[0]
    assert chave.has_scope("hedge:telemetry")
    assert chave.has_scope("hedge:setup:read")
    assert chave.has_scope("hedge:analysis")


def test_the_secret_is_not_stored_in_the_database(logged_in, db_session) -> None:
    """O valor em claro existe na resposta que o cria e nunca mais."""
    import re

    resposta = logged_in.post(
        "/dashboard/hedge/key", data={"name": "notebook"}, follow_redirects=True
    )
    achado = re.search(r"(tt_[A-Za-z0-9_\-]{20,})", resposta.text)
    assert achado, "a chave deveria aparecer uma vez"
    segredo = achado.group(1)

    from app.database.repositories.api_token_repository import ApiTokenRepository

    chave = ApiTokenRepository(db_session).list_all()[0]
    assert chave.token_hash != segredo
    assert segredo not in chave.token_hash
    assert chave.prefix == segredo[:8]


def test_the_secret_does_not_survive_a_reload(logged_in) -> None:
    """Ele viaja uma vez, na URL do redirect, e some na navegacao
    seguinte."""
    logged_in.post("/dashboard/hedge/key", data={"name": "notebook"}, follow_redirects=True)

    html = logged_in.get("/dashboard/hedge").text

    assert "Guarde esta chave agora" not in html


def test_the_secret_never_reaches_the_audit_log(logged_in, db_session) -> None:
    import re

    resposta = logged_in.post(
        "/dashboard/hedge/key", data={"name": "notebook"}, follow_redirects=True
    )
    segredo = re.search(r"(tt_[A-Za-z0-9_\-]{20,})", resposta.text).group(1)

    from app.database.models.audit_log import AuditLog

    registros = db_session.query(AuditLog).filter(AuditLog.action == "hedge_key_create").all()

    assert registros
    for registro in registros:
        assert segredo not in (registro.detail or "")
        # So o prefixo, que identifica sem revelar.
        assert segredo[:8] in registro.detail


def test_revoking_a_key_marks_it_inactive(logged_in, db_session) -> None:
    logged_in.post("/dashboard/hedge/key", data={"name": "notebook"}, follow_redirects=True)

    from app.database.repositories.api_token_repository import ApiTokenRepository

    chave = ApiTokenRepository(db_session).list_all()[0]
    logged_in.post(
        "/dashboard/hedge/key/revoke", data={"token_id": chave.id}, follow_redirects=True
    )

    db_session.expire_all()
    assert ApiTokenRepository(db_session).list_all()[0].is_active is False


def test_the_page_warns_about_sharing_config_files(logged_in) -> None:
    """Arquivo `.set` viaja em claro junto com o resto da configuracao, e
    e para onde a chave vai parar se ninguem avisar."""
    html = logged_in.get("/dashboard/hedge").text

    assert ".set" in html


def test_the_page_shows_the_webrequest_url_to_authorize(logged_in) -> None:
    """A causa numero um de "nao funciona" e a URL nao liberada."""
    html = logged_in.get("/dashboard/hedge").text

    assert "WebRequest" in html
    assert "Strategy Tester" in html


# --- aba do JEV -------------------------------------------------------------


def test_the_jev_tab_lists_the_four_categories(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    for categoria in (
        "CALM",
        "EVENT_RISK_NEAR",
        "ELEVATED_UNCERTAINTY",
        "INSUFFICIENT_CONTEXT",
    ):
        assert categoria in html, categoria


def test_the_jev_tab_shows_the_daily_usage(logged_in) -> None:
    """O operador precisa poder ver quanto ja gastou antes de descobrir
    pela cota esgotada."""
    html = logged_in.get("/dashboard/hedge").text

    assert "Consumo hoje" in html


def test_the_jev_tab_states_observation_mode(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    assert "observação" in html or "observacao" in html
    assert "não altera as probabilidades" in html or "nao altera as probabilidades" in html


# --- download dos fontes MQL5 ----------------------------------------------


def test_the_zip_carries_the_four_files_in_the_right_folders(logged_in) -> None:
    """A estrutura e o ponto: um `.mqh` fora de Include/TraderTop/ nao e
    encontrado pelo `#include`, e o erro de compilacao nao diz onde ele
    deveria estar."""
    import io
    import zipfile

    resposta = logged_in.get("/dashboard/hedge/download")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"] == "application/zip"
    assert "TraderTopHedge-MQL5.zip" in resposta.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(resposta.content)) as zf:
        nomes = set(zf.namelist())

    assert nomes == {
        "MQL5/Experts/TraderTopHedgeConnector.mq5",
        "MQL5/Indicators/TraderTopHedgeIndicator.mq5",
        "MQL5/Include/TraderTop/HedgeChannel.mqh",
        "MQL5/Include/TraderTop/HedgeJson.mqh",
        "LEIA-ME.txt",
    }


def test_the_zip_contents_match_the_sources(logged_in) -> None:
    """Entregar o arquivo errado seria pior que nao entregar: o operador
    compila, anexa, e o erro aparece como schema incompativel."""
    import io
    import pathlib
    import zipfile

    with zipfile.ZipFile(io.BytesIO(logged_in.get("/dashboard/hedge/download").content)) as zf:
        entregue = zf.read("MQL5/Experts/TraderTopHedgeConnector.mq5").decode()

    original = pathlib.Path("scripts/mql5/TraderTopHedgeConnector.mq5").read_text()
    assert entregue == original


def test_the_readme_warns_about_the_three_silent_failures(logged_in) -> None:
    """URL nao liberada, AutoTrading desligado e Strategy Tester. As tres
    falham sem mensagem util no terminal."""
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(logged_in.get("/dashboard/hedge/download").content)) as zf:
        leiame = zf.read("LEIA-ME.txt").decode()

    assert "WebRequest" in leiame
    assert "4014" in leiame
    assert "AutoTrading" in leiame
    assert "Strategy Tester" in leiame
    assert "NAO ENVIA ORDENS" in leiame


@pytest.mark.parametrize(
    ("qual", "nome"),
    [
        ("connector", "TraderTopHedgeConnector.mq5"),
        ("indicator", "TraderTopHedgeIndicator.mq5"),
        ("channel", "HedgeChannel.mqh"),
        ("json", "HedgeJson.mqh"),
    ],
)
def test_each_file_downloads_on_its_own(logged_in, qual: str, nome: str) -> None:
    resposta = logged_in.get(f"/dashboard/hedge/download/{qual}")

    assert resposta.status_code == 200
    assert nome in resposta.headers["content-disposition"]
    assert len(resposta.content) > 500


def test_an_unknown_file_is_refused(logged_in) -> None:
    assert logged_in.get("/dashboard/hedge/download/../../etc/passwd").status_code == 404
    assert logged_in.get("/dashboard/hedge/download/qualquer").status_code == 404


def test_the_download_needs_a_login(client) -> None:
    """Os fontes nao sao segredo, mas a rota nao tem por que ser publica —
    e a sessao ja existe em todo caminho que leva ate ela."""
    resposta = client.get("/dashboard/hedge/download", follow_redirects=False)

    assert resposta.status_code in (302, 303, 401, 403)


def test_the_panel_offers_the_download(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    assert "/dashboard/hedge/download" in html
    assert "Baixar tudo" in html


# --- chave do JEV pelo painel -----------------------------------------------

CHAVE_JEV = "tsk_exemplo_de_chave_do_jev_1234567890"


@pytest.fixture(autouse=True)
def _limpa_jev(db_session):
    from app.database.models.system_setting import SystemSetting

    def apagar() -> None:
        db_session.query(SystemSetting).filter(
            SystemSetting.key.in_(("jev_api_key_encrypted", "jev_enabled"))
        ).delete(synchronize_session=False)
        db_session.commit()

    apagar()
    yield
    apagar()


def test_the_panel_offers_the_key_field(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    assert 'name="jev_api_key"' in html
    assert 'type="password"' in html
    assert "/dashboard/hedge/jev" in html


def test_saving_a_key_stores_it_encrypted(logged_in, db_session) -> None:
    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )

    from app.database.repositories.system_setting_repository import SystemSettingRepository

    gravado = SystemSettingRepository(db_session).get("jev_api_key_encrypted")

    assert gravado
    assert CHAVE_JEV not in gravado


def test_the_key_never_comes_back_to_the_screen(logged_in) -> None:
    """Nem no campo, nem num atributo escondido, nem na confirmacao."""
    resposta = logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )

    assert CHAVE_JEV not in resposta.text
    assert CHAVE_JEV not in logged_in.get("/dashboard/hedge").text


def test_only_the_last_four_are_shown(logged_in) -> None:
    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )

    html = logged_in.get("/dashboard/hedge").text

    assert CHAVE_JEV[-4:] in html
    assert CHAVE_JEV[:20] not in html


def test_an_empty_field_keeps_the_current_key(logged_in, db_session) -> None:
    """A regra que impede salvar o liga/desliga de apagar a credencial.
    E a mesma que vale para a senha do MT5 neste projeto."""
    from app.database.repositories.system_setting_repository import SystemSettingRepository

    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )
    antes = SystemSettingRepository(db_session).get("jev_api_key_encrypted")

    # Salva so o toggle, campo da chave vazio.
    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "", "jev_api_key": ""},
        follow_redirects=True,
    )
    db_session.expire_all()
    depois = SystemSettingRepository(db_session).get("jev_api_key_encrypted")

    assert depois == antes, "salvar o toggle apagou a chave"


def test_removing_the_key_is_an_explicit_action(logged_in, db_session) -> None:
    from app.database.repositories.system_setting_repository import SystemSettingRepository

    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )
    logged_in.post("/dashboard/hedge/jev/remove", follow_redirects=True)

    db_session.expire_all()
    assert not SystemSettingRepository(db_session).get("jev_api_key_encrypted")


def test_a_truncated_key_is_refused_with_a_reason(logged_in) -> None:
    resposta = logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": "curta"},
        follow_redirects=True,
    )

    assert "incompleta" in resposta.text


def test_the_key_never_reaches_the_audit_log(logged_in, db_session) -> None:
    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )

    from app.database.models.audit_log import AuditLog

    registros = db_session.query(AuditLog).filter(
        AuditLog.action == "hedge_jev_config"
    ).all()

    assert registros
    for registro in registros:
        assert CHAVE_JEV not in (registro.detail or "")
        assert CHAVE_JEV[-4:] in registro.detail


def test_enabled_without_a_key_says_which_one_is_missing(logged_in) -> None:
    """"Desligado" seria uma explicacao errada para quem ligou e nao
    cadastrou a chave."""
    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": ""},
        follow_redirects=True,
    )

    html = logged_in.get("/dashboard/hedge").text

    assert "sem chave" in html


def test_the_test_button_needs_a_key_first(logged_in) -> None:
    resposta = logged_in.post("/dashboard/hedge/jev/test", follow_redirects=True)

    assert "Cadastre a chave antes de testar" in resposta.text


def test_a_failing_test_reports_without_leaking(logged_in, monkeypatch) -> None:
    """O teste de conexao faz uma chamada real. Quando ela falha, o
    operador precisa do motivo — e nada alem dele."""
    import httpx

    from app.jev import client as modulo

    class _Recusa(modulo.JevClient):
        def system_one(self, **_kwargs):
            raise modulo.JevUnavailable(
                modulo.JevFailure.UNAUTHORIZED, "chave do Jev nao aceita"
            )

    monkeypatch.setattr("app.api.routes.hedge_dashboard.JevClient", _Recusa)
    del httpx

    logged_in.post(
        "/dashboard/hedge/jev",
        data={"jev_enabled": "1", "jev_api_key": CHAVE_JEV},
        follow_redirects=True,
    )
    resposta = logged_in.post("/dashboard/hedge/jev/test", follow_redirects=True)

    assert "nao aceita" in resposta.text
    assert CHAVE_JEV not in resposta.text


# --- criar x editar: eram o mesmo estado, e nao sao ------------------------


def test_a_second_setup_can_be_created(logged_in, db_session, hedge_user) -> None:
    """O bug que tornava o formulario confuso: `setup_id` vinha sempre
    preenchido com o setup selecionado, entao depois do primeiro nao havia
    como criar um segundo — todo Salvar virava uma versao nova."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Primeiro"), follow_redirects=True)
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Segundo"), follow_redirects=True)

    nomes = {s.name for s in HedgeRepository(db_session).list_setups(user_id=hedge_user)}

    assert nomes == {"Primeiro", "Segundo"}


def test_the_form_creates_by_default(logged_in) -> None:
    """Sem `?edit=`, o formulario cria. Ele nao pode vir preso a um setup
    so porque existe um na lista."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Existente"), follow_redirects=True)

    html = logged_in.get("/dashboard/hedge").text

    assert "Novo setup" in html
    assert 'name="setup_id"' not in html.split('id="setup-form"')[1].split("</form>")[0]


def test_edit_mode_is_explicit_and_cancellable(logged_in, db_session, hedge_user) -> None:
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Alvo"), follow_redirects=True)
    setup = HedgeRepository(db_session).list_setups(user_id=hedge_user)[0]

    html = logged_in.get(f"/dashboard/hedge?edit={setup.id}").text

    assert "Editando" in html
    assert "Alvo" in html
    assert f'name="setup_id" value="{setup.id}"' in html
    assert "cancelar" in html


def test_the_button_says_which_version_it_will_create(logged_in, db_session, hedge_user) -> None:
    """"Salvar" sozinho nao diz que o resultado e uma versao nova."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Alvo"), follow_redirects=True)
    setup = HedgeRepository(db_session).list_setups(user_id=hedge_user)[0]

    html = logged_in.get(f"/dashboard/hedge?edit={setup.id}").text

    assert "Salvar versão 2" in html


def test_renaming_on_edit_actually_renames(logged_in, db_session, hedge_user) -> None:
    """Antes o campo aceitava o texto novo, a tela confirmava o salvamento
    e o nome continuava o antigo — sem erro, sem aviso."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Nome antigo"), follow_redirects=True)
    repo = HedgeRepository(db_session)
    setup = repo.list_setups(user_id=hedge_user)[0]

    logged_in.post(
        "/dashboard/hedge/setup",
        data=_form(name="Nome novo", setup_id=str(setup.id)),
        follow_redirects=True,
    )

    db_session.expire_all()
    assert repo.get_setup(setup.id, user_id=hedge_user).name == "Nome novo"


def test_a_closed_setup_falls_back_to_create_mode(logged_in, db_session, hedge_user) -> None:
    """Encerrado nao recebe alteracao. Abrir o formulario nele deixaria o
    operador preencher tudo para receber um erro no fim."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Fechado"), follow_redirects=True)
    setup = HedgeRepository(db_session).list_setups(user_id=hedge_user)[0]
    logged_in.post(
        "/dashboard/hedge/setup/close", data={"setup_id": setup.id}, follow_redirects=True
    )

    html = logged_in.get(f"/dashboard/hedge?edit={setup.id}").text

    assert "Novo setup" in html
    assert "Editando" not in html


def test_another_users_setup_cannot_be_edited(logged_in, db_session, hedge_user) -> None:
    """`?edit=` e um parametro da URL: trocar o numero nao pode abrir o
    setup de outra pessoa."""
    from app.core.security import hash_password
    from app.database.repositories.user_repository import UserRepository

    outro = UserRepository(db_session).create_user(
        username="hedge_outro_dono",
        email="outro@example.com",
        password_hash=hash_password("Sup3rSecret!"),
    )
    db_session.commit()
    alheio, _ = HedgeRepository(db_session).create_setup(
        user_id=outro.id, name="Do outro", mode="SIMULATION",
        objective="FAVOR_DESK_TAKE", params=_PARAMS_MIN,
    )
    db_session.commit()

    html = logged_in.get(f"/dashboard/hedge?edit={alheio.id}").text

    assert "Do outro" not in html
    assert "Editando" not in html


def test_browsing_history_does_not_put_the_form_in_edit_mode(logged_in, db_session, hedge_user) -> None:
    """`?setup=` escolhe o que o HISTORICO mostra; `?edit=` o que o
    formulario altera. Eram a mesma coisa."""
    logged_in.post("/dashboard/hedge/setup", data=_form(name="Olhando"), follow_redirects=True)
    setup = HedgeRepository(db_session).list_setups(user_id=hedge_user)[0]

    html = logged_in.get(f"/dashboard/hedge?setup={setup.id}").text

    assert "Novo setup" in html
    assert "Editando" not in html


# --- o formulario em si -----------------------------------------------------


def test_the_advanced_fields_are_collapsed_when_creating(logged_in) -> None:
    """Vinte e tres campos de uma vez sao a definicao de confuso. O
    essencial fica visivel; especificacao, custos e prazo ficam atras de
    um `details`."""
    html = logged_in.get("/dashboard/hedge").text

    assert "Ajustes avançados" in html
    assert "<details" in html


def test_the_conversion_preview_exists(logged_in) -> None:
    """A conversao precisa aparecer ENQUANTO se digita: depois de salvar
    o operador ja confirmou um nivel sem saber em que preço ele cai."""
    html = logged_in.get("/dashboard/hedge").text

    assert 'id="preview"' in html
    assert 'id="pv-take"' in html


def test_the_tick_versus_point_trap_is_explained_where_it_is_set(logged_in) -> None:
    html = logged_in.get("/dashboard/hedge").text

    assert "menor passo negociável" in html
    assert "25×" in html
