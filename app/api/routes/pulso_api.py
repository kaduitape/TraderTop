"""API publica do Pulso — o contrato que o indicador MT5 consome.

## Por que ela e diferente de `/api/foto-analise`

Mesma analise, formato diferente. O consumidor aqui e MQL5, que nao tem
parser de JSON: quem escreve o indicador extrai valor por valor, com busca
de string. Isso muda o desenho da resposta e as escolhas nao sao esteticas:

- **objeto raso.** Cada nivel de aninhamento vira mais codigo de parsing
  no indicador — e mais codigo de parsing e mais lugar para errar em
  silencio;
- **numeros sem aspas, nunca `null`.** `"take": null` obriga o cliente a
  distinguir ausencia de zero em string; ausencia vira `0` e uma flag
  booleana ao lado;
- **`zones[]` plano**, com `kind` dizendo o que desenhar. O indicador
  percorre a lista sem conhecer a semantica de cada zona, entao adicionar
  uma zona nova aqui nao exige recompilar o indicador.

## O que esta rota nao faz

Nao envia ordem, nao cancela ordem, nao muda o modo do sistema. O
interruptor liga e desliga o DESENHO. Um botao no grafico parece um botao
de robo, e a resposta diz em texto que nao e — para que ninguem descubra a
diferenca do jeito caro.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies.api_token import get_api_token
from app.database.models.api_token import ApiToken
from app.database.repositories.audit_log_repository import AuditLogRepository
from app.database.repositories.symbol_repository import SymbolRepository
from app.database.session import get_db
from app.foto_analise.heatmap import HeatmapDetail
from app.foto_analise.service import FotoAnaliseService
from app.foto_analise.toggle import load_analysis_toggle, set_symbol_enabled
from app.market.catalog import resolve_broker_symbol
from app.market.multi_timeframe import ANALYSIS_TIMEFRAMES, SymbolNotFoundError
from app.mt5.market_data import Timeframe

router = APIRouter(prefix="/api/pulso", tags=["pulso"])

CONTRACT_VERSION = 1
"""Versao do formato. O indicador compara e avisa o operador quando o
servidor mudou de contrato — melhor uma mensagem no grafico do que zonas
desenhadas no lugar errado por um campo que mudou de nome."""

MIN_BAND_SCORE_DRAWN = 62.0
"""Faixas abaixo disso nao viram zona no grafico do MetaTrader.

