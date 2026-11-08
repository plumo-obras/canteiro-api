"""Contrato da porta `Arquivos`: o mesmo conjunto roda contra memória, disco, banco (pedaços na porta de
armazenamento, em memória e no Mongo do compose) e o Supabase Storage (um falso que responde como o de verdade).

O que importa aqui é o isolamento: um cliente nunca lê o arquivo de outro, e caminho torto (travessia, subpasta,
tenant estranho) falha alto em vez de gravar ou ler fora do lugar. No banco, também a integridade: pedaço trocado ou
faltando não vira outra foto.
"""
import json
import secrets
import uuid

import httpx
import pytest

from canteiro_api.arquivos import (
    COLECAO_INDICE,
    COLECAO_PEDACOS,
    MAX_TAMANHO,
    PEDACO,
    ArquivosDisco,
    ArquivosMemoria,
    ArquivosNoBanco,
    ArquivosSupabase,
    ErroArquivos,
    _id_arquivo,
    criar_arquivos,
)
from canteiro_api.config import Config

CHAVE_FALSA = "sb_secret_FALSA-de-teste-0000"
URL_FALSA = "https://falso.supabase.co"


class StorageFalso:
    """O pedaço da API REST do Storage que a porta usa: grava sem sobrescrever (409), lê autenticado (404 se não há)."""

    def __init__(self, bucket="comprovantes"):
        self.objetos: dict[str, bytes] = {}
        self.bucket = bucket
        self.pedidos: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req)
        if req.headers.get("apikey") != CHAVE_FALSA:
            return httpx.Response(401, json={"code": "InvalidJWT"})
        caminho = req.url.path
        if req.method == "POST" and caminho.startswith(f"/storage/v1/object/{self.bucket}/"):
            chave = caminho.removeprefix(f"/storage/v1/object/{self.bucket}/")
            if chave in self.objetos:
                return httpx.Response(409, json={"code": "KeyAlreadyExists"})
            self.objetos[chave] = req.content
            return httpx.Response(200, json={"Key": f"{self.bucket}/{chave}"})
        if req.method == "GET" and caminho.startswith(f"/storage/v1/object/authenticated/{self.bucket}/"):
            chave = caminho.removeprefix(f"/storage/v1/object/authenticated/{self.bucket}/")
            if chave not in self.objetos:
                return httpx.Response(404, json={"code": "NoSuchKey"})
            return httpx.Response(200, content=self.objetos[chave])
        return httpx.Response(400, json={"code": "InvalidRequest"})


def _supabase(storage=None):
    return ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "comprovantes",
                            transporte=httpx.MockTransport(storage or StorageFalso()))


@pytest.fixture(params=["memoria", "disco", "banco", "supabase"])
def arquivos(request, tmp_path, arm):
    if request.param == "memoria":
        return ArquivosMemoria()
    if request.param == "disco":
        return ArquivosDisco(str(tmp_path / "arquivos"))
    if request.param == "supabase":
        return _supabase()
    return ArquivosNoBanco(arm)


def _ids():
    return f"t-{uuid.uuid4().hex[:10]}", str(uuid.uuid4())


async def test_guarda_e_devolve_o_mesmo_conteudo(arquivos):
    tenant, obra = _ids()
    caminho, conteudo = f"{obra}/c1.jpg", secrets.token_bytes(PEDACO + 300_000)   # mais de um pedaço no banco
    await arquivos.salvar(tenant, caminho, conteudo)
    assert await arquivos.ler(tenant, caminho) == conteudo
    # mesmo caminho de novo (o nome sai do sha256: é o mesmo arquivo) não quebra nem muda nada
    await arquivos.salvar(tenant, caminho, conteudo)
    assert await arquivos.ler(tenant, caminho) == conteudo
    assert await arquivos.ler(tenant, f"{obra}/nao-existe.jpg") is None


async def test_outro_tenant_nao_le(arquivos):
    tenant, obra = _ids()
    outro, _ = _ids()
    caminho = f"{obra}/c1.pdf"
    await arquivos.salvar(tenant, caminho, b"%PDF-1.7 x")
    # o mesmo caminho pedido por outro cliente: nada, sem erro que revele que o arquivo existe
    assert await arquivos.ler(outro, caminho) is None
    assert await arquivos.ler(f"{tenant}-outro", caminho) is None


@pytest.mark.parametrize("caminho", [
    "{o}/../../fora.jpg",
    "{o}/..",
    "../{o}/x.jpg",
    "../x.jpg",
    "/{o}/x.jpg",
    "{o}/sub/x.jpg",
    "{o}/a\\..\\b.jpg",
    "{o}\\x.jpg",
    "{o}/.oculto",
    "{o}/",
    "/x.jpg",
    "x.jpg",
    "",
])
async def test_caminho_torto_e_recusado(arquivos, caminho):
    tenant, obra = _ids()
    caminho = caminho.format(o=obra)
    with pytest.raises(ValueError):
        await arquivos.salvar(tenant, caminho, b"x")
    with pytest.raises(ValueError):
        await arquivos.ler(tenant, caminho)


