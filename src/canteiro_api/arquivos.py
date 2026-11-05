"""Arquivos privados (comprovantes de gasto): no próprio banco (padrão e o único aceito em produção), em memória
(testes) ou no disco (só desenvolvimento).

Fica atrás de `Arquivos`, como o `Cofre`, para trocar por um bucket privado sem mexer nas rotas. Ninguém acessa o
arquivo direto: ele só sai pela rota da API, que confere login e tenant, e a própria porta separa por tenant de novo,
para um erro de rota não virar vazamento entre clientes. Comprovante tem dado pessoal (nome, CPF, conta): nunca vai
para o git, e o diretório local do modo disco fica fora dele (`.arquivos/` no .gitignore).

O caminho é sempre `<obra_id>/<nome>`, montado pela API com IDs nossos, e o tenant vem à parte. Caminho fora disso é
bug ou ataque: a porta falha alto (`ValueError`) em vez de gravar ou ler em lugar errado. Arquivo de outro tenant
simplesmente não existe para quem pergunta (None), sem erro que revele que ele está lá.

No banco, o arquivo vira pedaços de 512 KiB, um documento cada, e um documento-índice que diz quantos são. É a mesma
porta de armazenamento do resto (Mongo hoje, Firestore depois): o Firestore recusa documento acima de 1 MiB, então o
pedaço fica bem abaixo disso. IDs derivados do caminho: lê-se por ID, sem consulta e sem índice. O plano gratuito do
Render apaga o disco a cada deploy; o banco não.
"""
import hashlib
import re
from pathlib import Path
from typing import Protocol

from canteiro_dominio.armazenamento import ConflitoDeId

PEDACO = 512 * 1024
# Teto de um arquivo no banco: 8 pedaços + o índice = 9 escritas e ~4 MiB numa transação só. Cabe com folga no Mongo
# (16 MB por documento; a transação não tem teto de tamanho desde o 4.2) e no Firestore (500 escritas e 10 MiB por
# commit; 1 MiB por documento). Quem envia (servicos/gastos.py) já para em 4 MiB; isto é a trava da porta.
MAX_TAMANHO = 8 * PEDACO
COLECAO_INDICE = "arquivos"
COLECAO_PEDACOS = "arquivos_pedacos"


class Arquivos(Protocol):
    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None: ...
    # None quando não existe OU é de outro tenant: quem pergunta não descobre que o arquivo existe
    async def ler(self, tenant_id: str, caminho: str) -> bytes | None: ...


_PARTE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


def conferir_caminho(tenant_id: str, caminho: str) -> None:
    """Tenant simples e caminho de duas partes simples (`obra/nome`): sem `..`, barra invertida, caminho absoluto,
    subpasta ou arquivo oculto."""
    if not isinstance(tenant_id, str) or not _PARTE.match(tenant_id):
        raise ValueError("Tenant inválido para arquivo.")
    partes = caminho.split("/") if isinstance(caminho, str) else []
    if len(partes) != 2 or not all(_PARTE.match(p) for p in partes):
        raise ValueError("Caminho de arquivo inválido.")


class ArquivosMemoria:
    def __init__(self):
        self._dados: dict[tuple[str, str], bytes] = {}

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        conferir_caminho(tenant_id, caminho)
        self._dados[(tenant_id, caminho)] = conteudo

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        conferir_caminho(tenant_id, caminho)
        return self._dados.get((tenant_id, caminho))


class ArquivosDisco:
    """Só para desenvolvimento: o disco do contêiner some a cada deploy (a config recusa em produção)."""

    def __init__(self, raiz: str):
        self._raiz = Path(raiz).resolve()

    def _alvo(self, tenant_id: str, caminho: str) -> Path:
        conferir_caminho(tenant_id, caminho)
        alvo = (self._raiz / tenant_id / caminho).resolve()
        # segunda trava, depois de `conferir_caminho`: nem link simbólico tira o arquivo da raiz
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
    """Pedaços de 512 KiB + índice, pela porta de armazenamento (abordagem do João Sotto): portável para o Firestore,
    tenant em toda leitura e sha256 conferido ao ler."""

    def __init__(self, arm):
        self._arm = arm

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        conferir_caminho(tenant_id, caminho)
        if len(conteudo) > MAX_TAMANHO:
            raise ValueError("Arquivo maior que o teto do armazenamento no banco.")
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
        conferir_caminho(tenant_id, caminho)
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


TIPOS = ("banco", "disco", "memoria")


def criar_arquivos(tipo: str, raiz: str, arm=None) -> Arquivos:
    """`banco` usa o armazenamento da app (`arm`), que só existe depois que ela sobe."""
    if tipo == "banco":
        if arm is None:
            raise RuntimeError("ARQUIVOS=banco precisa do armazenamento.")
        return ArquivosNoBanco(arm)
    if tipo == "memoria":
        return ArquivosMemoria()
    if tipo == "disco":
        return ArquivosDisco(raiz)
    raise RuntimeError(f"ARQUIVOS desconhecido: {tipo!r} (use {', '.join(TIPOS)}).")
