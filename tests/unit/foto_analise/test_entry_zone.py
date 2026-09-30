"""EntryZoneEngine.

A pergunta que estes testes guardam nao e "achou a zona?". E se o motor
sabe DISTINGUIR entrar agora de esperar de perseguir — porque as tres
levam a acoes diferentes, e confundi-las custa dinheiro em direcoes
opostas.
"""

from __future__ import annotations

from app.foto_analise.entry_zone import EntryStatus, EntryZoneEngine
from app.foto_analise.heatmap import HeatmapBand
from app.strategies.base import SignalDirection

TICK = 0.25


def _banda(price: float, buy: float, sell: float = 40.0) -> HeatmapBand:
    return HeatmapBand(price=price, buy_score=buy, sell_score=sell)


def _mapa() -> list[HeatmapBand]:
    """Pico de compra em 24584, decaindo para os lados."""
    return [
        _banda(24570, 40),
        _banda(24576, 58),
        _banda(24580, 78),
        _banda(24584, 88),
        _banda(24588, 80),
        _banda(24594, 55),
        _banda(24600, 45),
    ]


def _zona(current_price: float, bands=None, direction=SignalDirection.LONG):
    return EntryZoneEngine().build(
        _mapa() if bands is None else bands,
        direction=direction,
        current_price=current_price,
        tick_size=TICK,
    )


# --- a zona ----------------------------------------------------------------


def test_the_sweet_spot_is_the_strongest_band() -> None:
    assert _zona(24601).sweet_spot == 24584


def test_the_zone_is_a_range_not_a_price() -> None:
    """Perseguir um preco exato faz perder entradas boas por um tick.

    A zona nao precisa ter o sweet spot no centro — o pico pode ficar numa
    borda. O que ela precisa e ter largura e conte-lo.
    """
    zona = _zona(24601)

    assert zona.max > zona.min
    assert zona.min <= zona.sweet_spot <= zona.max


def test_the_zone_only_absorbs_comparable_neighbours() -> None:
    """Faixa forte cercada de fracas e mais provavelmente ruido que
    regiao — a zona precisa ser algo que o preco percorre."""
    bands = [_banda(24570, 30), _banda(24584, 88), _banda(24600, 30)]

    zona = _zona(24601, bands=bands)

    # A zona nao absorve as vizinhas fracas, mas continua sendo uma faixa:
    # ela cobre meia distancia ate cada lado.
    assert zona.sweet_spot == 24584
    assert zona.min < 24584 < zona.max
    assert zona.max - zona.min < 24600 - 24570


def test_an_isolated_band_is_still_a_region() -> None:
    """`min == max` daria ao operador um preco unico disfarcado de zona."""
    bands = [_banda(24570, 30), _banda(24584, 88), _banda(24600, 30)]

    zona = _zona(24601, bands=bands)

    assert zona.max > zona.min


# --- entrar agora, esperar ou perseguir ------------------------------------


def test_price_inside_the_zone_is_ready() -> None:
    assert _zona(24584).status == EntryStatus.IN_ZONE


def test_price_above_a_buy_zone_means_wait() -> None:
    zona = _zona(24601)

    assert zona.status == EntryStatus.WAIT_PULLBACK
    assert zona.distance_ticks > 0


def test_price_below_a_buy_zone_means_missed() -> None:
    """Zona ACIMA do preco numa compra significa que o movimento ja saiu
    de la — comprar aqui e entrar tarde, nao antecipado."""
    assert _zona(24560).status == EntryStatus.MISSED


def test_the_short_side_mirrors_the_logic() -> None:
    bands = [
        _banda(24570, 30, 45),
        _banda(24584, 30, 88),
        _banda(24600, 30, 50),
    ]

    esperando = _zona(24560, bands=bands, direction=SignalDirection.SHORT)
    tarde = _zona(24601, bands=bands, direction=SignalDirection.SHORT)

    assert esperando.status == EntryStatus.WAIT_PULLBACK
    assert tarde.status == EntryStatus.MISSED


def test_distance_is_measured_in_ticks() -> None:
    """Ticks e a unidade em que o operador pensa o alvo e o risco."""
    zona = _zona(24601)

    assert zona.distance_ticks == int(round((24601 - zona.max) / TICK))


def test_distance_is_zero_inside_the_zone() -> None:
    assert _zona(24584).distance_ticks == 0


# --- ausencia de oportunidade ----------------------------------------------


def test_a_weak_map_is_reported_as_no_setup() -> None:
    fracas = [_banda(24580, 51), _banda(24584, 53), _banda(24588, 50)]

    assert _zona(24601, bands=fracas).status == EntryStatus.NO_SETUP


def test_no_setup_still_returns_where_it_could_appear() -> None:
    """"Nada" nao ajuda ninguem a esperar a coisa certa; "compraria em X se
    voltasse" ajuda."""
    fracas = [_banda(24580, 51), _banda(24584, 53), _banda(24588, 50)]

    zona = _zona(24601, bands=fracas)

    assert zona is not None
    assert zona.sweet_spot == 24584
    assert zona.is_actionable is False


def test_an_empty_map_returns_nothing() -> None:
    assert _zona(24601, bands=[]) is None