@pytest.mark.parametrize("tenant", ["", "..", ".", "a/b", "a\\b", "/abs"])
async def test_tenant_torto_e_recusado(arquivos, tenant):
    _, obra = _ids()
    with pytest.raises(ValueError):
        await arquivos.salvar(tenant, f"{obra}/x.jpg", b"x")
    with pytest.raises(ValueError):
        await arquivos.ler(tenant, f"{obra}/x.jpg")


async def test_disco_nao_sai_da_raiz_nem_por_link(tmp_path):
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "segredo.jpg").write_bytes(b"de outro lugar")
    raiz = tmp_path / "arquivos"
    tenant, obra = _ids()
    (raiz / tenant / obra).mkdir(parents=True)
    (raiz / tenant / obra / "link.jpg").symlink_to(fora / "segredo.jpg")
    disco = ArquivosDisco(str(raiz))
    with pytest.raises(ValueError):
        await disco.ler(tenant, f"{obra}/link.jpg")
    with pytest.raises(ValueError):
        await disco.salvar(tenant, f"{obra}/link.jpg", b"x")
    assert (fora / "segredo.jpg").read_bytes() == b"de outro lugar"


# --- no banco: pedaços, índice, integridade ---

async def test_arquivo_grande_volta_igual_em_varios_pedacos(arm, tenant):
    a = ArquivosNoBanco(arm)
    conteudo = secrets.token_bytes(PEDACO * 2 + 123)
    await a.salvar(tenant, "obra/c1.jpg", conteudo)
    assert await a.ler(tenant, "obra/c1.jpg") == conteudo
    indice = await arm.obter(COLECAO_INDICE, tenant, _id_arquivo(tenant, "obra/c1.jpg"))
    assert (indice["pedacos"], indice["tamanho"]) == (3, len(conteudo))


async def test_o_maior_arquivo_cabe_numa_transacao(arm, tenant):
    # 4 MiB = 8 pedaços + o índice: 9 escritas, abaixo do teto portável da porta; um byte a mais é recusado
    a = ArquivosNoBanco(arm)
    conteudo = secrets.token_bytes(MAX_TAMANHO)
    await a.salvar(tenant, "obra/maior.pdf", conteudo)
    assert await a.ler(tenant, "obra/maior.pdf") == conteudo
    indice = await arm.obter(COLECAO_INDICE, tenant, _id_arquivo(tenant, "obra/maior.pdf"))
    assert indice["pedacos"] == 8
    with pytest.raises(ValueError):
        await a.salvar(tenant, "obra/demais.pdf", conteudo + b"x")
    assert await a.ler(tenant, "obra/demais.pdf") is None


def test_o_limite_do_envio_cabe_no_banco():
    from canteiro_api.servicos.gastos import MAX_ARQUIVO, MAX_IMAGEM

    assert MAX_IMAGEM < MAX_ARQUIVO <= MAX_TAMANHO


async def test_pequeno_vazio_e_inexistente(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/p.png", b"\x89PNG")
    assert await a.ler(tenant, "obra/p.png") == b"\x89PNG"
    await a.salvar(tenant, "obra/vazio.pdf", b"")
    assert await a.ler(tenant, "obra/vazio.pdf") == b""
    assert await a.ler(tenant, "obra/nao-existe.jpg") is None


async def test_pedaco_adulterado_nao_vira_outra_foto(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/c1.jpg", b"original")
    await arm.atualizar(COLECAO_PEDACOS, tenant, f"{_id_arquivo(tenant, 'obra/c1.jpg')}-000", {"dados": b"trocado!"})
    assert await a.ler(tenant, "obra/c1.jpg") is None


# --- Supabase Storage: bucket privado, tenant no prefixo, nunca sobrescreve ---

async def test_supabase_guarda_com_o_tenant_no_prefixo_e_sem_sobrescrever():
    storage = StorageFalso()
    a = _supabase(storage)
    tenant, obra = _ids()
    await a.salvar(tenant, f"{obra}/c1.jpg", b"original")
    assert list(storage.objetos) == [f"{tenant}/{obra}/c1.jpg"]
    post = storage.pedidos[0]
    assert post.headers["x-upsert"] == "false" and "authorization" not in post.headers   # chave nova: só no apikey
    # o mesmo nome com outro conteúdo (bug de rota) não troca a foto já guardada
    await a.salvar(tenant, f"{obra}/c1.jpg", b"trocado")
    assert await a.ler(tenant, f"{obra}/c1.jpg") == b"original"


def test_supabase_chave_antiga_vai_tambem_no_authorization():
    a = ArquivosSupabase(URL_FALSA, "eyJ.falso.jwt", "comprovantes")
    assert a._cabecalhos == {"apikey": "eyJ.falso.jwt", "Authorization": "Bearer eyJ.falso.jwt"}


@pytest.mark.parametrize("resposta, esperado", [
    (httpx.Response(400, json={"statusCode": "409", "error": "Duplicate"}), "salvo"),     # formato antigo do erro
    (httpx.Response(400, json={"statusCode": "404", "error": "not_found"}), None),
])
async def test_supabase_entende_o_formato_antigo_de_erro(resposta, esperado):
    a = ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "comprovantes", transporte=httpx.MockTransport(lambda _: resposta))
    tenant, obra = _ids()
    if esperado == "salvo":
        await a.salvar(tenant, f"{obra}/x.jpg", b"x")
    else:
        assert await a.ler(tenant, f"{obra}/x.jpg") is None


async def test_supabase_falha_alto_sem_vazar_a_chave():
    def recusa(req):
        return httpx.Response(403, json={"message": f"chave {req.headers['apikey']} sem acesso"})

    def cai(req):
        raise httpx.ConnectError("sem rede", request=req)

    tenant, obra = _ids()
    for transporte in (recusa, cai):
        a = ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "comprovantes", transporte=httpx.MockTransport(transporte))
        for chamada in (a.salvar(tenant, f"{obra}/x.jpg", b"x"), a.ler(tenant, f"{obra}/x.jpg")):
            with pytest.raises(ErroArquivos) as e:
                await chamada
            assert CHAVE_FALSA not in str(e.value) and e.value.__cause__ is None


