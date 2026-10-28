"""Toda configuracao documentada precisa ALCANCAR o container.

## O bug que este modulo existe para impedir

`docker-compose.yml` nao usa `env_file`: ele lista as variaveis uma a
uma. Entao adicionar um campo em `Settings` e documenta-lo no
`.env.example` NAO e suficiente — se o nome nao estiver na lista do
servico `app`, o operador preenche o `.env`, reinicia, e nada acontece.

Foi exatamente o que aconteceu com `JEV_API_KEY`: a chave era lida de
`Settings`, estava documentada, e nunca chegava ao processo. O painel
dizia "desligado" e nao havia como descobrir por que — o pior tipo de
falha de configuracao, porque tudo parece certo.

## A lista de excecoes e o ponto

`FORA_DO_CONTAINER` nao e uma lista de perdoes: e documentacao de quais
variaveis sao deliberadamente do worker Windows ou de recursos nao
usados nesta VPS. Adicionar algo ali e uma decisao visivel no diff, que
e exatamente o que faltava antes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.config import Settings

COMPOSE = Path("docker-compose.yml")
ENV_EXAMPLE = Path(".env.example")

FORA_DO_CONTAINER = {
    # --- worker Windows -----------------------------------------------------
    # O pacote MetaTrader5 so publica wheel para Windows. Estas variaveis
    # sao lidas na maquina do operador, nao no container Linux.
    "MT5_TERMINAL_PATH",
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_TIMEOUT_MS",
    "MT5_MAX_RECONNECT_ATTEMPTS",
    "MT5_RECONNECT_BACKOFF_SECONDS",
    "MT5_HEARTBEAT_INTERVAL_SECONDS",
    # --- adaptador cTrader --------------------------------------------------
    # `BROKER` nao e passado ao container de proposito: a VPS opera apenas
    # com `mt5`, e sem poder trocar o broker as credenciais do cTrader nao
    # teriam uso. Trocar isso exige passar os cinco juntos.
    "BROKER",
    "CTRADER_CLIENT_ID",
    "CTRADER_CLIENT_SECRET",
    "CTRADER_ACCESS_TOKEN",
    "CTRADER_ACCOUNT_ID",
    "CTRADER_ACCOUNT_IS_DEMO",
    # --- cosmeticos e de desenvolvimento ------------------------------------
    "APP_NAME",
    "APP_DEBUG",
    "DB_ECHO",
    "LOG_JSON",
    "AUTH_JWT_ALGORITHM",
    "AUTH_ACCESS_TOKEN_EXPIRE_MINUTES",
    "QUALITY_MIN_SCORE",
    "TICK_RETENTION_DAYS",
}


def _compose_app_env() -> set[str]:
    dados = yaml.safe_load(COMPOSE.read_text())
    return set(dados["services"]["app"]["environment"])


def _env_example_keys() -> set[str]:
    return {
        m.group(1)
        for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.M)
    }


def test_every_documented_setting_reaches_the_container() -> None:
    """O teste central. Falha sempre que alguem documentar uma variavel
    que o container nunca vera."""
    campos = {nome.upper() for nome in Settings.model_fields}
    documentadas = _env_example_keys() & campos

    faltando = sorted(documentadas - _compose_app_env() - FORA_DO_CONTAINER)

    assert not faltando, (
        "estas variaveis estao no .env.example e sao lidas por Settings, mas "
        "nao chegam ao container (docker-compose nao usa env_file): "
        + ", ".join(faltando)
        + ". Adicione-as ao servico `app` ou a FORA_DO_CONTAINER com o motivo."
    )


def test_the_jev_settings_reach_the_container() -> None:
    """Regressao nomeada: foi esta a variavel que ficou pelo caminho, e o
    sintoma era o painel dizer 'desligado' sem explicacao."""
    env = _compose_app_env()

    for nome in (
        "JEV_ENABLED",
        "JEV_API_KEY",
        "JEV_API_BASE_URL",
        "JEV_MODEL",
        "JEV_TIMEOUT_SECONDS",
        "JEV_CACHE_TTL_SECONDS",
        "JEV_DAILY_CALL_BUDGET",
    ):
        assert nome in env, nome


def test_the_paid_api_budgets_are_adjustable_in_production() -> None:
    """Teto de API paga preso no default nao e teto: e um numero que
    ninguem consegue baixar quando a conta aperta. Este projeto ja
    esgotou a cota de uma delas."""
    env = _compose_app_env()

    assert "NEWS_DAILY_CALL_BUDGET" in env
    assert "JEV_DAILY_CALL_BUDGET" in env


def test_no_secret_has_a_real_default_in_the_compose() -> None:
    """Um segredo com valor embutido no compose vaza para o repositorio e
    para qualquer `docker compose config`. Chaves entram vazias."""
    dados = yaml.safe_load(COMPOSE.read_text())
    ambiente = dados["services"]["app"]["environment"]

    for nome, valor in ambiente.items():
        if not any(p in nome for p in ("KEY", "PASSWORD", "SECRET", "TOKEN")):
            continue
        texto = str(valor)
        assert texto.startswith("${"), f"{nome} nao vem do ambiente"

        # Aceitaveis: vazio (`${X:-}`), obrigatorio (`${X:?msg}`) e o
        # placeholder CHANGE_ME, que e a convencao deste projeto e falha
        # alto na primeira conexao. Inaceitavel e um valor que PARECE
        # funcionar — uma chave de verdade colada aqui vaza para o
        # repositorio e para qualquer `docker compose config`.
        if ":-}" in texto or ":?" in texto:
            continue
        padrao = texto.split(":-", 1)[1].rstrip("}") if ":-" in texto else texto
        assert "CHANGE_ME" in padrao, (
            f"{nome} tem valor padrao embutido no compose: {texto}"
        )


def test_the_exception_list_stays_honest() -> None:
    """Uma excecao para variavel que JA esta no compose e lixo que esconde
    a proxima de verdade."""
    env = _compose_app_env()
    redundantes = sorted(FORA_DO_CONTAINER & env)

    assert not redundantes, (
        "estas estao em FORA_DO_CONTAINER e tambem no compose: "
        + ", ".join(redundantes)
    )
