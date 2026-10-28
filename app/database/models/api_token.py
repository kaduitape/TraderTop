"""Chaves de API para clientes que nao sao um navegador.

## Por que nao reusar o JWT

O JWT do painel expira em 30 minutos e nasce de um login com usuario e
senha. Um indicador rodando dentro do MetaTrader nao tem como refazer esse
login: ele ficaria mudo meia hora depois de aberto, no meio do pregao, sem
ninguem por perto para digitar nada.

Chave de API resolve o problema certo — credencial de MAQUINA, de vida
longa, revogavel individualmente sem derrubar a sessao de ninguem.

## Por que SHA-256 e nao bcrypt

Senha de gente tem pouca entropia, e por isso precisa de um hash lento: a
lentidao e o que torna a forca bruta cara. Uma chave gerada aqui tem 256
bits de aleatoriedade — forca bruta ja e impossivel, e o custo do bcrypt
so apareceria como latencia em cada requisicao do indicador, que consulta
a cada poucos segundos.

O que continua valendo da regra da senha: o valor em claro NUNCA volta do
banco. Ele existe uma vez, na resposta que o cria, e nunca mais.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin

PREFIX_LENGTH = 8
"""Quantos caracteres do inicio da chave ficam legiveis.

Sem isso a tela lista "3 chaves ativas" e o operador nao tem como saber
QUAL revogar. O prefixo identifica sem revelar: os 8 primeiros caracteres
de um segredo de 43 nao ajudam ninguem a adivinhar o resto."""


class ApiToken(Base, TimestampMixin):
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    """Para que serve esta chave. "Indicador MT5 do notebook" permite
    revogar a certa quando o notebook some."""

    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    """SHA-256 hex do segredo. O segredo em si nao existe aqui."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    """Revogar desativa em vez de apagar: a auditoria precisa continuar
    podendo explicar o que aquela chave fez enquanto valia."""

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Uma chave que nunca foi usada e uma chave que pode ser revogada sem
    medo — e uma que parou de ser usada e um indicador que caiu."""

    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    """`SET NULL`: apagar um usuario nao pode apagar o rastro de auditoria
    das chaves que ele criou."""

    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    """Permissoes, separadas por virgula. Vazio = a chave legada, que so
    alcanca o Pulso.

    Lista explicita em vez de "pode tudo" porque a chave do conector Hedge
    mora num terminal MetaTrader de um cliente — uma maquina que este
    projeto nao administra. Se ela vazar, o que ela alcanca precisa ser o
    minimo: telemetria, leitura de setup e analise. Nunca ordem, nunca
    mudanca de modo, nunca setup de outro usuario."""

    def has_scope(self, scope: str) -> bool:
        return scope in {s.strip() for s in self.scopes.split(",") if s.strip()}

    def __repr__(self) -> str:
        # Sem hash, sem prefixo completo: `repr` acaba em log com facilidade.
        return f"ApiToken(id={self.id!r}, name={self.name!r}, active={self.is_active!r})"
