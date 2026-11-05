"""Contrato da porta `Arquivos`: o mesmo conjunto roda contra memória, disco e GridFS (Mongo do compose).

O que importa aqui é o isolamento: um cliente nunca lê o arquivo de outro, e caminho torto (travessia, outro tenant)
falha alto em vez de gravar ou ler fora do lugar.
"""
import secrets
import uuid

import pytest

from canteiro_api.arquivos import ArquivosDisco, ArquivosMemoria, criar_arquivos
from canteiro_api.config import Config


@pytest.fixture(params=["memoria", "disco", "mongo"])
def arquivos(request, tmp_path):
    if request.param == "memoria":
        return ArquivosMemoria()
    if request.param == "disco":
        return ArquivosDisco(str(tmp_path / "arquivos"))
    return request.getfixturevalue("_mongo_sessao").arquivos()


def _ids():
    return f"t-{uuid.uuid4().hex[:10]}", str(uuid.uuid4())


async def test_guarda_e_devolve_o_mesmo_conteudo(arquivos):
    tenant, obra = _ids()
    caminho, conteudo = f"{tenant}/{obra}/c1.jpg", secrets.token_bytes(300_000)   # mais de um pedaço no GridFS
    await arquivos.salvar(tenant, obra, caminho, conteudo, "image/jpeg")
    assert await arquivos.ler(tenant, caminho) == conteudo
    # mesmo caminho de novo (o nome sai do sha256: é o mesmo arquivo) não quebra nem muda nada
    await arquivos.salvar(tenant, obra, caminho, conteudo, "image/jpeg")
    assert await arquivos.ler(tenant, caminho) == conteudo
    assert await arquivos.ler(tenant, f"{tenant}/{obra}/nao-existe.jpg") is None


async def test_outro_tenant_nao_le(arquivos):
    tenant, obra = _ids()
    outro, _ = _ids()
    caminho = f"{tenant}/{obra}/c1.pdf"
    await arquivos.salvar(tenant, obra, caminho, b"%PDF-1.7 x", "application/pdf")
    # pedindo o caminho do outro cliente com o próprio tenant: nada, sem erro que revele que o arquivo existe
    assert await arquivos.ler(outro, caminho) is None
    # e trocando só o prefixo: é outro arquivo, que não existe
    assert await arquivos.ler(outro, f"{outro}/{obra}/c1.pdf") is None


@pytest.mark.parametrize("modelo", [
    "{t}/{o}/../../fora.jpg",
    "{t}/{o}/..",
    "{t}/../{o}/x.jpg",
    "/{t}/{o}/x.jpg",
    "{t}/{o}/sub/x.jpg",
    "{t}/{o}/a\\..\\b.jpg",
    "{t}/{o}/.oculto",
    "{t}//x.jpg",
])
async def test_caminho_torto_e_recusado(arquivos, modelo):
    tenant, obra = _ids()
    caminho = modelo.format(t=tenant, o=obra)
    with pytest.raises(ValueError):
        await arquivos.salvar(tenant, obra, caminho, b"x", "image/jpeg")
    if caminho.startswith(f"{tenant}/"):
        with pytest.raises(ValueError):
            await arquivos.ler(tenant, caminho)
    else:
        assert await arquivos.ler(tenant, caminho) is None


async def test_salvar_exige_tenant_e_obra_no_caminho(arquivos):
    tenant, obra = _ids()
    outro, outra_obra = _ids()
    with pytest.raises(ValueError):
        await arquivos.salvar(tenant, obra, f"{outro}/{obra}/x.jpg", b"x", "image/jpeg")
    with pytest.raises(ValueError):
        await arquivos.salvar(tenant, obra, f"{tenant}/{outra_obra}/x.jpg", b"x", "image/jpeg")
    with pytest.raises(ValueError):
        await arquivos.salvar("", obra, f"/{obra}/x.jpg", b"x", "image/jpeg")


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
        await disco.ler(tenant, f"{tenant}/{obra}/link.jpg")
    with pytest.raises(ValueError):
        await disco.salvar(tenant, obra, f"{tenant}/{obra}/link.jpg", b"x", "image/jpeg")
    assert (fora / "segredo.jpg").read_bytes() == b"de outro lugar"


async def test_gridfs_guarda_tenant_e_obra_nos_metadados(_mongo_sessao):
    tenant, obra = _ids()
    caminho = f"{tenant}/{obra}/c1.png"
    await _mongo_sessao.arquivos().salvar(tenant, obra, caminho, b"\x89PNG\r\n\x1a\nx", "image/png")
    await _mongo_sessao.arquivos().salvar(tenant, obra, caminho, b"\x89PNG\r\n\x1a\nx", "image/png")
    docs = [d async for d in _mongo_sessao._db["arquivos.files"].find({"filename": caminho})]
    assert len(docs) == 1
    assert docs[0]["metadata"] == {"tenant_id": tenant, "obra_id": obra, "tipo_conteudo": "image/png", "tamanho": 9}


# --- configuração: produção não sobe com arquivo em lugar que some ---

def _config(**kw):
    return Config(jwt_segredo=secrets.token_urlsafe(48), _env_file=None, **kw)


def test_padrao_e_gridfs():
    assert _config().arquivos == "mongo"


@pytest.mark.parametrize("arquivos", ["disco", "memoria"])
def test_producao_recusa_arquivo_efemero(arquivos):
    with pytest.raises(RuntimeError, match="ARQUIVOS"):
        _config(ambiente="producao", arquivos=arquivos).conferir()
    with pytest.raises(RuntimeError, match="ARQUIVOS"):
        _config(ambiente="producao", armazenamento="memoria").conferir()
    _config(ambiente="producao", arquivos="mongo").conferir()
    _config(arquivos=arquivos).conferir()                       # em desenvolvimento, pode


def test_ambiente_desconhecido_e_recusado():
    with pytest.raises(RuntimeError, match="AMBIENTE"):
        _config(ambiente="prod").conferir()


def test_gridfs_exige_o_mongo():
    from canteiro_api.armazenamento import ArmazenamentoMemoria

    with pytest.raises(RuntimeError, match="ARMAZENAMENTO=mongo"):
        criar_arquivos("mongo", "", ArmazenamentoMemoria())
    with pytest.raises(RuntimeError, match="desconhecido"):
        criar_arquivos("s3", "")


def test_app_com_mongo_usa_o_gridfs_do_mesmo_banco(_mongo_sessao, config):
    from canteiro_api.armazenamento.mongo import ArquivosMongo
    from canteiro_api.main import create_app

    app = create_app(config.model_copy(update={"arquivos": "mongo"}), _mongo_sessao)
    assert isinstance(app.state.arquivos, ArquivosMongo)
