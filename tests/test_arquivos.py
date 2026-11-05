"""Contrato da porta `Arquivos`: o mesmo conjunto roda contra memória, disco e banco (pedaços na porta de
armazenamento, em memória e no Mongo do compose).

O que importa aqui é o isolamento: um cliente nunca lê o arquivo de outro, e caminho torto (travessia, subpasta,
tenant estranho) falha alto em vez de gravar ou ler fora do lugar. No banco, também a integridade: pedaço trocado ou
faltando não vira outra foto.
"""
import secrets
import uuid

import pytest

from canteiro_api.arquivos import (
    COLECAO_INDICE,
    COLECAO_PEDACOS,
    MAX_TAMANHO,
    PEDACO,
    ArquivosDisco,
    ArquivosMemoria,
    ArquivosNoBanco,
    _id_arquivo,
    criar_arquivos,
)
from canteiro_api.config import Config


@pytest.fixture(params=["memoria", "disco", "banco"])
def arquivos(request, tmp_path, arm):
    if request.param == "memoria":
        return ArquivosMemoria()
    if request.param == "disco":
        return ArquivosDisco(str(tmp_path / "arquivos"))
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


# --- configuração: produção não sobe com arquivo em lugar que some ---

def _config(**kw):
    return Config(jwt_segredo=secrets.token_urlsafe(48), _env_file=None, **kw)


def test_padrao_e_o_banco():
    assert _config().arquivos == "banco"


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
    with pytest.raises(RuntimeError, match="armazenamento"):
        criar_arquivos("banco", "")
    with pytest.raises(RuntimeError, match="desconhecido"):
        criar_arquivos("mongo", "")


def test_app_guarda_os_arquivos_no_mesmo_armazenamento(arm, config):
    from canteiro_api.main import create_app

    app = create_app(config.model_copy(update={"arquivos": "banco"}), arm)
    assert isinstance(app.state.arquivos, ArquivosNoBanco)
