"""Autenticacao por chave de API, para clientes que nao sao navegador.

Header: `X-API-Key: tt_...`

Separado de `get_current_user` de proposito. Sao duas populacoes com
necessidades opostas: sessao de gente deve expirar rapido; credencial de
maquina deve durar e ser revogavel uma a uma. Misturar as duas obrigaria a
escolher entre um JWT longo demais para o navegador ou um curto demais
para o indicador.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.database.models.api_token import ApiToken
from app.database.repositories.api_token_repository import ApiTokenRepository
from app.database.session import get_db

_INVALID = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    # Mensagem unica para ausente, malformada e revogada: distinguir os
    # casos diria a quem esta tentando adivinhar se ele acertou o formato.
    detail="Chave de API ausente ou invalida. Envie o header X-API-Key.",
    headers={"WWW-Authenticate": "X-API-Key"},
)


def get_api_token(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> ApiToken:
    """Valida a chave e registra o uso.

    O `commit` do uso acontece aqui, antes de a rota rodar: se a analise
    demorar ou falhar, o registro de "esta chave esta viva" ja existe — e
    e justamente num erro que saber se o indicador chegou a chamar importa.
    """
    repo = ApiTokenRepository(db)
    token = repo.resolve((x_api_key or "").strip())
    if token is None:
        raise _INVALID

    repo.touch(token)
    db.commit()
    return token


# --- escopos ---------------------------------------------------------------
#
# A chave do conector Hedge mora num terminal MetaTrader de um cliente —
# uma maquina que este projeto nao administra e nao consegue auditar. Se
# ela vazar, o alcance precisa ser o minimo possivel.
#
# Chave sem escopo nenhum e a chave LEGADA do Pulso: continua funcionando
# como antes e nao alcanca nada do Hedge. Tratar vazio como "pode tudo"
# daria ao parque instalado um acesso que ninguem concedeu.

SCOPE_TELEMETRY = "hedge:telemetry"
"""Enviar snapshots de cotacao. Nao le setup nem historico."""

SCOPE_SETUP_READ = "hedge:setup:read"
"""Listar os setups DO PROPRIO dono da chave."""

SCOPE_ANALYSIS = "hedge:analysis"
"""Receber a analise de volta e consultar o historico."""

HEDGE_SCOPES = (SCOPE_TELEMETRY, SCOPE_SETUP_READ, SCOPE_ANALYSIS)

_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Esta chave nao tem permissao para este recurso.",
)

_NO_OWNER = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    # Falha fechado: sem dono nao da para decidir de quem sao os setups, e
    # o default seguro e nao alcancar nenhum.
    detail=(
        "Esta chave nao esta vinculada a um usuario e por isso nao alcanca "
        "setups. Gere uma nova chave no painel do Hedge AI."
    ),
)


def require_scopes(*scopes: str):
    """Dependencia que exige escopos E vinculo com usuario.

    Devolve o par `(token, user_id)` para que a rota nao tenha como
    esquecer de filtrar por dono: o `user_id` chega junto da
    autenticacao, e nao de um parametro que o cliente poderia escolher.
    """

    def dependency(token: ApiToken = Depends(get_api_token)) -> tuple[ApiToken, int]:
        for escopo in scopes:
            if not token.has_scope(escopo):
                raise _FORBIDDEN
        if token.created_by_user_id is None:
            raise _NO_OWNER
        return token, token.created_by_user_id

    return dependency
