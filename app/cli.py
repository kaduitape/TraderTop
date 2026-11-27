"""CLI da aplicacao. Uso: `python -m app.cli <comando> <subcomando> [opcoes]`.

Comandos somente-leitura em relacao ao MetaTrader: `mt5 check`, `mt5
symbols`, `collect candles`, `collect ticks` (incrementais e com
checagem de qualidade), `quality check`, `data retention` (aplica a
politica de retencao na hora), `data purge-ticks`,
`features build` (indicadores/features/regime), `backtest run` (backtest
por candle de uma estrategia), `backtest compare` (relatorio comparativo
entre todas as estrategias registradas), `backtest run-ticks` (backtest
por tick — fills realistas com latencia, spread real, slippage e
rejeicao), `ml build-dataset` (gera o dataset de sinais rotulado por
barreira tripla, Fase 8), `ml train` (treina + calibra + avalia +
registra um modelo, Fase 8), `ml evaluate` (recarrega uma versao
registrada e recalcula suas metricas, Fase 8) e, a partir da Fase 9,
`backtest walk-forward` (estabilidade entre janelas cronologicas),
`backtest monte-carlo` (risco de ruina empirico por reamostragem de
trades) e `backtest stress-test` (degradacao sob custos aumentados) para
estrategias, alem de `ml walk-forward` (janelas expansivas + relatorio de
aprovacao formal) para modelos. A partir da Fase 10, `mode show`/`mode
set` (maquina de estados do sistema) e `paper run`/`paper status` (paper
trading incremental, nunca envia ordem real).

A partir da Fase 11, `demo run`/`demo status` enviam ordens reais de
mercado a uma conta DEMO — e continuam recusando qualquer conta que nao
seja demo, porque chamam `app.mt5.orders.send_market_order` sem
`allow_real_account`. Passam por um motor de risco com poder de veto
(`app.risk`) antes de qualquer envio e exigem o modo `DEMO`
(`mode set DEMO`, so alcancavel apos `PAPER`).

`REAL_LOCKED`/`REAL_ENABLED` foram liberados por decisao explicita do
dono do sistema e sao alcancaveis pela escada de modos (nunca pulando
degraus). Operar em conta real e feito pela tela `/dashboard/trading`,
escolhendo o modo REAL; a CLI nao tem um comando que envie ordem a conta
real.

A partir da Fase 13, `monitor model` compara um modelo registrado contra
um dataset recente (drift de features via PSI, degradacao de calibracao/
desempenho em relacao ao treino) e `monitor feed` verifica se o feed de
candles de um simbolo/timeframe esta atualizado — ambos persistem
ocorrencias `WARNING`/`CRITICAL` em `drift_events`, nunca decidem
sozinhos desativar um modelo ou parar o sistema. O motor de risco
(`app.risk.engine.evaluate_signal`) tambem passou a rejeitar sinais
quando o feed esta atrasado (`app.risk.feed_health`), fechando uma
pendencia deixada em aberto na Fase 11.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from app.apexflow.config import load_apexflow_config
from app.apexflow.engine import analyze as apexflow_analyze
from app.apexflow.mtf import UnsupportedEntryTimeframeError
from app.backtesting.comparison import build_comparison_row, format_comparison_table
from app.backtesting.costs import CostModel
from app.backtesting.engine import BacktestConfig, BacktestResult, CandleBacktestEngine
from app.backtesting.fills import TickCostModel
from app.backtesting.monte_carlo import simulate_bootstrap
from app.backtesting.reports import build_report, format_report_text, report_to_dict
from app.backtesting.robustness import run_cost_stress_test
from app.backtesting.tick_engine import TickBacktestConfig, TickBacktestEngine
from app.backtesting.walk_forward import run_walk_forward
from app.core.config import get_settings
from app.core.enums import SystemMode
from app.core.exceptions import MT5ConnectionError, MT5RealAccountError
from app.core.logging import configure_logging
from app.core.system_mode import SystemModeError
from app.database.models.candle import Candle
from app.database.repositories.apexflow_decision_repository import (
    ApexFlowDecisionRepository,
)
from app.database.repositories.audit_log_repository import AuditLogRepository
from app.database.repositories.candle_repository import CandleRepository
from app.database.repositories.data_quality_repository import DataQualityEventRepository
from app.database.repositories.drift_event_repository import DriftEventRepository
from app.database.repositories.live_trade_repository import LiveTradeRepository
from app.database.repositories.paper_trade_repository import PaperTradeRepository
from app.database.repositories.symbol_repository import SymbolRepository
from app.database.repositories.system_setting_repository import get_current_mode, set_mode
from app.database.repositories.tick_repository import TickRepository
from app.database.session import get_session_factory
from app.execution.automation_settings import load_trading_automation_config
from app.execution.autopilot import run_autopilot_cycle
from app.execution.autopilot_status import (
    AutopilotStatusPublisher,
    load_autopilot_status,
)
from app.execution.engine import (
    DemoExecutionEngine,
    OrderRejectedByBroker,
    PositionClosed,
    PositionOpened,
    PositionReconciling,
    SignalRejected,
)
from app.market import features as features_module
from app.market import regimes as regimes_module
from app.market.data_quality import (
    DataQualityIssue,
    check_candles,
    check_ticks,
    compute_score,
    is_acceptable,
)
from app.market.multi_timeframe import SymbolNotFoundError
from app.ml.approval import evaluate_approval
from app.ml.calibration import calibrate_model, split_fit_calibration
from app.ml.datasets import (
    ML_CATEGORICAL_FEATURE_COLUMNS,
    ML_NUMERIC_FEATURE_COLUMNS,
    build_signal_dataset,
)
from app.ml.explainability import explain_model
from app.ml.registry import ModelRegistry, ModelRegistryError
from app.ml.splits import temporal_train_test_split
from app.ml.train import MODEL_NAMES, train_model
from app.ml.validation import compute_classification_metrics, compute_trading_metrics
from app.ml.walk_forward import run_ml_walk_forward
from app.monitoring.drift import DriftSeverity, detect_feature_drift, detect_metric_drift
from app.monitoring.preflight import CheckStatus, run_all_checks, worst_status
from app.mt5.account import fetch_account_snapshot
from app.mt5.connection import MT5Connection, MT5ConnectionConfig
from app.mt5.market_data import (
    TIMEFRAME_SECONDS,
    Timeframe,
    fetch_candles_from_pos,
    fetch_candles_range,
    fetch_server_time,
    fetch_ticks_range,
)
from app.mt5.symbol_mapper import fetch_symbol_specification, list_symbols
from app.mt5.terminal_health import fetch_terminal_health
from app.news.call_log import ORIGIN_CLI, calls_from
from app.paper_trading.engine import PaperTradeClosed, PaperTradeOpened, PaperTradingEngine
from app.risk.config import RiskLimits
from app.risk.feed_health import check_feed_health
from app.services.analysis_service import AnalysisReport, analyze_symbol
from app.strategies.base import Strategy
from app.strategies.registry import STRATEGY_NAMES, create_strategy
from app.strategies.trend.ma_crossover import EmaCrossoverConfig, EmaCrossoverStrategy

_ML_FEATURE_COLUMNS = list(ML_NUMERIC_FEATURE_COLUMNS) + list(ML_CATEGORICAL_FEATURE_COLUMNS)

_APPROVAL_CHECKLIST = """Criterios de aprovacao (prompt mestre, secao 12) — avaliacao MANUAL, nunca automatica:
  1. Supera o baseline fora da amostra, DEPOIS de custos.
  2. Probabilidades razoavelmente calibradas (ver curva de calibracao).
  3. Ainda e util (edge positivo) depois de custos reais.
  4. Numero de trades suficiente para conclusao estatistica.
  5. Estavel entre periodos/regimes, nao dependente de uma janela excepcional."""

logger = logging.getLogger(__name__)


def _print_json(payload: object) -> None:
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    print(json.dumps(payload, default=str, ensure_ascii=False, indent=2))


def _print_issues(issues: list[DataQualityIssue]) -> None:
    for issue in issues:
        print(f"  [{issue.severity.value}] {issue.check}: {issue.message}", file=sys.stderr)


def _senha_interativa() -> str | None:
    """Le a senha do terminal, duas vezes, sem ecoar.

    A senha NUNCA e argumento de linha de comando. Argumento aparece no
    historico do shell, no `ps` de qualquer usuario da maquina e no log de
    quem gravou a sessao — e isso vale ainda mais aqui, onde o comando roda
    numa VPS compartilhada com o terminal da corretora.
    """
    import getpass

    try:
        senha = getpass.getpass("Nova senha: ")
        confirmacao = getpass.getpass("Repita a nova senha: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelado.", file=sys.stderr)
        return None

    if senha != confirmacao:
        print("ERRO: as senhas nao conferem.", file=sys.stderr)
        return None
    if len(senha) < 8:
        print("ERRO: use ao menos 8 caracteres.", file=sys.stderr)
        return None
    return senha


def cmd_user_list(_args: argparse.Namespace) -> int:
    """Quem existe, com papel e estado.

    E o primeiro passo de "nao consigo entrar": a causa pode ser senha
    errada, mas tambem usuario inativo, nome diferente do lembrado, ou
    nenhum usuario cadastrado — e sao quatro correcoes diferentes.
    """
    from app.database.models.user import User

    session = get_session_factory()()
    try:
        usuarios = session.query(User).order_by(User.id).all()
        if not usuarios:
            print(
                "Nenhum usuario cadastrado. Crie um com:\n"
                "  python -m app.cli user create --username admin --email voce@exemplo.com --admin"
            )
            return 0

        print(f"\n{'ID':>4}  {'USUARIO':<20} {'EMAIL':<32} {'ESTADO':<8} PAPEIS")
        for usuario in usuarios:
            papeis = ", ".join(papel.name for papel in usuario.roles) or "—"
            estado = "ativo" if usuario.is_active else "INATIVO"
            print(
                f"{usuario.id:>4}  {usuario.username:<20} {usuario.email:<32} "
                f"{estado:<8} {papeis}"
            )
        print()
        return 0
    finally:
        session.close()


def cmd_user_reset_password(args: argparse.Namespace) -> int:
    """Redefine a senha de um usuario. So a partir do servidor."""
    from app.core.security import hash_password
    from app.database.repositories.user_repository import UserRepository

    session = get_session_factory()()
    try:
        repo = UserRepository(session)
        usuario = repo.get_by_username(args.username)
        if usuario is None:
            print(
                f"ERRO: usuario '{args.username}' nao existe. "
                "Veja os cadastrados com `python -m app.cli user list`.",
                file=sys.stderr,
            )
            return 1

        senha = _senha_interativa()
        if senha is None:
            return 1

        usuario.password_hash = hash_password(senha)
        del senha
        if args.activate and not usuario.is_active:
            usuario.is_active = True

        AuditLogRepository(session).record(
            user_id=usuario.id,
            action="password_reset",
            entity="user",
            # Registra QUE mudou, nunca para qual valor.
            detail=f"senha redefinida via CLI para {usuario.username}",
        )
        session.commit()
        print(f"Senha de '{usuario.username}' redefinida.")
        if not usuario.is_active:
            print(
                "ATENCAO: o usuario esta INATIVO e o login vai continuar "
                "recusando. Repita com --activate.",
                file=sys.stderr,
            )
        return 0
    finally:
        session.close()


def cmd_user_create(args: argparse.Namespace) -> int:
    """Cria um usuario. Necessario quando a tabela esta vazia — nesse caso
    redefinir senha nao resolve, porque nao ha o que redefinir."""
    from app.core.security import hash_password
    from app.database.repositories.user_repository import UserRepository

    session = get_session_factory()()
    try:
        repo = UserRepository(session)
        if repo.get_by_username(args.username) is not None:
            print(
                f"ERRO: '{args.username}' ja existe. Para trocar a senha use "
                "`python -m app.cli user reset-password`.",
                file=sys.stderr,
            )
            return 1
        if repo.get_by_email(args.email) is not None:
            print(f"ERRO: e-mail '{args.email}' ja cadastrado.", file=sys.stderr)
            return 1

        senha = _senha_interativa()
        if senha is None:
            return 1

        papeis = [repo.get_or_create_role("ADMIN")] if args.admin else []
        usuario = repo.create_user(
            username=args.username,
            email=args.email,
            password_hash=hash_password(senha),
            roles=papeis,
        )
        del senha
        AuditLogRepository(session).record(
            user_id=usuario.id,
            action="user_create",
            entity="user",
            detail=f"{usuario.username} criado via CLI"
            + (" com papel ADMIN" if args.admin else ""),
        )
        session.commit()
        print(f"Usuario '{usuario.username}' criado.")
        return 0
    finally:
        session.close()


def cmd_mt5_bridge(args: argparse.Namespace) -> int:
    """Diagnostica a ponte com o MetaTrader sob Wine, passo a passo.

    "Nao conecta" nao e diagnostico. Entre o painel e o terminal ha seis
    coisas que podem estar erradas, cada uma com uma correcao diferente —
    este comando diz QUAL delas.
    """
    from app.mt5.bridge import DEFAULT_BRIDGE_PORT
    from app.mt5.bridge_check import check_bridge

    settings = get_settings()
    host = args.host or getattr(settings, "mt5_bridge_host", None)
    porta = args.port or int(
        getattr(settings, "mt5_bridge_port", DEFAULT_BRIDGE_PORT)
    )

    if not host:
        print(
            "Nenhuma ponte configurada. Defina MT5_BRIDGE_HOST no .env ou "
            "passe --host.",
            file=sys.stderr,
        )
        return 1

    relatorio = check_bridge(host, porta, timeout=args.timeout)
    print(f"\nPonte MetaTrader — {host}:{porta}\n")
    for passo in relatorio.steps:
        print(f"  [{passo.icon}] {passo.name}: {passo.detail}")

    if relatorio.ok:
        print("\nPonte funcionando. O painel consegue falar com o terminal.")
        return 0

    falha = relatorio.first_failure
    print(f"\nParou em: {falha.name}" if falha else "\nFalhou.")
    return 1


def cmd_mt5_check(_args: argparse.Namespace) -> int:
    settings = get_settings()
    config = MT5ConnectionConfig.from_settings(settings)
    try:
        with MT5Connection(config) as connection:
            health = fetch_terminal_health(connection.client)
            account = fetch_account_snapshot(connection.client)
    except MT5ConnectionError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    if health is None or account is None:
        print("ERRO: conectado, mas terminal/conta nao respondeu.", file=sys.stderr)
        return 1

    _print_json({"terminal": dataclasses.asdict(health), "account": dataclasses.asdict(account)})
    return 0


def cmd_mt5_symbols(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = MT5ConnectionConfig.from_settings(settings)
    try:
        with MT5Connection(config) as connection:
            names = list_symbols(connection.client, group=args.group)
    except MT5ConnectionError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    for name in names[: args.limit]:
        print(name)
    print(f"--- {len(names)} simbolo(s) no total ---", file=sys.stderr)
    return 0


def cmd_collect_candles(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = MT5ConnectionConfig.from_settings(settings)
    timeframe = Timeframe(args.timeframe)
    timeframe_seconds = TIMEFRAME_SECONDS[timeframe]

    session = get_session_factory()()
    try:
        existing_symbol = SymbolRepository(session).get_by_name(args.symbol)
        last_open_time = (
            CandleRepository(session).get_last_open_time(existing_symbol.id, timeframe.value)
            if existing_symbol is not None
            else None
        )

        try:
            with MT5Connection(config) as connection:
                spec = fetch_symbol_specification(connection.client, args.symbol)
                if spec is None:
                    print(f"ERRO: simbolo '{args.symbol}' nao encontrado.", file=sys.stderr)
                    return 1

                if last_open_time is not None:
                    date_from = last_open_time + timedelta(seconds=timeframe_seconds)
                    server_now = fetch_server_time(connection.client, args.symbol)
                    candles = fetch_candles_range(
                        connection.client,
                        args.symbol,
                        timeframe,
                        date_from,
                        server_now if server_now is not None else datetime.now(UTC),
                    )
                    mode = "incremental"
                else:
                    candles = fetch_candles_from_pos(
                        connection.client, args.symbol, timeframe, args.count
                    )
                    mode = "backfill"
        except MT5ConnectionError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1

        symbol = SymbolRepository(session).upsert_from_specification(spec)
        inserted = CandleRepository(session).bulk_upsert(symbol.id, timeframe.value, candles)

        issues = check_candles(candles, timeframe_seconds=timeframe_seconds)
        DataQualityEventRepository(session).bulk_insert(symbol.id, timeframe.value, issues)

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    score = compute_score(issues)
    print(
        f"modo: {mode} | candles buscadas: {len(candles)} | novas inseridas: {inserted} | "
        f"qualidade: {score}/100 ({len(issues)} ocorrencia(s))"
    )
    _print_issues(issues)
    return 0


def cmd_collect_ticks(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = MT5ConnectionConfig.from_settings(settings)
    date_to = datetime.now(UTC)

    session = get_session_factory()()
    try:
        existing_symbol = SymbolRepository(session).get_by_name(args.symbol)
        last_timestamp = (
            TickRepository(session).get_last_timestamp(existing_symbol.id)
            if existing_symbol is not None
            else None
        )

        if last_timestamp is not None:
            date_from = last_timestamp + timedelta(microseconds=1)
            mode = "incremental"
        else:
            date_from = date_to - timedelta(seconds=args.seconds)
            mode = "backfill"

        try:
            with MT5Connection(config) as connection:
                spec = fetch_symbol_specification(connection.client, args.symbol)
                if spec is None:
                    print(f"ERRO: simbolo '{args.symbol}' nao encontrado.", file=sys.stderr)
                    return 1
                ticks = fetch_ticks_range(connection.client, args.symbol, date_from, date_to)
        except MT5ConnectionError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1

        symbol = SymbolRepository(session).upsert_from_specification(spec)
        inserted = TickRepository(session).bulk_upsert(symbol.id, ticks)

        issues = check_ticks(
            ticks,
            point=float(symbol.point),
            max_spread_points=settings.quality_max_spread_points,
            now=date_to,
            max_feed_delay_seconds=settings.quality_max_feed_delay_seconds,
        )
        DataQualityEventRepository(session).bulk_insert(symbol.id, None, issues)

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    score = compute_score(issues)
    print(
        f"modo: {mode} | ticks buscados: {len(ticks)} | novos inseridos: {inserted} | "
        f"qualidade: {score}/100 ({len(issues)} ocorrencia(s))"
    )
    _print_issues(issues)
    return 0


def cmd_quality_check(args: argparse.Namespace) -> int:
    settings = get_settings()
    now = datetime.now(UTC)

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1

        issues: list[DataQualityIssue] = []

        if args.timeframe:
            timeframe = Timeframe(args.timeframe)
            candles = CandleRepository(session).get_recent(symbol.id, timeframe.value, args.limit)
            if not candles:
                print(
                    f"Nenhuma candle {timeframe.value} armazenada para '{args.symbol}'.",
                    file=sys.stderr,
                )
            issues.extend(check_candles(candles, timeframe_seconds=TIMEFRAME_SECONDS[timeframe]))

        ticks = TickRepository(session).get_recent(symbol.id, args.limit)
        if ticks:
            issues.extend(
                check_ticks(
                    ticks,
                    point=float(symbol.point),
                    max_spread_points=settings.quality_max_spread_points,
                    now=now,
                    max_feed_delay_seconds=settings.quality_max_feed_delay_seconds,
                )
            )
    finally:
        session.close()

    score = compute_score(issues)
    acceptable = is_acceptable(issues, min_score=settings.quality_min_score)
    print(f"qualidade: {score}/100 | aceitavel: {acceptable} | {len(issues)} ocorrencia(s)")
    _print_issues(issues)
    return 0 if acceptable else 1


def cmd_data_purge_ticks(args: argparse.Namespace) -> int:
    settings = get_settings()
    retention_days = (
        args.older_than_days if args.older_than_days is not None else settings.tick_retention_days
    )

    session = get_session_factory()()
    try:
        deleted = TickRepository(session).purge_older_than(retention_days, now=datetime.now(UTC))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(f"ticks removidos (mais antigos que {retention_days} dia(s)): {deleted}")
    return 0


def cmd_data_retention(args: argparse.Namespace) -> int:
    """Aplica a politica de retencao inteira, agora.

    A mesma que a thread de manutencao executa — este comando existe para a
    PRIMEIRA limpeza, contra um acumulo que levou meses para se formar, e
    para quem quer decidir a hora em vez de esperar a madrugada.
    """
    from app.database.repositories.data_quality_repository import DataQualityEventRepository
    from app.maintenance.retention import (
        RetentionPolicy,
        apply_retention,
        optimize_tables,
    )

    settings = get_settings()
    policy = RetentionPolicy.from_settings(settings)
    if args.chunk_size is not None:
        policy = dataclasses.replace(policy, chunk_size=args.chunk_size)
    if args.max_chunks is not None:
        policy = dataclasses.replace(policy, max_chunks_per_run=args.max_chunks)

    problemas = policy.validate()
    if problemas:
        for problema in problemas:
            print(f"configuracao invalida: {problema}")
        return 1

    print(
        f"politica: ticks {policy.tick_days}d | qualidade {policy.quality_days}d "
        f"(CRITICAL {policy.quality_critical_days}d) | lotes de {policy.chunk_size}, "
        f"teto {policy.max_chunks_per_run}"
    )

    session = get_session_factory()()
    try:
        antes = TickRepository(session).summary()
        ticks_antes = sum(linha[1] for linha in antes)
        qualidade_antes = DataQualityEventRepository(session).count_by_severity()
        print(
            f"antes: {ticks_antes} tick(s) | eventos de qualidade "
            f"{sum(qualidade_antes.values())} {dict(sorted(qualidade_antes.items()))}"
        )

        if args.dry_run:
            # Nao ha "contagem do que seria apagado" barata numa tabela de
            # milhoes de linhas: um COUNT com o mesmo WHERE varre o mesmo
            # indice e custa quase o que a purga custaria. O util aqui e
            # mostrar o corte e deixar a pessoa decidir.
            agora = datetime.now(UTC)
            print(f"[dry-run] corte de ticks: {agora - timedelta(days=policy.tick_days)}")
            print(
                f"[dry-run] corte de qualidade: "
                f"{agora - timedelta(days=policy.quality_days)} "
                f"(CRITICAL {agora - timedelta(days=policy.quality_critical_days)})"
            )
            return 0

        resultado = apply_retention(session, policy, now=datetime.now(UTC))
        print(resultado.describe())

        if args.optimize:
            # Depois da purga, nunca antes: reconstruir a tabela com as
            # linhas velhas ainda dentro copiaria justamente o que ia sair.
            print(
                "OPTIMIZE TABLE: reconstroi as tabelas e BLOQUEIA ESCRITA enquanto "
                "roda. Em tabela de gigabytes sao minutos com a coleta parada."
            )
            for tabela, estado in optimize_tables(session).items():
                print(f"  {tabela}: {estado}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return 0


def cmd_features_build(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    lookback = features_module.required_lookback_bars() + args.rows

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1

        candles = CandleRepository(session).get_recent(symbol.id, timeframe.value, lookback)
        point = float(symbol.point)
    finally:
        session.close()

    if len(candles) < 2:
        print(
            f"ERRO: dados insuficientes ({len(candles)} candle(s)) para '{args.symbol}' "
            f"{timeframe.value}. Colete mais dados primeiro.",
            file=sys.stderr,
        )
        return 1

    feature_frame = features_module.build_candle_features(candles, point=point)
    regime_frame = regimes_module.classify_regime_series(feature_frame)
    combined = feature_frame.join(regime_frame)

    display_columns = [
        "open_time",
        "close",
        "rsi_14",
        "adx_14",
        "atr_14",
        "trend",
        "volatility",
        "spread_adequate",
        "liquidity_adequate",
    ]
    print(combined[display_columns].tail(args.rows).to_string(index=False))

    latest_regime = regimes_module.classify_latest_regime(feature_frame)
    print()
    print(f"Regime atual: {latest_regime}")

    required = features_module.required_lookback_bars()
    if len(candles) < required:
        print(
            f"AVISO: apenas {len(candles)} candle(s) disponivel(is); {required} recomendadas "
            "para que todas as features fiquem sem NaN (ex.: ema_200).",
            file=sys.stderr,
        )

    return 0


def cmd_backtest_run(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1
        candles = CandleRepository(session).get_all(symbol.id, timeframe.value)
        point = float(symbol.point)
        contract_size = float(symbol.trade_contract_size)
    finally:
        session.close()

    if len(candles) < 2:
        print(
            f"ERRO: dados insuficientes ({len(candles)} candle(s)) para '{args.symbol}' "
            f"{timeframe.value}. Colete mais dados primeiro.",
            file=sys.stderr,
        )
        return 1

    bar_seconds = TIMEFRAME_SECONDS[timeframe]
    strategy = _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds)

    config = BacktestConfig(
        volume=args.volume,
        cost_model=CostModel(
            commission_per_lot=args.commission_per_lot,
            slippage_points=args.slippage_points,
        ),
    )
    engine = CandleBacktestEngine(
        strategy,
        config,
        point=point,
        contract_size=contract_size,
        initial_balance=args.initial_balance,
    )
    result = engine.run(candles, symbol=args.symbol, timeframe=timeframe.value)
    report = build_report(result)

    print(format_report_text(report))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as json_file:
            json.dump(report_to_dict(report), json_file, default=str, ensure_ascii=False, indent=2)
        print(f"\nRelatorio completo salvo em: {args.json_out}")

    return 0


def cmd_backtest_compare(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1
        candles = CandleRepository(session).get_all(symbol.id, timeframe.value)
        point = float(symbol.point)
        contract_size = float(symbol.trade_contract_size)
    finally:
        session.close()

    if len(candles) < 2:
        print(
            f"ERRO: dados insuficientes ({len(candles)} candle(s)) para '{args.symbol}' "
            f"{timeframe.value}. Colete mais dados primeiro.",
            file=sys.stderr,
        )
        return 1

    bar_seconds = TIMEFRAME_SECONDS[timeframe]
    backtest_config = BacktestConfig(
        volume=args.volume,
        cost_model=CostModel(
            commission_per_lot=args.commission_per_lot,
            slippage_points=args.slippage_points,
        ),
    )

    rows = []
    for name in STRATEGY_NAMES:
        strategy = create_strategy(name, point=point, bar_seconds=bar_seconds)
        engine = CandleBacktestEngine(
            strategy,
            backtest_config,
            point=point,
            contract_size=contract_size,
            initial_balance=args.initial_balance,
        )
        result = engine.run(candles, symbol=args.symbol, timeframe=timeframe.value)
        rows.append(build_comparison_row(result, initial_balance=args.initial_balance))

    print(format_comparison_table(rows))
    print(
        "\nNenhuma estrategia foi eleita 'vencedora' automaticamente — isso exige avaliar "
        "robustez (Fase 9), nao so o lucro liquido aqui.",
        file=sys.stderr,
    )
    return 0


def _build_strategy_from_args(
    args: argparse.Namespace, *, point: float, bar_seconds: int
) -> Strategy:
    if args.strategy == "ema_crossover":
        return EmaCrossoverStrategy(
            EmaCrossoverConfig(
                fast_column=f"ema_{args.fast}",
                slow_column=f"ema_{args.slow}",
                stop_loss_points=args.stop_points,
                take_profit_points=args.target_points,
            ),
            point=point,
            bar_seconds=bar_seconds,
        )
    return create_strategy(args.strategy, point=point, bar_seconds=bar_seconds)


def _add_strategy_selection_arguments(subparser: argparse.ArgumentParser) -> None:
    """Argumentos de seleção/configuração de estratégia compartilhados pelos
    comandos `backtest walk-forward/monte-carlo/stress-test` — os mesmos de
    `backtest run` (ver `_build_strategy_from_args`)."""
    subparser.add_argument("--symbol", required=True)
    subparser.add_argument("--timeframe", default="M1", choices=[t.value for t in Timeframe])
    subparser.add_argument("--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES))
    subparser.add_argument("--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS))
    subparser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    subparser.add_argument("--stop-points", type=float, default=100.0)
    subparser.add_argument("--target-points", type=float, default=200.0)
    subparser.add_argument("--volume", type=float, default=0.01)
    subparser.add_argument("--commission-per-lot", type=float, default=0.0)
    subparser.add_argument("--slippage-points", type=float, default=0.0)
    subparser.add_argument("--initial-balance", type=float, default=10_000.0)


def cmd_backtest_run_ticks(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1
        candles = CandleRepository(session).get_all(symbol.id, timeframe.value)
        if len(candles) < 2:
            print(
                f"ERRO: dados de candle insuficientes ({len(candles)}) para '{args.symbol}' "
                f"{timeframe.value}. Colete mais dados primeiro.",
                file=sys.stderr,
            )
            return 1

        point = float(symbol.point)
        contract_size = float(symbol.trade_contract_size)

        tick_start = candles[0].open_time
        tick_end = candles[-1].open_time + timedelta(seconds=bar_seconds * 5)
        ticks = TickRepository(session).get_range(symbol.id, start=tick_start, end=tick_end)
    finally:
        session.close()

    if len(ticks) < 2:
        print(
            f"ERRO: dados de tick insuficientes ({len(ticks)}) para '{args.symbol}' no periodo "
            "coberto pelas candles. Colete ticks primeiro (collect ticks).",
            file=sys.stderr,
        )
        return 1

    strategy = _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds)

    cost_model = TickCostModel(
        latency_ms=args.latency_ms,
        slippage_points=args.slippage_points,
        max_spread_points=args.max_spread_points,
        max_tick_gap_seconds=args.max_tick_gap_seconds,
        commission_per_lot=args.commission_per_lot,
    )
    config = TickBacktestConfig(
        volume=args.volume,
        cost_model=cost_model,
        max_holding_seconds=args.max_holding_seconds,
        trailing_stop_points=args.trailing_stop_points,
    )
    engine = TickBacktestEngine(
        strategy,
        config,
        point=point,
        contract_size=contract_size,
        bar_seconds=bar_seconds,
        initial_balance=args.initial_balance,
    )
    result = engine.run(candles, ticks, symbol=args.symbol, timeframe=timeframe.value)

    adapted_result = BacktestResult(
        symbol=result.symbol,
        timeframe=result.timeframe,
        strategy_name=result.strategy_name,
        initial_balance=result.initial_balance,
        trades=[trade.as_trade() for trade in result.trades],
        equity_curve=result.equity_curve,
    )
    report = build_report(adapted_result)
    print(format_report_text(report))

    liquidity_warnings = sum(1 for trade in result.trades if trade.liquidity_warning)
    print(
        f"\nRejeicoes de entrada: {len(result.rejections)} | "
        f"Trades com aviso de liquidez (gap de ticks): {liquidity_warnings}",
        file=sys.stderr,
    )
    for rejection in result.rejections[:10]:
        print(
            f"  [rejeitada] {rejection.direction.value}: {rejection.fill.rejection_reason}",
            file=sys.stderr,
        )

    if args.json_out:
        payload = report_to_dict(report)
        payload["rejections"] = [dataclasses.asdict(r) for r in result.rejections]
        payload["trades_audit"] = [
            {
                "entry_fill": dataclasses.asdict(t.entry_fill),
                "exit_fill": dataclasses.asdict(t.exit_fill),
            }
            for t in result.trades
        ]
        with open(args.json_out, "w", encoding="utf-8") as json_file:
            json.dump(payload, json_file, default=str, ensure_ascii=False, indent=2)
        print(
            f"Relatorio completo (com auditoria de fills) salvo em: {args.json_out}",
            file=sys.stderr,
        )

    return 0


def cmd_ml_build_dataset(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return 1
        candles = CandleRepository(session).get_all(symbol.id, timeframe.value)
        point = float(symbol.point)
    finally:
        session.close()

    required = features_module.required_lookback_bars() + args.max_horizon_bars
    if len(candles) < required:
        print(
            f"ERRO: dados insuficientes ({len(candles)} candle(s)) para '{args.symbol}' "
            f"{timeframe.value}. Minimo recomendado: {required}.",
            file=sys.stderr,
        )
        return 1

    strategy = _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds)
    dataset = build_signal_dataset(
        strategy,
        candles,
        symbol=args.symbol,
        timeframe=timeframe.value,
        point=point,
        max_horizon_bars=args.max_horizon_bars,
        entry_delay_bars=args.entry_delay_bars,
    )

    settings = get_settings()
    out_dir = Path(settings.ml_datasets_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (
        Path(args.out)
        if args.out
        else out_dir / f"{args.symbol}_{timeframe.value}_{strategy.name}.csv"
    )
    dataset.to_csv(out_path, index=False)

    print(f"dataset salvo em: {out_path} | sinais: {len(dataset)}")
    if dataset.empty:
        print(
            "AVISO: nenhum sinal gerado por esta estrategia neste periodo — "
            "dataset vazio (sem linhas, mas com as colunas corretas).",
            file=sys.stderr,
        )
    else:
        positives = int(dataset["label"].sum())
        print(
            f"label=1 (TARGET_FIRST): {positives}/{len(dataset)} ({positives / len(dataset):.1%})"
        )
    return 0


def cmd_ml_train(args: argparse.Namespace) -> int:
    dataset = pd.read_csv(args.dataset, parse_dates=["signal_time"])
    if dataset.empty:
        print(f"ERRO: dataset '{args.dataset}' esta vazio.", file=sys.stderr)
        return 1

    try:
        split = temporal_train_test_split(
            dataset, test_fraction=args.test_fraction, embargo_samples=args.embargo_samples
        )
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    try:
        fit_calib = split_fit_calibration(
            split.train[_ML_FEATURE_COLUMNS],
            split.train["label"],
            calibration_fraction=args.calibration_fraction,
        )
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    base_pipeline = train_model(args.model, fit_calib.x_fit, fit_calib.y_fit)
    calibrated = calibrate_model(
        base_pipeline, fit_calib.x_calib, fit_calib.y_calib, method=args.calibration_method
    )

    x_test = split.test[_ML_FEATURE_COLUMNS]
    y_test = split.test["label"].to_numpy()
    y_prob = calibrated.predict_proba(x_test)[:, 1]

    classification_metrics = compute_classification_metrics(
        y_test, y_prob, threshold=args.threshold
    )

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco.",
                file=sys.stderr,
            )
            return 1
        point = float(symbol.point)
    finally:
        session.close()

    cost_model = CostModel(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
    )
    trading_metrics = compute_trading_metrics(
        split.test,
        y_prob,
        threshold=args.threshold,
        cost_model=cost_model,
        point=point,
        volume=args.volume,
    )
    top_features = explain_model(base_pipeline, fit_calib.x_fit)[:15]

    print(
        f"treino: {len(fit_calib.x_fit)} | calibracao: {len(fit_calib.x_calib)} | "
        f"teste: {len(x_test)} | embargo descartado: {split.embargo_samples_dropped}"
    )
    print(f"\n--- Classificacao (fora da amostra, limiar={args.threshold}) ---")
    print(dataclasses.asdict(classification_metrics))
    print("\n--- Trading apos custos (fora da amostra) ---")
    print(dataclasses.asdict(trading_metrics))
    print("\n--- Top features (importancia) ---")
    for item in top_features:
        print(f"  {item.feature}: {item.importance:.6f}")
    print(f"\n{_APPROVAL_CHECKLIST}")

    metrics_payload = {
        "classification": dataclasses.asdict(classification_metrics),
        "trading": dataclasses.asdict(trading_metrics),
        "top_features": [dataclasses.asdict(item) for item in top_features],
        "train_rows": len(fit_calib.x_fit),
        "calibration_rows": len(fit_calib.x_calib),
        "test_rows": len(x_test),
        "embargo_samples_dropped": split.embargo_samples_dropped,
        "threshold": args.threshold,
    }

    registry = ModelRegistry(get_settings().ml_models_dir)
    version = registry.register(
        calibrated,
        model_name=args.model,
        symbol=args.symbol,
        timeframe=args.timeframe,
        strategy_name=args.strategy_name,
        feature_columns=_ML_FEATURE_COLUMNS,
        metrics=metrics_payload,
        approved=args.approve,
    )
    registry.save_test_set(version, split.test)

    print(
        f"\nmodelo registrado: versao={version} | aprovado={args.approve} "
        f"(aprovacao e sempre uma decisao manual — use --approve apos revisar os criterios acima)"
    )
    return 0


def cmd_ml_evaluate(args: argparse.Namespace) -> int:
    registry = ModelRegistry(get_settings().ml_models_dir)
    try:
        version = args.version or registry.current_version()
        if version is None:
            print("ERRO: nenhuma versao 'current' registrada ainda.", file=sys.stderr)
            return 1
        entry = registry.get_entry(version)
        calibrated = registry.load(version)
        test_set = registry.load_test_set(version)
    except ModelRegistryError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    x_test = test_set[entry.feature_columns]
    y_test = test_set["label"].to_numpy()
    y_prob = calibrated.predict_proba(x_test)[:, 1]

    classification_metrics = compute_classification_metrics(
        y_test, y_prob, threshold=args.threshold
    )

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(entry.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{entry.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1
        point = float(symbol.point)
    finally:
        session.close()

    cost_model = CostModel(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
    )
    trading_metrics = compute_trading_metrics(
        test_set,
        y_prob,
        threshold=args.threshold,
        cost_model=cost_model,
        point=point,
        volume=args.volume,
    )

    print(
        f"versao: {entry.version} | modelo: {entry.model_name} | simbolo: {entry.symbol} "
        f"{entry.timeframe} | estrategia: {entry.strategy_name} | treinado em: {entry.trained_at} "
        f"| aprovado: {entry.approved}"
    )
    print(f"\n--- Classificacao (fora da amostra, limiar={args.threshold}) ---")
    print(dataclasses.asdict(classification_metrics))
    print("\n--- Trading apos custos (fora da amostra) ---")
    print(dataclasses.asdict(trading_metrics))
    print("\n--- Top features (calculado no treino) ---")
    for item in entry.metrics.get("top_features", [])[:15]:
        print(f"  {item['feature']}: {item['importance']:.6f}")
    print(f"\n{_APPROVAL_CHECKLIST}")
    return 0


def _load_candles_and_symbol_for_backtest(
    args: argparse.Namespace, timeframe: Timeframe
) -> tuple[list[Candle], float, float] | None:
    """Retorna `(candles, point, contract_size)` ou `None` (com a mensagem
    de erro ja impressa) quando o simbolo nao existe ou nao ha dados
    suficientes — usado pelos comandos `backtest walk-forward/monte-carlo/
    stress-test`, que compartilham exatamente essa preparacao com
    `cmd_backtest_run`."""
    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(
                f"ERRO: simbolo '{args.symbol}' nao encontrado no banco. Colete dados primeiro.",
                file=sys.stderr,
            )
            return None
        candles = CandleRepository(session).get_all(symbol.id, timeframe.value)
        point = float(symbol.point)
        contract_size = float(symbol.trade_contract_size)
    finally:
        session.close()

    if len(candles) < 2:
        print(
            f"ERRO: dados insuficientes ({len(candles)} candle(s)) para '{args.symbol}' "
            f"{timeframe.value}. Colete mais dados primeiro.",
            file=sys.stderr,
        )
        return None

    return candles, point, contract_size


def cmd_backtest_walk_forward(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    loaded = _load_candles_and_symbol_for_backtest(args, timeframe)
    if loaded is None:
        return 1
    candles, point, contract_size = loaded
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    config = BacktestConfig(
        volume=args.volume,
        cost_model=CostModel(
            commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
        ),
    )

    try:
        report = run_walk_forward(
            lambda: _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds),
            candles,
            n_windows=args.n_windows,
            config=config,
            point=point,
            contract_size=contract_size,
            initial_balance=args.initial_balance,
            symbol=args.symbol,
            timeframe=timeframe.value,
            min_trades_per_window=args.min_trades_per_window,
        )
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    for window in report.windows:
        print(
            f"Janela {window.index}: {window.start_time} -> {window.end_time} "
            f"({window.num_candles} candles) | trades={window.metrics.num_trades} | "
            f"lucro_liquido={window.metrics.net_profit:.2f} | "
            f"profit_factor={window.metrics.profit_factor}"
        )

    print("\n--- Agregado (todas as janelas encadeadas) ---")
    print(
        f"lucro_liquido={report.aggregate_metrics.net_profit:.2f} | "
        f"trades={report.aggregate_metrics.num_trades} | "
        f"max_drawdown_pct={report.aggregate_metrics.max_drawdown_pct:.2f}"
    )
    share_text = (
        f"{report.max_single_window_profit_share:.0%}"
        if report.max_single_window_profit_share is not None
        else "n/a"
    )
    print(
        f"\njanelas lucrativas: {report.profitable_window_ratio:.0%} | "
        f"maior fatia do lucro numa unica janela: {share_text}"
    )
    print(f"ESTAVEL: {report.is_stable}")
    for note in report.stability_notes:
        print(f"  - {note}", file=sys.stderr)

    if args.json_out:
        payload = {
            "windows": [
                {
                    "index": w.index,
                    "start_time": w.start_time,
                    "end_time": w.end_time,
                    "num_candles": w.num_candles,
                    "metrics": dataclasses.asdict(w.metrics),
                }
                for w in report.windows
            ],
            "aggregate_metrics": dataclasses.asdict(report.aggregate_metrics),
            "profitable_window_ratio": report.profitable_window_ratio,
            "max_single_window_profit_share": report.max_single_window_profit_share,
            "is_stable": report.is_stable,
            "stability_notes": report.stability_notes,
        }
        with open(args.json_out, "w", encoding="utf-8") as json_file:
            json.dump(payload, json_file, default=str, ensure_ascii=False, indent=2)
        print(f"\nRelatorio completo salvo em: {args.json_out}")

    return 0


def cmd_backtest_monte_carlo(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    loaded = _load_candles_and_symbol_for_backtest(args, timeframe)
    if loaded is None:
        return 1
    candles, point, contract_size = loaded
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    strategy = _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds)
    config = BacktestConfig(
        volume=args.volume,
        cost_model=CostModel(
            commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
        ),
    )
    engine = CandleBacktestEngine(
        strategy,
        config,
        point=point,
        contract_size=contract_size,
        initial_balance=args.initial_balance,
    )
    result = engine.run(candles, symbol=args.symbol, timeframe=timeframe.value)

    if not result.trades:
        print("ERRO: nenhum trade gerado — nada para simular.", file=sys.stderr)
        return 1

    try:
        mc_result = simulate_bootstrap(
            result.trades,
            initial_balance=args.initial_balance,
            num_simulations=args.num_simulations,
            ruin_threshold_pct=args.ruin_threshold_pct,
            random_state=args.random_state,
        )
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    print(
        f"trades={mc_result.num_trades} | simulacoes={mc_result.num_simulations} | "
        f"limiar_de_ruina={mc_result.ruin_threshold_balance:.2f} "
        f"({args.ruin_threshold_pct:.0f}% do saldo inicial)"
    )
    print(f"probabilidade_de_ruina={mc_result.ruin_probability:.2%}")
    print("percentis do saldo final:")
    for p, value in mc_result.final_balance_percentiles.items():
        print(f"  p{p}: {value:.2f}")
    print("percentis do drawdown maximo (%):")
    for p, value in mc_result.max_drawdown_pct_percentiles.items():
        print(f"  p{p}: {value:.2f}")

    return 0


def cmd_backtest_stress_test(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    loaded = _load_candles_and_symbol_for_backtest(args, timeframe)
    if loaded is None:
        return 1
    candles, point, contract_size = loaded
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    base_config = BacktestConfig(
        volume=args.volume,
        cost_model=CostModel(
            commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
        ),
    )

    result = run_cost_stress_test(
        lambda: _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds),
        candles,
        base_config=base_config,
        point=point,
        contract_size=contract_size,
        initial_balance=args.initial_balance,
        symbol=args.symbol,
        timeframe=timeframe.value,
        slippage_multiplier=args.slippage_multiplier,
        commission_multiplier=args.commission_multiplier,
    )

    print(
        f"baseline: lucro_liquido={result.baseline_metrics.net_profit:.2f} | "
        f"expectativa={result.baseline_metrics.expectancy:.4f} | "
        f"trades={result.baseline_metrics.num_trades}"
    )
    print(
        f"stress (slippage x{result.slippage_multiplier}, comissao x{result.commission_multiplier}): "
        f"lucro_liquido={result.stressed_metrics.net_profit:.2f} | "
        f"expectativa={result.stressed_metrics.expectancy:.4f} | "
        f"trades={result.stressed_metrics.num_trades}"
    )
    if result.net_profit_degradation_pct is not None:
        print(f"degradacao_do_lucro_liquido: {result.net_profit_degradation_pct:.1f}%")
    print(f"SOBREVIVE (expectativa ainda positiva sob stress): {result.survives}")

    return 0


def cmd_ml_walk_forward(args: argparse.Namespace) -> int:
    dataset = pd.read_csv(args.dataset, parse_dates=["signal_time"])
    if dataset.empty:
        print(f"ERRO: dataset '{args.dataset}' esta vazio.", file=sys.stderr)
        return 1

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{args.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1
        point = float(symbol.point)
    finally:
        session.close()

    cost_model = CostModel(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
    )

    try:
        report = run_ml_walk_forward(
            dataset,
            model_name=args.model,
            n_windows=args.n_windows,
            feature_columns=_ML_FEATURE_COLUMNS,
            embargo_samples=args.embargo_samples,
            calibration_fraction=args.calibration_fraction,
            calibration_method=args.calibration_method,
            threshold=args.threshold,
            cost_model=cost_model,
            point=point,
            volume=args.volume,
        )
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    if not report.windows:
        print(
            "ERRO: nenhuma janela produziu resultado avaliavel (dataset pequeno demais "
            "para o numero de janelas pedido).",
            file=sys.stderr,
        )
        return 1

    for window in report.windows:
        print(
            f"Janela {window.index}: teste {window.test_start} -> {window.test_end} "
            f"(treino={window.train_rows}, teste={window.test_rows}) | "
            f"trades={window.trading_metrics.num_trades} | "
            f"expectativa={window.trading_metrics.expectancy_after_costs:.6f} | "
            f"roc_auc={window.classification_metrics.roc_auc}"
        )

    print(
        f"\njanelas lucrativas: {report.profitable_window_ratio:.0%} | "
        f"expectativa media: {report.mean_expectancy_after_costs:.6f} | "
        f"desvio-padrao entre janelas: {report.std_expectancy_after_costs:.6f}"
    )

    approval = evaluate_approval(report)
    print(f"\n{_APPROVAL_CHECKLIST}")
    print("\n--- Veredito por criterio (recomendacao, nao uma aprovacao automatica) ---")
    for criterion in approval.criteria:
        status = "PASSOU" if criterion.passed else "FALHOU"
        print(f"  [{status}] {criterion.name}: {criterion.detail}")
    print(f"\nTODOS OS CRITERIOS PASSARAM: {approval.all_passed}")

    return 0


def cmd_mode_show(_args: argparse.Namespace) -> int:
    session = get_session_factory()()
    try:
        mode = get_current_mode(session)
    finally:
        session.close()
    print(f"modo atual: {mode.value}")
    return 0


def cmd_mode_set(args: argparse.Namespace) -> int:
    target = SystemMode(args.mode)
    session = get_session_factory()()
    try:
        try:
            set_mode(session, target, reason=args.reason or "definido via CLI")
        except SystemModeError as exc:
            session.rollback()
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1
        session.commit()
    finally:
        session.close()
    print(f"modo alterado para: {target.value}")
    return 0


def cmd_paper_run(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    mode_session = get_session_factory()()
    try:
        current_mode = get_current_mode(mode_session)
    finally:
        mode_session.close()

    if current_mode != SystemMode.PAPER:
        print(
            f"ERRO: sistema esta em modo {current_mode.value}, nao PAPER. "
            "Rode 'python -m app.cli mode set PAPER' primeiro.",
            file=sys.stderr,
        )
        return 1

    connection_config = MT5ConnectionConfig.from_settings(get_settings())

    for iteration in range(args.iterations):
        session = get_session_factory()()
        try:
            existing_symbol = SymbolRepository(session).get_by_name(args.symbol)
            last_open_time = (
                CandleRepository(session).get_last_open_time(existing_symbol.id, timeframe.value)
                if existing_symbol is not None
                else None
            )

            try:
                with MT5Connection(connection_config) as connection:
                    spec = fetch_symbol_specification(connection.client, args.symbol)
                    if spec is None:
                        print(f"ERRO: simbolo '{args.symbol}' nao encontrado.", file=sys.stderr)
                        return 1

                    if last_open_time is not None:
                        date_from = last_open_time + timedelta(seconds=bar_seconds)
                        server_now = fetch_server_time(connection.client, args.symbol)
                        new_candles = fetch_candles_range(
                            connection.client,
                            args.symbol,
                            timeframe,
                            date_from,
                            server_now if server_now is not None else datetime.now(UTC),
                        )
                    else:
                        new_candles = fetch_candles_from_pos(
                            connection.client, args.symbol, timeframe, args.lookback_bars
                        )
            except MT5ConnectionError as exc:
                print(f"ERRO: {exc}", file=sys.stderr)
                return 1

            symbol = SymbolRepository(session).upsert_from_specification(spec)
            CandleRepository(session).bulk_upsert(symbol.id, timeframe.value, new_candles)

            point = float(symbol.point)
            contract_size = float(symbol.trade_contract_size)
            lookback = features_module.required_lookback_bars() + 5
            candles = CandleRepository(session).get_recent(symbol.id, timeframe.value, lookback)

            if len(candles) < 2:
                print(f"[{iteration}] dados insuficientes ainda ({len(candles)} candle(s)).")
            else:
                strategy = _build_strategy_from_args(args, point=point, bar_seconds=bar_seconds)
                cost_model = CostModel(
                    commission_per_lot=args.commission_per_lot,
                    slippage_points=args.slippage_points,
                )
                engine = PaperTradingEngine(
                    session,
                    strategy,
                    symbol=args.symbol,
                    symbol_id=symbol.id,
                    timeframe=timeframe.value,
                    bar_seconds=bar_seconds,
                    point=point,
                    contract_size=contract_size,
                    volume=args.volume,
                    cost_model=cost_model,
                )
                result = engine.step(candles)

                print(f"[{iteration}] barras novas processadas: {result.processed_bars}")
                for event in result.events:
                    if isinstance(event, PaperTradeOpened):
                        print(
                            f"  ABERTA posicao #{event.trade_id}: {event.direction.value} "
                            f"@ {event.entry_price:.5f} em {event.entry_time} "
                            f"(stop={event.stop_loss:.5f}, alvo={event.take_profit:.5f})"
                        )
                    elif isinstance(event, PaperTradeClosed):
                        print(
                            f"  FECHADA posicao #{event.trade_id}: {event.exit_reason} "
                            f"@ {event.exit_price:.5f} em {event.exit_time} pnl={event.net_pnl:.2f}"
                        )

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if iteration < args.iterations - 1 and args.poll_seconds > 0:
            time.sleep(args.poll_seconds)

    return 0


def cmd_paper_status(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{args.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1

        bar_seconds = TIMEFRAME_SECONDS[timeframe]
        strategy = _build_strategy_from_args(
            args, point=float(symbol.point), bar_seconds=bar_seconds
        )
        trades = PaperTradeRepository(session).list_recent(
            symbol.id, timeframe.value, strategy.name, limit=args.limit
        )
    finally:
        session.close()

    if not trades:
        print("nenhum paper trade registrado ainda.")
        return 0

    for trade in trades:
        if trade.status == "OPEN":
            print(
                f"#{trade.id} [OPEN] {trade.direction} entrada={trade.entry_price} "
                f"em {trade.entry_time}"
            )
        else:
            print(
                f"#{trade.id} [CLOSED] {trade.direction} entrada={trade.entry_price} "
                f"saida={trade.exit_price} motivo={trade.exit_reason} pnl={trade.net_pnl}"
            )
    return 0


def cmd_demo_run(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    bar_seconds = TIMEFRAME_SECONDS[timeframe]

    mode_session = get_session_factory()()
    try:
        current_mode = get_current_mode(mode_session)
    finally:
        mode_session.close()

    if current_mode != SystemMode.DEMO:
        print(
            f"ERRO: sistema esta em modo {current_mode.value}, nao DEMO. "
            "Rode 'python -m app.cli mode set DEMO' primeiro (apos PAPER).",
            file=sys.stderr,
        )
        return 1

    connection_config = MT5ConnectionConfig.from_settings(get_settings())
    risk_limits = RiskLimits(
        risk_per_trade_pct=args.risk_per_trade_pct,
        max_daily_loss_pct=args.max_daily_loss_pct,
        max_consecutive_losses=args.max_consecutive_losses,
        max_simultaneous_positions=args.max_simultaneous_positions,
        max_trades_per_day=args.max_trades_per_day,
        min_seconds_between_trades=args.min_seconds_between_trades,
        max_spread_points=args.max_spread_points,
    )

    for iteration in range(args.iterations):
        session = get_session_factory()()
        try:
            existing_symbol = SymbolRepository(session).get_by_name(args.symbol)
            last_open_time = (
                CandleRepository(session).get_last_open_time(existing_symbol.id, timeframe.value)
                if existing_symbol is not None
                else None
            )

            try:
                with MT5Connection(connection_config) as connection:
                    account = fetch_account_snapshot(connection.client)
                    if account is None:
                        print("ERRO: conectado, mas a conta nao respondeu.", file=sys.stderr)
                        return 1
                    if not account.is_demo:
                        print(
                            f"ERRO: a conta conectada ({account.login}@{account.server}) "
                            "nao e demo -- envio de ordem recusado.",
                            file=sys.stderr,
                        )
                        return 1

                    spec = fetch_symbol_specification(connection.client, args.symbol)
                    if spec is None:
                        print(f"ERRO: simbolo '{args.symbol}' nao encontrado.", file=sys.stderr)
                        return 1

                    if last_open_time is not None:
                        date_from = last_open_time + timedelta(seconds=bar_seconds)
                        server_now = fetch_server_time(connection.client, args.symbol)
                        new_candles = fetch_candles_range(
                            connection.client,
                            args.symbol,
                            timeframe,
                            date_from,
                            server_now if server_now is not None else datetime.now(UTC),
                        )
                    else:
                        new_candles = fetch_candles_from_pos(
                            connection.client, args.symbol, timeframe, args.lookback_bars
                        )

                    symbol = SymbolRepository(session).upsert_from_specification(spec)
                    CandleRepository(session).bulk_upsert(symbol.id, timeframe.value, new_candles)

                    point = float(symbol.point)
                    lookback = features_module.required_lookback_bars() + 5
                    candles = CandleRepository(session).get_recent(
                        symbol.id, timeframe.value, lookback
                    )

                    if len(candles) < 2:
                        print(
                            f"[{iteration}] dados insuficientes ainda ({len(candles)} candle(s))."
                        )
                    else:
                        strategy = _build_strategy_from_args(
                            args, point=point, bar_seconds=bar_seconds
                        )
                        engine = DemoExecutionEngine(
                            session,
                            connection.client,
                            strategy,
                            symbol=args.symbol,
                            symbol_id=symbol.id,
                            timeframe=timeframe.value,
                            point=point,
                            account=account,
                            symbol_spec=spec,
                            risk_limits=risk_limits,
                            magic=args.magic,
                        )
                        result = engine.step(candles)

                        print(f"[{iteration}] barras novas processadas: {result.processed_bars}")
                        for event in result.events:
                            if isinstance(event, SignalRejected):
                                print(
                                    f"  SINAL REJEITADO (risco) #{event.trade_id}: {event.reason}"
                                )
                            elif isinstance(event, OrderRejectedByBroker):
                                print(
                                    f"  ORDEM REJEITADA (broker) #{event.trade_id}: {event.reason}"
                                )
                            elif isinstance(event, PositionOpened):
                                print(
                                    f"  POSICAO ABERTA #{event.trade_id}: {event.direction.value} "
                                    f"{event.volume} lotes @ {event.entry_price:.5f} "
                                    f"(ticket MT5 {event.mt5_position_ticket})"
                                )
                            elif isinstance(event, PositionClosed):
                                print(
                                    f"  POSICAO FECHADA #{event.trade_id}: "
                                    f"@ {event.exit_price:.5f} pnl={event.net_pnl:.2f}"
                                )
                            elif isinstance(event, PositionReconciling):
                                print(
                                    f"  RECONCILIACAO PENDENTE #{event.trade_id}: broker nao "
                                    "reporta mais a posicao, mas nenhum deal de fechamento foi "
                                    "encontrado ainda -- revisao manual pode ser necessaria."
                                )
            except MT5ConnectionError as exc:
                print(f"ERRO: {exc}", file=sys.stderr)
                return 1
            except MT5RealAccountError as exc:
                print(f"ERRO: {exc}", file=sys.stderr)
                return 1

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        if iteration < args.iterations - 1 and args.poll_seconds > 0:
            time.sleep(args.poll_seconds)

    return 0


def cmd_demo_status(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{args.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1

        bar_seconds = TIMEFRAME_SECONDS[timeframe]
        strategy = _build_strategy_from_args(
            args, point=float(symbol.point), bar_seconds=bar_seconds
        )
        trades = LiveTradeRepository(session).list_recent(
            symbol.id, timeframe.value, strategy.name, limit=args.limit
        )
    finally:
        session.close()

    if not trades:
        print("nenhum live trade registrado ainda.")
        return 0

    for trade in trades:
        if trade.order_state in ("RISK_REJECTED", "REJECTED"):
            print(
                f"#{trade.id} [{trade.order_state}] {trade.direction} motivo={trade.rejection_reason}"
            )
        elif trade.order_state == "POSITION_OPEN":
            print(
                f"#{trade.id} [POSITION_OPEN] {trade.direction} entrada={trade.entry_price} "
                f"em {trade.entry_time} (ticket {trade.mt5_position_ticket})"
            )
        elif trade.order_state == "CLOSED":
            print(
                f"#{trade.id} [CLOSED] {trade.direction} entrada={trade.entry_price} "
                f"saida={trade.exit_price} pnl={trade.net_pnl}"
            )
        else:
            print(f"#{trade.id} [{trade.order_state}] {trade.direction}")
    return 0


def cmd_autopilot_run(args: argparse.Namespace) -> int:
    """Roda o piloto automatico N vezes contra o terminal MT5 local.

    Mesmo caminho de codigo do worker Windows (`app.mt5.auto_sync`) — aqui
    ele so fica em primeiro plano, util para acompanhar as decisoes no
    terminal enquanto o robo opera.
    """
    connection_config = MT5ConnectionConfig.from_settings(get_settings())
    publisher = AutopilotStatusPublisher(get_session_factory(), worker_id="cli")

    try:
        with MT5Connection(connection_config) as connection:
            account = fetch_account_snapshot(connection.client)
            if account is None:
                print("ERRO: conectado, mas a conta nao respondeu.", file=sys.stderr)
                return 1
            available_symbols = list_symbols(connection.client)

            for iteration in range(args.iterations):
                session = get_session_factory()()
                try:
                    config = load_trading_automation_config(session)
                    if args.symbol:
                        config = dataclasses.replace(config, symbol=args.symbol.strip().upper())
                    if not config.enabled and not args.force:
                        print(
                            "Piloto automatico desligado na configuracao. Ligue no "
                            "dashboard (/dashboard/autopilot) ou use --force para um "
                            "ciclo avulso.",
                            file=sys.stderr,
                        )
                        return 1

                    result = run_autopilot_cycle(
                        session,
                        connection.client,
                        config=dataclasses.replace(config, enabled=True),
                        account=account,
                        publisher=publisher,
                        available_symbols=available_symbols,
                    )
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
                finally:
                    session.close()

                playbook = result.playbook
                print(f"[{iteration}] {result.phase.value}: {result.message}")
                if playbook is not None:
                    print(
                        f"      operacional={playbook.kind.value} "
                        f"timeframe={playbook.timeframe} "
                        f"score_minimo={playbook.analysis_threshold:.0f} "
                        f"risco={playbook.risk_factor:.2f} "
                        f"aderencia={playbook.fit_score:.0f}"
                    )
                    for reason in playbook.blockers or playbook.reasons:
                        print(f"      - {reason}")
                if result.blocking_error:
                    print(f"      BLOQUEIO: {result.blocking_error}", file=sys.stderr)

                if iteration < args.iterations - 1 and args.poll_seconds > 0:
                    time.sleep(args.poll_seconds)
    except MT5ConnectionError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    except MT5RealAccountError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    return 0


def cmd_autopilot_status(args: argparse.Namespace) -> int:
    session = get_session_factory()()
    try:
        status = load_autopilot_status(session)
    finally:
        session.close()

    if args.json:
        _print_json(dataclasses.asdict(status))
        return 0

    freshness = "ao vivo" if status.is_fresh() else "DESATUALIZADO"
    print(f"piloto automatico: {'ligado' if status.enabled else 'desligado'} ({freshness})")
    print(f"fase: {status.phase_label} — {status.headline}")
    if status.detail:
        print(f"      {status.detail}")
    if status.broker_symbol:
        print(f"moeda: {status.broker_symbol} ({status.timeframe or '-'})")
    if status.playbook_label:
        print(f"operacional: {status.playbook_label}")
    if status.session_label:
        print(f"sessao: {status.session_label} [{status.active_sessions}]")
    if status.volume_label:
        ratio = f" ({status.volume_ratio:.2f}x)" if status.volume_ratio is not None else ""
        print(f"volume: {status.volume_label}{ratio}")
    if status.analysis_score is not None:
        print(
            f"score: {status.analysis_score:.1f} / minimo "
            f"{status.analysis_threshold:.0f} -> {status.analysis_recommendation}"
        )
    if status.open_position:
        print(f"posicao aberta: {status.open_position}")
    print(f"operacoes hoje: {status.trades_today} | resultado: {status.pnl_today:+.2f}")
    for blocker in status.blockers:
        print(f"  BLOQUEIO: {blocker}")
    if status.last_error:
        print(f"  ERRO: {status.last_error}")

    if status.activities:
        print("\n--- atividades recentes ---")
        for activity in reversed(status.activities[-args.limit :]):
            print(f"{activity.at[11:19]} [{activity.level}] {activity.message}")
    return 0


def cmd_apexflow_analyze(args: argparse.Namespace) -> int:
    """Roda o motor ApexFlow sobre os dados JA coletados e explica a decisao.

    Consultivo: nao envia ordem nem grava no Learning Engine (para isso o
    motor precisa rodar dentro do piloto automatico). Util para inspecionar
    por que o robo esta (ou nao esta) operando.
    """
    timeframe = Timeframe(args.timeframe)
    session = get_session_factory()()
    try:
        config = load_apexflow_config(session)
        if args.min_confidence is not None:
            config = dataclasses.replace(config, min_confidence=args.min_confidence)
        try:
            analysis = apexflow_analyze(
                session, symbol=args.symbol, timeframe=timeframe, config=config
            )
        except SymbolNotFoundError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1
        except UnsupportedEntryTimeframeError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 1
    finally:
        session.close()

    decision = analysis.decision
    if args.json:
        _print_json(
            {
                "symbol": analysis.symbol,
                "timeframe": timeframe.value,
                "action": decision.action.value,
                "probability_buy": decision.probability_buy,
                "probability_sell": decision.probability_sell,
                "probability_abstain": decision.probability_abstain,
                "confidence": decision.confidence,
                "min_confidence": decision.min_confidence,
                "model_version": decision.model_version,
                "feature_version": decision.feature_version,
                "completeness": decision.completeness,
                "context": analysis.context.state.value,
                "vetoes": list(decision.vetoes),
                "reasons": list(decision.reasons),
            }
        )
        return 0

    print(f"=== ApexFlow AI — {analysis.symbol} {timeframe.value} ===")
    print(f"DECISAO: {decision.label.upper()}")
    print(
        f"  compra {decision.probability_buy * 100:.1f}% | "
        f"venda {decision.probability_sell * 100:.1f}% | "
        f"abstencao {decision.probability_abstain * 100:.1f}% "
        f"(minimo para operar: {decision.min_confidence * 100:.0f}%)"
    )
    print(f"  modelo={decision.model_version} features={decision.feature_version} "
          f"cobertura={decision.completeness * 100:.0f}%")
    print(f"\nCONTEXTO: {analysis.context.label}")
    for reason in analysis.context.reasons:
        print(f"  - {reason}")

    print("\nLEITURAS:")
    print(f"  fluxo: {analysis.flow.tick_count} ticks", end="")
    if analysis.flow.ticks_per_second is not None:
        print(f", {analysis.flow.ticks_per_second:.2f}/s", end="")
    if analysis.flow.efficiency is not None:
        print(f", eficiencia {analysis.flow.efficiency:.2f}", end="")
    print()
    print(f"  spread: {analysis.spread.label}")
    print(f"  volatilidade: {analysis.volatility.label}")
    print(f"  momentum: {analysis.momentum.label}")
    print(f"  liquidez: {analysis.liquidity.label}")
    print(f"  multi-timeframe: alinhamento {analysis.mtf.alignment_score:+.2f} "
          f"(cobertura {analysis.mtf.coverage * 100:.0f}%)")
    print(f"  sessao: {analysis.session.headline}")
    print(f"  volume: {analysis.volume.label}")

    if decision.vetoes:
        print("\nVETOS (nenhuma probabilidade sobrepoe um veto):")
        for veto in decision.vetoes:
            print(f"  ! {veto}")

    print("\nJUSTIFICATIVA:")
    for reason in decision.reasons:
        print(f"  - {reason}")

    if analysis.warnings:
        print("\nAVISOS:")
        for warning in analysis.warnings:
            print(f"  * {warning}")
    return 0


def cmd_apexflow_history(args: argparse.Namespace) -> int:
    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol) if args.symbol else None
        if args.symbol and symbol is None:
            print(f"ERRO: simbolo '{args.symbol}' nao encontrado.", file=sys.stderr)
            return 1
        symbol_id = symbol.id if symbol is not None else None
        repository = ApexFlowDecisionRepository(session)
        performance = repository.performance(symbol_id=symbol_id)
        records = repository.list_recent(symbol_id=symbol_id, limit=args.limit)
    finally:
        session.close()

    def optional(value: float | None, fmt: str) -> str:
        return format(value, fmt) if value is not None else "-"

    print("=== Learning Engine (ApexFlow) ===")
    print(
        f"decisoes={performance.total_decisions} entradas={performance.entries} "
        f"abstencoes={performance.abstentions} encerradas={performance.closed_trades}"
    )
    if performance.has_statistics:
        print(
            f"win rate={optional(performance.win_rate, '.1%')} "
            f"profit factor={optional(performance.profit_factor, '.2f')} "
            f"expectancia={optional(performance.expectancy, '.2f')} "
            f"resultado={performance.net_pnl:+.2f}"
        )
    else:
        print(
            f"amostra insuficiente para estatisticas ({performance.closed_trades} "
            "operacao(oes) encerrada(s)) — nenhum numero e estimado."
        )

    if not records:
        print("\nnenhuma decisao registrada ainda.")
        return 0

    print("\n--- decisoes recentes ---")
    for record in records:
        result = (
            f" resultado={float(record.result_net_pnl):+.2f}"
            if record.result_net_pnl is not None
            else ""
        )
        print(
            f"{record.decided_at:%d/%m %H:%M} [{record.action}] "
            f"confianca={float(record.confidence) * 100:.0f}% "
            f"contexto={record.context_state}{result}"
        )
    return 0


def cmd_monitor_model(args: argparse.Namespace) -> int:
    registry = ModelRegistry(get_settings().ml_models_dir)
    try:
        version = args.version or registry.current_version()
        if version is None:
            print("ERRO: nenhuma versao 'current' registrada ainda.", file=sys.stderr)
            return 1
        entry = registry.get_entry(version)
        calibrated = registry.load(version)
        reference_set = registry.load_test_set(version)
    except ModelRegistryError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    recent_dataset = pd.read_csv(args.recent_dataset, parse_dates=["signal_time"])
    if recent_dataset.empty:
        print(f"ERRO: dataset recente '{args.recent_dataset}' esta vazio.", file=sys.stderr)
        return 1

    threshold = float(entry.metrics.get("threshold", 0.5))
    numeric_feature_columns = [c for c in entry.feature_columns if c in ML_NUMERIC_FEATURE_COLUMNS]

    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(entry.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{entry.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1
        point = float(symbol.point)
        symbol_id = symbol.id
    finally:
        session.close()

    events: list[dict[str, object]] = []

    print(f"--- Drift de features (versao {entry.version}) ---")
    feature_results = detect_feature_drift(
        reference_set, recent_dataset, feature_columns=numeric_feature_columns
    )
    any_feature_drift = False
    for result in sorted(feature_results, key=lambda r: r.psi, reverse=True):
        if result.severity == DriftSeverity.NONE:
            continue
        any_feature_drift = True
        print(f"  [{result.severity.value}] {result.feature}: PSI={result.psi:.4f}")
        events.append(
            {
                "drift_type": "FEATURE",
                "severity": result.severity.value,
                "metric_name": result.feature,
                "current_value": result.psi,
                "baseline_value": None,
                "threshold_value": 0.25 if result.severity == DriftSeverity.CRITICAL else 0.10,
                "detail": f"PSI={result.psi:.4f}",
            }
        )
    if not any_feature_drift:
        print("  nenhum drift de feature detectado.")

    x_recent = recent_dataset[entry.feature_columns]
    y_recent = recent_dataset["label"].to_numpy()
    y_prob = calibrated.predict_proba(x_recent)[:, 1]

    recent_classification = compute_classification_metrics(y_recent, y_prob, threshold=threshold)
    cost_model = CostModel(
        commission_per_lot=args.commission_per_lot, slippage_points=args.slippage_points
    )
    recent_trading = compute_trading_metrics(
        recent_dataset,
        y_prob,
        threshold=threshold,
        cost_model=cost_model,
        point=point,
        volume=args.volume,
    )

    baseline_classification = entry.metrics.get("classification", {})
    baseline_trading = entry.metrics.get("trading", {})

    metric_checks: list[tuple[str, float, float, bool]] = []
    if baseline_classification.get("brier_score") is not None:
        metric_checks.append(
            (
                "brier_score",
                baseline_classification["brier_score"],
                recent_classification.brier_score or 0.0,
                False,
            )
        )
    if baseline_trading.get("expectancy_after_costs") is not None:
        metric_checks.append(
            (
                "expectancy_after_costs",
                baseline_trading["expectancy_after_costs"],
                recent_trading.expectancy_after_costs,
                True,
            )
        )

    print(f"\n--- Drift de calibração/desempenho (versão {entry.version}) ---")
    for metric_name, baseline_value, current_value, higher_is_better in metric_checks:
        drift = detect_metric_drift(
            metric_name, baseline_value, current_value, higher_is_better=higher_is_better
        )
        print(
            f"  [{drift.severity.value}] {metric_name}: baseline={drift.baseline_value:.6f} "
            f"atual={drift.current_value:.6f} (degradação {drift.degradation_pct:+.1f}%)"
        )
        if drift.severity != DriftSeverity.NONE:
            drift_type = "CALIBRATION" if metric_name == "brier_score" else "PERFORMANCE"
            events.append(
                {
                    "drift_type": drift_type,
                    "severity": drift.severity.value,
                    "metric_name": metric_name,
                    "current_value": drift.current_value,
                    "baseline_value": drift.baseline_value,
                    "threshold_value": None,
                    "detail": f"degradação {drift.degradation_pct:+.1f}% em relação ao treino",
                }
            )

    if events:
        event_session = get_session_factory()()
        try:
            repo = DriftEventRepository(event_session)
            for event in events:
                repo.record(
                    model_version=entry.version,
                    symbol_id=symbol_id,
                    timeframe=entry.timeframe,
                    **event,  # type: ignore[arg-type]
                )
            event_session.commit()
        finally:
            event_session.close()
        print(f"\n{len(events)} evento(s) de drift registrado(s).")
    else:
        print("\nnenhum evento de drift registrado (tudo dentro do esperado).")

    return 0


def cmd_monitor_feed(args: argparse.Namespace) -> int:
    timeframe = Timeframe(args.timeframe)
    session = get_session_factory()()
    try:
        symbol = SymbolRepository(session).get_by_name(args.symbol)
        if symbol is None:
            print(f"ERRO: simbolo '{args.symbol}' nao encontrado no banco.", file=sys.stderr)
            return 1

        last_open_time = CandleRepository(session).get_last_open_time(symbol.id, timeframe.value)
        if last_open_time is None:
            print(
                f"ERRO: nenhuma candle coletada ainda para '{args.symbol}' {timeframe.value}.",
                file=sys.stderr,
            )
            return 1

        # `last_open_time` vem do banco -- SQLite (e, na pratica, tambem o
        # driver MySQL usado aqui) devolve `DateTime(timezone=True)` como
        # naive na leitura (mesma observacao das Fases 10/11). Normaliza
        # `now` para naive tambem, em vez de assumir tzinfo em dados do
        # banco que podem ou nao te-lo.
        now = datetime.now(UTC).replace(tzinfo=None)
        result = check_feed_health(
            last_update_time=last_open_time.replace(tzinfo=None),
            now=now,
            max_delay_seconds=args.max_delay_seconds,
        )

        print(
            f"simbolo: {args.symbol} {timeframe.value} | ultima atualização: {last_open_time} | "
            f"idade: {result.age_seconds:.0f}s | saudável: {result.is_healthy}"
        )

        if not result.is_healthy:
            DriftEventRepository(session).record(
                drift_type="DATA_FEED",
                severity="CRITICAL",
                metric_name="feed_age_seconds",
                current_value=result.age_seconds,
                threshold_value=args.max_delay_seconds,
                detail=result.reason or "feed atrasado",
                symbol_id=symbol.id,
                timeframe=timeframe.value,
            )
            session.commit()
            print(f"ERRO: {result.reason}", file=sys.stderr)
            return 1

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return 0


def _print_analysis_report_text(report: AnalysisReport) -> None:
    print(
        f"=== Analise: {report.symbol} ({report.timeframe.value}) — "
        f"{report.generated_at.isoformat()} ==="
    )
    print(f"Tendencia: {report.trend.value}")
    if report.dominant_pattern is not None:
        print(
            f"Padrao dominante: {report.dominant_pattern.name.value} "
            f"({report.dominant_pattern.direction.value})"
        )

    print("\nAlinhamento multi-timeframe:")
    for timeframe, label in report.multi_timeframe_alignment.items():
        print(f"  {timeframe.value}: {label}")

    print(
        f"\nScore composto: {report.score.total_score:.1f}/100 (limiar: {report.score.threshold:.1f})"
    )
    for factor in report.score.factors:
        print(
            f"  - {factor.name} (peso {factor.weight * 100:.0f}%): {factor.raw_score:.1f} "
            f"-> contribuicao {factor.weighted_contribution:.1f}"
        )
        for line in factor.rationale:
            print(f"      {line}")

    if report.confluences:
        print("\nConfluencias:")
        for confluence in report.confluences:
            print(f"  - {confluence}")

    banner = "ENTRAR" if report.recommendation == "ENTER" else "NAO OPERAR"
    print(f"\n>>> {banner} <<<")

    if report.recommendation == "DO_NOT_ENTER":
        print("Motivos:")
        for reason in report.rejection_reasons:
            print(f"  - {reason}")
    elif report.trade_levels is not None:
        levels = report.trade_levels
        print("Niveis de trade (consultivo — nunca alimenta execucao automatica):")
        print(f"  Entrada: {levels.entry:.5f}")
        print(f"  Stop: {levels.stop_loss:.5f}")
        print(f"  TP1 ({levels.risk_reward_1:.1f}R): {levels.take_profit_1:.5f}")
        print(f"  TP2 ({levels.risk_reward_2:.1f}R): {levels.take_profit_2:.5f}")
        print(f"  TP3 ({levels.risk_reward_3:.1f}R): {levels.take_profit_3:.5f}")
        print(f"  Break-even em: {levels.break_even_price:.5f}")
        print(f"  Trailing ativa em: {levels.trailing_activation_price:.5f}")

    print(
        f"\nProbabilidade estimada (reescalonamento heuristico do score, NAO calibrada "
        f"estatisticamente): {report.probability_estimate:.1f}"
    )


def cmd_scanner_run(args: argparse.Namespace) -> int:
    """Varre o mercado e mostra o ranking de oportunidades.

    Nao envia ordem nenhuma. Com `--record`, grava a escolha no diario de
    observacao para que, semanas depois, de para responder se as escolhas do
    scanner foram melhores do que operar um par fixo — a unica forma honesta
    de saber se a complexidade se pagou.
    """
    from app.calendar_feed.factory import get_calendar_provider
    from app.market.correlation import check_exposure
    from app.market.scan_journal import record_scan, summarize
    from app.market.scanner import scan_market

    settings = get_settings()
    agora = datetime.now(UTC)

    session = get_session_factory()()
    try:
        calendario = get_calendar_provider(settings).fetch_events(
            now=agora, horizon_minutes=120
        )
        resultado = scan_market(
            session,
            now=agora,
            timeframe=args.timeframe or settings.analysis_default_timeframe,
            calendar=calendario,
        )

        print(f"\nVarredura — {agora.strftime('%Y-%m-%d %H:%M')} UTC")
        print(f"  calendario: {calendario.status.value}")
        print(f"  {len(resultado.candidates)} instrumento(s) avaliados\n")

        print(f"  {'#':<3}{'ATIVO':<10}{'NOTA':>6}  {'SESSAO':>6}{'VOL':>6}{'CUSTO':>7}  DETALHE")
        for posicao, candidato in enumerate(resultado.candidates[: args.limit], start=1):
            detalhe = candidato.blocked_reason or (
                f"{candidato.session_label}, volume {candidato.volume_label}"
            )
            marca = "  " if candidato.tradable else "X "
            print(
                f"{marca}{posicao:<3}{candidato.symbol:<10}{candidato.score:>6.0f}  "
                f"{candidato.session_score:>6.0f}{candidato.volume_score:>6.0f}"
                f"{candidato.cost_score:>7.0f}  {detalhe[:60]}"
            )

        melhor = resultado.best
        print("")
        if melhor is None:
            print("Nenhum instrumento aprovado agora.")
            return 0

        print(f"ESCOLHA: {melhor.symbol} (nota {melhor.score:.0f})")
        for razao in melhor.reasons:
            print(f"  - {razao}")

        if args.open_symbols:
            abertos = [item.strip().upper() for item in args.open_symbols.split(",") if item.strip()]
            veredito = check_exposure(session, candidate=melhor.symbol, open_symbols=abertos)
            print("")
            if veredito.allowed:
                print(f"EXPOSICAO: liberada. {veredito.reason}")
            else:
                print(f"EXPOSICAO: RECUSADA. {veredito.reason}")

        if args.record:
            gravado = record_scan(session, resultado)
            session.commit()
            if gravado is not None:
                resumo = summarize(session)
                print(f"\nRegistrado. Diario tem {resumo.total} observacao(oes).")
                if resumo.average_margin is not None:
                    print(
                        f"  margem media para o 2o colocado: {resumo.average_margin:.1f} pontos"
                    )
                    if resumo.average_margin < 5.0:
                        print(
                            "  ATENCAO: margem baixa — o ranking esta quase "
                            "empatando, ou seja, discriminando pouco."
                        )
    finally:
        session.close()
    return 0


def cmd_calendar_check(args: argparse.Namespace) -> int:
    """Mostra o que o portao de eventos VE agora, sem esperar um evento.

    Existe para responder tres perguntas de uma vez: o arquivo esta sendo
    lido? os horarios estao no fuso certo? o robo bloquearia neste momento?
    Sem isso, so um payroll de verdade diria se a integracao funciona — e
    descobrir que nao funciona nesse momento e o pior momento possivel.
    """
    from app.calendar_feed.blackout import (
        BlackoutWindow,
        currencies_for_symbol,
        describe,
        find_blocking_event,
    )
    from app.calendar_feed.factory import get_calendar_provider, reset_calendar_provider

    settings = get_settings()
    reset_calendar_provider()  # leitura fresca, sem cache
    provider = get_calendar_provider(settings)

    agora = datetime.now(UTC)
    horizonte = max(args.horizon, 60)
    snapshot = provider.fetch_events(now=agora, horizon_minutes=horizonte)

    print(f"\nCalendario economico — {agora.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  fonte    : {settings.calendar_file_path or '(nao configurada)'}")
    print(f"  situacao : {snapshot.status.value}")
    if snapshot.message:
        print(f"  detalhe  : {snapshot.message}")

    if not snapshot.usable:
        print(
            "\nO filtro esta INATIVO. O robo continua operando — sem esta "
            "protecao.\nConfigure CALENDAR_FILE_PATH e rode o "
            "CalendarExporter.mq5 no MetaTrader."
        )
        return 1

    moedas = currencies_for_symbol(args.symbol)
    janela = BlackoutWindow(
        minutes_before=settings.calendar_blackout_before_minutes,
        minutes_after=settings.calendar_blackout_after_minutes,
    )
    print(f"  moedas de {args.symbol}: {', '.join(sorted(moedas)) or '(nao deduzidas)'}")
    print(f"  janela   : -{janela.minutes_before}min / +{janela.minutes_after}min")

    relevantes = [
        evento
        for evento in sorted(snapshot.events, key=lambda item: item.scheduled_at)
        if not moedas or not evento.currency or evento.currency.upper() in moedas
    ]

    print(f"\nProximos eventos relevantes ({len(relevantes)} de {len(snapshot.events)}):")
    if not relevantes:
        print("  nenhum na janela consultada.")
    for evento in relevantes[: args.limit]:
        minutos = (evento.scheduled_at - agora).total_seconds() / 60
        quando = f"em {minutos:6.0f} min" if minutos >= 0 else f"ha {abs(minutos):6.0f} min"
        print(
            f"  [{evento.impact:<6}] {evento.currency or '---':<4} {quando}  "
            f"{evento.scheduled_at.strftime('%d/%m %H:%M')}Z  {evento.title}"
        )

    bloqueio = find_blocking_event(
        snapshot.events,
        symbol=args.symbol,
        now=agora,
        window=janela,
        min_impact=settings.calendar_min_impact,
    )
    print("")
    if bloqueio is None:
        print(f"AGORA: entrada LIBERADA em {args.symbol} (nenhum evento na janela).")
    else:
        print(f"AGORA: entrada BLOQUEADA em {args.symbol}.")
        print(f"       {describe(bloqueio, now=agora)}")

    print(
        "\nConfira o fuso: um evento conhecido (payroll dos EUA sai 12:30 ou "
        "13:30 UTC)\ndeve aparecer no horario certo acima. Se estiver "
        "deslocado em horas, o\nexportador nao converteu o fuso do servidor."
    )
    return 0


def cmd_analysis_calibrate(args: argparse.Namespace) -> int:
    """Mede que score o mercado REAL produz, em vez de acreditar no numero
    redondo da especificacao.

    Percorre o historico ja coletado, reavalia a analise a cada N barras e
    mostra a distribuicao dos scores. Um limiar so faz sentido depois de
    saber quantas oportunidades ele deixaria passar: 90 nao significa nada
    se o percentil 99 do seu ativo for 78.

    Nao consulta a MarketPulse (`enforce_gates=False` e provedores locais):
    calibracao nao pode custar cota, e o que se quer medir aqui e a parte
    tecnica do score.
    """
    from app.market.multi_timeframe import ANALYSIS_TIMEFRAMES
    from app.news.unconfigured import skipped_fundamentals, skipped_news

    settings = get_settings()
    timeframe = Timeframe(args.timeframe or settings.analysis_default_timeframe)

    class _Local:
        """Provedores que nunca saem para a rede."""

        def __init__(self, factory):
            self._factory = factory

        def fetch_assessment(self, symbol: str, *, now: datetime):
            return self._factory("Calibracao local: MarketPulse nao consultada.")

    session = get_session_factory()()
    try:
        symbol_row = SymbolRepository(session).get_by_name(args.symbol)
        if symbol_row is None:
            print(f"ERRO: simbolo {args.symbol} nunca foi coletado.", file=sys.stderr)
            return 1

        candles = CandleRepository(session).get_recent(
            symbol_row.id, timeframe.value, args.bars + 250
        )
        if len(candles) < 260:
            print(
                f"ERRO: apenas {len(candles)} candle(s) de {args.symbol}/{timeframe.value}; "
                "colete mais historico antes de calibrar.",
                file=sys.stderr,
            )
            return 1

        scores: list[float] = []
        blocked = 0
        for offset in range(0, min(args.bars, len(candles) - 250), args.step):
            momento = candles[len(candles) - 1 - offset].open_time
            try:
                report = analyze_symbol(
                    session,
                    symbol=args.symbol,
                    primary_timeframe=timeframe,
                    threshold=0.0,
                    news_provider=_Local(skipped_news),
                    fundamentals_provider=_Local(skipped_fundamentals),
                    now=momento,
                    enforce_gates=False,
                    as_of=momento,
                )
            except (SymbolNotFoundError, NotImplementedError):
                blocked += 1
                continue
            scores.append(report.score.total_score)
    finally:
        session.close()

    if not scores:
        print("ERRO: nenhuma janela pode ser avaliada.", file=sys.stderr)
        return 1

    ordenado = sorted(scores)

    def percentil(p: float) -> float:
        indice = min(len(ordenado) - 1, int(round((len(ordenado) - 1) * p)))
        return ordenado[indice]

    resultado = {
        "symbol": args.symbol,
        "timeframe": timeframe.value,
        "amostras": len(ordenado),
        "minimo": ordenado[0],
        "mediana": percentil(0.50),
        "p75": percentil(0.75),
        "p90": percentil(0.90),
        "p95": percentil(0.95),
        "p99": percentil(0.99),
        "maximo": ordenado[-1],
        "timeframes_analisados": len(ANALYSIS_TIMEFRAMES),
    }

    if args.json:
        print(json.dumps(resultado, indent=2, ensure_ascii=False))
        return 0

    print(f"\nCalibracao de {args.symbol} / {timeframe.value}")
    print(f"  amostras avaliadas : {resultado['amostras']}")
    print(f"  score minimo       : {resultado['minimo']:.1f}")
    print(f"  mediana            : {resultado['mediana']:.1f}")
    print(f"  percentil 75       : {resultado['p75']:.1f}")
    print(f"  percentil 90       : {resultado['p90']:.1f}")
    print(f"  percentil 95       : {resultado['p95']:.1f}")
    print(f"  percentil 99       : {resultado['p99']:.1f}")
    print(f"  score maximo       : {resultado['maximo']:.1f}")
    print(
        "\nLeitura: um limiar acima do percentil 99 nunca dispara neste ativo. "
        "\nEscolher pelo percentil 90 significa mirar as 10% melhores janelas."
    )
    print(
        "\nATENCAO: score alto NAO significa operacao lucrativa. Isto mede o "
        "\nque o seu criterio considera bom, nao se esse criterio ganha dinheiro "
        "\n— para isso e preciso backtest com custos."
    )
    return 0


def cmd_analysis_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    timeframe = Timeframe(args.timeframe or settings.analysis_default_timeframe)
    threshold = (
        args.threshold if args.threshold is not None else settings.analysis_default_threshold
    )

    session = get_session_factory()()
    try:
        with calls_from(ORIGIN_CLI):
            report = analyze_symbol(
                session,
                symbol=args.symbol,
                primary_timeframe=timeframe,
                threshold=threshold,
                now=datetime.now(UTC),
                enforce_gates=not args.no_gates,
            )
    except SymbolNotFoundError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    if args.json:
        _print_json(report)
    else:
        _print_analysis_report_text(report)
    return 0


def cmd_preflight_check(_args: argparse.Namespace) -> int:
    settings = get_settings()
    session = get_session_factory()()
    try:
        checks = run_all_checks(settings, session)
    finally:
        session.close()

    labels = {CheckStatus.OK: "OK", CheckStatus.WARN: "AVISO", CheckStatus.FAIL: "FALHA"}
    for check in checks:
        print(f"[{labels[check.status]}] {check.name}: {check.detail}")

    overall = worst_status(checks)
    print(f"\nresultado geral: {labels[overall]}")

    return 1 if overall == CheckStatus.FAIL else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    user_parser = subparsers.add_parser(
        "user", help="Usuarios do painel: listar, criar e redefinir senha"
    )
    user_subparsers = user_parser.add_subparsers(dest="user_command", required=True)

    user_subparsers.add_parser("list", help="Lista usuarios, papeis e estado")

    reset_parser = user_subparsers.add_parser(
        "reset-password", help="Redefine a senha de um usuario (senha pedida no terminal)"
    )
    reset_parser.add_argument("--username", required=True)
    reset_parser.add_argument(
        "--activate",
        action="store_true",
        help="Reativa o usuario se estiver inativo (senha nova nao adianta em conta inativa)",
    )

    create_parser = user_subparsers.add_parser(
        "create", help="Cria um usuario (senha pedida no terminal)"
    )
    create_parser.add_argument("--username", required=True)
    create_parser.add_argument("--email", required=True)
    create_parser.add_argument("--admin", action="store_true", help="Concede o papel ADMIN")

    mt5_parser = subparsers.add_parser("mt5", help="Comandos de diagnostico do MetaTrader 5")
    mt5_subparsers = mt5_parser.add_subparsers(dest="mt5_command", required=True)

    mt5_subparsers.add_parser("check", help="Conecta e reporta saude do terminal/conta")

    bridge_parser = mt5_subparsers.add_parser(
        "bridge", help="Diagnostica a ponte com o MetaTrader sob Wine/Docker"
    )
    bridge_parser.add_argument("--host", default=None, help="Padrao: MT5_BRIDGE_HOST")
    bridge_parser.add_argument("--port", type=int, default=None, help="Padrao: 18812")
    bridge_parser.add_argument("--timeout", type=float, default=10.0)
    bridge_parser.set_defaults(func=cmd_mt5_bridge)

    symbols_parser = mt5_subparsers.add_parser("symbols", help="Lista simbolos disponiveis")
    symbols_parser.add_argument("--group", default=None, help="Filtro de grupo (ex.: '*USD*')")
    symbols_parser.add_argument("--limit", type=int, default=50)

    collect_parser = subparsers.add_parser("collect", help="Coleta e persiste dados de mercado")
    collect_subparsers = collect_parser.add_subparsers(dest="collect_command", required=True)

    candles_parser = collect_subparsers.add_parser("candles", help="Coleta candles (OHLCV)")
    candles_parser.add_argument("--symbol", required=True)
    candles_parser.add_argument("--timeframe", default="M1", choices=[t.value for t in Timeframe])
    candles_parser.add_argument(
        "--count", type=int, default=500, help="Usado apenas na primeira coleta (backfill)"
    )

    ticks_parser = collect_subparsers.add_parser("ticks", help="Coleta ticks (bid/ask)")
    ticks_parser.add_argument("--symbol", required=True)
    ticks_parser.add_argument(
        "--seconds",
        type=int,
        default=60,
        help="Janela de coleta em segundos, a partir de agora (usado apenas no backfill)",
    )

    quality_parser = subparsers.add_parser("quality", help="Verificacoes de qualidade de dados")
    quality_subparsers = quality_parser.add_subparsers(dest="quality_command", required=True)

    quality_check_parser = quality_subparsers.add_parser(
        "check", help="Roda checagens de qualidade sobre dados ja armazenados"
    )
    quality_check_parser.add_argument("--symbol", required=True)
    quality_check_parser.add_argument(
        "--timeframe", default=None, choices=[t.value for t in Timeframe]
    )
    quality_check_parser.add_argument("--limit", type=int, default=500)

    data_parser = subparsers.add_parser("data", help="Manutencao de dados armazenados")
    data_subparsers = data_parser.add_subparsers(dest="data_command", required=True)

    purge_parser = data_subparsers.add_parser(
        "purge-ticks", help="Remove ticks mais antigos que a retencao configurada"
    )
    purge_parser.add_argument(
        "--older-than-days", type=int, default=None, help="Sobrescreve TICK_RETENTION_DAYS"
    )

    retention_parser = data_subparsers.add_parser(
        "retention",
        help="Aplica a politica de retencao inteira (ticks + eventos de qualidade)",
    )
    retention_parser.add_argument(
        "--dry-run", action="store_true", help="So mostra os cortes, nao apaga"
    )
    retention_parser.add_argument(
        "--chunk-size", type=int, default=None, help="Sobrescreve RETENTION_CHUNK_SIZE"
    )
    retention_parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Sobrescreve RETENTION_MAX_CHUNKS_PER_RUN (use alto na primeira limpeza)",
    )
    retention_parser.add_argument(
        "--optimize",
        action="store_true",
        help="OPTIMIZE TABLE depois da purga: devolve o espaco ao disco, bloqueia escrita",
    )

    features_parser = subparsers.add_parser(
        "features", help="Indicadores, features e regime de mercado"
    )
    features_subparsers = features_parser.add_subparsers(dest="features_command", required=True)

    features_build_parser = features_subparsers.add_parser(
        "build", help="Calcula indicadores/features e classifica o regime atual"
    )
    features_build_parser.add_argument("--symbol", required=True)
    features_build_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    features_build_parser.add_argument(
        "--rows", type=int, default=5, help="Quantas barras finais mostrar na tabela"
    )

    backtest_parser = subparsers.add_parser("backtest", help="Backtest por candle")
    backtest_subparsers = backtest_parser.add_subparsers(dest="backtest_command", required=True)

    backtest_run_parser = backtest_subparsers.add_parser(
        "run", help="Roda uma estrategia sobre dados ja armazenados"
    )
    backtest_run_parser.add_argument("--symbol", required=True)
    backtest_run_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    backtest_run_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    backtest_run_parser.add_argument(
        "--fast",
        type=int,
        default=9,
        choices=list(features_module.EMA_PERIODS),
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_parser.add_argument(
        "--slow",
        type=int,
        default=21,
        choices=list(features_module.EMA_PERIODS),
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_parser.add_argument(
        "--stop-points",
        type=float,
        default=100.0,
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_parser.add_argument(
        "--target-points",
        type=float,
        default=200.0,
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_parser.add_argument("--volume", type=float, default=0.01)
    backtest_run_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    backtest_run_parser.add_argument("--slippage-points", type=float, default=0.0)
    backtest_run_parser.add_argument("--initial-balance", type=float, default=10_000.0)
    backtest_run_parser.add_argument(
        "--json-out", default=None, help="Caminho para salvar o relatorio completo em JSON"
    )

    backtest_compare_parser = backtest_subparsers.add_parser(
        "compare", help="Roda todas as estrategias registradas e mostra um relatorio comparativo"
    )
    backtest_compare_parser.add_argument("--symbol", required=True)
    backtest_compare_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    backtest_compare_parser.add_argument("--volume", type=float, default=0.01)
    backtest_compare_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    backtest_compare_parser.add_argument("--slippage-points", type=float, default=0.0)
    backtest_compare_parser.add_argument("--initial-balance", type=float, default=10_000.0)

    backtest_run_ticks_parser = backtest_subparsers.add_parser(
        "run-ticks",
        help="Roda uma estrategia com fills simulados contra a sequencia real de ticks",
    )
    backtest_run_ticks_parser.add_argument("--symbol", required=True)
    backtest_run_ticks_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    backtest_run_ticks_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    backtest_run_ticks_parser.add_argument(
        "--fast",
        type=int,
        default=9,
        choices=list(features_module.EMA_PERIODS),
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_ticks_parser.add_argument(
        "--slow",
        type=int,
        default=21,
        choices=list(features_module.EMA_PERIODS),
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_ticks_parser.add_argument(
        "--stop-points",
        type=float,
        default=100.0,
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_ticks_parser.add_argument(
        "--target-points",
        type=float,
        default=200.0,
        help="Usado apenas com --strategy ema_crossover",
    )
    backtest_run_ticks_parser.add_argument("--volume", type=float, default=0.01)
    backtest_run_ticks_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    backtest_run_ticks_parser.add_argument("--slippage-points", type=float, default=0.0)
    backtest_run_ticks_parser.add_argument(
        "--latency-ms", type=int, default=0, help="Atraso simulado entre decisao e execucao"
    )
    backtest_run_ticks_parser.add_argument(
        "--max-spread-points",
        type=float,
        default=50.0,
        help="Entradas sao rejeitadas se o spread no momento do fill exceder isto",
    )
    backtest_run_ticks_parser.add_argument(
        "--max-tick-gap-seconds",
        type=float,
        default=5.0,
        help="Gap entre ticks maior que isto gera aviso de liquidez insuficiente",
    )
    backtest_run_ticks_parser.add_argument(
        "--max-holding-seconds",
        type=float,
        default=None,
        help="Fecha a posicao por tempo se nenhum stop/alvo for atingido antes disso",
    )
    backtest_run_ticks_parser.add_argument(
        "--trailing-stop-points",
        type=float,
        default=None,
        help="Ativa stop movel a esta distancia do melhor preco atingido",
    )
    backtest_run_ticks_parser.add_argument("--initial-balance", type=float, default=10_000.0)
    backtest_run_ticks_parser.add_argument(
        "--json-out",
        default=None,
        help="Caminho para salvar o relatorio completo (com auditoria de fills) em JSON",
    )

    backtest_walk_forward_parser = backtest_subparsers.add_parser(
        "walk-forward",
        help="Roda a estrategia em janelas cronologicas nao sobrepostas e mede estabilidade",
    )
    _add_strategy_selection_arguments(backtest_walk_forward_parser)
    backtest_walk_forward_parser.add_argument("--n-windows", type=int, default=5)
    backtest_walk_forward_parser.add_argument(
        "--min-trades-per-window",
        type=int,
        default=5,
        help="Janelas com menos trades que isto sao excluidas do julgamento de estabilidade",
    )
    backtest_walk_forward_parser.add_argument(
        "--json-out", default=None, help="Caminho para salvar o relatorio completo em JSON"
    )

    backtest_monte_carlo_parser = backtest_subparsers.add_parser(
        "monte-carlo",
        help="Reamostra (bootstrap) os trades de um backtest para estimar risco de ruina",
    )
    _add_strategy_selection_arguments(backtest_monte_carlo_parser)
    backtest_monte_carlo_parser.add_argument("--num-simulations", type=int, default=1000)
    backtest_monte_carlo_parser.add_argument(
        "--ruin-threshold-pct",
        type=float,
        default=50.0,
        help="Percentual do saldo inicial abaixo do qual conta-se como ruina",
    )
    backtest_monte_carlo_parser.add_argument(
        "--random-state", type=int, default=None, help="Semente para reprodutibilidade"
    )

    backtest_stress_test_parser = backtest_subparsers.add_parser(
        "stress-test",
        help="Reexecuta o backtest com slippage/comissao multiplicados e mede a degradacao",
    )
    _add_strategy_selection_arguments(backtest_stress_test_parser)
    backtest_stress_test_parser.add_argument("--slippage-multiplier", type=float, default=3.0)
    backtest_stress_test_parser.add_argument("--commission-multiplier", type=float, default=3.0)

    ml_parser = subparsers.add_parser("ml", help="Pipeline de machine learning (Fase 8)")
    ml_subparsers = ml_parser.add_subparsers(dest="ml_command", required=True)

    ml_build_dataset_parser = ml_subparsers.add_parser(
        "build-dataset",
        help="Gera o dataset de sinais (features + rotulo de barreira tripla) e salva em CSV",
    )
    ml_build_dataset_parser.add_argument("--symbol", required=True)
    ml_build_dataset_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    ml_build_dataset_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    ml_build_dataset_parser.add_argument(
        "--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS)
    )
    ml_build_dataset_parser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    ml_build_dataset_parser.add_argument("--stop-points", type=float, default=100.0)
    ml_build_dataset_parser.add_argument("--target-points", type=float, default=200.0)
    ml_build_dataset_parser.add_argument(
        "--max-horizon-bars", type=int, default=50, help="Horizonte maximo da barreira tripla"
    )
    ml_build_dataset_parser.add_argument(
        "--entry-delay-bars",
        type=int,
        default=1,
        help="Barras entre o sinal e a execucao (mesma convencao dos backtests)",
    )
    ml_build_dataset_parser.add_argument(
        "--out", default=None, help="Caminho do CSV de saida (padrao: ML_DATASETS_DIR)"
    )

    ml_train_parser = ml_subparsers.add_parser(
        "train", help="Treina, calibra e avalia um modelo a partir de um dataset de sinais"
    )
    ml_train_parser.add_argument(
        "--dataset", required=True, help="Caminho do CSV gerado por build-dataset"
    )
    ml_train_parser.add_argument(
        "--symbol", required=True, help="Usado para custos (point do simbolo)"
    )
    ml_train_parser.add_argument("--timeframe", default="M1", choices=[t.value for t in Timeframe])
    ml_train_parser.add_argument(
        "--strategy-name", required=True, help="Metadado registrado no manifesto"
    )
    ml_train_parser.add_argument(
        "--model", default="logistic_regression", choices=list(MODEL_NAMES)
    )
    ml_train_parser.add_argument("--test-fraction", type=float, default=0.3)
    ml_train_parser.add_argument("--embargo-samples", type=int, default=5)
    ml_train_parser.add_argument("--calibration-fraction", type=float, default=0.2)
    ml_train_parser.add_argument(
        "--calibration-method", default="sigmoid", choices=["sigmoid", "isotonic"]
    )
    ml_train_parser.add_argument("--threshold", type=float, default=0.5)
    ml_train_parser.add_argument("--volume", type=float, default=0.01)
    ml_train_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    ml_train_parser.add_argument("--slippage-points", type=float, default=0.0)
    ml_train_parser.add_argument(
        "--approve",
        action="store_true",
        help="Marca a versao como aprovada no manifesto (decisao manual, nunca automatica)",
    )

    ml_evaluate_parser = ml_subparsers.add_parser(
        "evaluate", help="Recarrega uma versao registrada e recalcula suas metricas"
    )
    ml_evaluate_parser.add_argument(
        "--version", default=None, help="Versao a avaliar (padrao: a versao 'current')"
    )
    ml_evaluate_parser.add_argument("--threshold", type=float, default=0.5)
    ml_evaluate_parser.add_argument("--volume", type=float, default=0.01)
    ml_evaluate_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    ml_evaluate_parser.add_argument("--slippage-points", type=float, default=0.0)

    ml_walk_forward_parser = ml_subparsers.add_parser(
        "walk-forward",
        help="Treina/calibra/avalia em multiplas janelas expansivas e reporta aprovacao formal",
    )
    ml_walk_forward_parser.add_argument(
        "--dataset", required=True, help="Caminho do CSV gerado por build-dataset"
    )
    ml_walk_forward_parser.add_argument(
        "--symbol", required=True, help="Usado para custos (point do simbolo)"
    )
    ml_walk_forward_parser.add_argument(
        "--model", default="logistic_regression", choices=list(MODEL_NAMES)
    )
    ml_walk_forward_parser.add_argument("--n-windows", type=int, default=5)
    ml_walk_forward_parser.add_argument("--embargo-samples", type=int, default=5)
    ml_walk_forward_parser.add_argument("--calibration-fraction", type=float, default=0.2)
    ml_walk_forward_parser.add_argument(
        "--calibration-method", default="sigmoid", choices=["sigmoid", "isotonic"]
    )
    ml_walk_forward_parser.add_argument("--threshold", type=float, default=0.5)
    ml_walk_forward_parser.add_argument("--volume", type=float, default=0.01)
    ml_walk_forward_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    ml_walk_forward_parser.add_argument("--slippage-points", type=float, default=0.0)

    mode_parser = subparsers.add_parser("mode", help="Maquina de estados do sistema (Fase 10)")
    mode_subparsers = mode_parser.add_subparsers(dest="mode_command", required=True)

    mode_subparsers.add_parser("show", help="Mostra o modo atual do sistema")

    mode_set_parser = mode_subparsers.add_parser(
        "set",
        help=(
            "Transiciona o sistema para outro modo (DEMO/REAL_LOCKED/REAL_ENABLED "
            "bloqueados nesta fase)"
        ),
    )
    mode_set_parser.add_argument("mode", choices=[m.value for m in SystemMode])
    mode_set_parser.add_argument("--reason", default=None, help="Motivo registrado na auditoria")

    paper_parser = subparsers.add_parser(
        "paper", help="Paper trading incremental (Fase 10) — nunca envia ordens reais"
    )
    paper_subparsers = paper_parser.add_subparsers(dest="paper_command", required=True)

    paper_run_parser = paper_subparsers.add_parser(
        "run", help="Roda N iteracoes de coleta+decisao de paper trading (exige modo PAPER)"
    )
    paper_run_parser.add_argument("--symbol", required=True)
    paper_run_parser.add_argument("--timeframe", default="M1", choices=[t.value for t in Timeframe])
    paper_run_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    paper_run_parser.add_argument(
        "--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS)
    )
    paper_run_parser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    paper_run_parser.add_argument("--stop-points", type=float, default=100.0)
    paper_run_parser.add_argument("--target-points", type=float, default=200.0)
    paper_run_parser.add_argument("--volume", type=float, default=0.01)
    paper_run_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    paper_run_parser.add_argument("--slippage-points", type=float, default=0.0)
    paper_run_parser.add_argument("--iterations", type=int, default=1)
    paper_run_parser.add_argument(
        "--poll-seconds", type=float, default=5.0, help="Pausa entre iteracoes (0 = sem pausa)"
    )
    paper_run_parser.add_argument(
        "--lookback-bars",
        type=int,
        default=500,
        help="Usado apenas na primeira coleta (backfill) para este simbolo/timeframe",
    )

    paper_status_parser = paper_subparsers.add_parser(
        "status", help="Lista os paper trades mais recentes de um simbolo/estrategia"
    )
    paper_status_parser.add_argument("--symbol", required=True)
    paper_status_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    paper_status_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    paper_status_parser.add_argument(
        "--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS)
    )
    paper_status_parser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    paper_status_parser.add_argument("--stop-points", type=float, default=100.0)
    paper_status_parser.add_argument("--target-points", type=float, default=200.0)
    paper_status_parser.add_argument("--limit", type=int, default=20)

    demo_parser = subparsers.add_parser(
        "demo",
        help=(
            "Executor em conta demo (Fase 11) — envia ordens reais a uma conta DEMO, "
            "nunca a uma conta real"
        ),
    )
    demo_subparsers = demo_parser.add_subparsers(dest="demo_command", required=True)

    demo_run_parser = demo_subparsers.add_parser(
        "run",
        help="Roda N iteracoes de coleta+risco+envio de ordem (exige modo DEMO e conta demo)",
    )
    demo_run_parser.add_argument("--symbol", required=True)
    demo_run_parser.add_argument("--timeframe", default="M1", choices=[t.value for t in Timeframe])
    demo_run_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    demo_run_parser.add_argument(
        "--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS)
    )
    demo_run_parser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    demo_run_parser.add_argument("--stop-points", type=float, default=100.0)
    demo_run_parser.add_argument("--target-points", type=float, default=200.0)
    demo_run_parser.add_argument("--magic", type=int, default=0)
    demo_run_parser.add_argument("--risk-per-trade-pct", type=float, default=1.0)
    demo_run_parser.add_argument("--max-daily-loss-pct", type=float, default=3.0)
    demo_run_parser.add_argument("--max-consecutive-losses", type=int, default=3)
    demo_run_parser.add_argument("--max-simultaneous-positions", type=int, default=1)
    demo_run_parser.add_argument("--max-trades-per-day", type=int, default=10)
    demo_run_parser.add_argument("--min-seconds-between-trades", type=int, default=60)
    demo_run_parser.add_argument("--max-spread-points", type=float, default=30.0)
    demo_run_parser.add_argument("--iterations", type=int, default=1)
    demo_run_parser.add_argument(
        "--poll-seconds", type=float, default=5.0, help="Pausa entre iteracoes (0 = sem pausa)"
    )
    demo_run_parser.add_argument(
        "--lookback-bars",
        type=int,
        default=500,
        help="Usado apenas na primeira coleta (backfill) para este simbolo/timeframe",
    )

    demo_status_parser = demo_subparsers.add_parser(
        "status", help="Lista os live trades mais recentes de um simbolo/estrategia"
    )
    demo_status_parser.add_argument("--symbol", required=True)
    demo_status_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    demo_status_parser.add_argument(
        "--strategy", default="ema_crossover", choices=list(STRATEGY_NAMES)
    )
    demo_status_parser.add_argument(
        "--fast", type=int, default=9, choices=list(features_module.EMA_PERIODS)
    )
    demo_status_parser.add_argument(
        "--slow", type=int, default=21, choices=list(features_module.EMA_PERIODS)
    )
    demo_status_parser.add_argument("--stop-points", type=float, default=100.0)
    demo_status_parser.add_argument("--target-points", type=float, default=200.0)
    demo_status_parser.add_argument("--limit", type=int, default=20)

    monitor_parser = subparsers.add_parser(
        "monitor", help="Detecção de drift de modelos e saúde do feed (Fase 13)"
    )
    monitor_subparsers = monitor_parser.add_subparsers(dest="monitor_command", required=True)

    monitor_model_parser = monitor_subparsers.add_parser(
        "model",
        help=(
            "Compara um modelo registrado contra um dataset recente: drift de "
            "features (PSI) e degradação de calibração/desempenho"
        ),
    )
    monitor_model_parser.add_argument(
        "--version", default=None, help="Versão a avaliar (padrão: a versão 'current')"
    )
    monitor_model_parser.add_argument(
        "--recent-dataset",
        required=True,
        help="CSV gerado por 'ml build-dataset' com dados recentes",
    )
    monitor_model_parser.add_argument("--volume", type=float, default=0.01)
    monitor_model_parser.add_argument("--commission-per-lot", type=float, default=0.0)
    monitor_model_parser.add_argument("--slippage-points", type=float, default=0.0)

    monitor_feed_parser = monitor_subparsers.add_parser(
        "feed", help="Verifica se o feed de candles está atualizado para um símbolo/timeframe"
    )
    monitor_feed_parser.add_argument("--symbol", required=True)
    monitor_feed_parser.add_argument(
        "--timeframe", default="M1", choices=[t.value for t in Timeframe]
    )
    monitor_feed_parser.add_argument("--max-delay-seconds", type=float, default=300.0)

    analysis_parser = subparsers.add_parser(
        "analysis",
        help=(
            "Motor de analise Price Action / SMC / multi-timeframe (Fase 18) — "
            "somente consultivo, nunca envia ordem"
        ),
    )
    analysis_subparsers = analysis_parser.add_subparsers(dest="analysis_command", required=True)

    analysis_run_parser = analysis_subparsers.add_parser(
        "run",
        help="Analisa um simbolo e retorna ENTER/DO_NOT_ENTER com justificativa completa",
    )
    analysis_run_parser.add_argument("--symbol", required=True)
    analysis_run_parser.add_argument(
        "--timeframe", default=None, choices=[t.value for t in Timeframe]
    )
    analysis_run_parser.add_argument("--threshold", type=float, default=None)
    analysis_run_parser.add_argument(
        "--no-gates",
        action="store_true",
        help=(
            "Modo pesquisa: ignora os portoes duros (cobertura, volume, fontes "
            "externas). Os portoes sao independentes do limiar — sem esta flag "
            "eles valem em qualquer limiar. Nunca use para decidir operacao real."
        ),
    )
    analysis_run_parser.add_argument("--json", action="store_true")

    analysis_calibrate_parser = analysis_subparsers.add_parser(
        "calibrate",
        help=(
            "Mede a distribuicao real de scores no historico coletado e ajuda a "
            "escolher o limiar com dado, nao com numero redondo"
        ),
    )
    analysis_calibrate_parser.add_argument("--symbol", required=True)
    analysis_calibrate_parser.add_argument(
        "--timeframe", default=None, choices=[t.value for t in Timeframe]
    )
    analysis_calibrate_parser.add_argument(
        "--bars", type=int, default=500, help="Quantas barras recentes percorrer"
    )
    analysis_calibrate_parser.add_argument(
        "--step", type=int, default=5, help="Avaliar a cada N barras (padrao 5)"
    )
    analysis_calibrate_parser.add_argument("--json", action="store_true")

    scanner_parser = subparsers.add_parser(
        "scanner",
        help="Varredura de oportunidades entre todos os instrumentos coletados",
    )
    scanner_subparsers = scanner_parser.add_subparsers(
        dest="scanner_command", required=True
    )
    scanner_run_parser = scanner_subparsers.add_parser(
        "run", help="Ranking de oportunidades agora (nao envia ordem)"
    )
    scanner_run_parser.add_argument(
        "--timeframe", default=None, choices=[t.value for t in Timeframe]
    )
    scanner_run_parser.add_argument("--limit", type=int, default=15)
    scanner_run_parser.add_argument(
        "--record", action="store_true", help="Grava a escolha no diario de observacao"
    )
    scanner_run_parser.add_argument(
        "--open-symbols",
        default="",
        help="Posicoes ja abertas (separadas por virgula) para checar correlacao",
    )

    calendar_parser = subparsers.add_parser(
        "calendar",
        help="Calendario economico usado pelo filtro de eventos de alto impacto",
    )
    calendar_subparsers = calendar_parser.add_subparsers(
        dest="calendar_command", required=True
    )
    calendar_check_parser = calendar_subparsers.add_parser(
        "check",
        help="Mostra os eventos lidos e se o robo bloquearia a entrada agora",
    )
    calendar_check_parser.add_argument("--symbol", default="EURUSD")
    calendar_check_parser.add_argument(
        "--horizon", type=int, default=1440, help="Janela consultada, em minutos"
    )
    calendar_check_parser.add_argument("--limit", type=int, default=15)

    apexflow_parser = subparsers.add_parser(
        "apexflow",
        help=(
            "Motor ApexFlow AI: fluxo de ticks, microestrutura e price action "
            "(COMPRAR / VENDER / NAO OPERAR)"
        ),
    )
    apexflow_subparsers = apexflow_parser.add_subparsers(
        dest="apexflow_command", required=True
    )
    apexflow_analyze_parser = apexflow_subparsers.add_parser(
        "analyze",
        help="Explica a decisao do motor para um simbolo (consultivo, nao opera)",
    )
    apexflow_analyze_parser.add_argument("--symbol", required=True)
    apexflow_analyze_parser.add_argument(
        "--timeframe",
        default="M5",
        choices=["M1", "M5", "M15"],
        help="Timeframe de ENTRADA. H1 fornece contexto e nunca entrada.",
    )
    apexflow_analyze_parser.add_argument("--min-confidence", type=float, default=None)
    apexflow_analyze_parser.add_argument("--json", action="store_true")

    apexflow_history_parser = apexflow_subparsers.add_parser(
        "history", help="Desempenho registrado pelo Learning Engine"
    )
    apexflow_history_parser.add_argument("--symbol", default=None)
    apexflow_history_parser.add_argument("--limit", type=int, default=20)

    autopilot_parser = subparsers.add_parser(
        "autopilot",
        help=(
            "Piloto automatico: escolhe o operacional pelo horario/volume do par e "
            "opera em conta DEMO"
        ),
    )
    autopilot_subparsers = autopilot_parser.add_subparsers(
        dest="autopilot_command", required=True
    )
    autopilot_run_parser = autopilot_subparsers.add_parser(
        "run", help="Roda N ciclos do piloto automatico (exige modo DEMO e conta demo)"
    )
    autopilot_run_parser.add_argument(
        "--symbol",
        default=None,
        help="Sobrescreve a moeda configurada no dashboard, so para esta execucao",
    )
    autopilot_run_parser.add_argument("--iterations", type=int, default=1)
    autopilot_run_parser.add_argument("--poll-seconds", type=float, default=15.0)
    autopilot_run_parser.add_argument(
        "--force",
        action="store_true",
        help="Roda mesmo com a automacao desligada na configuracao (ciclo avulso)",
    )
    autopilot_status_parser = autopilot_subparsers.add_parser(
        "status", help="Mostra o que o piloto automatico esta fazendo agora"
    )
    autopilot_status_parser.add_argument("--limit", type=int, default=10)
    autopilot_status_parser.add_argument("--json", action="store_true")

    preflight_parser = subparsers.add_parser(
        "preflight", help="Checagens de prontidão operacional (Fase 15)"
    )
    preflight_subparsers = preflight_parser.add_subparsers(dest="preflight_command", required=True)
    preflight_subparsers.add_parser(
        "check",
        help=(
            "Valida segredo de aplicação, banco/migrations, diretórios de artefato e "
            "credenciais MT5"
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, log_dir=settings.log_dir, json_format=settings.log_json
    )

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "user":
        if args.user_command == "list":
            return cmd_user_list(args)
        if args.user_command == "reset-password":
            return cmd_user_reset_password(args)
        if args.user_command == "create":
            return cmd_user_create(args)

    if args.command == "mt5":
        if args.mt5_command == "check":
            return cmd_mt5_check(args)
        if args.mt5_command == "symbols":
            return cmd_mt5_symbols(args)
        if args.mt5_command == "bridge":
            return cmd_mt5_bridge(args)

    if args.command == "collect":
        if args.collect_command == "candles":
            return cmd_collect_candles(args)
        if args.collect_command == "ticks":
            return cmd_collect_ticks(args)

    if args.command == "quality" and args.quality_command == "check":
        return cmd_quality_check(args)

    if args.command == "data" and args.data_command == "purge-ticks":
        return cmd_data_purge_ticks(args)

    if args.command == "data" and args.data_command == "retention":
        return cmd_data_retention(args)

    if args.command == "features" and args.features_command == "build":
        return cmd_features_build(args)

    if args.command == "backtest" and args.backtest_command == "run":
        return cmd_backtest_run(args)

    if args.command == "backtest" and args.backtest_command == "compare":
        return cmd_backtest_compare(args)

    if args.command == "backtest" and args.backtest_command == "run-ticks":
        return cmd_backtest_run_ticks(args)

    if args.command == "backtest" and args.backtest_command == "walk-forward":
        return cmd_backtest_walk_forward(args)

    if args.command == "backtest" and args.backtest_command == "monte-carlo":
        return cmd_backtest_monte_carlo(args)

    if args.command == "backtest" and args.backtest_command == "stress-test":
        return cmd_backtest_stress_test(args)

    if args.command == "ml":
        if args.ml_command == "build-dataset":
            return cmd_ml_build_dataset(args)
        if args.ml_command == "train":
            return cmd_ml_train(args)
        if args.ml_command == "evaluate":
            return cmd_ml_evaluate(args)
        if args.ml_command == "walk-forward":
            return cmd_ml_walk_forward(args)

    if args.command == "mode":
        if args.mode_command == "show":
            return cmd_mode_show(args)
        if args.mode_command == "set":
            return cmd_mode_set(args)

    if args.command == "paper":
        if args.paper_command == "run":
            return cmd_paper_run(args)
        if args.paper_command == "status":
            return cmd_paper_status(args)

    if args.command == "demo":
        if args.demo_command == "run":
            return cmd_demo_run(args)
        if args.demo_command == "status":
            return cmd_demo_status(args)

    if args.command == "apexflow":
        if args.apexflow_command == "analyze":
            return cmd_apexflow_analyze(args)
        if args.apexflow_command == "history":
            return cmd_apexflow_history(args)

    if args.command == "autopilot":
        if args.autopilot_command == "run":
            return cmd_autopilot_run(args)
        if args.autopilot_command == "status":
            return cmd_autopilot_status(args)

    if args.command == "monitor":
        if args.monitor_command == "model":
            return cmd_monitor_model(args)
        if args.monitor_command == "feed":
            return cmd_monitor_feed(args)

    if args.command == "preflight" and args.preflight_command == "check":
        return cmd_preflight_check(args)

    if args.command == "analysis" and args.analysis_command == "run":
        return cmd_analysis_run(args)
    if args.command == "analysis" and args.analysis_command == "calibrate":
        return cmd_analysis_calibrate(args)
    if args.command == "calendar" and args.calendar_command == "check":
        return cmd_calendar_check(args)
    if args.command == "scanner" and args.scanner_command == "run":
        return cmd_scanner_run(args)

    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
