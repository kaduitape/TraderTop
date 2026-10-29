"""Painel do Hedge AI: as seis areas, e as acoes que mudam estado.

Router proprio em vez de crescer `dashboard.py` (que ja passa de 2400
linhas): as rotas daqui compartilham o mesmo contexto de setup e usuario,
e mistura-las com o resto tornaria as duas partes mais dificeis de ler.

## O segredo aparece uma vez

A chave em claro existe na resposta que a cria e nunca mais. Ela nao vai
para `system_settings`, nao volta em listagem e nao entra em log. Perdeu,
gera outra — e isso e proposital: se o sistema conseguisse mostrar de
novo, um vazamento do banco tambem conseguiria.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.dependencies.api_token import (
    SCOPE_ANALYSIS,
    SCOPE_SETUP_READ,
    SCOPE_TELEMETRY,
)
from app.api.dependencies.auth import get_current_user_for_web
from app.api.templates_engine import templates
from app.core.config import get_settings
from app.core.crypto import MASK
from app.database.models.user import User
from app.database.repositories.api_token_repository import ApiTokenRepository
from app.database.repositories.audit_log_repository import AuditLogRepository
from app.database.repositories.hedge_repository import HedgeRepository
from app.database.repositories.symbol_repository import SymbolRepository
from app.database.session import get_db
from app.hedge.engine import Mode, Objective
from app.hedge.forecast.registry import available_versions, default_engine
from app.hedge.sessions import evaluate_freshness
from app.hedge.units import LevelUnit, level_to_price
from app.jev.api_settings import (
    clear_api_key,
    load_jev_settings,
    save_api_key,
    set_enabled,
)
from app.jev.client import JevClient, JevUnavailable, noul_question
from app.jev.factory import reset_context_assessor
from app.jev.hedge_context import BUDGET_SETTING as JEV_BUDGET_SETTING
from app.news.budget import read_usage

router = APIRouter(tags=["hedge-dashboard"])

HEDGE_SCOPE_SET = f"{SCOPE_TELEMETRY},{SCOPE_SETUP_READ},{SCOPE_ANALYSIS}"


def _dec(valor: str, *, default: str = "0") -> Decimal:
    try:
        return Decimal((valor or "").strip().replace(",", ".") or default)
    except InvalidOperation:
        return Decimal(default)


def _voltar(mensagem: str = "", erro: str = "", ancora: str = "") -> RedirectResponse:
    partes = []
    if mensagem:
        partes.append(f"saved={quote(mensagem)}")
    if erro:
        partes.append(f"error={quote(erro)}")
    query = ("?" + "&".join(partes)) if partes else ""
    return RedirectResponse(url=f"/dashboard/hedge{query}{ancora}", status_code=303)


@router.get("/dashboard/hedge", response_class=HTMLResponse)
def dashboard_hedge(
    request: Request,
    saved: str | None = None,
    error: str | None = None,
    new_key: str | None = None,
    setup: int | None = None,
    edit: int | None = None,
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """As seis areas numa pagina so.

    `new_key` chega pela URL de um redirect imediatamente apos a criacao e
    some na proxima navegacao. E a unica vez que o segredo aparece.

    `setup` escolhe qual setup o HISTORICO mostra; `edit` escolhe qual o
    FORMULARIO altera. Eram a mesma coisa, e o resultado era que o
    formulario vinha sempre preenchido com o setup selecionado e preso a
    ele: depois do primeiro, nao havia como criar um segundo — todo
    "Salvar" virava uma versao nova do que estava na tela.
    """
    repo = HedgeRepository(db)
    settings = get_settings()
    agora = datetime.now(UTC)

    setups = repo.list_setups(user_id=user.id)

    # Qual setup o historico mostra.
    selecionado = None
    if setup is not None:
        selecionado = repo.get_setup(setup, user_id=user.id)
    if selecionado is None and setups:
        selecionado = setups[0]

    # Qual setup o formulario altera. `None` significa CRIAR, que e o
    # padrao: so se entra em edicao clicando "editar" numa linha.
    editando = repo.get_setup(edit, user_id=user.id) if edit is not None else None
    if editando is not None and editando.closed_at is not None:
        editando = None   # encerrado nao recebe alteracao; o form volta a criar
    versao_editada = repo.current_version(editando) if editando else None

    linhas = []
    for item in setups:
        versao = repo.current_version(item)
        ultima = repo.latest_analysis(item.id)
        linhas.append({"setup": item, "version": versao, "latest": ultima})

    sessoes = []
    for sessao in repo.list_sessions(setup_ids=[s.id for s in setups]):
        frescor = evaluate_freshness(
            last_seen_at=sessao.last_seen_at, now=agora, max_age_seconds=30
        )
        sessoes.append({"session": sessao, "freshness": frescor})

    historico = repo.history(setup_id=selecionado.id, limit=50) if selecionado else []
    versoes = repo.list_versions(selecionado.id) if selecionado else []

    # So as chaves DESTE usuario: a lista de chaves de outro operador nao
    # tem por que aparecer aqui.
    chaves = [
        chave
        for chave in ApiTokenRepository(db).list_all()
        if chave.created_by_user_id == user.id
    ]

    motor = default_engine()

    return templates.TemplateResponse(
        request,
        "dashboard/hedge.html",
        {
            "user": user,
            "saved": saved,
            "error": error,
            "new_key": new_key,
            "rows": linhas,
            "selected": selecionado,
            "selected_version": repo.current_version(selecionado) if selecionado else None,
            "editing": editando,
            "editing_version": versao_editada,
            "versions": versoes,
            "history": historico,
            "sessions": sessoes,
            "keys": chaves,
            "symbols": SymbolRepository(db).list_active(),
            "objectives": list(Objective),
            "modes": list(Mode),
            "level_units": list(LevelUnit),
            "engine_version": motor.version,
            "engine_kind": str(motor.kind),
            "engines": available_versions(),
            "jev": load_jev_settings(db, settings),
            "jev_model": settings.jev_model,
            "jev_timeout": settings.jev_timeout_seconds,
            "jev_cache_ttl": settings.jev_cache_ttl_seconds,
            "jev_usage": read_usage(
                db, limit=settings.jev_daily_call_budget, key=JEV_BUDGET_SETTING
            ),
            "api_base": str(request.base_url).rstrip("/"),
            "now": agora,
        },
    )


@router.post("/dashboard/hedge/setup")
def hedge_setup_save(
    name: str = Form(...),
    desk_symbol: str = Form(...),
    desk_direction: str = Form("LONG"),
    desk_entry: str = Form(...),
    desk_take: str = Form(...),
    desk_stop: str = Form(...),
    desk_contracts: str = Form("1"),
    level_unit: str = Form("PRICE"),
    desk_tick_size: str = Form("0.25"),
    desk_tick_value: str = Form("0.50"),
    desk_point: str = Form("0.01"),
    desk_commission: str = Form("0"),
    desk_slippage: str = Form("0"),
    broker_symbol: str = Form(...),
    coverage_ratio: str = Form("1"),
    price_ratio: str = Form(""),
    broker_fixed_lots: str = Form(""),
    broker_commission: str = Form("0"),
    broker_slippage: str = Form("0"),
    account_currency: str = Form("USD"),
    horizon_minutes: str = Form("60"),
    display_timezone: str = Form("UTC"),
    data_max_age_seconds: str = Form("30"),
    objective: str = Form("FAVOR_DESK_TAKE"),
    mode: str = Form("SIMULATION"),
    mt5_position_ticket: str = Form(""),
    desk_reference: str = Form(""),
    setup_id: str = Form(""),
    change_note: str = Form(""),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Cria ou versiona um setup.

    Os niveis podem chegar em qualquer unidade e sao convertidos para
    PRECO aqui, UMA vez. A unidade original fica gravada: so o preco final
    nao permite reconstruir o que o operador quis dizer.
    """
    try:
        unidade = LevelUnit(level_unit.upper())
    except ValueError:
        return _voltar(erro=f"unidade invalida: {level_unit}")

    comprado = desk_direction.upper() in {"LONG", "COMPRA", "BUY"}
    entrada = _dec(desk_entry)
    tick = _dec(desk_tick_size, default="0.25")
    ponto = _dec(desk_point, default="0.01")
    valor_tick = _dec(desk_tick_value, default="0.5")
    contratos = _dec(desk_contracts, default="1")

    if entrada <= 0 or tick <= 0 or ponto <= 0:
        return _voltar(erro="entrada, tick size e point precisam ser maiores que zero.")

    try:
        take = level_to_price(
            value=_dec(desk_take),
            unit=unidade,
            entry=entrada,
            is_profit_side=True,
            is_long=comprado,
            tick_size=tick,
            point=ponto,
            tick_value=valor_tick,
            volume=contratos,
        )
        stop = level_to_price(
            value=_dec(desk_stop),
            unit=unidade,
            entry=entrada,
            is_profit_side=False,
            is_long=comprado,
            tick_size=tick,
            point=ponto,
            tick_value=valor_tick,
            volume=contratos,
        )
    except Exception as exc:  # ConversionError e afins
        return _voltar(erro=f"nao foi possivel converter os niveis: {exc}")

    # Em TICKS, POINTS e MONEY o lado e deduzido da direcao e sai sempre
    # coerente. Em PRICE o operador digita dois numeros livres, e nada
    # impede trocar take com stop. Recusar AQUI, antes de gravar: o motor
    # tambem recusa, mas ai o setup invalido ja estaria no banco, visivel
    # na lista e pronto para ser anexado a um grafico.
    if not ((take > entrada > stop) if comprado else (take < entrada < stop)):
        esperado = (
            "take acima e stop abaixo da entrada"
            if comprado
            else "take abaixo e stop acima da entrada"
        )
        return _voltar(
            erro=(
                f"Niveis invertidos para uma {'compra' if comprado else 'venda'}: "
                f"esperado {esperado}. Entrada {entrada}, take {take}, stop {stop}."
            )
        )

    horizonte = max(60, int(_dec(horizon_minutes, default="60") * 60))

    params = {
        "desk_symbol": desk_symbol.strip().upper()[:40],
        "desk_is_long": comprado,
        "desk_entry": entrada,
        "desk_take": take,
        "desk_stop": stop,
        "desk_contracts": contratos,
        "level_unit": unidade.value,
        "desk_tick_size": tick,
        "desk_tick_value": valor_tick,
        "desk_point": ponto,
        "desk_commission": _dec(desk_commission),
        "desk_slippage": _dec(desk_slippage),
        "broker_symbol": broker_symbol.strip().upper()[:40],
        "coverage_ratio": _dec(coverage_ratio, default="1"),
        "price_ratio": _dec(price_ratio) if price_ratio.strip() else None,
        "broker_fixed_lots": (
            _dec(broker_fixed_lots) if broker_fixed_lots.strip() else None
        ),
        "broker_commission": _dec(broker_commission),
        "broker_slippage": _dec(broker_slippage),
        "account_currency": account_currency.strip().upper()[:8] or "USD",
        "horizon_seconds": horizonte,
        "deadline_utc": datetime.now(UTC) + timedelta(seconds=horizonte),
        "display_timezone": display_timezone.strip()[:64] or "UTC",
        "data_max_age_seconds": max(5, int(_dec(data_max_age_seconds, default="30"))),
        "mt5_position_ticket": (
            int(_dec(mt5_position_ticket)) if mt5_position_ticket.strip() else None
        ),
        "desk_reference": desk_reference.strip()[:120] or None,
    }

    repo = HedgeRepository(db)

    if setup_id.strip():
        existente = repo.get_setup(int(setup_id), user_id=user.id)
        if existente is None:
            return _voltar(erro="setup nao encontrado.")
        if existente.closed_at is not None:
            return _voltar(
                erro="setup encerrado nao recebe alteracao. Crie um novo.",
            )
        versao = repo.update_setup(
            existente,
            params=params,
            note=change_note.strip() or "alteracao pelo painel",
            user_id=user.id,
            mode=mode,
            objective=objective,
            name=name,
        )
        AuditLogRepository(db).record(
            action="hedge_setup_version",
            entity="hedge_setup",
            detail=f"setup {existente.id} -> versao {versao.version} por {user.username}",
            user_id=user.id,
        )
        db.commit()
        return _voltar(f"Setup atualizado (versao {versao.version}).", ancora="#setups")

    novo, _versao = repo.create_setup(
        user_id=user.id,
        name=name,
        mode=mode,
        objective=objective,
        params=params,
    )
    AuditLogRepository(db).record(
        action="hedge_setup_create",
        entity="hedge_setup",
        detail=f"setup {novo.id} '{novo.name}' por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar(f"Setup criado (id {novo.id}).", ancora="#setups")


@router.post("/dashboard/hedge/setup/close")
def hedge_setup_close(
    setup_id: int = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    repo = HedgeRepository(db)
    alvo = repo.get_setup(setup_id, user_id=user.id)
    if alvo is None:
        return _voltar(erro="setup nao encontrado.")

    repo.close_setup(alvo, reason="MANUAL")
    AuditLogRepository(db).record(
        action="hedge_setup_close",
        entity="hedge_setup",
        detail=f"setup {alvo.id} encerrado a mao por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar("Setup encerrado.", ancora="#setups")


@router.post("/dashboard/hedge/key")
def hedge_key_create(
    name: str = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Gera uma chave do conector.

    Os escopos sao FIXOS: telemetria, leitura de setup e analise. Nao ha
    campo para escolher porque nao ha caso de uso para uma chave de
    conector com mais do que isso, e um seletor so criaria a chance de
    alguem marcar algo a mais sem precisar.
    """
    repo = ApiTokenRepository(db)
    token, segredo = repo.create(name=name, user_id=user.id)
    token.scopes = HEDGE_SCOPE_SET

    AuditLogRepository(db).record(
        action="hedge_key_create",
        entity="api_token",
        # O prefixo identifica sem revelar. O segredo nao entra no log.
        detail=f"chave {token.prefix}... '{token.name}' criada por {user.username}",
        user_id=user.id,
    )
    db.commit()

    # O segredo viaja uma vez, na URL do redirect, e some na proxima
    # navegacao. Guarda-lo em sessao ou banco criaria um segundo lugar de
    # onde ele poderia vazar.
    return RedirectResponse(
        url=f"/dashboard/hedge?new_key={quote(segredo)}#chaves", status_code=303
    )


@router.post("/dashboard/hedge/key/revoke")
def hedge_key_revoke(
    token_id: int = Form(...),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    repo = ApiTokenRepository(db)
    alvo = next(
        (
            chave
            for chave in repo.list_all()
            if chave.id == token_id and chave.created_by_user_id == user.id
        ),
        None,
    )
    if alvo is None:
        return _voltar(erro="chave nao encontrada.")

    repo.revoke(alvo.id)
    AuditLogRepository(db).record(
        action="hedge_key_revoke",
        entity="api_token",
        detail=f"chave {alvo.prefix}... revogada por {user.username}",
        user_id=user.id,
    )
    db.commit()
    return _voltar("Chave revogada. O conector para na proxima consulta.", ancora="#chaves")


# --- download dos fontes MQL5 ----------------------------------------------
#
# Servidos pelo painel, e nao pelo GitHub, porque quem instala esta com o
# painel aberto na frente — e porque a versao entregue aqui e sempre a que
# conversa com ESTE servidor. Baixar de outro lugar abre espaco para um
# conector de uma versao e uma API de outra, e a divergencia aparece como
# erro 409 de schema no meio do pregao.

MQL5_FILES = {
    "connector": ("TraderTopHedgeConnector.mq5", "Experts"),
    "indicator": ("TraderTopHedgeIndicator.mq5", "Indicators"),
    "channel": ("Include/TraderTop/HedgeChannel.mqh", "Include/TraderTop"),
    "json": ("Include/TraderTop/HedgeJson.mqh", "Include/TraderTop"),
}


def _mql5_root() -> Path:
    """Raiz de `scripts/mql5`, resolvida a partir deste arquivo.

    Nao usa o diretorio de trabalho: no container o processo sobe em `/app`
    e em desenvolvimento na raiz do repositorio, e um caminho relativo daria
    404 em um dos dois.
    """
    return Path(__file__).resolve().parents[3] / "scripts" / "mql5"


def _ausente(nome: str) -> HTTPException:
    # Estado real: a imagem foi construida sem `scripts/mql5`. Dizer isso e
    # melhor que um 500 — o proximo passo e reconstruir, nao depurar.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"{nome} nao esta nesta instalacao. A imagem foi construida sem "
            "scripts/mql5 — rode `docker compose build app`."
        ),
    )


@router.get("/dashboard/hedge/download/{qual}")
def hedge_download_file(
    qual: str,
    _user: User = Depends(get_current_user_for_web),
) -> FileResponse:
    """Um arquivo por vez, para quem prefere copiar na mao."""
    entrada = MQL5_FILES.get(qual)
    if entrada is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"arquivo desconhecido: {qual}"
        )

    relativo, _pasta = entrada
    caminho = _mql5_root() / relativo
    if not caminho.is_file():
        raise _ausente(relativo)

    nome = caminho.name
    return FileResponse(
        caminho,
        media_type="text/plain; charset=utf-8",
        filename=nome,
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )


@router.get("/dashboard/hedge/download")
def hedge_download_zip(
    _user: User = Depends(get_current_user_for_web),
) -> Response:
    """Os quatro arquivos num ZIP, ja com a estrutura de pastas do MQL5.

    Existe porque baixar quatro arquivos soltos e depois adivinhar em qual
    pasta cada um vai e o passo onde a instalacao erra: `.mqh` em
    `Experts/` nao e encontrado pelo `#include`, e o sintoma e um erro de
    compilacao que nao diz onde o arquivo deveria estar.

    Montado em memoria: sao quatro arquivos de texto, e gravar em disco
    criaria um arquivo temporario por download para limpar depois.
    """
    raiz = _mql5_root()
    faltando = [rel for rel, _ in MQL5_FILES.values() if not (raiz / rel).is_file()]
    if faltando:
        raise _ausente(", ".join(faltando))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for relativo, pasta in MQL5_FILES.values():
            caminho = raiz / relativo
            zf.writestr(f"MQL5/{pasta}/{caminho.name}", caminho.read_text(encoding="utf-8"))
        zf.writestr("LEIA-ME.txt", _LEIAME)

    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="TraderTopHedge-MQL5.zip"'
        },
    )


_LEIAME = """TraderTop Hedge AI - arquivos para o MetaTrader 5
=================================================

ONDE COLOCAR
------------
Abra o MetaTrader, Arquivo > Abrir pasta de dados. Isso abre uma pasta que
contem a pasta MQL5. Copie o conteudo do ZIP PARA DENTRO dela, mantendo a
estrutura:

    MQL5/Experts/TraderTopHedgeConnector.mq5
    MQL5/Indicators/TraderTopHedgeIndicator.mq5
    MQL5/Include/TraderTop/HedgeChannel.mqh
    MQL5/Include/TraderTop/HedgeJson.mqh

A estrutura importa: um .mqh fora de Include/TraderTop/ nao e encontrado
pelo #include, e o erro de compilacao nao diz onde ele deveria estar.

COMPILAR
--------
Abra os dois .mq5 no MetaEditor e pressione F7 em cada um. Os .mqh nao se
compilam sozinhos - eles sao incluidos pelos outros dois.

ANTES DE ANEXAR, TRES COISAS QUE FALHAM EM SILENCIO
---------------------------------------------------
1. Libere a URL do painel em Ferramentas > Opcoes > Expert Advisors >
   "Permitir WebRequest para as URLs listadas". Sem isso o WebRequest
   devolve -1 com erro 4014 e nada aparece. E a causa numero um de
   "nao funciona".
2. Ligue o AutoTrading. O conector NAO opera - mas com o botao desligado
   o terminal nao executa EA nenhum.
3. O Strategy Tester nao executa WebRequest. Testar o conector ali devolve
   sempre -1. Use conta demo real.

ANEXAR
------
- Conector (Experts): informe ApiKey e SetupId. Pode ficar num grafico
  separado, se o principal ja tem outro EA.
- Indicador (Indicators): mesmo SetupId, no grafico que voce quer ver.
- Se estiverem em graficos diferentes, informe o MESMO ChartIdOverride
  nos dois - senao cada um usa o id do proprio grafico e eles falam em
  espacos diferentes do canal local.

A CHAVE
-------
Cole-a no campo ApiKey do EA, no proprio terminal. NAO a salve em arquivos
.set, perfis ou templates que voce compartilhe: eles viajam em claro.

ESTE CONECTOR NAO ENVIA ORDENS. Ele le cotacao, consulta o painel e
publica o resultado para o indicador desenhar.
"""


# --- chave do Jev -----------------------------------------------------------
#
# O desenho original deixava esta chave so no `.env`. O campo existe por
# pedido do dono do repositorio, e a razao e boa: mexer no `.env` exige SSH
# na VPS, e quem paga a conta da API nem sempre tem.
#
# Ela e a UNICA credencial deste painel que e cifrada no banco
# (`app/core/crypto.py`), e nao guardada em claro como a da AIsa. O valor
# em claro nunca volta: a tela mostra mascara, a auditoria guarda so os
# quatro ultimos caracteres.


@router.post("/dashboard/hedge/jev")
def hedge_jev_save(
    jev_enabled: str = Form(""),
    jev_api_key: str = Form(""),
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Salva o liga/desliga e, se houver, uma chave nova.

    Campo da chave VAZIO significa "mantem a atual", nunca "apaga". Essa
    e a regra que impede salvar o liga/desliga de apagar a credencial —
    e e a mesma que vale para a senha do MT5 neste projeto. Apagar exige
    a acao explicita de remover.
    """
    ligado = bool(jev_enabled)
    set_enabled(db, ligado)

    dica = ""
    bruta = jev_api_key.strip()
    if bruta and bruta != MASK:
        try:
            dica = save_api_key(db, bruta)
        except ValueError as exc:
            return _voltar(erro=str(exc), ancora="#jev")

    AuditLogRepository(db).record(
        action="hedge_jev_config",
        entity="jev",
        # Nunca a chave. Os quatro ultimos identificam qual foi cadastrada
        # quando ha mais de uma conta, sem revelar o resto.
        detail=(
            f"Jev {'ligado' if ligado else 'desligado'}"
            + (f", chave nova (final {dica})" if dica else "")
            + f" por {user.username}"
        ),
        user_id=user.id,
    )
    db.commit()
    reset_context_assessor()

    mensagem = "Configuracao do Jev salva."
    if dica:
        mensagem += f" Chave cadastrada (final {dica})."
    return _voltar(mensagem, ancora="#jev")


@router.post("/dashboard/hedge/jev/remove")
def hedge_jev_remove(
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove a chave gravada pelo painel.

    Volta a valer o `.env`, se houver — que e o que "removi o que eu
    tinha posto aqui" significa.
    """
    clear_api_key(db)
    AuditLogRepository(db).record(
        action="hedge_jev_key_remove",
        entity="jev",
        detail=f"chave do Jev removida por {user.username}",
        user_id=user.id,
    )
    db.commit()
    reset_context_assessor()
    return _voltar("Chave do Jev removida do painel.", ancora="#jev")


@router.post("/dashboard/hedge/jev/test")
def hedge_jev_test(
    user: User = Depends(get_current_user_for_web),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Uma chamada real, para o operador saber que a chave serve.

    Vale a chamada paga: a alternativa e descobrir que a chave esta
    errada quando o primeiro evento economico aparecer, horas depois, com
    o veredito saindo indisponivel sem explicacao.

    Nao consome o orcamento diario, de proposito — e um teste de
    configuracao, nao uma classificacao. Por isso o cliente e montado
    aqui em vez de passar pelo `HedgeContextAssessor`.
    """
    del user
    vigente = load_jev_settings(db, get_settings())
    if not vigente.configured:
        return _voltar(erro="Cadastre a chave antes de testar.", ancora="#jev")

    settings = get_settings()
    try:
        cliente = JevClient(
            api_key=vigente.api_key or "",
            base_url=settings.jev_api_base_url,
            model=settings.jev_model,
            timeout_seconds=settings.jev_timeout_seconds,
        )
        resultado = cliente.system_one(
            state={"probe": "verificacao de configuracao do painel"},
            questions={
                "ok": noul_question("Esta mensagem e uma verificacao de conexao.")
            },
        )
    except JevUnavailable as exc:
        # `exc.detail` ja e a frase acionavel do cliente, e ela nunca
        # carrega corpo de resposta nem a chave.
        return _voltar(erro=f"Jev recusou: {exc.detail or exc.reason.value}", ancora="#jev")
    except Exception:  # noqa: BLE001
        return _voltar(erro="Falha inesperada ao falar com o Jev.", ancora="#jev")

    return _voltar(
        f"Conexao com o Jev OK (modelo {resultado.model or 'nao informado'}).",
        ancora="#jev",
    )
