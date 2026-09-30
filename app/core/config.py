"""Configuracao tipada da aplicacao.

Toda variavel de ambiente usada pelo sistema passa por aqui. Nenhum outro
modulo deve chamar `os.environ` diretamente — isso garante que a validacao
(via Pydantic) e o valor por omissao fiquem centralizados e testaveis, e que
segredos nunca sejam lidos de forma ad-hoc espalhada pelo codigo.

A aplicacao falha rapido (ValidationError na inicializacao) se uma variavel
obrigatoria estiver ausente, em vez de operar com valores indefinidos.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import SystemMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Aplicacao ---------------------------------------------------------
    app_name: str = "MT5 AI Scalper"
    app_env: str = "development"
    app_debug: bool = True
    app_secret_key: str = Field(default="CHANGE_ME_in_dot_env", min_length=8)
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    system_mode: SystemMode = SystemMode.DISABLED

    # --- Autenticacao / JWT --------------------------------------------------
    auth_jwt_algorithm: str = "HS256"
    auth_access_token_expire_minutes: int = 30

    # ATENCAO: bypass temporario de desenvolvimento -- nunca True em producao.
    # Quando True, `get_current_user_for_web` (dashboard HTML) libera acesso
    # sem cookie/login, autenticando automaticamente como um usuario "dev"
    # (criado sob demanda). Nao afeta `/api/*` (JWT via Authorization
    # continua exigido normalmente).
    dashboard_auth_disabled: bool = False

    # --- Login com Google (OpenID Connect) ---------------------------------
    # O recurso permanece oculto/inativo enquanto as tres variaveis nao
    # estiverem preenchidas. O callback deve ser cadastrado exatamente como
    # informado no Google Cloud Console.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_oauth_enabled(self) -> bool:
        return bool(
            self.google_oauth_client_id
            and self.google_oauth_client_secret
            and self.google_oauth_redirect_uri
        )

    # --- Banco de dados ------------------------------------------------------
    db_driver: str = "mysql+pymysql"
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "mt5_ai_scalper"
    db_user: str = "CHANGE_ME"
    db_password: str = "CHANGE_ME"
    db_pool_size: int = 5
    db_pool_max_overflow: int = 10
    db_echo: bool = False

    # --- Logging ---------------------------------------------------------------
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_json: bool = True

    # --- MetaTrader 5 (conector somente leitura a partir da Fase 2) ------------
    mt5_terminal_path: str | None = None
    mt5_login: int | None = None
    mt5_password: str | None = None
    mt5_server: str | None = None
    mt5_bridge_host: str | None = None
    """Host da ponte para um MetaTrader sob Wine (mt5-wine em Docker).

    Preenchido, o sistema fala com o terminal a partir do Linux e o worker
    Windows deixa de ser necessario. Vazio, mantem o comportamento antigo:
    pacote `MetaTrader5` local, so no Windows."""

    mt5_bridge_port: int = 18812
    mt5_timeout_ms: int = 10_000
    mt5_max_reconnect_attempts: int = 5
    mt5_reconnect_backoff_seconds: float = 2.0
    mt5_heartbeat_interval_seconds: float = 30.0

    # --- Qualidade de dados e retencao (Fase 3) --------------------------------
    quality_max_spread_points: float = 50.0
    quality_max_feed_delay_seconds: int = 300
    quality_min_score: int = 70
    tick_retention_days: int = 30

    # --- Machine learning (Fase 8) ---------------------------------------------
    ml_datasets_dir: str = "datasets"
    ml_models_dir: str = "models"

    # --- Noticias/fundamentos externos (Fase 18.6) ------------------------------
    # Fallback via .env; a chave tambem pode ser configurada em runtime via
    # /dashboard/settings/aisa (persistida em system_settings, tem prioridade
    # sobre este valor -- ver app/news/factory.py).
    credentials_encryption_key: str | None = None
    """Chave dedicada para cifrar credenciais guardadas no banco (ver
    `app/core/crypto.py`). Ausente, o segredo da aplicacao e usado — trocar
    qualquer um dos dois invalida o que ja estava cifrado."""

    aisa_api_key: str | None = None
    aisa_api_base_url: str | None = None

    # Cada analise consome duas chamadas da MarketPulse (noticias +
    # fundamentos). O sentimento agregado das ultimas manchetes nao muda a
    # cada segundo, entao a resposta boa e reaproveitada por este prazo.
    # Zero desliga o cache. Ver app/news/cache.py.
    news_cache_ttl_seconds: float = 600.0

    # Teto DURO de chamadas por dia UTC, compartilhado entre o servidor web e
    # o conector Windows. O cache evita repeticao; isto evita surpresa. Ao
    # atingir o limite, noticias/fundamentos saem do calculo em vez de
    # gastar mais. Zero = sem limite.
    news_daily_call_budget: int = 300

    # --- Calendario economico ---------------------------------------------------
    # Fonte do filtro "nao entre em cima de evento de alto impacto". Vazio
    # desliga o filtro (o robo segue operando -- ver
    # CALENDAR_BLOCK_WHEN_UNAVAILABLE). O caminho aponta para um JSON mantido
    # por fora do processo, tipicamente exportado do calendario nativo do
    # MetaTrader 5 por um EA. Ver docs/calendar.md.
    calendar_file_path: str | None = None
    calendar_blackout_before_minutes: int = 30
    calendar_blackout_after_minutes: int = 15
    """O perigo nao acaba na divulgacao: depois dela o spread abre e o preco
    chicoteia."""

    calendar_min_impact: str = "HIGH"
    calendar_cache_ttl_seconds: float = 900.0
    calendar_max_age_hours: int = 36
    """Acima disso o arquivo e considerado desatualizado. Calendario velho e
    pior que nenhum: os eventos de hoje nao estao la e o sistema acreditaria
    que o dia esta limpo."""

    calendar_block_when_unavailable: bool = False
    """Decisao explicita do dono do sistema: sem calendario, o robo CONTINUA
    operando. Bloquear recriaria o problema que o projeto acabou de resolver
    (parar por falta de dado externo). A ausencia aparece no status e no
    relatorio do dia -- nunca passa despercebida."""

    # --- Corretora de execucao --------------------------------------------------
    # `mt5` (padrao) e o unico caminho hoje validado contra conta real neste
    # projeto. `ctrader` liga o adaptador da Open API -- ver
    # app/broker/factory.py e docs/broker.md. A troca e deliberada e falha
    # alto se faltar credencial: nunca volta ao MT5 em silencio.
    broker: str = "mt5"

    ctrader_client_id: str | None = None
    ctrader_client_secret: str | None = None
    ctrader_access_token: str | None = None
    ctrader_account_id: int | None = None
    ctrader_account_is_demo: bool | None = None
    """Tipo esperado da conta. Usado quando a Open API nao informa `isLive`:
    sem isso o sistema recusa operar em vez de adivinhar se o dinheiro e
    de verdade."""

    ctrader_order_label: str = "ai-trader-pro"

    # --- Motor de analise Price Action / SMC / multi-timeframe (Fase 18) --------
    analysis_default_timeframe: str = "M15"
    analysis_default_threshold: float = 90.0

    # --- Jev / TypeSafe: classificacao VISUAL do Pulso --------------------------
    # Camada opcional que recebe um resumo tecnico JA CALCULADO e devolve um
    # rotulo entre quatro (STRONG_SETUP / CAUTION / WAIT / INSUFFICIENT_DATA).
    # Nao produz preco, zona, stop, take, direcao nem ordem: tudo isso continua
    # vindo exclusivamente do motor tecnico local. Ver app/jev/ e
    # docs/indicador-mt5.md.
    jev_enabled: bool = False

    jev_api_key: str | None = None
    """Lida SOMENTE do ambiente.

    Ao contrario de `aisa_api_key`, esta chave nao tem — e nao deve ganhar —
    um campo equivalente em `system_settings`. Guardar segredo no banco cria
    um segundo lugar de onde ele pode vazar (backup, dump, tela de
    configuracao) e este nao precisa disso: quem opera a VPS ja edita o
    `.env`. Reaproveitar `AISA_API_KEY` aqui tambem esta fora de questao —
    sao servicos distintos, e uma chave compartilhada faz o vazamento de um
    virar o vazamento dos dois."""

    jev_api_base_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-latest"

    jev_timeout_seconds: float = 2.5
    """Curto de proposito. Este passo e ACESSORIO: o Pulso ja tem resposta
    completa sem ele. Um timeout generoso transformaria uma melhoria visual
    em atraso no desenho do grafico, que e o oposto do objetivo."""

    jev_cache_ttl_seconds: float = 300.0
    """Teto de idade da classificacao. O corte real e a candle: enquanto a
    candle e o resumo tecnico nao mudam, a resposta e reaproveitada. Este
    prazo so existe para que uma candle longa (H4, D1) nao congele o
    veredito por horas."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_test_env(self) -> bool:
        return self.app_env.lower() == "test"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """URL de conexao SQLAlchemy.

        Em ambiente de teste (`APP_ENV=test`), usa SQLite em memoria — o
        projeto ainda nao tem um MySQL 8 disponivel neste momento (ver
        docs/assumptions.md secao 2.2). A URL de producao continua sendo
        MySQL, montada a partir das variaveis DB_*.
        """
        if self.is_test_env:
            return "sqlite+pysqlite:///:memory:"
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    """Retorna a instancia (cacheada) de configuracao da aplicacao."""
    return Settings()
