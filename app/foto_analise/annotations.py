"""ChartAnnotationService — o desenho.

## Por que SVG gerado no servidor

O painel nao tem biblioteca de grafico e nao ha CDN disponivel (o CSP e as
regras do projeto proibem). SVG inline resolve sem dependencia nova, sem
canvas e sem uma segunda copia da logica de posicionamento em JavaScript:
quem calcula a zona de entrada e quem a desenha compartilham a mesma
escala, no mesmo lugar.

## O que o desenho promete

Os candles sao REAIS. As zonas vem dos motores. A seta de projecao e a
unica coisa ilustrativa da tela — e por isso ela e desenhada tracejada e
rotulada como projecao, nunca como previsao.

## Regra de leitura

A tela precisa responder em 2-3 segundos: tendencia, lado, entrar ou
esperar, melhor regiao, sweet spot, take, invalidacao. Cada elemento aqui
existe para uma dessas perguntas; nada e desenhado por enfeite.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from app.foto_analise.service import STATUS_LABELS, FotoAnalise

WIDTH = 960
HEIGHT = 520
PADDING_LEFT = 12
PADDING_RIGHT = 88
"""Espaco a direita para os rotulos de preco — sem ele os textos saem
cortados na borda, que e o defeito classico de grafico gerado a mao."""
PADDING_TOP = 34
PADDING_BOTTOM = 26


@dataclass(frozen=True, slots=True)
class _Scale:
    """Converte preco em coordenada de tela.

    Guardado como objeto para que zona, niveis e candles usem exatamente a
    mesma escala. Duas escalas ligeiramente diferentes produziriam um
    desenho onde a linha do take nao encosta no candle que a atingiu.
    """

    low: float
    high: float

    def y(self, price: float) -> float:
        if self.high <= self.low:
            return HEIGHT / 2
        proporcao = (price - self.low) / (self.high - self.low)
        util = HEIGHT - PADDING_TOP - PADDING_BOTTOM
        return PADDING_TOP + (1 - proporcao) * util


class ChartAnnotationService:
    """Monta o SVG a partir de um `FotoAnalise` ja pronto."""

    def render(self, foto: FotoAnalise, *, show_heatmap: bool = True) -> str:
        if not foto.candles:
            return (
                '<div class="text-muted text-center py-5">'
                "Sem candles para desenhar neste timeframe.</div>"
            )

        escala = self._scale(foto)
        partes: list[str] = [
            f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" width="100%" '
            f'role="img" aria-label="Foto Analise de {escape(foto.symbol)}" '
            'style="max-width:100%;height:auto;font-family:inherit">'
        ]

        if show_heatmap:
            partes.append(self._heatmap(foto, escala))
        partes.append(self._zones(foto, escala))
        partes.append(self._candles(foto, escala))
        partes.append(self._levels(foto, escala))
        partes.append(self._current_price(foto, escala))
        partes.append(self._projection(foto, escala))
        partes.append(self._header(foto))
        partes.append(self._freshness(foto))
        partes.append("</svg>")
        return "".join(partes)

    # --- escala -----------------------------------------------------------

    def _scale(self, foto: FotoAnalise) -> _Scale:
        """Inclui candles E anotacoes no enquadramento.

        Enquadrar so pelos candles esconderia justamente o take ou o stop
        quando eles ficam fora do range recente — e sao esses dois que
        respondem "vale a pena?".
        """
        precos = [c.high for c in foto.candles] + [c.low for c in foto.candles]
        for valor in (foto.stop, foto.take, foto.decision_level):
            if valor is not None:
                precos.append(valor)
        if foto.entry_zone is not None:
            precos.extend([foto.entry_zone.min, foto.entry_zone.max])
        if foto.heatmap:
            precos.extend([b.price for b in foto.heatmap])

        baixo, alto = min(precos), max(precos)
        folga = (alto - baixo) * 0.06 or max(foto.tick_size, 1e-8)
        return _Scale(low=baixo - folga, high=alto + folga)

    # --- camadas ----------------------------------------------------------

    def _heatmap(self, foto: FotoAnalise, escala: _Scale) -> str:
        if len(foto.heatmap) < 2:
            return ""

        comprando = foto.bias == "LONG"
        faixas = sorted(foto.heatmap, key=lambda b: b.price)
        altura = abs(escala.y(faixas[0].price) - escala.y(faixas[1].price))

        saida: list[str] = ['<g class="heat">']
        for faixa in faixas:
            score = faixa.buy_score if comprando else faixa.sell_score
            cor, opacidade = _heat_color(score)
            y = escala.y(faixa.price) - altura / 2
            saida.append(
                f'<rect x="{PADDING_LEFT}" y="{y:.1f}" '
                f'width="{WIDTH - PADDING_LEFT - PADDING_RIGHT}" '
                f'height="{altura:.1f}" fill="{cor}" opacity="{opacidade}"/>'
            )
        saida.append("</g>")
        return "".join(saida)

    def _zones(self, foto: FotoAnalise, escala: _Scale) -> str:
        zona = foto.entry_zone
        if zona is None:
            return ""

        comprando = foto.bias == "LONG"
        cor = "#22c55e" if comprando else "#ef4444"
        rotulo = "BUY ZONE" if comprando else "SELL ZONE"
        topo = escala.y(zona.max)
        base = escala.y(zona.min)
        largura = WIDTH - PADDING_LEFT - PADDING_RIGHT

        faixa = (
            f'<rect x="{PADDING_LEFT}" y="{topo:.1f}" width="{largura}" '
            f'height="{max(abs(base - topo), 3):.1f}" fill="{cor}" opacity="0.22" '
            f'stroke="{cor}" stroke-opacity="0.7" stroke-width="1.5"/>'
            + _badge(
                PADDING_LEFT + 6,
                topo - 4,
                f"{rotulo} {_fmt(zona.min, foto.tick_size)}–{_fmt(zona.max, foto.tick_size)}",
                cor,
                tamanho=12,
            )
        )
        return faixa + self._risk_area(foto, escala) + self._star(
            foto, escala, zona.sweet_spot, cor
        )

    def _risk_area(self, foto: FotoAnalise, escala: _Scale) -> str:
        """A regiao ALEM do stop, pintada de vermelho.

        O stop ja era uma linha, e uma linha nao comunica "dali para baixo
        o cenario acabou". Area comunica: o operador ve de relance onde
        NAO estar, que era metade do pedido — a outra metade e onde entrar.

        Vai do stop ate a borda do enquadramento, do lado da invalidacao.
        """
        if foto.stop is None:
            return ""

        comprando = foto.bias == "LONG"
        y_stop = escala.y(foto.stop)
        # Numa compra a invalidacao e ABAIXO: o retangulo desce ate o fim.
        y0 = y_stop if comprando else PADDING_TOP
        altura = (HEIGHT - PADDING_BOTTOM - y_stop) if comprando else (y_stop - PADDING_TOP)
        if altura <= 0:
            return ""

        return (
            f'<rect x="{PADDING_LEFT}" y="{y0:.1f}" '
            f'width="{WIDTH - PADDING_LEFT - PADDING_RIGHT}" height="{altura:.1f}" '
            'fill="#ef4444" opacity="0.14"/>'
            + _badge(
                PADDING_LEFT + 6,
                y_stop + (16 if comprando else -4),
                "ZONA DE RISCO",
                "#ef4444",
                tamanho=11,
            )
        )

    def _star(self, foto: FotoAnalise, escala: _Scale, price: float, cor: str) -> str:
        """O ponto mais interessante da zona, marcado para ser achado de
        relance — e nao lido depois de procurar."""
        y = escala.y(price)
        x = WIDTH - PADDING_RIGHT
        texto = f"★ MELHOR ENTRADA {_fmt(price, foto.tick_size)}"
        return (
            f'<line x1="{PADDING_LEFT}" y1="{y:.1f}" x2="{x}" y2="{y:.1f}" '
            f'stroke="{cor}" stroke-width="2.5"/>'
            + _badge(PADDING_LEFT + 6, y - 9, texto, cor, tamanho=12)
        )

    def _candles(self, foto: FotoAnalise, escala: _Scale) -> str:
        total = len(foto.candles)
        util = WIDTH - PADDING_LEFT - PADDING_RIGHT
        passo = util / total
        corpo = max(passo * 0.62, 1.0)

        saida: list[str] = ['<g class="candles">']
        for indice, vela in enumerate(foto.candles):
            centro = PADDING_LEFT + passo * (indice + 0.5)
            alta = vela.close >= vela.open
            cor = "#26a69a" if alta else "#ef5350"

            saida.append(
                f'<line x1="{centro:.1f}" y1="{escala.y(vela.high):.1f}" '
                f'x2="{centro:.1f}" y2="{escala.y(vela.low):.1f}" '
                f'stroke="{cor}" stroke-width="1"/>'
            )
            topo = escala.y(max(vela.open, vela.close))
            base = escala.y(min(vela.open, vela.close))
            saida.append(
                f'<rect x="{centro - corpo / 2:.1f}" y="{topo:.1f}" '
                f'width="{corpo:.1f}" height="{max(base - topo, 1):.1f}" fill="{cor}"/>'
            )
        saida.append("</g>")
        return "".join(saida)

    def _levels(self, foto: FotoAnalise, escala: _Scale) -> str:
        estilos = {
            "TAKE": ("#22c55e", f"TAKE +{foto.take_ticks} TICKS"),
            "STOP": ("#ef4444", "STOP / INVALIDACAO"),
            "DECISION": ("#a78bfa", "⚡ DECISION LEVEL"),
            "RESISTANCE": ("#f97316", "Resistencia"),
            "SUPPORT": ("#38bdf8", "Suporte"),
        }
        # Take e stop sao os dois niveis que decidem "vale a pena?" — eles
        # ganham traco mais grosso que suporte/resistencia de contexto.
        espessura = {"TAKE": 2.5, "STOP": 2.5, "DECISION": 2.0}

        saida: list[str] = []
        for nivel in foto.levels:
            if nivel.kind not in estilos:
                continue
            cor, texto = estilos[nivel.kind]
            y = escala.y(nivel.price)
            saida.append(
                f'<line x1="{PADDING_LEFT}" y1="{y:.1f}" '
                f'x2="{WIDTH - PADDING_RIGHT}" y2="{y:.1f}" stroke="{cor}" '
                f'stroke-width="{espessura.get(nivel.kind, 1.4)}" '
                f'stroke-dasharray="8 5"/>'
                # Preco na regua da direita, rotulo dentro do grafico: o
                # olho encontra o "o que e" sem sair do desenho.
                f'<text x="{WIDTH - PADDING_RIGHT + 6}" y="{y + 4:.1f}" fill="{cor}" '
                f'font-size="12" font-weight="600">'
                f'{_fmt(nivel.price, foto.tick_size)}</text>'
                + _badge(
                    WIDTH - PADDING_RIGHT - 8, y - 8, escape(texto), cor,
                    tamanho=10, ancora="end",
                )
            )
        return "".join(saida)

    def _current_price(self, foto: FotoAnalise, escala: _Scale) -> str:
        """ONDE ESTAMOS — a primeira pergunta que a tela precisa responder.

        Sem esta linha, o operador tem que deduzir o preco atual pela ponta
        dos candles, e comparar mentalmente com a zona. Com ela, "entrar
        agora ou esperar" vira distancia visivel entre duas linhas.
        """
        y = escala.y(foto.current_price)
        sufixo = " (tick)" if foto.price_source == "TICK" else ""
        return (
            f'<line x1="{PADDING_LEFT}" y1="{y:.1f}" x2="{WIDTH - PADDING_RIGHT}" '
            f'y2="{y:.1f}" stroke="#e2e8f0" stroke-width="1.5" '
            'stroke-dasharray="2 3" opacity="0.95"/>'
            + _badge(
                WIDTH - PADDING_RIGHT - 8,
                y + 4,
                f"AGORA {_fmt(foto.current_price, foto.tick_size)}{sufixo}",
                "#e2e8f0",
                tamanho=11,
                ancora="end",
                texto_escuro=True,
            )
        )

    def _freshness(self, foto: FotoAnalise) -> str:
        """Selo de dados velhos, atravessado no grafico.

        Discreto nao serve: uma foto bonita sobre precos de ontem PARECE
        atual, e e assim que alguem opera em cima do passado sem perceber.
        """
        if not foto.is_stale:
            return ""
        idade = (
            f"{foto.data_age_minutes:.0f} min"
            if foto.data_age_minutes is not None
            else "desconhecida"
        )
        return (
            f'<rect x="{PADDING_LEFT}" y="{HEIGHT / 2 - 26:.0f}" '
            f'width="{WIDTH - PADDING_LEFT - PADDING_RIGHT}" height="52" '
            'fill="#7f1d1d" opacity="0.82"/>'
            f'<text x="{WIDTH / 2:.0f}" y="{HEIGHT / 2 - 4:.0f}" fill="#fff" '
            'font-size="17" font-weight="800" text-anchor="middle">'
            "DADOS DESATUALIZADOS</text>"
            f'<text x="{WIDTH / 2:.0f}" y="{HEIGHT / 2 + 16:.0f}" fill="#fecaca" '
            'font-size="12" text-anchor="middle">'
            f"ultima candle ha {escape(idade)} — o coletor MT5 parece parado</text>"
        )

    def _projection(self, foto: FotoAnalise, escala: _Scale) -> str:
        """A seta do cenario esperado — tracejada porque e projecao.

        Solida, ela leria como afirmacao sobre o futuro. O rotulo repete a
        palavra "projecao" pelo mesmo motivo.
        """
        zona = foto.entry_zone
        if zona is None or foto.take is None:
            return ""

        x0 = WIDTH - PADDING_RIGHT - 210
        x1 = WIDTH - PADDING_RIGHT - 40
        y_atual = escala.y(foto.current_price)
        y_zona = escala.y(zona.sweet_spot)
        y_take = escala.y(foto.take)
        meio = (x0 + x1) / 2

        return (
            '<defs><marker id="seta" markerWidth="9" markerHeight="9" refX="6" '
            'refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#eab308"/>'
            "</marker></defs>"
            f'<path d="M {x0} {y_atual:.1f} L {meio:.1f} {y_zona:.1f} '
            f'L {x1} {y_take:.1f}" fill="none" stroke="#eab308" stroke-width="2" '
            'stroke-dasharray="7 5" marker-end="url(#seta)" opacity="0.9"/>'
            f'<text x="{x0}" y="{HEIGHT - 8}" fill="#eab308" font-size="10" '
            'opacity="0.85">projecao do cenario, nao previsao</text>'
        )

    def _header(self, foto: FotoAnalise) -> str:
        comprando = foto.bias == "LONG"
        cor = "#22c55e" if comprando else "#ef4444"
        rotulo = "COMPRA" if comprando else "VENDA"

        estado = STATUS_LABELS.get(foto.status, foto.status)

        return (
            f'<text x="{PADDING_LEFT}" y="20" fill="{cor}" font-size="17" '
            f'font-weight="800">{rotulo}</text>'
            f'<text x="{PADDING_LEFT + 92}" y="20" fill="#cbd5e1" font-size="13">'
            f"Forca do setup: {foto.score:.0f}/100</text>"
            f'<text x="{WIDTH - PADDING_RIGHT}" y="20" fill="#e2e8f0" font-size="13" '
            f'font-weight="700" text-anchor="end">{escape(estado)}</text>'
        )


def _badge(
    x: float,
    y: float,
    texto: str,
    cor: str,
    *,
    tamanho: int = 11,
    ancora: str = "start",
    texto_escuro: bool = False,
) -> str:
    """Rotulo com fundo solido.

    Texto colorido direto sobre candles e mapa de calor fica ilegivel
    exatamente onde ele mais importa — em cima da zona de entrada, que e a
    regiao mais pintada do grafico. O fundo custa alguns pixels e devolve a
    leitura em um relance, que era o requisito da tela.
    """
    largura = len(texto) * tamanho * 0.58 + 12
    esquerda = x - largura if ancora == "end" else x
    return (
        f'<rect x="{esquerda:.1f}" y="{y - tamanho:.1f}" width="{largura:.1f}" '
        f'height="{tamanho + 7}" rx="3" fill="{cor}" opacity="0.92"/>'
        f'<text x="{esquerda + 6:.1f}" y="{y - 1:.1f}" '
        f'fill="{"#0f172a" if texto_escuro else "#0b1220"}" font-size="{tamanho}" '
        f'font-weight="700">{texto}</text>'
    )


def _heat_color(score: float) -> tuple[str, float]:
    """Escala verde-forte → vermelho-forte, como especificado.

    Opacidade cresce com a intensidade para que as regioes neutras nao
    competam visualmente com as decisivas — o olho deve cair primeiro no
    verde forte.
    """
    if score >= 80:
        return "#22c55e", 0.42
    if score >= 65:
        return "#4ade80", 0.26
    if score >= 45:
        return "#eab308", 0.16
    if score >= 30:
        return "#f97316", 0.20
    return "#ef4444", 0.30


def _fmt(price: float, tick_size: float) -> str:
    """Casas decimais vindas do tick real, nunca fixas.

    Duas casas transformariam 1.10345 em 1.10 — e o operador de forex
    perderia exatamente a informacao que importa.
    """
    if tick_size <= 0:
        return f"{price:.5f}".rstrip("0").rstrip(".")
    texto = f"{tick_size:.10f}".rstrip("0")
    casas = len(texto.split(".")[1]) if "." in texto else 0
    return f"{price:.{min(casas, 8)}f}"
