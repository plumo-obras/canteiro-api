"""Cofre de segredos de cliente (chave própria da Anthropic). Hoje: AES-GCM com CHAVE_CIFRA do ambiente.

Fica atrás de `Cofre` para trocar pelo Secret Manager (ou KMS) sem mexer nas rotas. O texto cifrado leva o tenant como
dado associado: copiar o valor para outro cliente não decifra.
"""
import base64
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Cofre(Protocol):
    def cifrar(self, segredo: str, *, tenant_id: str) -> str: ...
    def decifrar(self, cifrado: str, *, tenant_id: str) -> str: ...


class SegredoIlegivel(Exception):
    """Não decifrou (chave trocada ou dado adulterado). A mensagem nunca carrega o segredo."""


class CofreAesGcm:
    _VERSAO = "v1"

    def __init__(self, chave_b64: str):
        try:
            chave = base64.urlsafe_b64decode(chave_b64 + "=" * (-len(chave_b64) % 4))
        except ValueError:
            chave = b""
        if len(chave) != 32:
            raise RuntimeError("CHAVE_CIFRA inválida: precisa de 32 bytes em base64 (veja .env.exemplo).")
        self._aes = AESGCM(chave)

    def cifrar(self, segredo: str, *, tenant_id: str) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, segredo.encode(), tenant_id.encode())
        return f"{self._VERSAO}:{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decifrar(self, cifrado: str, *, tenant_id: str) -> str:
        try:
            versao, _, corpo = cifrado.partition(":")
            if versao != self._VERSAO:
                raise ValueError
            bruto = base64.urlsafe_b64decode(corpo)
            return self._aes.decrypt(bruto[:12], bruto[12:], tenant_id.encode()).decode()
        except Exception:
            raise SegredoIlegivel("Não foi possível decifrar o segredo guardado.") from None


class SemCofre:
    """Ambiente sem CHAVE_CIFRA: chave própria fica indisponível (em vez de guardar em claro)."""

    def cifrar(self, segredo: str, *, tenant_id: str) -> str:
        raise RuntimeError("CHAVE_CIFRA não configurada: não dá para guardar a chave própria.")

    def decifrar(self, cifrado: str, *, tenant_id: str) -> str:
        raise RuntimeError("CHAVE_CIFRA não configurada.")


def criar_cofre(chave_cifra: str) -> Cofre:
    return CofreAesGcm(chave_cifra) if chave_cifra else SemCofre()
