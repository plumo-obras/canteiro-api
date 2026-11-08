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

    def __init__(self, bucket="comprovantes", tipos=("image/jpeg", "image/png", "image/webp", "application/pdf")):
        self.objetos: dict[str, bytes] = {}
        self.bucket = bucket
        self.tipos = tipos                      # o bucket restrito a esses tipos, como o roteiro do deploy sugere
        self.pedidos: list[httpx.Request] = []
        self.falhar_em: set[str] = set()        # chaves cujo envio responde 500 (Storage instável)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos.append(req)
        if req.headers.get("apikey") != CHAVE_FALSA:
            return httpx.Response(401, json={"code": "InvalidJWT"})
        caminho = req.url.path
        if req.method == "POST" and caminho.startswith(f"/storage/v1/object/{self.bucket}/"):
            chave = caminho.removeprefix(f"/storage/v1/object/{self.bucket}/")
            if chave in self.falhar_em:
                return httpx.Response(500, json={"code": "InternalError"})
            if self.tipos and req.headers.get("content-type") not in self.tipos:
                return httpx.Response(400, json={"code": "InvalidMimeType", "statusCode": "415"})
            if chave in self.objetos:
                return httpx.Response(409, json={"code": "KeyAlreadyExists"})
            self.objetos[chave] = req.content
            return httpx.Response(200, json={"Key": f"{self.bucket}/{chave}"})
        if req.method == "GET" and caminho.startswith(f"/storage/v1/object/authenticated/{self.bucket}/"):
            chave = caminho.removeprefix(f"/storage/v1/object/authenticated/{self.bucket}/")
            if chave not in self.objetos:
                return httpx.Response(404, json={"code": "NoSuchKey"})
            return httpx.Response(200, content=self.objetos[chave])
        if req.method == "GET" and caminho == f"/storage/v1/bucket/{self.bucket}":
            return httpx.Response(200, json={"id": self.bucket, "public": False})
        if caminho.startswith("/storage/v1/"):
            return httpx.Response(404, json={"code": "NoSuchBucket"})
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


async def test_supabase_conferir_o_bucket():
    await _supabase().conferir()
    outro = ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "outro-bucket", transporte=httpx.MockTransport(StorageFalso()))
    with pytest.raises(ErroArquivos, match="HTTP 404"):
        await outro.conferir()


