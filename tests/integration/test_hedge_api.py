"""O contrato HTTPS do Hedge AI e as chaves que o protegem.

O risco que domina aqui nao e numero errado: e uma chave que vive num
terminal MetaTrader de um cliente alcancando coisa que nao deveria. Por
isso o arquivo comeca pelo isolamento e pela revogacao, e so depois
verifica o formato.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.api.dependencies.api_token import (
    SCOPE_ANALYSIS,
    SCOPE_SETUP_READ,
    SCOPE_TELEMETRY,
)
from app.database.repositories.api_token_repository import ApiTokenRepository
from app.database.repositories.hedge_repository import HedgeRepository
from app.hedge.ratelimit import snapshot_limiter

TODOS_ESCOPOS = f"{SCOPE_TELEMETRY},{SCOPE_SETUP_READ},{SCOPE_ANALYSIS}"


@pytest.fixture(autouse=True)
def _limpa(engine):
    del engine

    def apagar() -> None:
        from app.database.models.api_token import ApiToken
        from app.database.models.hedge import (
            HedgeAnalysis,
            HedgeSession,
            HedgeSetup,
            HedgeSetupVersion,
        )
        from app.database.models.user import User
        from app.database.session import get_session_factory

        sessao = get_session_factory()()
        try:
            sessao.query(HedgeAnalysis).delete()
            sessao.query(HedgeSession).delete()
            sessao.query(HedgeSetupVersion).delete()
            sessao.query(HedgeSetup).delete()
            sessao.query(ApiToken).delete()
            sessao.query(User).filter(User.username.like("hedge_%")).delete(
                synchronize_session=False
            )
            sessao.commit()
        finally:
            sessao.close()

    apagar()
    snapshot_limiter.reset()
    yield
    apagar()
    snapshot_limiter.reset()


def _usuario(db_session, nome: str):
    from app.core.security import hash_password
    from app.database.repositories.user_repository import UserRepository

    return UserRepository(db_session).create_user(
        username=nome, email=f"{nome}@exemplo.test", password_hash=hash_password("x" * 12)
    )


def _chave(db_session, user_id: int, *, scopes: str = TODOS_ESCOPOS) -> str:
    token, segredo = ApiTokenRepository(db_session).create(
        name="conector de teste", user_id=user_id
    )
    token.scopes = scopes
    db_session.commit()
    return segredo


_PARAMS = {
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
    "coverage_ratio": Decimal("1"),
    "account_currency": "USD",
    "horizon_seconds": 3600,
    "data_max_age_seconds": 30,
}


def _setup(db_session, user_id: int, *, nome: str = "Cobertura MNQ", **extra):
    params = {**_PARAMS, **extra}
    setup, _versao = HedgeRepository(db_session).create_setup(
        user_id=user_id,
        name=nome,
        mode="SIMULATION",
        objective="FAVOR_DESK_TAKE",
        params=params,
    )
    db_session.commit()
    return setup


def _cabecalho(chave: str) -> dict:
    return {"X-API-Key": chave}


def _corpo(setup_id: int, *, seq: int = 1, bid: str = "20000", ask: str = "20000.25") -> dict:
    return {
        "setup_id": setup_id,
        "terminal_id": "T1",
        "chart_id": "C1",
        "symbol": "MNQ",
        "sequence": seq,
        "captured_at": datetime.now(UTC).isoformat(),
        "bid": bid,
        "ask": ask,
        "connector_version": "1.0.0",
        "tick_value": "0.50",
        "tick_size": "0.25",
        "point": "0.01",
        "volume_min": "0.01",
        "volume_max": "100",
        "volume_step": "0.01",
        "contract_size": "1",
        "account_currency": "USD",
        "atr": 12.0,
        "atr_period_seconds": 900.0,
    }


# --- isolamento entre usuarios ---------------------------------------------


def test_a_key_cannot_read_another_users_setup(client, db_session) -> None:
    """O requisito central: a chave de um cliente nao alcanca o setup de
    outro. Vale para setups, analise e historico."""
    dono = _usuario(db_session, "hedge_dono")
    intruso = _usuario(db_session, "hedge_intruso")
    db_session.commit()

    setup = _setup(db_session, dono.id)
    chave_intruso = _chave(db_session, intruso.id)

    for caminho in (
        f"/api/v1/hedge/analysis/{setup.id}",
        f"/api/v1/hedge/history/{setup.id}",
    ):
        assert client.get(caminho, headers=_cabecalho(chave_intruso)).status_code == 404

    resposta = client.post(
        "/api/v1/hedge/snapshot",
        json=_corpo(setup.id),
        headers=_cabecalho(chave_intruso),
    )
    assert resposta.status_code == 404


def test_the_setup_list_shows_only_the_owners_setups(client, db_session) -> None:
    dono = _usuario(db_session, "hedge_a")
    outro = _usuario(db_session, "hedge_b")
    db_session.commit()

    _setup(db_session, dono.id, nome="do dono")
    _setup(db_session, outro.id, nome="do outro")

    dados = client.get(
        "/api/v1/hedge/setups", headers=_cabecalho(_chave(db_session, dono.id))
    ).json()

    assert [s["name"] for s in dados["setups"]] == ["do dono"]


def test_a_missing_setup_and_a_foreign_one_look_identical(client, db_session) -> None:
    """Distinguir "nao existe" de "nao e seu" contaria a uma chave vazada
    quais ids existem."""
    dono = _usuario(db_session, "hedge_dono2")
    intruso = _usuario(db_session, "hedge_intruso2")
    db_session.commit()
    setup = _setup(db_session, dono.id)
    chave = _chave(db_session, intruso.id)

    alheio = client.get(f"/api/v1/hedge/analysis/{setup.id}", headers=_cabecalho(chave))
    inexistente = client.get("/api/v1/hedge/analysis/999999", headers=_cabecalho(chave))

    assert alheio.status_code == inexistente.status_code == 404
    assert alheio.json()["detail"] == inexistente.json()["detail"]


# --- chaves ----------------------------------------------------------------


def test_a_revoked_key_stops_working_immediately(client, db_session) -> None:
    user = _usuario(db_session, "hedge_rev")
    db_session.commit()
    _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    assert client.get("/api/v1/hedge/ping", headers=_cabecalho(chave)).status_code == 200

    token = ApiTokenRepository(db_session).resolve(chave)
    ApiTokenRepository(db_session).revoke(token.id)
    db_session.commit()

    assert client.get("/api/v1/hedge/ping", headers=_cabecalho(chave)).status_code == 401


def test_a_legacy_key_without_scopes_reaches_nothing_in_hedge(client, db_session) -> None:
    """Chave ja emitida nasce sem escopo. Tratar vazio como "pode tudo"
    daria ao parque instalado um acesso que ninguem concedeu."""
    user = _usuario(db_session, "hedge_legado")
    db_session.commit()
    chave = _chave(db_session, user.id, scopes="")

    for caminho in ("/api/v1/hedge/ping", "/api/v1/hedge/setups"):
        assert client.get(caminho, headers=_cabecalho(chave)).status_code == 403


def test_each_scope_is_enforced_separately(client, db_session) -> None:
    """Telemetria nao da acesso a setup, e ler setup nao da acesso a
    analise."""
    user = _usuario(db_session, "hedge_escopo")
    db_session.commit()
    setup = _setup(db_session, user.id)
    so_telemetria = _chave(db_session, user.id, scopes=SCOPE_TELEMETRY)

    assert client.get("/api/v1/hedge/ping", headers=_cabecalho(so_telemetria)).status_code == 200
    assert (
        client.get("/api/v1/hedge/setups", headers=_cabecalho(so_telemetria)).status_code
        == 403
    )
    assert (
        client.get(
            f"/api/v1/hedge/analysis/{setup.id}", headers=_cabecalho(so_telemetria)
        ).status_code
        == 403
    )


def test_a_key_with_no_owner_reaches_no_setup(client, db_session) -> None:
    """Falha fechado: sem dono nao da para decidir de quem sao os setups."""
    token, segredo = ApiTokenRepository(db_session).create(name="orfa", user_id=None)
    token.scopes = TODOS_ESCOPOS
    db_session.commit()

    assert client.get("/api/v1/hedge/ping", headers=_cabecalho(segredo)).status_code == 403


def test_no_secret_comes_back_from_any_endpoint(client, db_session) -> None:
    user = _usuario(db_session, "hedge_segredo")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    for resposta in (
        client.get("/api/v1/hedge/ping", headers=_cabecalho(chave)),
        client.get("/api/v1/hedge/setups", headers=_cabecalho(chave)),
        client.post("/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave)),
    ):
        assert chave not in resposta.text


def test_the_key_never_travels_in_the_url(client, db_session) -> None:
    """Segredo em URL vai para log de servidor, proxy e historico. O
    header existe para isso."""
    user = _usuario(db_session, "hedge_url")
    db_session.commit()
    chave = _chave(db_session, user.id)

    assert client.get(f"/api/v1/hedge/ping?api_key={chave}").status_code == 401


def test_ping_identifies_which_key_is_in_use(client, db_session) -> None:
    """Com varias chaves emitidas, "conectado" sozinho nao diz se e a
    certa. O prefixo identifica sem revelar."""
    user = _usuario(db_session, "hedge_ping")
    db_session.commit()
    chave = _chave(db_session, user.id)

    dados = client.get("/api/v1/hedge/ping", headers=_cabecalho(chave)).json()

    assert dados["ok"] is True
    assert dados["token_prefix"] == chave[:8]
    assert set(dados["scopes"]) == {SCOPE_TELEMETRY, SCOPE_SETUP_READ, SCOPE_ANALYSIS}


# --- snapshot e analise ----------------------------------------------------


def test_a_snapshot_returns_a_complete_analysis(client, db_session) -> None:
    user = _usuario(db_session, "hedge_snap")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    dados = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave)
    ).json()

    assert dados["state"] == "CONNECTED"
    assert dados["schema_version"] == 1
    assert dados["forecast"]["available"] is True
    assert (
        dados["forecast"]["take_first_pct"]
        + dados["forecast"]["stop_first_pct"]
        + dados["forecast"]["no_barrier_pct"]
        == 100
    )
    assert dados["financial"]["available"] is True
    assert dados["financial"]["effective_lots"] == "1.00"
    assert set(dados["financial"]["scenarios"]) == {"DESK_TAKE", "DESK_STOP", "TIMEOUT"}


def test_an_out_of_order_snapshot_is_discarded(client, db_session) -> None:
    user = _usuario(db_session, "hedge_ordem")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    client.post("/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=10), headers=_cabecalho(chave))
    atrasado = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=4), headers=_cabecalho(chave)
    ).json()

    assert "fora de ordem" in atrasado["state_reason"]
    assert atrasado["forecast"]["available"] is False


def test_a_touched_barrier_closes_the_setup_and_ends_the_forecast(client, db_session) -> None:
    """Depois da barreira nao se recalcula como se estivesse aberta."""
    user = _usuario(db_session, "hedge_barreira")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    dados = client.post(
        "/api/v1/hedge/snapshot",
        json=_corpo(setup.id, bid="20100", ask="20100.25"),
        headers=_cabecalho(chave),
    ).json()

    assert dados["state"] == "CLOSED"
    assert dados["closed_reason"] == "DESK_TAKE"
    assert dados["forecast"]["available"] is False
    assert dados["forecast"]["reason"] == "BARRIER_HIT"

    # E permanece encerrado no ciclo seguinte.
    seguinte = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=2), headers=_cabecalho(chave)
    ).json()
    assert seguinte["state"] == "CLOSED"


def test_a_barrier_is_judged_by_executable_price(client, db_session) -> None:
    """Comprado sai no bid. Usar o meio declararia barreira tocada antes
    de o preco de saida ter chegado."""
    user = _usuario(db_session, "hedge_exec")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    # Meio = 20100.125 (acima do take), mas o bid ainda nao chegou.
    dados = client.post(
        "/api/v1/hedge/snapshot",
        json=_corpo(setup.id, bid="20099.75", ask="20100.5"),
        headers=_cabecalho(chave),
    ).json()

    assert dados["state"] != "CLOSED"


def test_an_expired_horizon_closes_by_time(client, db_session) -> None:
    user = _usuario(db_session, "hedge_prazo")
    db_session.commit()
    setup = _setup(
        db_session,
        user.id,
        deadline_utc=datetime.now(UTC) - timedelta(minutes=1),
    )
    chave = _chave(db_session, user.id)

    dados = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave)
    ).json()

    assert dados["state"] == "CLOSED"
    assert dados["closed_reason"] == "TIMEOUT"


def test_a_missing_tick_value_leaves_the_forecast_alive(client, db_session) -> None:
    """As camadas degradam de forma independente: sem tick_value o
    financeiro para, a previsao continua."""
    user = _usuario(db_session, "hedge_degrada")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    corpo = _corpo(setup.id)
    del corpo["tick_value"]

    dados = client.post(
        "/api/v1/hedge/snapshot", json=corpo, headers=_cabecalho(chave)
    ).json()

    assert dados["financial"]["available"] is False
    assert "tick_value" in dados["financial"]["reason"]
    assert dados["forecast"]["available"] is True


def test_an_unavailable_forecast_is_null_never_zero(client, db_session) -> None:
    """Zero seria lido como "estimamos que nao acontece"."""
    user = _usuario(db_session, "hedge_nulo")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    corpo = _corpo(setup.id)
    corpo["atr"] = None
    corpo["atr_period_seconds"] = None

    bloco = client.post(
        "/api/v1/hedge/snapshot", json=corpo, headers=_cabecalho(chave)
    ).json()["forecast"]

    assert bloco["available"] is False
    assert bloco["take_first"] is None
    assert bloco["stop_first"] is None
    assert bloco["no_barrier"] is None
    assert bloco["take_first_pct"] is None


def test_the_model_kind_declares_itself_theoretical(client, db_session) -> None:
    user = _usuario(db_session, "hedge_kind")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    bloco = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave)
    ).json()["forecast"]

    assert bloco["model_kind"] == "THEORETICAL_BASELINE"
    assert bloco["assumptions"]


def test_a_schema_mismatch_is_reported_not_guessed(client, db_session) -> None:
    """O EA e um binario na maquina do operador: atualizar o servidor nao
    atualiza o terminal dele."""
    user = _usuario(db_session, "hedge_schema")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    corpo = _corpo(setup.id)
    corpo["schema_version"] = 99

    resposta = client.post("/api/v1/hedge/snapshot", json=corpo, headers=_cabecalho(chave))

    assert resposta.status_code == 409
    assert "Atualize o EA" in resposta.json()["detail"]


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("0", "20000"), ("20000", "0"), ("20001", "20000")],
    ids=["bid zero", "ask zero", "ask abaixo do bid"],
)
def test_impossible_quotes_are_refused(client, db_session, bid, ask) -> None:
    user = _usuario(db_session, "hedge_cotacao")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    resposta = client.post(
        "/api/v1/hedge/snapshot",
        json=_corpo(setup.id, bid=bid, ask=ask),
        headers=_cabecalho(chave),
    )

    assert resposta.status_code == 422


def test_the_rate_limit_returns_429(client, db_session) -> None:
    user = _usuario(db_session, "hedge_taxa")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    codigos = [
        client.post(
            "/api/v1/hedge/snapshot",
            json=_corpo(setup.id, seq=i),
            headers=_cabecalho(chave),
        ).status_code
        for i in range(1, 40)
    ]

    assert 429 in codigos


# --- historico --------------------------------------------------------------


def test_the_history_records_the_absence_of_a_forecast(client, db_session) -> None:
    """Um historico que so guarda acertos nao serve para medir calibracao:
    faltariam as vezes em que o modelo se calou."""
    user = _usuario(db_session, "hedge_hist")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    com = _corpo(setup.id, seq=1)
    sem = _corpo(setup.id, seq=2)
    sem["atr"] = None
    sem["atr_period_seconds"] = None

    client.post("/api/v1/hedge/snapshot", json=com, headers=_cabecalho(chave))
    client.post("/api/v1/hedge/snapshot", json=sem, headers=_cabecalho(chave))

    itens = client.get(
        f"/api/v1/hedge/history/{setup.id}", headers=_cabecalho(chave)
    ).json()["items"]

    assert len(itens) == 2
    assert {i["forecast_available"] for i in itens} == {True, False}
    ausente = next(i for i in itens if not i["forecast_available"])
    assert ausente["unavailable_reason"] == "INSUFFICIENT_DATA"
    assert ausente["take_first"] is None


def test_the_history_keeps_the_setup_version(client, db_session) -> None:
    """Sem isso, avaliar calibracao misturaria previsoes feitas sob
    parametros diferentes como se fossem a mesma coisa."""
    user = _usuario(db_session, "hedge_versao")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    client.post("/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave))

    repo = HedgeRepository(db_session)
    repo.update_setup(
        repo.get_setup(setup.id, user_id=user.id),
        params={**_PARAMS, "desk_stop": Decimal("19900")},
        note="stop afastado",
        user_id=user.id,
    )
    db_session.commit()

    client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=2), headers=_cabecalho(chave)
    )

    versoes = {
        i["setup_version"]
        for i in client.get(
            f"/api/v1/hedge/history/{setup.id}", headers=_cabecalho(chave)
        ).json()["items"]
    }
    assert versoes == {1, 2}


def test_latest_analysis_reports_its_own_age(client, db_session) -> None:
    """Um numero sem idade na tela parece atual mesmo quando nao e."""
    user = _usuario(db_session, "hedge_idade")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    client.post("/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(chave))

    dados = client.get(
        f"/api/v1/hedge/analysis/{setup.id}", headers=_cabecalho(chave)
    ).json()

    assert dados["available"] is True
    assert dados["age_seconds"] is not None


def test_no_analysis_yet_is_waiting_not_an_error(client, db_session) -> None:
    user = _usuario(db_session, "hedge_vazio")
    db_session.commit()
    setup = _setup(db_session, user.id)
    chave = _chave(db_session, user.id)

    dados = client.get(
        f"/api/v1/hedge/analysis/{setup.id}", headers=_cabecalho(chave)
    ).json()

    assert dados["state"] == "WAITING_DATA"
    assert dados["available"] is False


# --- cores por objetivo -----------------------------------------------------


def test_the_objective_swaps_the_colors_not_the_level_names(client, db_session) -> None:
    """No modo favorecer corretora a regiao do stop fica verde. O NOME do
    nivel continua sendo stop da mesa."""
    user = _usuario(db_session, "hedge_cor")
    db_session.commit()
    chave = _chave(db_session, user.id)

    mesa = _setup(db_session, user.id, nome="favor mesa")
    corretora = HedgeRepository(db_session).create_setup(
        user_id=user.id,
        name="favor corretora",
        mode="SIMULATION",
        objective="FAVOR_BROKER_PROFIT",
        params=_PARAMS,
    )[0]
    db_session.commit()

    a = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(mesa.id), headers=_cabecalho(chave)
    ).json()["colors"]
    b = client.post(
        "/api/v1/hedge/snapshot",
        json={**_corpo(corretora.id), "chart_id": "C2"},
        headers=_cabecalho(chave),
    ).json()["colors"]

    assert a["take_region"] == "GREEN" and a["stop_region"] == "RED"
    assert b["take_region"] == "RED" and b["stop_region"] == "GREEN"


def test_the_side_decides_where_the_region_sits(client, db_session) -> None:
    """Nao fixar verde em cima: numa venda o take fica embaixo."""
    user = _usuario(db_session, "hedge_lado")
    db_session.commit()
    chave = _chave(db_session, user.id)

    vendido = _setup(
        db_session,
        user.id,
        nome="vendido",
        desk_is_long=False,
        desk_take=Decimal("19900"),
        desk_stop=Decimal("20050"),
    )

    cores = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(vendido.id), headers=_cabecalho(chave)
    ).json()["colors"]

    assert cores["take_above"] == "false"


# --- garantia estrutural ----------------------------------------------------


def test_the_hedge_module_never_touches_orders() -> None:
    """O EA conector e um canal de dados. O grafico do MetaTrader tem um
    botao que parece botao de robo, e alguem vai clicar nele."""
    import ast
    import pathlib

    arquivos = [
        *pathlib.Path("app/hedge").rglob("*.py"),
        pathlib.Path("app/api/routes/hedge_api.py"),
        pathlib.Path("app/database/repositories/hedge_repository.py"),
    ]

    for arquivo in arquivos:
        arvore = ast.parse(arquivo.read_text())
        importados: list[str] = []
        for no in ast.walk(arvore):
            if isinstance(no, ast.ImportFrom) and no.module:
                importados.append(no.module)
            elif isinstance(no, ast.Import):
                importados.extend(alias.name for alias in no.names)

        for proibido in (
            "app.execution",
            "app.paper_trading",
            "app.mt5.orders",
            "app.broker",
        ):
            assert not any(m.startswith(proibido) for m in importados), (
                f"{arquivo}: {proibido}"
            )


def test_the_mql5_sources_send_no_orders() -> None:
    """Os `.mq5` sao compilados pelo operador, sem lint nem revisao de
    tipo. A unica garantia possivel e esta."""
    import pathlib
    import re

    for nome in ("TraderTopHedgeConnector.mq5", "TraderTopHedgeIndicator.mq5"):
        caminho = pathlib.Path("scripts/mql5") / nome
        assert caminho.exists(), nome

        bruto = caminho.read_text()
        sem_bloco = re.sub(r"/\*.*?\*/", "", bruto, flags=re.DOTALL)
        fonte = re.sub(r"//[^\n]*", "", sem_bloco)

        for proibido in (
            "OrderSend",
            "PositionOpen",
            "PositionClose",
            "CTrade",
            "MqlTradeRequest",
            "OrderModify",
            "OrderClose",
        ):
            assert proibido not in fonte, f"{nome}: {proibido}"


# --- JEV: contexto em modo observacao --------------------------------------


@pytest.fixture
def _limpa_orcamento_jev(db_session):
    from app.database.models.system_setting import SystemSetting

    def apagar() -> None:
        db_session.query(SystemSetting).filter(
            SystemSetting.key == "jev_daily_usage"
        ).delete(synchronize_session=False)
        db_session.commit()

    apagar()
    yield
    apagar()


def _liga_jev(monkeypatch, handler, *, eventos=None):
    """Liga o Jev com um transporte falso e um calendario controlado."""
    import httpx

    from app.jev import factory
    from app.jev.cache import TTLCache
    from app.jev.client import JevClient
    from app.jev.hedge_context import (
        ContextEvent,
        HedgeContextAssessment,
        HedgeContextAssessor,
        HedgeContextInput,
    )

    classificador = HedgeContextAssessor(
        JevClient(api_key="k", transport=httpx.MockTransport(handler)),
        cache=TTLCache[HedgeContextAssessment](ttl_seconds=300),
        cache_ttl_seconds=300,
        daily_budget=100,
    )
    monkeypatch.setattr(factory, "get_context_assessor", lambda settings=None: classificador)

    # O calendario real le arquivo; aqui o contexto e injetado direto.
    from app.hedge import service as servico

    def contexto_falso(session, *, symbol, horizon_seconds, settings, now=None, provider=None):
        return HedgeContextInput(
            symbol=symbol,
            currencies=("USD",),
            events=tuple(
                ContextEvent(title=t, impact=i, minutes_until=m, currency="USD")
                # `is None`, nao `or`: lista VAZIA e o caso que este helper
                # precisa poder representar — "nenhum evento na janela" —, e
                # `or` a trataria como ausencia, voltando ao evento padrao.
                for t, i, m in (
                    [("CPI y/y", "HIGH", 12)] if eventos is None else eventos
                )
            ),
            horizon_minutes=60,
            calendar_usable=True,
        )

    monkeypatch.setattr(servico, "build_context", contexto_falso)
    return classificador


def _resposta_jev(categoria="EVENT_RISK_NEAR", confianca=0.81, revisao=0.7):
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    "market_context": {
                        "type": "choice",
                        "choice": categoria,
                        "confidence": confianca,
                        "probabilities": {categoria: confianca},
                    },
                    "needs_review": {"type": "noul", "noul": revisao},
                },
            },
        )

    return handler


def test_jev_off_leaves_the_block_unavailable(client, db_session) -> None:
    """Desligado e o padrao. O bloco existe e diz que nao ha
    classificacao — omiti-lo obrigaria o EA a tratar dois formatos."""
    user = _usuario(db_session, "hedge_jevoff")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    bloco = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k)
    ).json()["jev"]

    assert bloco["available"] is False
    assert bloco["observation_only"] is True
    assert bloco["confidence"] is None


def test_jev_classifies_the_context_and_it_reaches_the_payload(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    _liga_jev(monkeypatch, _resposta_jev())
    user = _usuario(db_session, "hedge_jevon")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    bloco = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k)
    ).json()["jev"]

    assert bloco["available"] is True
    assert bloco["category"] == "EVENT_RISK_NEAR"
    assert bloco["confidence"] == 0.81
    assert bloco["needs_review"] is True
    assert bloco["model"] == "jev-1.13.0"
    assert bloco["observation_only"] is True


def test_jev_does_not_change_the_forecast(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    """O teste central do modo observacao. A previsao com e sem Jev precisa
    ser IDENTICA — e ela e, porque a classificacao nasce depois dela e nao
    e passada ao motor estatistico."""
    user = _usuario(db_session, "hedge_jevneutro")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    sem = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=1), headers=_cabecalho(k)
    ).json()

    _liga_jev(monkeypatch, _resposta_jev(categoria="EVENT_RISK_NEAR", confianca=0.99))
    com = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=2), headers=_cabecalho(k)
    ).json()

    assert com["jev"]["available"] is True
    assert sem["jev"]["available"] is False

    for campo in ("take_first", "stop_first", "no_barrier", "model_kind", "model_version"):
        assert com["forecast"][campo] == sem["forecast"][campo], campo
    assert com["financial"]["effective_lots"] == sem["financial"]["effective_lots"]


def test_a_jev_outage_does_not_break_the_snapshot(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    """Camada acessoria: se o Jev cair, o motor financeiro e a previsao
    seguem."""
    import httpx

    def fora_do_ar(request):
        raise httpx.ConnectError("sem rota", request=request)

    _liga_jev(monkeypatch, fora_do_ar)
    user = _usuario(db_session, "hedge_jevqueda")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    resposta = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k)
    )

    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["jev"]["available"] is False
    assert dados["forecast"]["available"] is True
    assert dados["financial"]["available"] is True


def test_a_broken_context_builder_does_not_break_the_snapshot(
    client, db_session, monkeypatch
) -> None:
    """A ponte com o calendario e a parte mais fragil do caminho. Uma
    excecao ali nao pode custar o snapshot."""
    from app.hedge import service as servico

    def explode(*args, **kwargs):
        raise RuntimeError("calendario em pedacos")

    monkeypatch.setattr(servico, "build_context", explode)
    user = _usuario(db_session, "hedge_jevexplode")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    resposta = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k)
    )

    assert resposta.status_code == 200
    assert resposta.json()["forecast"]["available"] is True


def test_the_classification_is_persisted_for_later_evaluation(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    """Gravada ao lado da analise: e assim que a influencia podera ser
    avaliada fora da amostra antes de ser permitida."""
    _liga_jev(monkeypatch, _resposta_jev(categoria="ELEVATED_UNCERTAINTY", confianca=0.64))
    user = _usuario(db_session, "hedge_jevgrava")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    client.post("/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k))

    registro = HedgeRepository(db_session).latest_analysis(setup.id)

    assert registro.jev_state == "ELEVATED_UNCERTAINTY"
    assert float(registro.jev_confidence) == pytest.approx(0.64)


def test_repeated_snapshots_cause_one_jev_call(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    """O conector manda snapshot a cada 1-2s. Uma chamada por snapshot
    seriam ~43 mil por dia por grafico."""
    chamadas = {"n": 0}
    base = _resposta_jev()

    def contando(request):
        chamadas["n"] += 1
        return base(request)

    _liga_jev(monkeypatch, contando)
    user = _usuario(db_session, "hedge_jevcache")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    for i in range(1, 6):
        client.post(
            "/api/v1/hedge/snapshot", json=_corpo(setup.id, seq=i), headers=_cabecalho(k)
        )

    assert chamadas["n"] == 1


def test_no_event_in_the_window_costs_no_call(
    client, db_session, monkeypatch, _limpa_orcamento_jev
) -> None:
    """O caso mais comum, e o mais barato."""
    chamadas = {"n": 0}

    def nunca(request):  # pragma: no cover
        chamadas["n"] += 1
        raise AssertionError("nao deveria chamar o Jev sem evento")

    _liga_jev(monkeypatch, nunca, eventos=[])
    user = _usuario(db_session, "hedge_jevcalmo")
    db_session.commit()
    setup = _setup(db_session, user.id)
    k = _chave(db_session, user.id)

    bloco = client.post(
        "/api/v1/hedge/snapshot", json=_corpo(setup.id), headers=_cabecalho(k)
    ).json()["jev"]

    assert chamadas["n"] == 0
    assert bloco["category"] == "CALM"
    assert bloco["available"] is False
    assert bloco["reason"] == "SEM_EVENTO_NA_JANELA"
