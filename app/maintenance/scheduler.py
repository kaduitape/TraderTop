"""O que faz a retencao acontecer sem ninguem mandar.

## Por que uma thread dentro da API

Na VPS sobem apenas dois servicos: `db` e `app`. O `mt5-worker`, que seria
o lugar natural de uma rotina residente, esta atras do profile `local-mt5`
e **nao roda la** de proposito. Nao ha cron no arranjo, e um cron no host
ficaria fora do repositorio — ou seja, fora do deploy, fora do teste e
fora da revisao.

Entao a rotina vive no unico processo que esta sempre de pe: o proprio
servidor web, como thread daemon iniciada no lifespan. O `app` roda em
uvicorn sem `--workers`, um processo so, entao nao ha multiplicacao de
agendadores.

## Regras que esta thread nao pode violar

1. **Nunca derrubar a API.** Qualquer excecao e registrada e engolida. Uma
   purga que falha e um problema de manutencao; a API fora do ar por causa
   dela seria um problema de producao.
2. **Nunca rodar na suite.** O lifespan dispara no `TestClient`, e uma
   thread apagando linhas em paralelo com os testes produziria falha
   intermitente — a pior especie. `APP_ENV=test` desliga.
3. **Nunca purgar a cada reinicio.** Um container que reinicia de hora em
   hora dispararia uma purga por reinicio. O horario da ultima execucao
   fica em `system_settings`, nao em memoria.

O registro em `system_settings` e guarda contra REINICIO, nao um mutex
distribuido: duas instancias simultaneas poderiam purgar juntas. Seria
desperdicio de IO, nao corrupcao — os lotes sao idempotentes e cada um
apaga por id. Com um processo so, o caso nao ocorre.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from app.core.config import Settings, get_settings
from app.database.repositories.system_setting_repository import SystemSettingRepository
from app.database.session import get_session_factory
from app.maintenance.retention import RetentionPolicy, RetentionResult, apply_retention

logger = logging.getLogger(__name__)

LAST_RUN_KEY = "retention_last_run_utc"

_FIRST_RUN_DELAY_SECONDS = 120.0
"""Espera antes da primeira checagem. O boot ja concorre com as migrations
e o preflight; somar uma purga de dois milhoes de linhas a isso atrasaria
o healthcheck e o container nasceria marcado como nao saudavel."""


def read_last_run(session) -> datetime | None:
    bruto = SystemSettingRepository(session).get(LAST_RUN_KEY)
    if not bruto:
        return None
    try:
        quando = datetime.fromisoformat(bruto)
    except ValueError:
        # Valor corrompido conta como "nunca rodou": melhor uma purga a
        # mais do que uma tabela que nunca mais e limpa porque alguem
        # editou a linha a mao.
        logger.warning("%s invalido (%r); tratando como nunca executado", LAST_RUN_KEY, bruto)
        return None
    return quando if quando.tzinfo is not None else quando.replace(tzinfo=UTC)


def write_last_run(session, quando: datetime) -> None:
    SystemSettingRepository(session).set(
        LAST_RUN_KEY,
        quando.isoformat(),
        description="Ultima execucao da retencao automatica (UTC).",
    )


def due(last_run: datetime | None, *, now: datetime, interval_hours: int) -> bool:
    if last_run is None:
        return True
    if last_run > now:
        # Relogio andou para tras (correcao de NTP, restore de banco). Sem
        # isto, a retencao ficaria parada ate o futuro gravado chegar.
        return True
    return (now - last_run) >= timedelta(hours=interval_hours)


def run_once(*, settings: Settings | None = None, now: datetime | None = None) -> RetentionResult:
    """Uma passada: decide se esta na hora, executa e registra.

    Separada do laco para que o CLI e o teste exercitem exatamente o que a
    thread executa, sem thread nenhuma.
    """
    s = settings or get_settings()
    agora = now or datetime.now(UTC)
    policy = RetentionPolicy.from_settings(s)

    session = get_session_factory()()
    try:
        if not due(read_last_run(session), now=agora, interval_hours=s.retention_interval_hours):
            return RetentionResult(skipped_reason="intervalo ainda nao completou")

        resultado = apply_retention(session, policy, now=agora)
        # Grava mesmo quando a politica e invalida: sem isso, uma
        # configuracao errada faria a thread reavaliar a cada ciclo e
        # encher o log com a mesma reclamacao.
        write_last_run(session, agora)
        session.commit()
        return resultado
    finally:
        session.close()


class RetentionScheduler:
    """Laco de manutencao. Inicia parado; `start` decide se vale iniciar."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._parar = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def should_start(self) -> tuple[bool, str]:
        if not self._settings.retention_enabled:
            return False, "RETENTION_ENABLED=false"
        if self._settings.app_env.lower() == "test":
            return False, "APP_ENV=test"
        problemas = RetentionPolicy.from_settings(self._settings).validate()
        if problemas:
            return False, "; ".join(problemas)
        return True, ""

    def start(self) -> bool:
        pode, motivo = self.should_start()
        if not pode:
            logger.info("Retencao automatica desligada (%s).", motivo)
            return False
        if self.running:
            return True

        self._parar.clear()
        self._thread = threading.Thread(
            target=self._loop, name="retention", daemon=True
        )
        self._thread.start()
        logger.info(
            "Retencao automatica ativa: ticks %d dia(s), eventos de qualidade "
            "%d dia(s) (%d para CRITICAL), a cada %dh.",
            self._settings.tick_retention_days,
            self._settings.quality_event_retention_days,
            self._settings.quality_event_critical_retention_days,
            self._settings.retention_interval_hours,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        # `Event.wait` em vez de `sleep`: o desligamento do container tem
        # 20s de carencia, e um `sleep` de uma hora ignoraria os dois.
        if self._parar.wait(_FIRST_RUN_DELAY_SECONDS):
            return

        intervalo = max(60.0, self._settings.retention_interval_hours * 3600 / 4)
        while not self._parar.is_set():
            try:
                resultado = run_once(settings=self._settings)
                if not resultado.skipped_reason:
                    logger.info("Retencao: %s", resultado.describe())
            except Exception:
                # Engolida de proposito — ver a regra 1 no topo do modulo.
                logger.exception("Retencao falhou; tentando de novo no proximo ciclo.")
            if self._parar.wait(intervalo):
                return
