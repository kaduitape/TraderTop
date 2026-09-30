"""Como o resto do sistema obtem (ou nao) um avaliador Jev.

O cache e o cliente sao criados UMA vez por processo. Um avaliador novo a
cada requisicao teria cache novo a cada requisicao — ou seja, cache
nenhum, e uma chamada paga a cada refresh do indicador.

`get_pulse_assessor` devolve `None` quando o recurso esta desligado ou sem
chave. `None` e a resposta, nao um erro: a rota segue com o contrato
tecnico completo e marca `signal_ai.available = false`.
"""

from __future__ import annotations

import threading

from app.core.config import Settings, get_settings
from app.jev.cache import TTLCache
from app.jev.client import JevClient, JevUnavailable
from app.jev.pulse_assessment import PulseAssessment, PulseAssessor

_lock = threading.Lock()
_assessor: PulseAssessor | None = None
_built_from: tuple | None = None


def _signature(settings: Settings) -> tuple:
    return (
        settings.jev_enabled,
        bool(settings.jev_api_key),
        settings.jev_api_base_url,
        settings.jev_model,
        settings.jev_timeout_seconds,
        settings.jev_cache_ttl_seconds,
    )


def get_pulse_assessor(settings: Settings | None = None) -> PulseAssessor | None:
    """O avaliador compartilhado, ou None quando o Jev nao esta em uso.

    A assinatura da configuracao entra na decisao de reconstruir: sem isso,
    trocar a chave ou a URL em um teste (ou num reload) continuaria usando
    o cliente antigo, e o sintoma apareceria longe da causa.
    """
    global _assessor, _built_from

    resolvidas = settings or get_settings()
    if not resolvidas.jev_enabled or not resolvidas.jev_api_key:
        return None

    assinatura = _signature(resolvidas)
    with _lock:
        if _assessor is not None and _built_from == assinatura:
            return _assessor

        try:
            cliente = JevClient(
                api_key=resolvidas.jev_api_key,
                base_url=resolvidas.jev_api_base_url,
                model=resolvidas.jev_model,
                timeout_seconds=resolvidas.jev_timeout_seconds,
            )
        except JevUnavailable:
            return None

        _assessor = PulseAssessor(
            cliente,
            cache=TTLCache[PulseAssessment](ttl_seconds=resolvidas.jev_cache_ttl_seconds),
            cache_ttl_seconds=resolvidas.jev_cache_ttl_seconds,
        )
        _built_from = assinatura
        return _assessor


def reset_pulse_assessor() -> None:
    """Esquece o avaliador. Existe para o teste nao herdar cache do anterior."""
    global _assessor, _built_from
    with _lock:
        _assessor = None
        _built_from = None
