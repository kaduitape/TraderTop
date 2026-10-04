"""Configuracao e telemetria persistentes do conector automatico MT5.

O dashboard e o worker Windows rodam em processos (e, normalmente,
sistemas operacionais) diferentes. O banco ja compartilhado entre eles e
usado como canal de controle: o painel grava o plano de sincronizacao e o
worker publica heartbeat/resultado. Credenciais do MT5 nunca passam pelo
banco nem pelo navegador; permanecem no ``.env`` do host Windows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.database.repositories.system_setting_repository import SystemSettingRepository
from app.market.catalog import MARKET_CATALOG, resolve_broker_symbol

MT5_SYNC_CONFIG_KEY = "mt5_auto_sync_config"
MT5_SYNC_STATUS_KEY = "mt5_auto_sync_status"

_CATALOG_CODES = frozenset(instrument.code for instrument in MARKET_CATALOG)
_DEFAULT_SYMBOLS = ("XAUUSD", "EURUSD", "GBPUSD", "USDJPY")
_DEFAULT_TIMEFRAMES = ("MN1", "W1", "D1", "H4", "H1", "M30", "M15", "M5", "M1")
_TIMEFRAME_CODES = frozenset(_DEFAULT_TIMEFRAMES)


@dataclass(frozen=True, slots=True)
class MT5SyncConfig:
    enabled: bool = False
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS
    timeframes: tuple[str, ...] = _DEFAULT_TIMEFRAMES
    interval_seconds: int = 15
    candle_backfill_count: int = 2_000
    collect_ticks: bool = True
    tick_lookback_seconds: int = 60
    sync_request_id: str = ""
    test_request_id: str = ""


@dataclass(frozen=True, slots=True)
class MT5SyncStatus:
    state: str = "OFFLINE"
    worker_online: bool = False
    connected: bool = False
    heartbeat_at: str | None = None
    last_sync_at: str | None = None
    worker_id: str | None = None
    terminal_name: str | None = None
    company: str | None = None
    broker_server: str | None = None
    account_login: int | None = None
    account_is_demo: bool | None = None
    selected_symbols: int = 0
    ready_symbols: int = 0
    candles_inserted: int = 0
    ticks_inserted: int = 0
    last_error: str | None = None
    handled_sync_request_id: str = ""
    handled_test_request_id: str = ""
    code_version: str | None = None
    """Versao do codigo que o WORKER esta rodando. Existe porque worker e
    servidor web sao processos separados: sem isso, uma correcao aplicada no
    repositorio e nao na maquina do worker fica invisivel, e o sintoma vira
    "consertei e continua igual"."""

    started_at: str | None = None
    consecutive_failures: int = 0


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(repository: SystemSettingRepository, key: str) -> dict[str, Any]:
    raw = repository.get(key)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def load_sync_config(session: Session) -> MT5SyncConfig:
    data = _read_json(SystemSettingRepository(session), MT5_SYNC_CONFIG_KEY)
    raw_symbols = data.get("symbols", _DEFAULT_SYMBOLS)
    symbols = tuple(
        dict.fromkeys(
            str(code).strip().upper()
            for code in raw_symbols
            if str(code).strip().upper() in _CATALOG_CODES
        )
    )
    raw_timeframes = data.get("timeframes", _DEFAULT_TIMEFRAMES)
    timeframes = tuple(
        dict.fromkeys(
            str(code).strip().upper()
            for code in raw_timeframes
            if str(code).strip().upper() in _TIMEFRAME_CODES
        )
    )
    return MT5SyncConfig(
        enabled=bool(data.get("enabled", False)),
        symbols=symbols or _DEFAULT_SYMBOLS,
        timeframes=timeframes or _DEFAULT_TIMEFRAMES,
        interval_seconds=_bounded_int(
            data.get("interval_seconds"), default=15, minimum=5, maximum=3_600
        ),
        candle_backfill_count=_bounded_int(
            data.get("candle_backfill_count"), default=2_000, minimum=200, maximum=100_000
        ),
        collect_ticks=bool(data.get("collect_ticks", True)),
        tick_lookback_seconds=_bounded_int(
            data.get("tick_lookback_seconds"), default=60, minimum=10, maximum=3_600
        ),
        sync_request_id=str(data.get("sync_request_id", ""))[:64],
        test_request_id=str(data.get("test_request_id", ""))[:64],
    )


def save_sync_config(session: Session, config: MT5SyncConfig) -> None:
    SystemSettingRepository(session).set(
        MT5_SYNC_CONFIG_KEY,
        json.dumps(asdict(config), ensure_ascii=True, separators=(",", ":")),
        description="Plano automatico de sincronizacao com o MetaTrader 5.",
    )


def load_sync_status(session: Session) -> MT5SyncStatus:
    data = _read_json(SystemSettingRepository(session), MT5_SYNC_STATUS_KEY)
    defaults = MT5SyncStatus()
    return MT5SyncStatus(
        state=str(data.get("state", defaults.state))[:24],
        worker_online=bool(data.get("worker_online", defaults.worker_online)),
        connected=bool(data.get("connected", defaults.connected)),
        heartbeat_at=data.get("heartbeat_at"),
        last_sync_at=data.get("last_sync_at"),
        worker_id=data.get("worker_id"),
        terminal_name=data.get("terminal_name"),
        company=data.get("company"),
        broker_server=data.get("broker_server"),
        account_login=data.get("account_login"),
        account_is_demo=data.get("account_is_demo"),
        selected_symbols=_bounded_int(
            data.get("selected_symbols"), default=0, minimum=0, maximum=10_000
        ),
        ready_symbols=_bounded_int(
            data.get("ready_symbols"), default=0, minimum=0, maximum=10_000
        ),
        candles_inserted=_bounded_int(
            data.get("candles_inserted"), default=0, minimum=0, maximum=100_000_000
        ),
        ticks_inserted=_bounded_int(
            data.get("ticks_inserted"), default=0, minimum=0, maximum=100_000_000
        ),
        last_error=str(data["last_error"])[:300] if data.get("last_error") else None,
        handled_sync_request_id=str(data.get("handled_sync_request_id", ""))[:64],
        handled_test_request_id=str(data.get("handled_test_request_id", ""))[:64],
        code_version=(
            str(data["code_version"])[:40] if data.get("code_version") else None
        ),
        started_at=data.get("started_at"),
        consecutive_failures=_bounded_int(
            data.get("consecutive_failures"), default=0, minimum=0, maximum=100_000
        ),
    )


def save_sync_status(session: Session, status: MT5SyncStatus) -> None:
    SystemSettingRepository(session).set(
        MT5_SYNC_STATUS_KEY,
        json.dumps(asdict(status), ensure_ascii=True, separators=(",", ":")),
        description="Heartbeat e resultado do conector Windows do MetaTrader 5.",
    )


def heartbeat_age_label(status: MT5SyncStatus) -> str | None:
    """Ha quanto tempo o conector nao da sinal, em texto.

    "Offline" sozinho nao ajuda ninguem: parou agora ou faz tres dias? A
    diferenca decide se voce espera ou vai atras do problema.
    """
    if not status.heartbeat_at:
        return None
    try:
        heartbeat = datetime.fromisoformat(status.heartbeat_at)
    except ValueError:
        return None
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)

    segundos = int((datetime.now(UTC) - heartbeat).total_seconds())
    if segundos < 60:
        return "ha menos de um minuto"
    if segundos < 3600:
        return f"ha {segundos // 60} min"
    if segundos < 86_400:
        return f"ha {segundos // 3600} h"
    return f"ha {segundos // 86_400} dia(s)"


def heartbeat_is_fresh(status: MT5SyncStatus, *, max_age_seconds: int = 90) -> bool:
    if not status.worker_online or not status.heartbeat_at:
        return False
    try:
        heartbeat = datetime.fromisoformat(status.heartbeat_at)
    except ValueError:
        return False
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return (datetime.now(UTC) - heartbeat).total_seconds() <= max_age_seconds


def stale_data_reason(session: Session, symbol: str) -> str:
    """Por que ESTE simbolo pode estar sem candles novas.

    Existe porque o aviso de dados velhos dizia uma frase so — "o coletor
    MT5 provavelmente esta parado" — e essa frase manda o operador olhar a
    tela do conector. Quando o conector esta online e saudavel, ele olha,
    ve tudo verde, e nao tem para onde ir.

    A causa mais comum nao e o coletor parado: e o simbolo nao estar na
    lista que o worker sincroniza. Sao duas listas diferentes, e nada na
    tela dizia isso. O seletor da FotoAnalise vem de `symbols` (o catalogo
    inteiro da corretora, sincronizado de uma vez), enquanto o worker
    percorre `config.symbols` (a selecao feita em Conexao MT5). Um ativo
    pode estar no primeiro e fora do segundo para sempre.

    Devolve texto vazio quando o simbolo ESTA coberto — nesse caso o
    diagnostico antigo vale e o aviso generico continua certo.
    """
    config = load_sync_config(session)

    coberto = any(
        resolve_broker_symbol(codigo, [symbol]) is not None for codigo in config.symbols
    )
    if not coberto:
        if symbol.strip().upper() not in _CATALOG_CODES and not any(
            resolve_broker_symbol(codigo, [symbol]) is not None for codigo in _CATALOG_CODES
        ):
            return (
                f"{symbol} nao faz parte do catalogo de ativos sincronizaveis, "
                "entao o conector nunca o coleta — por mais saudavel que ele esteja."
            )
        return (
            f"{symbol} nao esta na lista de ativos que o conector sincroniza. "
            "Marque-o em Conexao MT5 > Mercados para que passe a ser coletado."
        )

    if not config.enabled:
        return (
            "A sincronizacao automatica esta pausada. Ative-a em Conexao MT5 "
            "para o conector voltar a coletar."
        )

    return ""
