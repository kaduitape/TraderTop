"""As tres probabilidades: somam 1, nao se inventam, e acabam na barreira.

O teste mais importante deste arquivo nao verifica um valor: verifica que
NAO HA valor quando falta dado (`test_no_volatility_means_no_forecast`).
Preencher a interface com percentuais fabricados e o modo de falha que
este modulo existe para impedir.
"""

from __future__ import annotations

import pytest

from app.hedge.forecast.base import Forecast, ForecastInputs, ModelKind, Unavailable
from app.hedge.forecast.baseline import BaselineForecastEngine, survival_probability
from app.hedge.forecast.registry import available_versions, default_engine, get_engine

MOTOR = BaselineForecastEngine()


def _inputs(**overrides) -> ForecastInputs:
    base = {
        "symbol": "MNQ",
        "is_long": True,
        "entry": 20000.0,
        "take": 20100.0,
        "stop": 19950.0,
        "current_price": 20000.0,
        "seconds_remaining": 3600.0,
        "atr": 12.0,
        "atr_period_seconds": 900.0,
    }
    base.update(overrides)
    return ForecastInputs(**base)  # type: ignore[arg-type]


# --- a soma -----------------------------------------------------------------


@pytest.mark.parametrize("segundos", [60, 600, 3600, 86400])
def test_the_three_always_sum_to_one(segundos: int) -> None:
    f = MOTOR.predict(_inputs(seconds_remaining=float(segundos)))

    assert f.available
    assert abs(f.take_first + f.stop_first + f.no_barrier - 1.0) < 1e-9


@pytest.mark.parametrize("segundos", [60, 137, 600, 3600, 86400, 604800])
def test_the_percentages_sum_to_exactly_one_hundred(segundos: int) -> None:
    """Arredondar cada um por conta propria da 33/33/33. Uma tela que nao
    fecha em 100 faz o operador desconfiar do numero inteiro, com razao."""
    f = MOTOR.predict(_inputs(seconds_remaining=float(segundos)))

    assert sum(f.percentages()) == 100


def test_a_forecast_that_does_not_sum_to_one_cannot_be_built() -> None:
    """A invariante mora no tipo, nao na boa vontade de quem o constroi."""
    with pytest.raises(ValueError, match="somam"):
        Forecast(
            available=True,
            model_kind=ModelKind.THEORETICAL_BASELINE,
            model_version="x",
            take_first=0.5,
            stop_first=0.5,
            no_barrier=0.5,
        )


def test_a_probability_outside_zero_to_one_is_refused() -> None:
    with pytest.raises(ValueError):
        Forecast(
            available=True,
            model_kind=ModelKind.THEORETICAL_BASELINE,
            model_version="x",
            take_first=1.5,
            stop_first=-0.5,
            no_barrier=0.0,
        )


# --- ordenacao: o que esta mais perto e mais provavel -----------------------


def test_the_closer_barrier_is_more_likely_to_come_first() -> None:
    """Stop a 50 pontos, take a 100: o stop vem primeiro mais vezes. Nao e
    opiniao sobre o mercado — e a geometria do passeio aleatorio."""
    f = MOTOR.predict(_inputs(seconds_remaining=86400.0))

    assert f.stop_first > f.take_first


def test_symmetric_barriers_give_symmetric_probabilities() -> None:
    f = MOTOR.predict(
        _inputs(take=20100.0, stop=19900.0, seconds_remaining=864000.0)
    )

    assert abs(f.take_first - f.stop_first) < 1e-6


def test_a_short_setup_mirrors_the_long_one() -> None:
    """Vendido: take embaixo, stop em cima. Com as mesmas distancias, as
    probabilidades sao as mesmas — trocar o lado nao pode inverter quem
    esta mais perto."""
    comprado = MOTOR.predict(
        _inputs(is_long=True, take=20100.0, stop=19950.0, seconds_remaining=86400.0)
    )
    vendido = MOTOR.predict(
        _inputs(is_long=False, take=19900.0, stop=20050.0, seconds_remaining=86400.0)
    )

    assert abs(comprado.take_first - vendido.take_first) < 1e-6
    assert abs(comprado.stop_first - vendido.stop_first) < 1e-6


def test_a_longer_horizon_leaves_less_mass_unresolved() -> None:
    """Mais tempo, mais chance de tocar algo. O contrario indicaria a
    serie de sobrevivencia invertida."""
    curto = MOTOR.predict(_inputs(seconds_remaining=600.0))
    longo = MOTOR.predict(_inputs(seconds_remaining=86400.0))

    assert longo.no_barrier < curto.no_barrier


# --- ausencia ---------------------------------------------------------------


