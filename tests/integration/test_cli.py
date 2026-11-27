"""Testes da CLI (Fases 2 a 13). `MT5Connection` e sempre substituida por
um fake — nenhum teste depende de um terminal MetaTrader 5 instalado."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from app import cli
from app.database.repositories.drift_event_repository import DriftEventRepository
from app.database.repositories.symbol_repository import SymbolRepository
from app.database.repositories.tick_repository import TickRepository
from app.mt5.market_data import RawTick
from tests.fixtures.fake_mt5_client import (
    FakeMT5Client,
    make_account_info,
    make_order_send_result,
    make_rates_array,
    make_symbol_info,
    make_terminal_info,
    make_ticks_array,
)


class _FakeConnectionContext:
    def __init__(self, client: FakeMT5Client) -> None:
        self.client = client
        self.is_connected = True

    def __enter__(self) -> _FakeConnectionContext:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_connection(monkeypatch: pytest.MonkeyPatch, client: FakeMT5Client) -> None:
    monkeypatch.setattr(cli, "MT5Connection", lambda config: _FakeConnectionContext(client))


def test_cmd_mt5_check_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    client = FakeMT5Client()
    client.terminal_info_result = make_terminal_info(company="Broker CLI Test")
    client.account_info_result = make_account_info(
        login=555, trade_mode=client.ACCOUNT_TRADE_MODE_DEMO
    )
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(["mt5", "check"])
    result = cli.cmd_mt5_check(args)
    captured = capsys.readouterr()

    assert result == 0
    payload = json.loads(captured.out)
    assert payload["terminal"]["company"] == "Broker CLI Test"
    assert payload["account"]["login"] == 555
    assert payload["account"]["is_demo"] is True


def test_cmd_mt5_check_fails_when_unhealthy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    client = FakeMT5Client()
    client.terminal_info_result = None
    client.last_error_result = (-10, "no ipc connection")
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(["mt5", "check"])
    result = cli.cmd_mt5_check(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_mt5_symbols_lists_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    client = FakeMT5Client()
    client.symbols_get_result = (make_symbol_info(name="EURUSD"), make_symbol_info(name="GBPUSD"))
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(["mt5", "symbols"])
    result = cli.cmd_mt5_symbols(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "EURUSD" in captured.out
    assert "GBPUSD" in captured.out


def test_cmd_collect_candles_persists_new_candles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_EURUSD")
    client.copy_rates_from_pos_result = make_rates_array(
        [
            (1_700_000_000, 1.1000, 1.1010, 1.0990, 1.1005, 120, 2, 0),
            (1_700_000_060, 1.1005, 1.1020, 1.1000, 1.1015, 130, 2, 0),
        ]
    )
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_EURUSD", "--timeframe", "M1", "--count", "2"]
    )
    result = cli.cmd_collect_candles(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "novas inseridas: 2" in captured.out

    symbol = SymbolRepository(db_session).get_by_name("CLI_EURUSD")
    assert symbol is not None

    # Rodar de novo com as mesmas candles nao deve duplicar.
    result_again = cli.cmd_collect_candles(args)
    captured_again = capsys.readouterr()
    assert result_again == 0
    assert "novas inseridas: 0" in captured_again.out


def test_cmd_collect_candles_fails_for_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = None
    client.last_error_result = (-10, "symbol not found")
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "UNKNOWN", "--timeframe", "M1"]
    )
    result = cli.cmd_collect_candles(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_collect_ticks_persists_new_ticks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_GBPUSD")
    client.copy_ticks_range_result = make_ticks_array(
        [
            (1_700_000_000, 1.2500, 1.2502, 0.0, 0.0, 1_700_000_000_000, 6, 0.0),
        ]
    )
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["collect", "ticks", "--symbol", "CLI_GBPUSD", "--seconds", "120"]
    )
    result = cli.cmd_collect_ticks(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "novos inseridos: 1" in captured.out

    symbol = SymbolRepository(db_session).get_by_name("CLI_GBPUSD")
    assert symbol is not None


def test_cmd_collect_candles_second_call_is_incremental(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_INCR")
    client.copy_rates_from_pos_result = make_rates_array(
        [(1_700_000_000, 1.1000, 1.1010, 1.0990, 1.1005, 120, 2, 0)]
    )
    _patch_connection(monkeypatch, client)

    backfill_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_INCR", "--timeframe", "M1", "--count", "1"]
    )
    result = cli.cmd_collect_candles(backfill_args)
    captured = capsys.readouterr()
    assert result == 0
    assert "modo: backfill" in captured.out
    assert client.copy_rates_range_calls == []

    client.copy_rates_range_result = make_rates_array(
        [(1_700_000_060, 1.1005, 1.1020, 1.1000, 1.1015, 130, 2, 0)]
    )

    incremental_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_INCR", "--timeframe", "M1"]
    )
    result_again = cli.cmd_collect_candles(incremental_args)
    captured_again = capsys.readouterr()

    assert result_again == 0
    assert "modo: incremental" in captured_again.out
    assert "novas inseridas: 1" in captured_again.out
    assert len(client.copy_rates_range_calls) == 1
    # date_from passado deve ser posterior a ultima candle conhecida (1_700_000_000).
    _, _, date_from, _ = client.copy_rates_range_calls[0]
    assert date_from > datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_cmd_collect_candles_reports_quality_issues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_BADCANDLE")
    # high < low -> ocorrencia CRITICAL.
    client.copy_rates_from_pos_result = make_rates_array(
        [(1_700_000_000, 1.1000, 1.0, 1.2, 1.1005, 120, 2, 0)]
    )
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_BADCANDLE", "--timeframe", "M1", "--count", "1"]
    )
    result = cli.cmd_collect_candles(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "1 ocorrencia" in captured.out
    assert "candle_high_below_low" in captured.err


def test_cmd_quality_check_reports_acceptable_when_clean(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_QCHECK")
    client.copy_rates_from_pos_result = make_rates_array(
        [
            (1_700_000_000, 1.1000, 1.1010, 1.0990, 1.1005, 120, 2, 0),
            (1_700_000_060, 1.1005, 1.1020, 1.1000, 1.1015, 130, 2, 0),
        ]
    )
    _patch_connection(monkeypatch, client)
    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_QCHECK", "--timeframe", "M1", "--count", "2"]
    )
    cli.cmd_collect_candles(collect_args)
    capsys.readouterr()

    check_args = cli.build_parser().parse_args(
        ["quality", "check", "--symbol", "CLI_QCHECK", "--timeframe", "M1"]
    )
    result = cli.cmd_quality_check(check_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "aceitavel: True" in captured.out


def test_cmd_quality_check_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["quality", "check", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_quality_check(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_data_purge_ticks_removes_old_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_PURGE")
    client.copy_ticks_range_result = make_ticks_array(
        [(1_700_000_000, 1.1000, 1.1002, 0.0, 0.0, 1_700_000_000_000, 6, 0.0)]
    )
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "ticks", "--symbol", "CLI_PURGE", "--seconds", "60"]
    )
    cli.cmd_collect_ticks(collect_args)
    capsys.readouterr()

    symbol = SymbolRepository(db_session).get_by_name("CLI_PURGE")
    assert len(TickRepository(db_session).get_recent(symbol.id, limit=10)) == 1

    purge_args = cli.build_parser().parse_args(["data", "purge-ticks", "--older-than-days", "0"])
    result = cli.cmd_data_purge_ticks(purge_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "ticks removidos" in captured.out
    assert len(TickRepository(db_session).get_recent(symbol.id, limit=10)) == 0


def test_cmd_data_retention_dry_run_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    """A primeira limpeza e contra um acumulo de meses. Quem a roda precisa
    poder ver os cortes antes de apagar."""
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_RETEN")
    client.copy_ticks_range_result = make_ticks_array(
        [(1_700_000_000, 1.1000, 1.1002, 0.0, 0.0, 1_700_000_000_000, 6, 0.0)]
    )
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "ticks", "--symbol", "CLI_RETEN", "--seconds", "60"]
    )
    cli.cmd_collect_ticks(collect_args)
    capsys.readouterr()

    symbol = SymbolRepository(db_session).get_by_name("CLI_RETEN")
    antes = len(TickRepository(db_session).get_recent(symbol.id, limit=10))
    assert antes == 1

    args = cli.build_parser().parse_args(["data", "retention", "--dry-run"])
    result = cli.cmd_data_retention(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "[dry-run] corte de ticks" in captured.out
    assert "[dry-run] corte de qualidade" in captured.out
    assert len(TickRepository(db_session).get_recent(symbol.id, limit=10)) == antes


def test_cmd_data_retention_refuses_an_inverted_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine
) -> None:
    """Sai com codigo 1 em vez de apagar com uma politica que faria os
    eventos CRITICAL sumirem antes dos triviais."""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quality_event_retention_days", 30, raising=False)
    monkeypatch.setattr(settings, "quality_event_critical_retention_days", 7, raising=False)

    args = cli.build_parser().parse_args(["data", "retention"])
    result = cli.cmd_data_retention(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "configuracao invalida" in captured.out


def _make_synthetic_rate_rows(
    n: int, *, start_time: int = 1_700_000_000, step_seconds: int = 60
) -> list[tuple[int, float, float, float, float, int, int, int]]:
    rows = []
    price = 1.1000
    for i in range(n):
        open_ = price
        close = price + 0.0001 * (1 if i % 2 == 0 else -1)
        high = max(open_, close) + 0.0002
        low = min(open_, close) - 0.0002
        rows.append((start_time + i * step_seconds, open_, high, low, close, 100 + i, 2, 0))
        price = close
    return rows


def test_cmd_features_build_reports_regime_with_enough_history(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_FEATURES_FULL", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(220))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        [
            "collect",
            "candles",
            "--symbol",
            "CLI_FEATURES_FULL",
            "--timeframe",
            "M1",
            "--count",
            "220",
        ]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    build_args = cli.build_parser().parse_args(
        ["features", "build", "--symbol", "CLI_FEATURES_FULL", "--timeframe", "M1", "--rows", "3"]
    )
    result = cli.cmd_features_build(build_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Regime atual:" in captured.out
    assert "rsi_14" in captured.out
    assert "AVISO" not in captured.err


def test_cmd_features_build_warns_when_history_is_short(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_FEATURES_SHORT", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(10))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        [
            "collect",
            "candles",
            "--symbol",
            "CLI_FEATURES_SHORT",
            "--timeframe",
            "M1",
            "--count",
            "10",
        ]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    build_args = cli.build_parser().parse_args(
        ["features", "build", "--symbol", "CLI_FEATURES_SHORT", "--timeframe", "M1"]
    )
    result = cli.cmd_features_build(build_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "AVISO" in captured.err


def test_cmd_features_build_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["features", "build", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_features_build(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_run_produces_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_BACKTEST", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_BACKTEST", "--timeframe", "M1", "--count", "250"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    json_out = tmp_path / "report.json"
    backtest_args = cli.build_parser().parse_args(
        [
            "backtest",
            "run",
            "--symbol",
            "CLI_BACKTEST",
            "--timeframe",
            "M1",
            "--fast",
            "9",
            "--slow",
            "21",
            "--commission-per-lot",
            "5",
            "--slippage-points",
            "1",
            "--json-out",
            str(json_out),
        ]
    )
    result = cli.cmd_backtest_run(backtest_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Backtest: ema_crossover_baseline | CLI_BACKTEST M1" in captured.out
    assert "Profit factor" in captured.out
    assert json_out.exists()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["symbol"] == "CLI_BACKTEST"
    assert "metrics" in payload
    assert "trades" in payload


def test_cmd_backtest_run_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["backtest", "run", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_backtest_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_run_fails_for_insufficient_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_BACKTEST_SHORT", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(1))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        [
            "collect",
            "candles",
            "--symbol",
            "CLI_BACKTEST_SHORT",
            "--timeframe",
            "M1",
            "--count",
            "1",
        ]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    backtest_args = cli.build_parser().parse_args(
        ["backtest", "run", "--symbol", "CLI_BACKTEST_SHORT", "--timeframe", "M1"]
    )
    result = cli.cmd_backtest_run(backtest_args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


@pytest.mark.parametrize(
    "strategy_name",
    ["trend_pullback", "range_breakout", "zscore_mean_reversion", "momentum_continuation"],
)
def test_cmd_backtest_run_works_with_each_fase6_strategy(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    engine,
    db_session,
    strategy_name: str,
) -> None:
    client = FakeMT5Client()
    symbol_name = f"CLI_STRAT_{strategy_name.upper()}"
    client.symbol_info_result = make_symbol_info(name=symbol_name, digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(220))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", symbol_name, "--timeframe", "M1", "--count", "220"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    backtest_args = cli.build_parser().parse_args(
        [
            "backtest",
            "run",
            "--symbol",
            symbol_name,
            "--timeframe",
            "M1",
            "--strategy",
            strategy_name,
        ]
    )
    result = cli.cmd_backtest_run(backtest_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Backtest:" in captured.out


def test_cmd_backtest_compare_runs_all_registered_strategies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_COMPARE", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(220))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_COMPARE", "--timeframe", "M1", "--count", "220"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    compare_args = cli.build_parser().parse_args(
        ["backtest", "compare", "--symbol", "CLI_COMPARE", "--timeframe", "M1"]
    )
    result = cli.cmd_backtest_compare(compare_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "estrategia" in captured.out
    assert "ema_crossover_baseline" in captured.out
    assert "trend_pullback" in captured.out
    assert "range_breakout" in captured.out
    assert "zscore_mean_reversion" in captured.out
    assert "momentum_continuation" in captured.out


def test_cmd_backtest_compare_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(["backtest", "compare", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_backtest_compare(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_run_ticks_produces_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    engine,
    db_session,
    tmp_path,
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_TICKBT", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(220))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_TICKBT", "--timeframe", "M1", "--count", "220"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    symbol = SymbolRepository(db_session).get_by_name("CLI_TICKBT")
    base_time = datetime.fromtimestamp(1_700_000_000, tz=UTC)
    ticks = [
        RawTick(
            timestamp=base_time + timedelta(seconds=i * 5),
            bid=1.1000 + 0.0001 * ((i % 7) - 3),
            ask=1.1002 + 0.0001 * ((i % 7) - 3),
            last=0.0,
            volume=0.0,
            flags=6,
        )
        for i in range(220 * 60 // 5 + 100)
    ]
    TickRepository(db_session).bulk_upsert(symbol.id, ticks)
    db_session.commit()

    json_out = tmp_path / "tick_report.json"
    backtest_args = cli.build_parser().parse_args(
        [
            "backtest",
            "run-ticks",
            "--symbol",
            "CLI_TICKBT",
            "--timeframe",
            "M1",
            "--strategy",
            "ema_crossover",
            "--latency-ms",
            "100",
            "--slippage-points",
            "1",
            "--json-out",
            str(json_out),
        ]
    )
    result = cli.cmd_backtest_run_ticks(backtest_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Backtest:" in captured.out
    assert json_out.exists()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["symbol"] == "CLI_TICKBT"
    assert "rejections" in payload
    assert "trades_audit" in payload


def test_cmd_backtest_run_ticks_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(["backtest", "run-ticks", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_backtest_run_ticks(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_run_ticks_fails_without_tick_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_TICKBT_NOTICKS", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(220))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        [
            "collect",
            "candles",
            "--symbol",
            "CLI_TICKBT_NOTICKS",
            "--timeframe",
            "M1",
            "--count",
            "220",
        ]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    backtest_args = cli.build_parser().parse_args(
        ["backtest", "run-ticks", "--symbol", "CLI_TICKBT_NOTICKS", "--timeframe", "M1"]
    )
    result = cli.cmd_backtest_run_ticks(backtest_args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def _make_ml_synthetic_rate_rows(
    n: int, *, start_time: int = 1_700_000_000, step_seconds: int = 60
) -> list[tuple[int, float, float, float, float, int, int, int]]:
    """Serie oscilante (duas senoides sobrepostas) deliberadamente projetada
    para gerar sinais frequentes da estrategia ema_crossover com desfechos
    (TARGET_FIRST/STOP_FIRST) de AMBAS as classes — necessario para que o
    dataset de ML tenha as duas classes em treino/calibracao/teste apos a
    divisao temporal (verificado empiricamente antes de escrever este
    teste)."""
    rows = []
    for i in range(n):
        price = (
            1.1000
            + 0.0020 * math.sin(i * 2 * math.pi / 20)
            + 0.0006 * math.sin(i * 2 * math.pi / 7)
        )
        close = price + 0.0001 * (1 if i % 2 == 0 else -1)
        high = max(price, close) + 0.0002
        low = min(price, close) - 0.0002
        rows.append((start_time + i * step_seconds, price, high, low, close, 100 + i, 2, 0))
    return rows


def _collect_ml_candles(monkeypatch: pytest.MonkeyPatch, symbol: str, n: int = 1500) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name=symbol, digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_ml_synthetic_rate_rows(n))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", symbol, "--timeframe", "M1", "--count", str(n)]
    )
    assert cli.cmd_collect_candles(collect_args) == 0


def test_cmd_ml_build_dataset_produces_labeled_csv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    _collect_ml_candles(monkeypatch, "CLI_ML_DATASET")
    capsys.readouterr()

    out_csv = tmp_path / "dataset.csv"
    args = cli.build_parser().parse_args(
        [
            "ml",
            "build-dataset",
            "--symbol",
            "CLI_ML_DATASET",
            "--timeframe",
            "M1",
            "--strategy",
            "ema_crossover",
            "--stop-points",
            "50",
            "--target-points",
            "50",
            "--max-horizon-bars",
            "30",
            "--out",
            str(out_csv),
        ]
    )
    result = cli.cmd_ml_build_dataset(args)
    captured = capsys.readouterr()

    assert result == 0
    assert out_csv.exists()
    assert "dataset salvo em" in captured.out

    import pandas as pd

    dataset = pd.read_csv(out_csv)
    assert len(dataset) > 20
    assert set(dataset["label"].unique()) == {0, 1}


def test_cmd_ml_build_dataset_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(["ml", "build-dataset", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_ml_build_dataset(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_ml_train_and_evaluate_round_trip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    _collect_ml_candles(monkeypatch, "CLI_ML_TRAIN")
    capsys.readouterr()

    models_dir = tmp_path / "models"
    settings = cli.get_settings()
    monkeypatch.setattr(settings, "ml_models_dir", str(models_dir))

    dataset_csv = tmp_path / "dataset.csv"
    build_args = cli.build_parser().parse_args(
        [
            "ml",
            "build-dataset",
            "--symbol",
            "CLI_ML_TRAIN",
            "--timeframe",
            "M1",
            "--strategy",
            "ema_crossover",
            "--stop-points",
            "50",
            "--target-points",
            "50",
            "--max-horizon-bars",
            "30",
            "--out",
            str(dataset_csv),
        ]
    )
    assert cli.cmd_ml_build_dataset(build_args) == 0
    capsys.readouterr()

    train_args = cli.build_parser().parse_args(
        [
            "ml",
            "train",
            "--dataset",
            str(dataset_csv),
            "--symbol",
            "CLI_ML_TRAIN",
            "--timeframe",
            "M1",
            "--strategy-name",
            "ema_crossover_baseline",
            "--model",
            "logistic_regression",
        ]
    )
    train_result = cli.cmd_ml_train(train_args)
    train_captured = capsys.readouterr()

    assert train_result == 0
    assert "modelo registrado" in train_captured.out
    assert (models_dir / "manifest.json").exists()

    evaluate_args = cli.build_parser().parse_args(["ml", "evaluate"])
    evaluate_result = cli.cmd_ml_evaluate(evaluate_args)
    evaluate_captured = capsys.readouterr()

    assert evaluate_result == 0
    assert "versao:" in evaluate_captured.out
    assert "Classificacao" in evaluate_captured.out


def test_cmd_ml_train_fails_for_empty_dataset(
    capsys: pytest.CaptureFixture, engine, tmp_path
) -> None:
    import pandas as pd

    from app.ml.datasets import ML_METADATA_COLUMNS, ML_NUMERIC_FEATURE_COLUMNS

    empty_csv = tmp_path / "empty.csv"
    pd.DataFrame(columns=list(ML_METADATA_COLUMNS) + list(ML_NUMERIC_FEATURE_COLUMNS)).to_csv(
        empty_csv, index=False
    )

    args = cli.build_parser().parse_args(
        [
            "ml",
            "train",
            "--dataset",
            str(empty_csv),
            "--symbol",
            "NEVER_COLLECTED",
            "--strategy-name",
            "s",
        ]
    )
    result = cli.cmd_ml_train(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_ml_evaluate_fails_without_registered_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, tmp_path
) -> None:
    settings = cli.get_settings()
    monkeypatch.setattr(settings, "ml_models_dir", str(tmp_path / "empty_models"))

    args = cli.build_parser().parse_args(["ml", "evaluate"])
    result = cli.cmd_ml_evaluate(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_walk_forward_reports_per_window_and_aggregate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_WALKFORWARD", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_WALKFORWARD", "--timeframe", "M1", "--count", "250"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    json_out = tmp_path / "walk_forward.json"
    args = cli.build_parser().parse_args(
        [
            "backtest",
            "walk-forward",
            "--symbol",
            "CLI_WALKFORWARD",
            "--timeframe",
            "M1",
            "--n-windows",
            "2",
            "--min-trades-per-window",
            "1",
            "--json-out",
            str(json_out),
        ]
    )
    result = cli.cmd_backtest_walk_forward(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Janela 0:" in captured.out
    assert "Janela 1:" in captured.out
    assert "ESTAVEL:" in captured.out
    assert json_out.exists()
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert len(payload["windows"]) == 2
    assert "aggregate_metrics" in payload


def test_cmd_backtest_walk_forward_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(
        ["backtest", "walk-forward", "--symbol", "NEVER_COLLECTED"]
    )
    result = cli.cmd_backtest_walk_forward(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_monte_carlo_reports_ruin_probability(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_MONTECARLO", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_MONTECARLO", "--timeframe", "M1", "--count", "250"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(
        [
            "backtest",
            "monte-carlo",
            "--symbol",
            "CLI_MONTECARLO",
            "--timeframe",
            "M1",
            "--num-simulations",
            "200",
            "--random-state",
            "42",
        ]
    )
    result = cli.cmd_backtest_monte_carlo(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "probabilidade_de_ruina" in captured.out
    assert "percentis do saldo final" in captured.out


def test_cmd_backtest_monte_carlo_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(["backtest", "monte-carlo", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_backtest_monte_carlo(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_backtest_stress_test_reports_degradation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_STRESSTEST", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_STRESSTEST", "--timeframe", "M1", "--count", "250"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(
        [
            "backtest",
            "stress-test",
            "--symbol",
            "CLI_STRESSTEST",
            "--timeframe",
            "M1",
            "--slippage-multiplier",
            "5",
            "--commission-multiplier",
            "5",
        ]
    )
    result = cli.cmd_backtest_stress_test(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "baseline:" in captured.out
    assert "stress (slippage x5" in captured.out
    assert "SOBREVIVE" in captured.out


def test_cmd_backtest_stress_test_fails_for_unknown_symbol(
    capsys: pytest.CaptureFixture, engine
) -> None:
    args = cli.build_parser().parse_args(["backtest", "stress-test", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_backtest_stress_test(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_ml_walk_forward_reports_windows_and_approval_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    _collect_ml_candles(monkeypatch, "CLI_ML_WALKFORWARD")
    capsys.readouterr()

    dataset_csv = tmp_path / "dataset.csv"
    build_args = cli.build_parser().parse_args(
        [
            "ml",
            "build-dataset",
            "--symbol",
            "CLI_ML_WALKFORWARD",
            "--timeframe",
            "M1",
            "--strategy",
            "ema_crossover",
            "--stop-points",
            "50",
            "--target-points",
            "50",
            "--max-horizon-bars",
            "30",
            "--out",
            str(dataset_csv),
        ]
    )
    assert cli.cmd_ml_build_dataset(build_args) == 0
    capsys.readouterr()

    walk_forward_args = cli.build_parser().parse_args(
        [
            "ml",
            "walk-forward",
            "--dataset",
            str(dataset_csv),
            "--symbol",
            "CLI_ML_WALKFORWARD",
            "--model",
            "logistic_regression",
            "--n-windows",
            "3",
        ]
    )
    result = cli.cmd_ml_walk_forward(walk_forward_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Janela 0:" in captured.out
    assert "Veredito por criterio" in captured.out
    assert "TODOS OS CRITERIOS PASSARAM:" in captured.out


def test_cmd_ml_walk_forward_fails_for_empty_dataset(
    capsys: pytest.CaptureFixture, engine, tmp_path
) -> None:
    import pandas as pd

    from app.ml.datasets import ML_METADATA_COLUMNS, ML_NUMERIC_FEATURE_COLUMNS

    empty_csv = tmp_path / "empty.csv"
    pd.DataFrame(columns=list(ML_METADATA_COLUMNS) + list(ML_NUMERIC_FEATURE_COLUMNS)).to_csv(
        empty_csv, index=False
    )

    args = cli.build_parser().parse_args(
        ["ml", "walk-forward", "--dataset", str(empty_csv), "--symbol", "NEVER_COLLECTED"]
    )
    result = cli.cmd_ml_walk_forward(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def _advance_mode_to(capsys: pytest.CaptureFixture, target: str) -> None:
    """`validate_transition` (Fase 10) so permite um passo por vez, na
    ordem DISABLED -> DATA_ONLY -> BACKTEST -> REPLAY -> PAPER. O modo e
    persistido no mesmo engine SQLite compartilhado por toda a sessao de
    testes (`cmd_mode_set` sempre commita), entao um teste anterior pode
    ja ter avancado o modo -- lê o modo atual primeiro em vez de assumir
    que comeca em DISABLED."""
    from app.database.repositories.system_setting_repository import get_current_mode

    order = ["DISABLED", "DATA_ONLY", "BACKTEST", "REPLAY", "PAPER", "DEMO"]
    session = cli.get_session_factory()()
    try:
        current = get_current_mode(session).value
    finally:
        session.close()

    if current == target:
        return

    current_index = order.index(current)
    target_index = order.index(target)

    if target_index < current_index:
        args = cli.build_parser().parse_args(["mode", "set", target])
        assert cli.cmd_mode_set(args) == 0
        capsys.readouterr()
        return

    for mode in order[current_index + 1 : target_index + 1]:
        args = cli.build_parser().parse_args(["mode", "set", mode])
        assert cli.cmd_mode_set(args) == 0
        capsys.readouterr()


def test_cmd_mode_show_defaults_to_disabled(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["mode", "show"])
    result = cli.cmd_mode_show(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "DISABLED" in captured.out


def test_cmd_mode_set_transitions_and_show_reflects_it(
    capsys: pytest.CaptureFixture, engine
) -> None:
    set_args = cli.build_parser().parse_args(["mode", "set", "DATA_ONLY", "--reason", "teste cli"])
    result = cli.cmd_mode_set(set_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "DATA_ONLY" in captured.out

    show_args = cli.build_parser().parse_args(["mode", "show"])
    cli.cmd_mode_show(show_args)
    captured = capsys.readouterr()
    assert "DATA_ONLY" in captured.out


def test_cmd_mode_set_rejects_invalid_transition(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["mode", "set", "PAPER"])  # pula estados
    result = cli.cmd_mode_set(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_mode_set_rejects_jumping_straight_to_real(
    capsys: pytest.CaptureFixture, engine
) -> None:
    """REAL existe, mas so no fim da escada: de DISABLED nao se chega la."""
    args = cli.build_parser().parse_args(["mode", "set", "REAL_ENABLED"])
    result = cli.cmd_mode_set(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_paper_run_requires_paper_mode(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["paper", "run", "--symbol", "EURUSD"])
    result = cli.cmd_paper_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err
    assert "PAPER" in captured.err


def test_cmd_paper_run_collects_and_processes_bars_when_in_paper_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "PAPER")

    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_PAPER1", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        [
            "paper",
            "run",
            "--symbol",
            "CLI_PAPER1",
            "--timeframe",
            "M1",
            "--lookback-bars",
            "250",
            "--iterations",
            "1",
        ]
    )
    result = cli.cmd_paper_run(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "barras novas processadas" in captured.out


def test_cmd_paper_run_fails_for_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "PAPER")

    client = FakeMT5Client()
    client.symbol_info_result = None
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["paper", "run", "--symbol", "NEVER_COLLECTED", "--iterations", "1"]
    )
    result = cli.cmd_paper_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_paper_status_reports_no_trades_when_none_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_PAPER2", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(5))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_PAPER2", "--timeframe", "M1", "--count", "5"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(["paper", "status", "--symbol", "CLI_PAPER2"])
    result = cli.cmd_paper_status(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "nenhum paper trade registrado" in captured.out


def test_cmd_paper_status_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["paper", "status", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_paper_status(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_demo_run_requires_demo_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "PAPER")

    args = cli.build_parser().parse_args(["demo", "run", "--symbol", "EURUSD"])
    result = cli.cmd_demo_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err
    assert "DEMO" in captured.err


def test_cmd_demo_run_refuses_real_account_even_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "DEMO")

    client = FakeMT5Client()
    client.account_info_result = make_account_info(trade_mode=client.ACCOUNT_TRADE_MODE_REAL)
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["demo", "run", "--symbol", "CLI_DEMO_REAL", "--iterations", "1"]
    )
    result = cli.cmd_demo_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err
    assert "nao e demo" in captured.err


def test_cmd_demo_run_processes_bars_on_demo_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "DEMO")

    client = FakeMT5Client()
    client.account_info_result = make_account_info(trade_mode=client.ACCOUNT_TRADE_MODE_DEMO)
    client.symbol_info_result = make_symbol_info(name="CLI_DEMO1", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(250))
    client.order_send_result = make_order_send_result()
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        [
            "demo",
            "run",
            "--symbol",
            "CLI_DEMO1",
            "--timeframe",
            "M1",
            "--lookback-bars",
            "250",
            "--iterations",
            "1",
        ]
    )
    result = cli.cmd_demo_run(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "barras novas processadas" in captured.out


def test_cmd_demo_run_fails_for_unknown_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _advance_mode_to(capsys, "DEMO")

    client = FakeMT5Client()
    client.account_info_result = make_account_info(trade_mode=client.ACCOUNT_TRADE_MODE_DEMO)
    client.symbol_info_result = None
    _patch_connection(monkeypatch, client)

    args = cli.build_parser().parse_args(
        ["demo", "run", "--symbol", "NEVER_COLLECTED", "--iterations", "1"]
    )
    result = cli.cmd_demo_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_demo_status_reports_no_trades_when_none_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_DEMO2", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(5))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_DEMO2", "--timeframe", "M1", "--count", "5"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(["demo", "status", "--symbol", "CLI_DEMO2"])
    result = cli.cmd_demo_status(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "nenhum live trade registrado" in captured.out


def test_cmd_demo_status_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["demo", "status", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_demo_status(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_monitor_feed_reports_healthy_for_fresh_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    now_ts = int(datetime.now(UTC).timestamp())
    rows = [(now_ts - (4 - i) * 60, 1.1000, 1.1010, 1.0990, 1.1005, 100, 2, 0) for i in range(5)]

    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_FEED_FRESH", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(rows)
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_FEED_FRESH", "--timeframe", "M1", "--count", "5"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(["monitor", "feed", "--symbol", "CLI_FEED_FRESH"])
    result = cli.cmd_monitor_feed(args)
    captured = capsys.readouterr()

    assert result == 0
    assert "saudável: True" in captured.out


def test_cmd_monitor_feed_reports_unhealthy_and_persists_drift_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    client = FakeMT5Client()
    client.symbol_info_result = make_symbol_info(name="CLI_FEED_STALE", digits=5, point=0.00001)
    client.copy_rates_from_pos_result = make_rates_array(_make_synthetic_rate_rows(5))
    _patch_connection(monkeypatch, client)

    collect_args = cli.build_parser().parse_args(
        ["collect", "candles", "--symbol", "CLI_FEED_STALE", "--timeframe", "M1", "--count", "5"]
    )
    assert cli.cmd_collect_candles(collect_args) == 0
    capsys.readouterr()

    args = cli.build_parser().parse_args(["monitor", "feed", "--symbol", "CLI_FEED_STALE"])
    result = cli.cmd_monitor_feed(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err

    events = DriftEventRepository(db_session).list_recent(limit=50)
    assert any(e.drift_type == "DATA_FEED" and e.metric_name == "feed_age_seconds" for e in events)


def test_cmd_monitor_feed_fails_for_unknown_symbol(capsys: pytest.CaptureFixture, engine) -> None:
    args = cli.build_parser().parse_args(["monitor", "feed", "--symbol", "NEVER_COLLECTED"])
    result = cli.cmd_monitor_feed(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_monitor_model_runs_end_to_end_against_registered_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    engine,
    db_session,
    tmp_path,
) -> None:
    _collect_ml_candles(monkeypatch, "CLI_MONITOR_MODEL")
    capsys.readouterr()

    models_dir = tmp_path / "models"
    settings = cli.get_settings()
    monkeypatch.setattr(settings, "ml_models_dir", str(models_dir))

    dataset_csv = tmp_path / "dataset.csv"
    build_args = cli.build_parser().parse_args(
        [
            "ml",
            "build-dataset",
            "--symbol",
            "CLI_MONITOR_MODEL",
            "--timeframe",
            "M1",
            "--strategy",
            "ema_crossover",
            "--stop-points",
            "50",
            "--target-points",
            "50",
            "--max-horizon-bars",
            "30",
            "--out",
            str(dataset_csv),
        ]
    )
    assert cli.cmd_ml_build_dataset(build_args) == 0
    capsys.readouterr()

    train_args = cli.build_parser().parse_args(
        [
            "ml",
            "train",
            "--dataset",
            str(dataset_csv),
            "--symbol",
            "CLI_MONITOR_MODEL",
            "--timeframe",
            "M1",
            "--strategy-name",
            "ema_crossover_baseline",
            "--model",
            "logistic_regression",
        ]
    )
    assert cli.cmd_ml_train(train_args) == 0
    capsys.readouterr()

    monitor_args = cli.build_parser().parse_args(
        ["monitor", "model", "--recent-dataset", str(dataset_csv)]
    )
    result = cli.cmd_monitor_model(monitor_args)
    captured = capsys.readouterr()

    assert result == 0
    assert "Drift de features" in captured.out
    assert "Drift de calibração" in captured.out


def test_cmd_monitor_model_fails_without_registered_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, tmp_path
) -> None:
    settings = cli.get_settings()
    monkeypatch.setattr(settings, "ml_models_dir", str(tmp_path / "empty_models"))

    args = cli.build_parser().parse_args(
        ["monitor", "model", "--recent-dataset", str(tmp_path / "nonexistent.csv")]
    )
    result = cli.cmd_monitor_model(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_preflight_check_runs_and_reports_all_checks(
    capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    args = cli.build_parser().parse_args(["preflight", "check"])
    result = cli.cmd_preflight_check(args)
    captured = capsys.readouterr()

    assert result in (0, 1)
    for name in (
        "secret_key",
        "database",
        "migrations",
        "log_dir",
        "ml_models_dir",
        "ml_datasets_dir",
        "mt5_credentials",
    ):
        assert name in captured.out
    assert "resultado geral" in captured.out


def test_cmd_preflight_check_fails_when_secret_key_is_placeholder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    settings = cli.get_settings()
    monkeypatch.setattr(settings, "app_secret_key", "CHANGE_ME_in_dot_env")
    monkeypatch.setattr(settings, "app_env", "production")

    args = cli.build_parser().parse_args(["preflight", "check"])
    result = cli.cmd_preflight_check(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "[FALHA] secret_key" in captured.out


def test_cmd_preflight_check_fails_when_directory_is_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, engine, db_session, tmp_path
) -> None:
    blocked = tmp_path / "blocked_logs"
    blocked.write_text("i am a file, not a directory")

    settings = cli.get_settings()
    monkeypatch.setattr(settings, "log_dir", str(blocked))

    args = cli.build_parser().parse_args(["preflight", "check"])
    result = cli.cmd_preflight_check(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "[FALHA] log_dir" in captured.out


def _make_analysis_uptrend_rows(n: int) -> list[tuple[float, float, float, float]]:
    """(open, high, low, close) de uma serie de alta com pequena oscilacao
    -- mesma ideia de `tests/unit/services/test_analysis_service.py`, sem
    depender de numpy aqui (a CLI so precisa de candles plausiveis, nao de
    um teste estatistico preciso)."""
    rows = []
    price = 100.0
    for i in range(n):
        price = 100.0 + (i / n) * 60.0 + 5 * math.sin(i * 0.6)
        open_ = price - 0.05
        close = price
        high = max(open_, close) + 0.05
        low = min(open_, close) - 0.05
        rows.append((open_, high, low, close))
    # Ultimo candle: marubozu de alta forte.
    last_open = rows[-1][3]
    last_close = last_open + 3.0
    rows[-1] = (last_open, last_close + 0.01, last_open - 0.01, last_close)
    return rows


def _seed_candles_for_analysis(db_session, symbol_name: str, timeframe: str, n: int) -> None:
    from app.database.repositories.candle_repository import CandleRepository
    from app.mt5.market_data import RawCandle
    from app.mt5.symbol_mapper import SymbolSpecification

    symbol = SymbolRepository(db_session).upsert_from_specification(
        SymbolSpecification(
            name=symbol_name,
            description="Test symbol",
            digits=5,
            point=0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_contract_size=100_000.0,
            spread=2,
            trade_mode=4,
            visible=True,
        )
    )
    start = datetime(2026, 6, 1, tzinfo=UTC) - timedelta(minutes=n)
    candles = [
        RawCandle(
            open_time=start + timedelta(minutes=i),
            open=row[0],
            high=row[1],
            low=row[2],
            close=row[3],
            tick_volume=100 + i,
            spread=2,
            real_volume=0,
        )
        for i, row in enumerate(_make_analysis_uptrend_rows(n))
    ]
    CandleRepository(db_session).bulk_upsert(symbol.id, timeframe, candles)
    db_session.commit()


def test_cmd_analysis_run_unknown_symbol_exits_1(
    capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    args = cli.build_parser().parse_args(["analysis", "run", "--symbol", "CLI_ANALYSIS_UNKNOWN"])
    result = cli.cmd_analysis_run(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "ERRO" in captured.err


def test_cmd_analysis_run_enters_on_clean_uptrend(
    capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _seed_candles_for_analysis(db_session, "CLI_ANALYSIS_UPTREND", "M15", 260)

    args = cli.build_parser().parse_args(
        [
            "analysis",
            "run",
            "--symbol",
            "CLI_ANALYSIS_UPTREND",
            "--timeframe",
            "M15",
            "--threshold",
            "40.0",
            # Este teste cobre a leitura do relatorio, nao a cobertura de
            # dados: so o M15 e semeado, e os portoes (independentes do
            # limiar) barrariam antes de chegar ao texto.
            "--no-gates",
        ]
    )
    result = cli.cmd_analysis_run(args)
    captured = capsys.readouterr()

    assert result == 0
    assert ">>> ENTRAR <<<" in captured.out
    assert "Niveis de trade" in captured.out
    assert "Score composto" in captured.out


def test_cmd_analysis_run_do_not_enter_on_choppy_market(
    capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    from app.database.repositories.candle_repository import CandleRepository
    from app.mt5.market_data import RawCandle
    from app.mt5.symbol_mapper import SymbolSpecification

    symbol = SymbolRepository(db_session).upsert_from_specification(
        SymbolSpecification(
            name="CLI_ANALYSIS_CHOPPY",
            description="Test symbol",
            digits=5,
            point=0.00001,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_contract_size=100_000.0,
            spread=2,
            trade_mode=4,
            visible=True,
        )
    )
    n = 260
    start = datetime(2026, 6, 1, tzinfo=UTC) - timedelta(minutes=n)
    candles = []
    price = 100.0
    for i in range(n):
        close = 100.0 + math.sin(i * 0.8) * 2.0
        candles.append(
            RawCandle(
                open_time=start + timedelta(minutes=i),
                open=price,
                high=max(price, close) + 0.05,
                low=min(price, close) - 0.05,
                close=close,
                tick_volume=100,
                spread=2,
                real_volume=0,
            )
        )
        price = close
    CandleRepository(db_session).bulk_upsert(symbol.id, "M15", candles)
    db_session.commit()

    args = cli.build_parser().parse_args(
        ["analysis", "run", "--symbol", "CLI_ANALYSIS_CHOPPY", "--timeframe", "M15"]
    )
    result = cli.cmd_analysis_run(args)
    captured = capsys.readouterr()

    assert result == 0
    assert ">>> NAO OPERAR <<<" in captured.out
    assert "Motivos:" in captured.out


def test_cmd_analysis_run_json_output_is_valid_json(
    capsys: pytest.CaptureFixture, engine, db_session
) -> None:
    _seed_candles_for_analysis(db_session, "CLI_ANALYSIS_JSON", "M15", 260)

    args = cli.build_parser().parse_args(
        ["analysis", "run", "--symbol", "CLI_ANALYSIS_JSON", "--timeframe", "M15", "--json"]
    )
    result = cli.cmd_analysis_run(args)
    captured = capsys.readouterr()

    assert result == 0
    payload = json.loads(captured.out)
    assert payload["symbol"] == "CLI_ANALYSIS_JSON"
    assert len(payload["score"]["factors"]) == 7
    assert payload["recommendation"] in ("ENTER", "DO_NOT_ENTER")


def _write_calendar(tmp_path, eventos) -> str:
    import json

    caminho = tmp_path / "calendar.json"
    caminho.write_text(json.dumps(eventos), encoding="utf-8")
    return str(caminho)


def test_cmd_calendar_check_reports_an_inactive_filter(
    capsys: pytest.CaptureFixture, engine, monkeypatch
) -> None:
    """Sem calendario o filtro fica INATIVO, e o comando precisa dizer isso
    com todas as letras — o robo segue operando sem essa protecao."""
    from app.calendar_feed.factory import reset_calendar_provider

    reset_calendar_provider()
    args = cli.build_parser().parse_args(["calendar", "check"])
    result = cli.cmd_calendar_check(args)
    captured = capsys.readouterr()

    assert result == 1
    assert "INATIVO" in captured.out


def test_cmd_calendar_check_blocks_on_an_imminent_event(
    capsys: pytest.CaptureFixture, engine, tmp_path, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.calendar_feed.factory import reset_calendar_provider
    from app.core.config import get_settings

    agora = datetime.now(UTC)
    caminho = _write_calendar(
        tmp_path,
        [
            {
                "title": "Non-Farm Payrolls",
                "scheduled_at": (agora + timedelta(minutes=10)).isoformat(),
                "currency": "USD",
                "impact": "HIGH",
            }
        ],
    )
    get_settings.cache_clear()
    monkeypatch.setenv("CALENDAR_FILE_PATH", caminho)
    reset_calendar_provider()

    args = cli.build_parser().parse_args(["calendar", "check", "--symbol", "EURUSD"])
    result = cli.cmd_calendar_check(args)
    captured = capsys.readouterr()

    get_settings.cache_clear()
    reset_calendar_provider()

    assert result == 0
    assert "BLOQUEADA" in captured.out
    assert "Non-Farm Payrolls" in captured.out


def test_cmd_calendar_check_ignores_an_event_of_another_currency(
    capsys: pytest.CaptureFixture, engine, tmp_path, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.calendar_feed.factory import reset_calendar_provider
    from app.core.config import get_settings

    agora = datetime.now(UTC)
    caminho = _write_calendar(
        tmp_path,
        [
            {
                "title": "BoJ Decision",
                "scheduled_at": (agora + timedelta(minutes=10)).isoformat(),
                "currency": "JPY",
                "impact": "HIGH",
            }
        ],
    )
    get_settings.cache_clear()
    monkeypatch.setenv("CALENDAR_FILE_PATH", caminho)
    reset_calendar_provider()

    args = cli.build_parser().parse_args(["calendar", "check", "--symbol", "EURUSD"])
    result = cli.cmd_calendar_check(args)
    captured = capsys.readouterr()

    get_settings.cache_clear()
    reset_calendar_provider()

    assert result == 0
    assert "LIBERADA" in captured.out
