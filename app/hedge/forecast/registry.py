"""Qual motor de previsao responde, e sob que versao.

Um registro em vez de um `import` direto porque a versao do modelo
precisa viajar junto com cada numero ate o historico. Sem isso, uma
troca de motor torna todo o passado incomparavel: nao da para saber se a
calibracao mudou porque o mercado mudou ou porque o modelo mudou.

Nenhum motor treinado acompanha esta versao. `default_engine()` devolve o
baseline teorico, e a tela diz isso — ver `ModelKind`.
"""

from __future__ import annotations

from app.hedge.forecast.base import ForecastEngine, ModelKind
from app.hedge.forecast.baseline import BaselineForecastEngine

_ENGINES: dict[str, ForecastEngine] = {}


def register(engine: ForecastEngine) -> ForecastEngine:
    _ENGINES[engine.version] = engine
    return engine


def get_engine(version: str | None = None) -> ForecastEngine:
    """O motor pedido, ou o padrao.

    Versao desconhecida levanta em vez de cair no padrao em silencio: um
    historico gravado com "a versao que existia" e um historico que nao
    da para reproduzir.
    """
    if version is None:
        return default_engine()
    motor = _ENGINES.get(version)
    if motor is None:
        raise KeyError(f"motor de previsao desconhecido: {version}")
    return motor


def default_engine() -> ForecastEngine:
    return _ENGINES[BaselineForecastEngine.version]


def available_versions() -> list[tuple[str, ModelKind]]:
    return sorted((v, e.kind) for v, e in _ENGINES.items())


register(BaselineForecastEngine())
