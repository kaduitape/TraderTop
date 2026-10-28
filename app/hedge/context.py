"""De onde vem o contexto textual que o Jev classifica.

O calendario economico, que este projeto ja mantem (`app/calendar_feed/`),
e lido de arquivo com cache proprio — custo zero por snapshot. E a unica
fonte de TEXTO disponivel aqui, e e exatamente o tipo de entrada para que
um classificador serve.

## O que nao entra

Preco, nivel, lote, resultado financeiro, dado de conta. O Jev classifica
texto; mandar o resto seria ampliar a superficie de vazamento sem ganho.

## Moeda do instrumento

`currencies_for_symbol` ja resolve sufixo de corretora e sabe que indice e
cripto nao tem agenda propria — para BTCUSDm devolve `{USD}`. Sem moeda
identificada nao ha como filtrar evento, e o classificador recebe
`SEM_MOEDA_IDENTIFICADA` em vez de uma lista de eventos do mundo todo.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.calendar_feed.blackout import currencies_for_symbol
from app.calendar_feed.factory import get_calendar_provider
from app.core.config import Settings
from app.jev.hedge_context import ContextEvent, HedgeContextInput

MAX_EVENTS = 8
"""Quantos eventos seguem para o classificador.

Teto porque o `state` precisa ficar pequeno e porque uma lista longa dilui
o que importa. Os mais PROXIMOS do agora vencem — um evento daqui a cinco
minutos pesa mais que um daqui a cinquenta."""


def build_context(
    session: Session,
    *,
    symbol: str,
    horizon_seconds: int,
    settings: Settings,
    now: datetime | None = None,
    provider=None,
) -> HedgeContextInput:
    """Monta a entrada do classificador a partir do calendario.

    Nunca levanta: calendario ausente ou ilegivel vira
    `calendar_usable=False`, e o classificador devolve indisponivel. Uma
    excecao aqui derrubaria o snapshot por causa de uma camada acessoria.
    """
    agora = now or datetime.now(UTC)
    moedas = tuple(sorted(currencies_for_symbol(symbol)))

    horizonte_min = max(15, int(horizon_seconds // 60))

    try:
        resolvido = provider or get_calendar_provider(settings)
        snapshot = resolvido.fetch_events(now=agora, horizon_minutes=horizonte_min)
    except Exception:  # noqa: BLE001 - camada acessoria nunca derruba o snapshot
        return HedgeContextInput(
            symbol=symbol,
            currencies=moedas,
            horizon_minutes=horizonte_min,
            calendar_usable=False,
        )

    if not snapshot.usable:
        return HedgeContextInput(
            symbol=symbol,
            currencies=moedas,
            horizon_minutes=horizonte_min,
            calendar_usable=False,
        )

    eventos: list[ContextEvent] = []
    for evento in snapshot.events:
        moeda = (evento.currency or "").upper()
        # Sem moeda identificada no instrumento, nao da para filtrar e o
        # classificador ja recebe o caso como indisponivel. Com moeda, so
        # os eventos dela interessam: PIB da Nova Zelandia nao move o
        # Nasdaq, e incluir tudo transformaria a classificacao em ruido.
        if moedas and moeda and moeda not in moedas:
            continue

        marcado = evento.scheduled_at
        if marcado.tzinfo is None:
            marcado = marcado.replace(tzinfo=UTC)

        eventos.append(
            ContextEvent(
                title=str(evento.title)[:120],
                impact=str(evento.impact or "MEDIUM").upper(),
                minutes_until=int((marcado - agora).total_seconds() // 60),
                currency=moeda,
            )
        )

    # Os mais proximos do agora primeiro, em valor absoluto: um evento que
    # acabou de sair pesa tanto quanto um que esta chegando.
    eventos.sort(key=lambda e: abs(e.minutes_until))

    return HedgeContextInput(
        symbol=symbol,
        currencies=moedas,
        events=tuple(eventos[:MAX_EVENTS]),
        horizon_minutes=horizonte_min,
        calendar_usable=True,
    )