O grafico dele nao e o painel: 33 retangulos translucidos sobre candles
reais viram sujeira, e sujeira esconde justamente o nivel que importa. O
mapa completo continua disponivel no painel."""


class ToggleIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    enabled: bool


def _timeframe(valor: str) -> Timeframe:
    try:
        timeframe = Timeframe(valor.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timeframe invalido: {valor}",
        ) from exc
    if timeframe not in ANALYSIS_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"timeframe {timeframe.value} fora da matriz de analise "
                f"({', '.join(t.value for t in ANALYSIS_TIMEFRAMES)})."
            ),
        )
    return timeframe


def _zonas(foto) -> list[dict]:
    """Zonas prontas para desenhar, sem semantica no cliente.

    O indicador so le `kind`, `price_min`, `price_max` e `color`. Toda a
    decisao de o que e favoravel ou perigoso ja foi tomada aqui — no lugar
    onde ela pode ser testada.
    """
    zonas: list[dict] = []
    comprando = foto.bias == "LONG"

    zona = foto.entry_zone
    if zona is not None:
        zonas.append(
            {
                "kind": "ENTRY",
                "label": "BUY ZONE" if comprando else "SELL ZONE",
                "price_min": zona.min,
                "price_max": zona.max,
                "color": "GREEN" if comprando else "RED",
                "score": zona.score,
            }
        )

    if foto.stop is not None:
        # A zona de risco e aberta de um lado: dali em diante o cenario
        # acabou. O indicador fecha o retangulo na borda da janela.
        zonas.append(
            {
                "kind": "RISK",
                "label": "ZONA DE RISCO",
                "price_min": 0.0 if comprando else foto.stop,
                "price_max": foto.stop if comprando else 0.0,
                "color": "RED",
                "score": 0.0,
            }
        )

    for faixa in foto.heatmap:
        score = faixa.buy_score if comprando else faixa.sell_score
        if score < MIN_BAND_SCORE_DRAWN:
            continue
        zonas.append(
            {
                "kind": "HEAT",
                "label": ", ".join(faixa.factors)[:60] or "confluencia",
                "price_min": faixa.price,
                "price_max": faixa.price,
                "color": "GREEN" if score >= 75 else "YELLOW",
                "score": score,
            }
        )
    return zonas


def _payload(foto, *, enabled: bool) -> dict:
    zona = foto.entry_zone
    return {
        "contract_version": CONTRACT_VERSION,
        "enabled": enabled,
        "symbol": foto.symbol,
        "timeframe": foto.timeframe.value,
        "generated_at": foto.generated_at.isoformat(),
        "server_time": datetime.now(UTC).isoformat(),
        "decision": foto.decision,
        "bias": foto.bias,
        "direction_source": foto.direction_source,
        "direction_rationale": foto.direction_rationale,
        "status": foto.status,
        "score": foto.score,
        "price": foto.current_price,
        "price_source": foto.price_source,
        "tick_size": foto.tick_size,
        "take_ticks": foto.take_ticks,
        # Ausencia vira 0 + flag: `null` obrigaria o parser do MQL5 a
        # distinguir "sem valor" de "zero" dentro de uma string.
        "has_entry": zona is not None,
        "entry_min": zona.min if zona else 0.0,
        "entry_max": zona.max if zona else 0.0,
        "sweet_spot": zona.sweet_spot if zona else 0.0,
        "distance_ticks": zona.distance_ticks if zona else 0,
        "has_take": foto.take is not None,
        "take": foto.take or 0.0,
        "has_stop": foto.stop is not None,
        "stop": foto.stop or 0.0,
        "has_decision_level": foto.decision_level is not None,
        "decision_level": foto.decision_level or 0.0,
        "is_stale": foto.is_stale,
        "data_age_minutes": foto.data_age_minutes or 0.0,
        "zones": _zonas(foto),
        "reasons_for": foto.reasons_for[:3],
        "reasons_against": foto.reasons_against[:2],
        # Texto pronto para a legenda do grafico. Formatar no MQL5 exigiria
        # replicar as regras de arredondamento por tick — duas formatacoes
        # divergem, e a divergencia apareceria como dois precos diferentes
        # para o mesmo nivel.
        "headline": _headline(foto, enabled),
        "disclaimer": (
            "Score e confluencia, nao probabilidade de lucro. "
            "Este indicador nao envia nem cancela ordens."
        ),
    }


def _headline(foto, enabled: bool) -> str:
    """Linha unica para a legenda do grafico.

    O simbolo e o timeframe entram SEMPRE, e nao so nos casos de erro. Quem
    configurou `SymbolOverride` errado ve niveis de outro ativo desenhados
    sobre o seu grafico — e o caso normal, em que tudo parece funcionar, era
    justamente o unico que nao dizia de onde os numeros vieram.
    """
    origem = f"{foto.symbol} {foto.timeframe.value}"
    if not enabled:
        return f"{origem}: analise da IA DESLIGADA"
    if foto.is_stale:
        return f"{origem}: DADOS DESATUALIZADOS ({foto.data_age_minutes:.0f} min)"
    estados = {
        "READY": "ENTRADA AGORA",
        "WAIT_PULLBACK": "AGUARDAR PULLBACK",
        "MISSED": "PRECO JA PASSOU",
        "NO_SETUP": "SEM ENTRADA BOA AGORA",
    }
    lado = "COMPRA" if foto.bias == "LONG" else "VENDA"
    return (
        f"{origem} | {lado} {foto.score:.0f}/100 — "
        f"{estados.get(foto.status, foto.status)}"
    )


def _desligado(symbol: str, timeframe: Timeframe) -> dict:
    """Resposta quando a IA esta desligada para este simbolo.

    Devolve 200 com `enabled: false`, e nao um erro: o indicador precisa
    distinguir "voce desligou" de "o servidor caiu". Um 4xx aqui faria a
    tela dele mostrar falha de conexao para uma escolha do operador.
    """
    return {
        "contract_version": CONTRACT_VERSION,
        "enabled": False,
        "symbol": symbol,
        "timeframe": timeframe.value,
        "server_time": datetime.now(UTC).isoformat(),
        "decision": "DESLIGADO",
        "bias": "",
        "status": "DISABLED",
        "score": 0.0,
        "price": 0.0,
        "has_entry": False,
        "has_take": False,
        "has_stop": False,
        "has_decision_level": False,
        "is_stale": False,
        "zones": [],
        "reasons_for": [],
        "reasons_against": [],
        "headline": f"{symbol} {timeframe.value}: analise da IA DESLIGADA",
        "disclaimer": "Este indicador nao envia nem cancela ordens.",
    }


def _resolve_collected_symbol(db: Session, requested_symbol: str) -> str:
    """Resolve o código do gráfico para o nome gravado pela corretora.

    O EA usa o símbolo que aparece no gráfico (por exemplo, ``USTEC``), mas
    uma corretora pode publicar e persistir o mesmo ativo como ``USTECm`` ou
    ``USTEC.cash``. A análise precisa receber o nome do banco; o interruptor
    permanece no código do gráfico para não criar estados duplicados.
    """
    active_names = [symbol.name for symbol in SymbolRepository(db).list_active()]
    return resolve_broker_symbol(requested_symbol, active_names) or requested_symbol


@router.get("")
def pulso(
    symbol: str = Query(min_length=1, max_length=32),
    timeframe: str = "M15",
    take_ticks: int = Query(default=20, ge=1, le=1000),
    direction: str = "AUTO",
    detail: str = HeatmapDetail.NORMAL.value,
    db: Session = Depends(get_db),
    _token: ApiToken = Depends(get_api_token),
) -> dict:
    """O cenario atual, pronto para desenhar."""
    simbolo = symbol.strip().upper()
    tf = _timeframe(timeframe)

    if not load_analysis_toggle(db).is_enabled(simbolo):
        return _desligado(simbolo, tf)

    try:
        detalhe = HeatmapDetail(detail.upper())
    except ValueError:
        detalhe = HeatmapDetail.NORMAL

    try:
        foto = FotoAnaliseService(db, detail=detalhe).build(
            symbol=_resolve_collected_symbol(db, simbolo),
            timeframe=tf,
            take_ticks=take_ticks,
            direction=direction,
        )
    except SymbolNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Simbolo {simbolo} nao tem candles coletadas. "
                "Colete dados em Dados de mercado antes de analisar."
            ),
        ) from exc

    return _payload(foto, enabled=True)


@router.get("/status")
def pulso_status(
    symbol: str = Query(min_length=1, max_length=32),
    db: Session = Depends(get_db),
    _token: ApiToken = Depends(get_api_token),
) -> dict:
    """So o estado do interruptor. Barato o suficiente para o indicador
    consultar em cada tick sem custar uma analise inteira."""
    simbolo = symbol.strip().upper()
    return {
        "contract_version": CONTRACT_VERSION,
        "symbol": simbolo,
        "enabled": load_analysis_toggle(db).is_enabled(simbolo),
    }


@router.post("/toggle")
def pulso_toggle(
    payload: ToggleIn,
    db: Session = Depends(get_db),
    token: ApiToken = Depends(get_api_token),
) -> dict:
    """Liga/desliga a analise deste simbolo.

    Auditado: uma mudanca de estado feita de fora do painel precisa deixar
    rastro de qual chave a fez, senao ninguem consegue explicar depois por
    que a tela ficou muda.
    """
    simbolo = payload.symbol.strip().upper()
    set_symbol_enabled(db, simbolo, enabled=payload.enabled)

    AuditLogRepository(db).record(
        action="foto_analise_toggle",
        entity="foto_analise",
        detail=(
            f"{simbolo} -> {'ligado' if payload.enabled else 'desligado'} "
            f"pela chave {token.prefix}… ({token.name})"
        ),
    )
    db.commit()

    return {
        "contract_version": CONTRACT_VERSION,
        "symbol": simbolo,
        "enabled": payload.enabled,
        "note": "Afeta apenas o desenho da analise. Nenhuma ordem foi tocada.",
    }