def test_supabase_exige_https_chave_e_bucket():
    for url, chave, bucket in [("http://x.supabase.co", CHAVE_FALSA, "b"), (URL_FALSA, "", "b"),
                               (URL_FALSA, CHAVE_FALSA, "../b")]:
        with pytest.raises(RuntimeError):
            ArquivosSupabase(url, chave, bucket)


async def test_rota_responde_503_quando_o_storage_cai(arm, config):
    from canteiro_api.main import create_app

    app = create_app(config.model_copy(update={"arquivos": "memoria"}), arm)

    class Fora:
        async def salvar(self, *a):
            raise ErroArquivos("O Storage não respondeu ao gravar (ConnectError).")

        async def ler(self, *a):
            raise ErroArquivos("O Storage não respondeu ao ler (ConnectError).")

    app.state.arquivos = Fora()

    @app.get("/_teste_arquivo")
    async def _rota():
        await app.state.arquivos.ler("t", "o/x.jpg")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/_teste_arquivo")
    assert r.status_code == 503 and "armazenamento" in json.loads(r.text)["detail"]


# --- configuração: produção não sobe com arquivo em lugar que some ---

def _config(**kw):
    return Config(jwt_segredo=secrets.token_urlsafe(48), _env_file=None, **kw)


def test_padrao_e_o_banco():
    assert _config().arquivos == "banco"


def test_producao_aceita_supabase_com_url_e_chave():
    _config(ambiente="producao", arquivos="supabase", supabase_url=URL_FALSA,
            supabase_chave_servico=CHAVE_FALSA).conferir()
    for falta in ({"supabase_url": URL_FALSA}, {"supabase_chave_servico": CHAVE_FALSA},
                  {"supabase_url": "http://x.supabase.co", "supabase_chave_servico": CHAVE_FALSA}):
        with pytest.raises(RuntimeError, match="SUPABASE"):
            _config(arquivos="supabase", **falta).conferir()


@pytest.mark.parametrize("arquivos", ["disco", "memoria", "mongo"])
def test_producao_recusa_arquivo_fora_do_banco(arquivos):
    with pytest.raises(RuntimeError, match="ARQUIVOS"):
        _config(ambiente="producao", arquivos=arquivos).conferir()
    with pytest.raises(RuntimeError, match="ARMAZENAMENTO"):
        _config(ambiente="producao", arquivos="banco", armazenamento="memoria").conferir()
    _config(ambiente="producao", arquivos="banco", armazenamento="mongo").conferir()
    _config(arquivos=arquivos).conferir()                       # em desenvolvimento, pode


def test_ambiente_desconhecido_e_recusado():
    with pytest.raises(RuntimeError, match="AMBIENTE"):
        _config(ambiente="prod").conferir()


def test_criar_arquivos():
    from canteiro_api.armazenamento import ArmazenamentoMemoria

    assert isinstance(criar_arquivos("banco", "", ArmazenamentoMemoria()), ArquivosNoBanco)
    assert isinstance(criar_arquivos("supabase", "", supabase_url=URL_FALSA, supabase_chave=CHAVE_FALSA),
                      ArquivosSupabase)
    with pytest.raises(RuntimeError, match="armazenamento"):
        criar_arquivos("banco", "")
    with pytest.raises(RuntimeError, match="desconhecido"):
        criar_arquivos("mongo", "")


def test_app_guarda_os_arquivos_no_mesmo_armazenamento(arm, config):
    from canteiro_api.main import create_app

    app = create_app(config.model_copy(update={"arquivos": "banco"}), arm)
    assert isinstance(app.state.arquivos, ArquivosNoBanco)
