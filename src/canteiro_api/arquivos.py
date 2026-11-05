"""Arquivos privados (comprovantes de gasto): no próprio banco (padrão), em memória (testes) ou no disco (local).

Fica atrás de `Arquivos`, como o `Cofre`, para trocar por um bucket privado sem mexer nas rotas. Ninguém acessa o
arquivo direto: ele só sai pela rota da API, que confere login e tenant. Comprovante tem dado pessoal (nome, CPF,
conta): nunca vai para o git, e o diretório local do modo disco fica fora dele (`.arquivos/` no .gitignore).

No banco, o arquivo vira pedaços de 512 KiB, um documento cada, e um documento-índice que diz quantos são. É a mesma
porta de armazenamento do resto (Mongo hoje, Firestore depois): o Firestore recusa documento acima de 1 MiB, então o
pedaço fica bem abaixo disso. IDs derivados do caminho: lê-se por ID, sem consulta e sem índice. O plano gratuito do
Render apaga o disco a cada deploy; o banco não.
"""
import hashlib
from pathlib import Path
from typing import Protocol

from canteiro_dominio.armazenamento import ConflitoDeId

PEDACO = 512 * 1024
COLECAO_INDICE = "arquivos"
COLECAO_PEDACOS = "arquivos_pedacos"


class Arquivos(Protocol):
    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None: ...
    async def ler(self, tenant_id: str, caminho: str) -> bytes | None: ...


class ArquivosMemoria:
    def __init__(self):
        self._dados: dict[tuple[str, str], bytes] = {}

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        self._dados[(tenant_id, caminho)] = conteudo

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        return self._dados.get((tenant_id, caminho))


class ArquivosDisco:
    """Só para desenvolvimento: o disco do contêiner some a cada deploy."""

    def __init__(self, raiz: str):
        self._raiz = Path(raiz).resolve()

    def _alvo(self, tenant_id: str, caminho: str) -> Path:
        alvo = (self._raiz / tenant_id / caminho).resolve()
        # o caminho é montado pela API com IDs nossos; mesmo assim, nada sai da raiz
        if not alvo.is_relative_to(self._raiz):
            raise ValueError("Caminho de arquivo fora da raiz.")
        return alvo

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        alvo = self._alvo(tenant_id, caminho)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(conteudo)

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        alvo = self._alvo(tenant_id, caminho)
        return alvo.read_bytes() if alvo.is_file() else None


def _id_arquivo(tenant_id: str, caminho: str) -> str:
    # o ID do documento é único na coleção inteira, não por cliente: o tenant entra no hash. E o Firestore não aceita
    # "/" no ID, que o caminho tem.
    return hashlib.sha256(f"{tenant_id}|{caminho}".encode()).hexdigest()


class ArquivosNoBanco:
    def __init__(self, arm):
        self._arm = arm

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        id_ = _id_arquivo(tenant_id, caminho)
        pedacos = [conteudo[i:i + PEDACO] for i in range(0, len(conteudo), PEDACO)] or [b""]

        async def corpo(tx):
            if await tx.obter(COLECAO_INDICE, tenant_id, id_):
                return   # o caminho sai do sha256 do arquivo: já guardado é o mesmo conteúdo
            for n, p in enumerate(pedacos):
                tx.criar(COLECAO_PEDACOS, {"id": f"{id_}-{n:03d}", "tenant_id": tenant_id, "dados": p})
            # o índice por último, na mesma transação: sem ele o arquivo não existe, nunca pela metade
            tx.criar(COLECAO_INDICE, {"id": id_, "tenant_id": tenant_id, "caminho": caminho, "pedacos": len(pedacos),
                                      "tamanho": len(conteudo), "sha256": hashlib.sha256(conteudo).hexdigest()})

        try:
            await self._arm.em_transacao(corpo)
        except ConflitoDeId:
            pass   # dois envios do mesmo arquivo ao mesmo tempo: o outro já guardou

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        id_ = _id_arquivo(tenant_id, caminho)
        indice = await self._arm.obter(COLECAO_INDICE, tenant_id, id_)
        if not indice:
            return None
        partes = []
        for n in range(indice["pedacos"]):
            p = await self._arm.obter(COLECAO_PEDACOS, tenant_id, f"{id_}-{n:03d}")
            if p is None:
                return None
            partes.append(bytes(p["dados"]))
        conteudo = b"".join(partes)
        # pedaço trocado ou faltando não pode virar a foto errada na revisão
        return conteudo if hashlib.sha256(conteudo).hexdigest() == indice["sha256"] else None


def criar_arquivos(tipo: str, raiz: str, arm=None) -> Arquivos:
    if tipo == "banco":
        if arm is None:
            raise RuntimeError("ARQUIVOS=banco precisa do armazenamento.")
        return ArquivosNoBanco(arm)
    if tipo == "memoria":
        return ArquivosMemoria()
    if tipo == "disco":
        return ArquivosDisco(raiz)
    raise RuntimeError(f"ARQUIVOS desconhecido: {tipo!r} (use banco, memoria ou disco).")
