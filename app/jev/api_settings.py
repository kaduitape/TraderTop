"""A chave do Jev configurada pelo painel, cifrada no banco.

## Por que isto existe, tendo eu argumentado contra

O desenho original deixava a chave so no `.env`: segredo no banco ganha
um segundo lugar de onde vazar (backup, replica, dump de suporte). O dono
do repositorio pediu o campo no painel, e a razao e boa — mexer no `.env`
exige SSH na VPS, e quem paga a conta da API nem sempre tem.

Entao o campo existe, mas pelo caminho mais forte que este projeto ja
tem para o mesmo problema.

## Cifrada, nao em texto puro

A chave da AIsa mora em `system_settings` em claro; a senha do MT5 e
cifrada com Fernet (`app/core/crypto.py`). Dos dois precedentes, este
modulo segue o segundo.

O que a cifra protege e o que nao protege, dito na cara: protege contra
vazamento do BANCO isolado — dump, backup, print de tabela; sem a chave
de cifra, o campo e ruido. NAO protege contra quem ja tem acesso ao
servidor da aplicacao, porque quem le o `.env` le a chave de cifra. E o
limite de qualquer cifra simetrica cujo material precisa estar disponivel
ao processo que decifra.

## Precedencia: banco sobrepoe `.env`

Mesma convencao de `app/news/api_settings.py`. Instalacao nova funciona
sem configurar nada; quem ja tem o `.env` ajustado nao ve mudanca ate
mexer no painel.

## O que nunca sai daqui

O valor em claro so existe em memoria, no instante da chamada HTTP. Nao
volta pela API, nao aparece na tela (so a mascara), nao entra em log nem
em auditoria — o registro guarda os ultimos caracteres, que identificam
sem revelar.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import CredentialCryptoError, decrypt_secret, encrypt_secret
from app.core.logging import get_logger
from app.database.repositories.system_setting_repository import SystemSettingRepository

logger = get_logger(__name__)

JEV_API_KEY_SETTING = "jev_api_key_encrypted"
"""O sufixo `_encrypted` e proposital: diz a quem abrir a tabela que o
campo nao e legivel, antes que alguem tente usa-lo direto."""

JEV_ENABLED_SETTING = "jev_enabled"

MIN_KEY_LENGTH = 16
"""Abaixo disto nao e chave, e provavelmente foi colagem incompleta.

Recusar e melhor que gravar: uma chave truncada so falharia na proxima
chamada, com 401, longe do campo onde o erro foi cometido."""


@dataclass(frozen=True, slots=True)
class JevRuntimeSettings:
    """O que vale AGORA, e de onde veio."""

    enabled: bool
    api_key: str | None
    key_source: str
    """"dashboard", "env" ou "" quando nao ha chave."""

    enabled_source: str
    key_hint: str = ""
    """Os ultimos quatro caracteres. Identifica QUAL chave esta em uso
    quando ha mais de uma conta — sem revelar o resto."""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def active(self) -> bool:
        """Ligado E com chave. Um sem o outro nao classifica nada, e a
        tela precisa poder dizer qual dos dois falta."""
        return self.enabled and self.configured

    def fingerprint(self) -> str:
        """Identidade da configuracao, para o cache do factory.

        Hash, e nao a chave: ela entraria numa tupla que vive na memoria
        do processo e que um `repr` de depuracao imprimiria inteira.
        """
        material = f"{self.enabled}|{self.api_key or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _read_bool(session: Session, key: str) -> bool | None:
    bruto = SystemSettingRepository(session).get(key)
    if bruto is None or not str(bruto).strip():
        return None
    return str(bruto).strip().lower() in {"1", "true", "sim", "yes", "on"}


def load_jev_settings(session: Session, settings: Settings) -> JevRuntimeSettings:
    """A configuracao vigente. Nunca levanta.

    Uma chave que nao pode ser decifrada (material de cifra trocado, ou
    registro adulterado) e tratada como AUSENTE, com aviso no log. A
    alternativa — deixar a excecao subir — derrubaria o snapshot por
    causa de uma camada acessoria, e ainda assim nao haveria chave.
    """
    repo = SystemSettingRepository(session)

    chave: str | None = None
    origem_chave = ""
    cifrada = repo.get(JEV_API_KEY_SETTING)
    if cifrada:
        try:
            chave = decrypt_secret(cifrada) or None
            origem_chave = "dashboard" if chave else ""
        except CredentialCryptoError:
            logger.warning("jev_chave_ilegivel")
            chave, origem_chave = None, ""

    if not chave and settings.jev_api_key:
        chave, origem_chave = settings.jev_api_key, "env"

    ligado = _read_bool(session, JEV_ENABLED_SETTING)
    origem_ligado = "dashboard" if ligado is not None else "env"
    if ligado is None:
        ligado = settings.jev_enabled

    return JevRuntimeSettings(
        enabled=bool(ligado),
        api_key=chave,
        key_source=origem_chave,
        enabled_source=origem_ligado,
        key_hint=chave[-4:] if chave and len(chave) >= 8 else "",
    )


def save_api_key(session: Session, plaintext: str) -> str:
    """Grava a chave cifrada. Devolve a dica (ultimos quatro).

    Campo VAZIO nao chega aqui: a rota trata "nao mexeu no campo" como
    "mantem a atual", e apagar exige a acao explicita de remover. Sem
    isso, salvar qualquer outro ajuste da tela apagaria a chave.
    """
    limpo = plaintext.strip()
    if len(limpo) < MIN_KEY_LENGTH:
        raise ValueError(
            f"a chave parece incompleta ({len(limpo)} caracteres). "
            "Cole o valor inteiro, sem espacos."
        )

    SystemSettingRepository(session).set(
        JEV_API_KEY_SETTING,
        encrypt_secret(limpo),
        description=(
            "Chave da API Jev/TypeSafe, cifrada. Nunca legivel pela API "
            "nem pela tela."
        ),
    )
    return limpo[-4:]


def clear_api_key(session: Session) -> None:
    """Remove a chave do banco.

    Grava vazio em vez de apagar a linha: `load_jev_settings` trata vazio
    como ausente e cai no `.env`, que e o comportamento esperado de
    "removi o que eu tinha posto no painel".
    """
    SystemSettingRepository(session).set(
        JEV_API_KEY_SETTING,
        "",
        description="Chave da API Jev removida pelo painel.",
    )


def set_enabled(session: Session, enabled: bool) -> None:
    SystemSettingRepository(session).set(
        JEV_ENABLED_SETTING,
        "true" if enabled else "false",
        description="Liga a classificacao de contexto Jev (sobrepoe o .env).",
    )
