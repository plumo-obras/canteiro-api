"""Arquivos privados (comprovantes de gasto): no próprio banco (padrão), num bucket privado do Supabase Storage (os dois
aceitos em produção), em memória (testes) ou no disco (só desenvolvimento).

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

import httpx

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


class ErroArquivos(RuntimeError):
    """O serviço de arquivos recusou ou não respondeu. A mensagem leva só o status, nunca a chave nem o corpo."""


# O tipo vai no envio porque o bucket pode restringir os tipos aceitos (e o Storage confere pelo cabeçalho). A extensão
# do caminho sai do `tipo_arquivo.detectar`, que olha os bytes: não é o que o navegador declarou.
_TIPOS_POR_EXTENSAO = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                       "pdf": "application/pdf"}


def _tipo(caminho: str) -> str:
    return _TIPOS_POR_EXTENSAO.get(caminho.rsplit(".", 1)[-1].lower(), "application/octet-stream")


class ArquivosSupabase:
    """Bucket PRIVADO do Supabase Storage, pela API REST, com a chave secreta do projeto (só no ambiente do Render).

    Tira os bytes do banco: no M0 do Atlas (512 MB para tudo) um banco cheio de fotos para a obra inteira, não só os
    comprovantes. O Mongo continua dono do comprovante (tamanho, sha256, caminho); o bucket guarda só o arquivo, em
    `<tenant>/<obra>/<nome>`. O tenant no prefixo é a mesma segunda trava das outras portas: ler pelo tenant errado
    procura em outra pasta e volta None.

    Grava sem sobrescrever (`x-upsert: false`): o nome sai do sha256 do conteúdo, então "já existe" é o mesmo arquivo e
    conta como sucesso, e nem um bug de rota troca a foto de um comprovante já revisado.
    """

    def __init__(self, url: str, chave: str, bucket: str, *, transporte: httpx.AsyncBaseTransport | None = None,
                 tempo: float = 30.0):
        if not url.startswith("https://") and transporte is None:
            raise RuntimeError("SUPABASE_URL tem de ser https.")
        if not chave or not _PARTE.match(bucket or ""):
            raise RuntimeError("ARQUIVOS=supabase precisa de SUPABASE_CHAVE_SERVICO e SUPABASE_BUCKET.")
        self._base = f"{url.rstrip('/')}/storage/v1/object"
        self._bucket = bucket
        # A chave nova (sb_secret_...) não é JWT e vai só no `apikey`: o gateway troca por um token para o Storage. A
        # antiga (service_role) é JWT e o Storage a quer também no Authorization.
        self._cabecalhos = {"apikey": chave}
        if chave.startswith("eyJ"):
            self._cabecalhos["Authorization"] = f"Bearer {chave}"
        self._transporte = transporte
        self._tempo = tempo

    def _cliente(self) -> httpx.AsyncClient:
        # um cliente por chamada: envio de comprovante é raro, e assim nada fica aberto entre deploys nem em teste
        return httpx.AsyncClient(headers=self._cabecalhos, timeout=self._tempo, transport=self._transporte)

    def _chave_objeto(self, tenant_id: str, caminho: str) -> str:
        conferir_caminho(tenant_id, caminho)
        return f"{self._bucket}/{tenant_id}/{caminho}"   # partes já conferidas: nada a escapar na URL

    @staticmethod
    def _codigo(r: httpx.Response) -> str:
        # o formato antigo de erro do Storage responde 400 e põe o motivo no corpo
        try:
            corpo = r.json()
        except ValueError:
            return ""
        if not isinstance(corpo, dict):
            return ""
        return " ".join(str(corpo.get(k, "")) for k in ("code", "error", "statusCode", "httpStatusCode"))

    async def conferir(self) -> None:
        """O bucket existe e a chave abre: é o ping que conta como atividade do projeto (o gratuito pausa parado)."""
        try:
            async with self._cliente() as c:
                r = await c.get(f"{self._base.removesuffix('/object')}/bucket/{self._bucket}")
        except httpx.HTTPError as e:
            raise ErroArquivos(f"O Storage não respondeu ao conferir ({type(e).__name__}).") from None
        if not r.is_success:
            raise ErroArquivos(f"O Storage não abriu o bucket (HTTP {r.status_code}).")

    async def salvar(self, tenant_id: str, caminho: str, conteudo: bytes) -> None:
        objeto = self._chave_objeto(tenant_id, caminho)
        try:
            async with self._cliente() as c:
                r = await c.post(f"{self._base}/{objeto}", content=conteudo,
                                 headers={"Content-Type": _tipo(caminho), "x-upsert": "false"})
        except httpx.HTTPError as e:
            raise ErroArquivos(f"O Storage não respondeu ao gravar ({type(e).__name__}).") from None
        if r.is_success:
            return
        codigo = self._codigo(r)
        if r.status_code == 409 or any(x in codigo for x in ("Duplicate", "already_exists", "AlreadyExists",
                                                              "409")):
            return
        raise ErroArquivos(f"O Storage recusou o arquivo (HTTP {r.status_code}).")

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        objeto = self._chave_objeto(tenant_id, caminho)
        try:
            async with self._cliente() as c:
                r = await c.get(f"{self._base}/authenticated/{objeto}")
        except httpx.HTTPError as e:
            raise ErroArquivos(f"O Storage não respondeu ao ler ({type(e).__name__}).") from None
        if r.is_success:
            return r.content
        codigo = self._codigo(r)
        # bucket errado ou apagado também responde 404: é falha de configuração (503), não "comprovante não existe"
        if "Bucket" not in codigo and (r.status_code == 404
                                       or any(x in codigo for x in ("not_found", "NoSuchKey", "404"))):
            return None
        raise ErroArquivos(f"O Storage não devolveu o arquivo (HTTP {r.status_code}).")


TIPOS = ("banco", "supabase", "disco", "memoria")


def criar_arquivos(tipo: str, raiz: str, arm=None, *, supabase_url: str = "", supabase_chave: str = "",
                   supabase_bucket: str = "comprovantes") -> Arquivos:
    """`banco` usa o armazenamento da app (`arm`), que só existe depois que ela sobe."""
    if tipo == "banco":
        if arm is None:
            raise RuntimeError("ARQUIVOS=banco precisa do armazenamento.")
        return ArquivosNoBanco(arm)
    if tipo == "supabase":
        return ArquivosSupabase(supabase_url, supabase_chave, supabase_bucket)
    if tipo == "memoria":
        return ArquivosMemoria()
    if tipo == "disco":
        return ArquivosDisco(raiz)
    raise RuntimeError(f"ARQUIVOS desconhecido: {tipo!r} (use {', '.join(TIPOS)}).")