async def test_ping_do_bucket_responde_e_guarda_o_resultado(arm, config):
    from canteiro_api.main import create_app

    storage = StorageFalso()
    app = create_app(config.model_copy(update={"arquivos": "memoria"}), arm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/saude/arquivos")                       # sem bucket: nada a conferir
        assert r.json() == {"ok": True}
        app.state.arquivos = _supabase(storage)
        for _ in range(3):
            assert (await c.get("/saude/arquivos")).status_code == 200
        assert len(storage.pedidos) == 1                         # chamada em laço não vira tráfego no Storage
        app.state.arquivos = ArquivosSupabase(URL_FALSA, "sb_secret_errada", "comprovantes",
                                              transporte=httpx.MockTransport(storage))
        app.state.saude_arquivos = None
        r = await c.get("/saude/arquivos")
        assert r.status_code == 503 and r.json() == {"ok": False}


# --- migração do banco para o bucket ---

async def test_migrar_copia_confere_e_so_apaga_quando_pedido(arm):
    from canteiro_api.migrar_arquivos import migrar

    banco, storage = ArquivosNoBanco(arm), StorageFalso()
    destino = _supabase(storage)
    (t1, obra1), (t2, obra2) = _ids(), _ids()
    grande = secrets.token_bytes(PEDACO * 2 + 7)
    await banco.salvar(t1, f"{obra1}/a.jpg", grande)
    await banco.salvar(t1, f"{obra1}/b.pdf", b"%PDF-1.7 b")
    await banco.salvar(t2, f"{obra2}/a.jpg", b"de outro cliente")

    rel = await migrar(arm, destino, tenants=[t1, t2], avisar=lambda _: None)
    assert (rel.copiados, rel.apagados, rel.problemas) == (3, 0, [])
    assert storage.objetos[f"{t1}/{obra1}/a.jpg"] == grande
    assert storage.objetos[f"{t2}/{obra2}/a.jpg"] == b"de outro cliente"
    assert await banco.ler(t1, f"{obra1}/a.jpg") == grande          # sem a opção, o banco fica como estava

    # de novo, agora apagando: o que já está no bucket conta como copiado e sai do banco, pedaços inclusive
    rel = await migrar(arm, destino, apagar_do_banco=True, tenants=[t1, t2], avisar=lambda _: None)
    assert (rel.copiados, rel.apagados) == (3, 3)
    assert await banco.ler(t1, f"{obra1}/a.jpg") is None
    id_ = _id_arquivo(t1, f"{obra1}/a.jpg")
    assert all([await arm.obter(COLECAO_PEDACOS, t1, f"{id_}-{n:03d}") is None for n in range(3)])
    assert await destino.ler(t1, f"{obra1}/a.jpg") == grande


async def test_migrar_nao_apaga_o_que_nao_confere_no_bucket(arm):
    from canteiro_api.migrar_arquivos import migrar

    banco, storage = ArquivosNoBanco(arm), StorageFalso()
    tenant, obra = _ids()
    await banco.salvar(tenant, f"{obra}/a.jpg", b"o do banco")
    storage.objetos[f"{tenant}/{obra}/a.jpg"] = b"outro conteudo"   # já havia outra coisa com esse nome
    await banco.salvar(tenant, f"{obra}/b.jpg", b"este quebrou")
    await arm.atualizar(COLECAO_PEDACOS, tenant, f"{_id_arquivo(tenant, f'{obra}/b.jpg')}-000", {"dados": b"x"})

    rel = await migrar(arm, _supabase(storage), apagar_do_banco=True, tenants=[tenant], avisar=lambda _: None)
    assert (rel.copiados, rel.apagados, len(rel.problemas)) == (0, 0, 2)
    assert await banco.ler(tenant, f"{obra}/a.jpg") == b"o do banco"
    assert f"{tenant}/{obra}/b.jpg" not in storage.objetos


async def test_migrar_acha_os_clientes_sozinho(arm):
    from canteiro_api.migrar_arquivos import migrar

    tenant, obra = _ids()
    await ArquivosNoBanco(arm).salvar(tenant, f"{obra}/a.jpg", b"x")
    assert tenant in await arm.tenants_da_colecao(COLECAO_INDICE)
    storage = StorageFalso()
    await migrar(arm, _supabase(storage), avisar=lambda _: None)        # sem lista: acha sozinho
    assert f"{tenant}/{obra}/a.jpg" in storage.objetos


async def test_apagar_ids_so_apaga_do_proprio_tenant(arm):
    tenant, obra = _ids()
    outro, _ = _ids()
    await ArquivosNoBanco(arm).salvar(tenant, f"{obra}/a.jpg", b"x")
    id_ = _id_arquivo(tenant, f"{obra}/a.jpg")
    await arm.apagar_ids(COLECAO_INDICE, outro, [id_])
    assert await arm.obter(COLECAO_INDICE, tenant, id_)
    with pytest.raises(ValueError):
        await arm.apagar_ids(COLECAO_INDICE, "", [id_])


async def test_migrar_termina_um_apagamento_que_parou_no_meio(arm):
    from canteiro_api.migrar_arquivos import migrar

    banco, storage = ArquivosNoBanco(arm), StorageFalso()
    tenant, obra = _ids()
    await banco.salvar(tenant, f"{obra}/a.jpg", b"conteudo")
    await migrar(arm, _supabase(storage), tenants=[tenant], avisar=lambda _: None)
    id_ = _id_arquivo(tenant, f"{obra}/a.jpg")
    await arm.apagar_ids(COLECAO_PEDACOS, tenant, [f"{id_}-000"])     # morreu depois dos pedaços, antes do índice
    rel = await migrar(arm, _supabase(storage), apagar_do_banco=True, tenants=[tenant], avisar=lambda _: None)
    assert (rel.copiados, rel.apagados, rel.problemas) == (1, 1, [])
    assert await arm.obter(COLECAO_INDICE, tenant, id_) is None


async def test_migrar_segue_quando_um_arquivo_falha_no_storage(arm):
    from canteiro_api.migrar_arquivos import migrar

    banco, storage = ArquivosNoBanco(arm), StorageFalso()
    tenant, obra = _ids()
    await banco.salvar(tenant, f"{obra}/a.jpg", b"um")
    await banco.salvar(tenant, f"{obra}/b.jpg", b"dois")
    storage.falhar_em.add(f"{tenant}/{obra}/a.jpg")
    rel = await migrar(arm, _supabase(storage), apagar_do_banco=True, tenants=[tenant], avisar=lambda _: None)
    assert (rel.copiados, rel.apagados, len(rel.problemas)) == (1, 1, 1) and "HTTP 500" in rel.problemas[0]
    assert await banco.ler(tenant, f"{obra}/a.jpg") == b"um"           # o que falhou continua no banco


async def test_supabase_envia_o_tipo_real_e_bucket_errado_nao_vira_arquivo_sumido():
    storage = StorageFalso()
    a = _supabase(storage)
    tenant, obra = _ids()
    for nome, tipo in [("a.jpg", "image/jpeg"), ("b.png", "image/png"), ("c.webp", "image/webp"),
                       ("d.pdf", "application/pdf")]:
        await a.salvar(tenant, f"{obra}/{nome}", b"x")                 # o bucket restrito aceita
        assert storage.pedidos[-1].headers["content-type"] == tipo
    errado = ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "apagado", transporte=httpx.MockTransport(storage))
    with pytest.raises(ErroArquivos, match="HTTP 404"):
        await errado.ler(tenant, f"{obra}/a.jpg")
    # o formato da doc atual põe o motivo só na mensagem
    antigo = httpx.Response(404, json={"code": "not_found", "message": "Bucket not found", "httpStatusCode": 404})
    sem_bucket = ArquivosSupabase(URL_FALSA, CHAVE_FALSA, "b", transporte=httpx.MockTransport(lambda _: antigo))
    with pytest.raises(ErroArquivos):
        await sem_bucket.ler(tenant, f"{obra}/a.jpg")


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