def test_no_volatility_means_no_forecast() -> None:
    """O teste que mais importa: sem base para estimar, nao se estima.
    Preencher a tela com percentuais fabricados e o modo de falha que
    este modulo existe para impedir."""
    f = MOTOR.predict(_inputs(atr=None, atr_period_seconds=None))

    assert not f.available
    assert f.unavailable_reason is Unavailable.INSUFFICIENT_DATA
    assert f.take_first is None
    assert f.stop_first is None
    assert f.no_barrier is None


def test_an_atr_without_a_timescale_is_useless() -> None:
    """ATR(14) em M15 e em M1 sao numeros diferentes para o mesmo mercado.
    Sem saber de qual se trata nao da para projetar para o horizonte."""
    f = MOTOR.predict(_inputs(atr=12.0, atr_period_seconds=None))

    assert not f.available
    assert f.unavailable_reason is Unavailable.INSUFFICIENT_DATA


def test_an_unavailable_forecast_has_no_percentages() -> None:
    f = MOTOR.predict(_inputs(atr=None, atr_period_seconds=None))

    assert f.percentages() is None


# --- fim da previsao --------------------------------------------------------


@pytest.mark.parametrize(
    ("preco", "rotulo"),
    [(20100.0, "take"), (20150.0, "passou do take"), (19950.0, "stop"), (19900.0, "passou do stop")],
)
def test_a_touched_barrier_ends_the_forecast(preco: float, rotulo: str) -> None:
    """Depois da barreira, nao se recalcula como se estivesse aberta: seria
    uma previsao sobre um evento que ja aconteceu."""
    f = MOTOR.predict(_inputs(current_price=preco))

    assert not f.available, rotulo
    assert f.unavailable_reason is Unavailable.BARRIER_HIT


def test_an_expired_horizon_is_not_a_data_problem() -> None:
    """Prazo esgotado e desfecho, nao falta de dado. Os dois pedem acoes
    diferentes de quem olha a tela."""
    f = MOTOR.predict(_inputs(seconds_remaining=0.0))

    assert f.unavailable_reason is Unavailable.EXPIRED


def test_levels_on_the_same_side_are_an_invalid_setup() -> None:
    f = MOTOR.predict(_inputs(take=20100.0, stop=20050.0, current_price=20075.0))

    assert not f.available
    assert f.unavailable_reason is Unavailable.INVALID_SETUP


# --- honestidade sobre a origem --------------------------------------------


def test_the_baseline_declares_itself_theoretical() -> None:
    """O numero viaja com a origem. O mesmo valor apresentado como modelo
    treinado seria falso."""
    f = MOTOR.predict(_inputs())

    assert f.model_kind is ModelKind.THEORETICAL_BASELINE
    assert f.model_kind is not ModelKind.TRAINED


def test_the_assumptions_travel_with_the_number() -> None:
    """Um baseline sem as hipoteses e um palpite com aparencia de conta."""
    f = MOTOR.predict(_inputs())

    assert len(f.assumptions) >= 4
    juntas = " ".join(f.assumptions).lower()
    assert "tendencia" in juntas
    assert "volatilidade" in juntas
    assert "gap" in juntas


def test_the_baseline_claims_no_measured_confidence() -> None:
    """Confianca e calibracao medida. Um modelo teorico nao tem nenhuma, e
    inventar um numero ali seria o pior tipo de precisao falsa."""
    assert MOTOR.predict(_inputs()).confidence is None


def test_no_trained_model_ships_in_this_version() -> None:
    """Se um dia houver, este teste falha e quem o mudar tera de olhar os
    criterios de validacao."""
    assert all(kind is not ModelKind.TRAINED for _v, kind in available_versions())


# --- registro ---------------------------------------------------------------


def test_the_default_engine_is_registered_and_versioned() -> None:
    motor = default_engine()

    assert motor.version
    assert get_engine(motor.version) is motor


def test_an_unknown_version_raises_instead_of_falling_back() -> None:
    """Cair no padrao em silencio gravaria o historico com "a versao que
    existia", e ele deixaria de ser reproduzivel."""
    with pytest.raises(KeyError):
        get_engine("modelo-que-nao-existe")


# --- a serie ----------------------------------------------------------------


def test_survival_falls_with_time_and_rises_with_distance() -> None:
    perto = survival_probability(up=10, down=10, sigma=1.0, seconds=100)
    longe = survival_probability(up=100, down=100, sigma=1.0, seconds=100)

    assert longe > perto
    assert survival_probability(up=10, down=10, sigma=1.0, seconds=1000) < perto


def test_survival_stays_inside_zero_and_one() -> None:
    """A serie trunca; truncar nao pode produzir probabilidade fora da
    faixa."""
    for segundos in (1, 10, 100, 10_000, 1_000_000):
        p = survival_probability(up=5, down=5, sigma=2.0, seconds=segundos)
        assert 0.0 <= p <= 1.0


def test_zero_width_has_no_survival() -> None:
    assert survival_probability(up=0, down=0, sigma=1.0, seconds=10) == 0.0
