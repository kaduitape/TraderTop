def test_health_returns_ok(client, db_session) -> None:
    from app.database.repositories.system_setting_repository import get_current_mode

    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["database_connected"] is True
    # `system_mode` e um valor global compartilhado por toda a suite de
    # testes (mesma razao documentada nas Fases 10/12) -- le o valor
    # ATUAL persistido em vez de assumir o padrao DISABLED, que outros
    # arquivos de teste ja avancaram a essa altura da suite.
    assert body["system_mode"] == get_current_mode(db_session).value


def test_root_redirects_to_dashboard(client) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"


def test_the_retention_thread_never_runs_during_the_suite(client) -> None:
    """O lifespan da API inicia a thread de manutencao. Se ela subisse aqui,
    estaria apagando linhas em paralelo com a suite inteira — que grava em
    SQLite compartilhado — e produziria falha intermitente em testes sem
    relacao nenhuma com retencao. O guarda e `APP_ENV=test`.

    Vai neste arquivo, e nao em `tests/unit/maintenance`, porque so aqui o
    lifespan de verdade e exercitado: o teste de unidade checa a decisao, e
    este checa que ela e consultada."""
    import threading

    assert client.app.state.retention.running is False
    assert [t for t in threading.enumerate() if t.name == "retention"] == []
