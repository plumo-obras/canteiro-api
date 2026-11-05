"""Arquivos privados (comprovantes de gasto): memória (testes), disco local (só desenvolvimento) ou GridFS no mesmo
banco do Mongo (produção; `ArquivosMongo` em `armazenamento/mongo.py`, o único módulo que conhece o pymongo).

Fica atrás de `Arquivos`, como o `Cofre`, para trocar por um bucket privado (GCS, junto com o Firestore) sem mexer nas
rotas. Ninguém acessa o arquivo direto: ele só sai pela rota da API, que confere login e tenant, e a própria porta
confere o tenant de novo (o caminho começa por ele), para um erro de rota não virar vazamento entre clientes.
Comprovante tem dado pessoal (nome, CPF, conta): nunca vai para o git, e o diretório local fica fora do repositório
versionado (`.arquivos/` no .gitignore).

O caminho é sempre `<tenant_id>/<obra_id>/<nome>`, montado pela API com IDs nossos.
"""
import re
from pathlib import Path
from typing import Protocol


class Arquivos(Protocol):
    async def salvar(self, tenant_id: str, obra_id: str, caminho: str, conteudo: bytes, tipo: str) -> None: ...
    # None quando não existe OU é de outro tenant: quem pergunta não descobre que o arquivo existe
    async def ler(self, tenant_id: str, caminho: str) -> bytes | None: ...


_PARTE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def conferir_caminho(caminho: str, tenant_id: str, obra_id: str | None = None) -> None:
    """Três partes simples (sem `..`, barra invertida ou caminho absoluto) e as duas primeiras = tenant e obra. Os IDs
    são nossos, então caminho fora disso é bug ou ataque: falha alto em vez de gravar em lugar errado."""
    partes = caminho.split("/")
    if len(partes) != 3 or not all(_PARTE.match(p) for p in partes):
        raise ValueError("Caminho de arquivo inválido.")
    if not tenant_id or partes[0] != tenant_id or (obra_id is not None and partes[1] != obra_id):
        raise ValueError("Caminho de arquivo fora do tenant/obra.")


def do_tenant(caminho: str, tenant_id: str) -> bool:
    return bool(tenant_id) and caminho.startswith(f"{tenant_id}/")


class ArquivosMemoria:
    def __init__(self):
        self._dados: dict[str, bytes] = {}

    async def salvar(self, tenant_id: str, obra_id: str, caminho: str, conteudo: bytes, tipo: str) -> None:
        conferir_caminho(caminho, tenant_id, obra_id)
        self._dados[caminho] = conteudo

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        if not do_tenant(caminho, tenant_id):
            return None
        conferir_caminho(caminho, tenant_id)
        return self._dados.get(caminho)


class ArquivosDisco:
    """Só para desenvolvimento: o disco do contêiner some a cada deploy (a config recusa em produção)."""

    def __init__(self, raiz: str):
        self._raiz = Path(raiz).resolve()

    def _alvo(self, caminho: str) -> Path:
        alvo = (self._raiz / caminho).resolve()
        # segunda trava, depois de `conferir_caminho`: nem link simbólico tira o arquivo da raiz
        if not alvo.is_relative_to(self._raiz):
            raise ValueError("Caminho de arquivo fora da raiz.")
        return alvo

    async def salvar(self, tenant_id: str, obra_id: str, caminho: str, conteudo: bytes, tipo: str) -> None:
        conferir_caminho(caminho, tenant_id, obra_id)
        alvo = self._alvo(caminho)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(conteudo)

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        if not do_tenant(caminho, tenant_id):
            return None
        conferir_caminho(caminho, tenant_id)
        alvo = self._alvo(caminho)
        return alvo.read_bytes() if alvo.is_file() else None


TIPOS = ("mongo", "disco", "memoria")


def criar_arquivos(tipo: str, raiz: str, arm=None) -> Arquivos:
    """`mongo` usa o banco do armazenamento (`arm`), que só existe depois que a app sobe."""
    if tipo == "memoria":
        return ArquivosMemoria()
    if tipo == "disco":
        return ArquivosDisco(raiz)
    if tipo == "mongo":
        if not hasattr(arm, "arquivos"):
            raise RuntimeError("ARQUIVOS=mongo exige ARMAZENAMENTO=mongo (o GridFS fica no mesmo banco).")
        return arm.arquivos()
    raise RuntimeError(f"ARQUIVOS desconhecido: {tipo!r} (use {', '.join(TIPOS)}).")
