"""Janela de bloqueio em torno de evento economico.

Duas regras, e as duas nasceram de defeitos reais do portao anterior:

1. **A hora e comparada contra o agendamento**, nao contra uma data de
   publicacao. O codigo antigo exigia `published_at` no futuro — condicao
   impossivel para uma noticia ja publicada — e por isso nunca bloqueou nada.

2. **A moeda do evento precisa ser a do instrumento.** O campo de moeda
   existia e era simplesmente ignorado: um dado do iene teria bloqueado uma
   entrada em EUR/USD. Um portao que barra o que nao deveria e tao ruim
   quanto um que nao barra nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.calendar_feed.provider import CalendarEvent
from app.market.catalog import MARKET_CATALOG

# Metais nao tem "moeda base" no sentido macro: XAU nao tem banco central.
# O que move ouro e prata e a economia americana, entao o lado que importa
# e o de cotacao.
_NON_MONETARY_BASES = frozenset({"XAU", "XAG", "XPT", "XPD", "USTEC", "BTC", "ETH"})
"""Bases que nao sao moeda e portanto nao tem agenda economica propria.

Para elas o filtro considera so a moeda de cotacao. Sem BTC/ETH aqui,
`currencies_for_symbol("BTCUSD")` cairia na divisao de seis letras, nao
reconheceria "BTC" como moeda e devolveria vazio — ou seja, "nao sei
filtrar", e o ativo passaria a ignorar ate os eventos do dolar, que o
movem de verdade."""

_CATALOG_BY_CODE = {item.code: item for item in MARKET_CATALOG}

# Codigos ISO 4217 negociaveis. Lista explicita, e nao derivada do catalogo,
# porque o catalogo cobre os pares que a interface oferece — nao todas as
# moedas que podem aparecer num nome de simbolo da corretora.
_KNOWN_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD",
        "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "TRY", "ZAR",
        "MXN", "SGD", "HKD", "CNH", "CNY", "RUB", "ILS", "THB",
    }
)


def currencies_for_symbol(symbol: str) -> frozenset[str]:
    """Moedas cujos eventos podem mover este instrumento.

    Usa o catalogo quando o simbolo e conhecido (inclusive com sufixo de
    corretora) e cai para a divisao em seis letras quando nao e — sem
    inventar: se nao der para deduzir, devolve vazio, e quem chama trata
    isso como "nao sei filtrar" em vez de "nao ha evento".
    """
    limpo = symbol.strip().upper()
    if not limpo:
        return frozenset()

    instrumento = _CATALOG_BY_CODE.get(limpo)
    if instrumento is None:
        # Sufixos de corretora: EURUSD.r, XAUUSD_i, GBPUSDpro.
        for code, item in _CATALOG_BY_CODE.items():
            if limpo.startswith(code):
                instrumento = item
                break

    if instrumento is not None:
        moedas = {instrumento.quote}
        if instrumento.base not in _NON_MONETARY_BASES:
            moedas.add(instrumento.base)
        return frozenset(moeda for moeda in moedas if moeda)

    if len(limpo) >= 6 and limpo[:6].isalpha():
        base, quote = limpo[:3], limpo[3:6]
        # Ambos os lados precisam ser moedas conhecidas. Sem essa checagem,
        # "USTECH" viraria {"UST", "ECH"} e o filtro passaria a descartar
        # eventos legitimos com base em siglas inventadas — pior que nao
        # filtrar, porque falha em silencio.
        if quote in _KNOWN_CURRENCIES and (
            base in _KNOWN_CURRENCIES or base in _NON_MONETARY_BASES
        ):
            moedas = {quote}
            if base not in _NON_MONETARY_BASES:
                moedas.add(base)
            return frozenset(moedas)

    return frozenset()


@dataclass(frozen=True, slots=True)
class BlackoutWindow:
    minutes_before: int = 30
    minutes_after: int = 15

    def covers(self, event_at: datetime, *, now: datetime) -> bool:
        inicio = event_at - timedelta(minutes=self.minutes_before)
        fim = event_at + timedelta(minutes=self.minutes_after)
        return inicio <= now <= fim


def find_blocking_event(
    events: list[CalendarEvent],
    *,
    symbol: str,
    now: datetime,
    window: BlackoutWindow,
    min_impact: str = "HIGH",
) -> CalendarEvent | None:
    """Primeiro evento que justifica nao operar agora — ou None.

    Devolve o evento inteiro (nao um booleano) para que a mensagem no painel
    possa dizer QUAL evento esta barrando e a que horas. "Bloqueado por
    noticia" sem dizer qual e o tipo de aviso que nao ajuda ninguem.
    """
    moedas = currencies_for_symbol(symbol)
    niveis = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    minimo = niveis.get(min_impact.upper(), 2)

    for evento in sorted(events, key=lambda item: item.scheduled_at):
        if niveis.get(evento.impact.upper(), 0) < minimo:
            continue
        # Sem moeda declarada o evento e tratado como global (feriado,
        # decisao de organismo internacional): melhor barrar do que ignorar.
        if evento.currency and moedas and evento.currency.upper() not in moedas:
            continue
        if window.covers(evento.scheduled_at, now=now):
            return evento
    return None


def describe(event: CalendarEvent, *, now: datetime) -> str:
    """Motivo em linguagem de operador, com o tempo relativo."""
    delta = (event.scheduled_at - now).total_seconds() / 60
    quando = (
        f"em {int(round(delta))} min" if delta >= 0 else f"ha {int(round(abs(delta)))} min"
    )
    moeda = f" [{event.currency.upper()}]" if event.currency else ""
    return f"Evento de alto impacto{moeda} {quando}: {event.title}."
