"""Arquivos dos comprovantes no próprio banco: pedaços de 512 KiB, índice por último, tenant separado, integridade."""
import secrets

from canteiro_api.arquivos import COLECAO_INDICE, COLECAO_PEDACOS, PEDACO, ArquivosNoBanco, _id_arquivo


async def test_arquivo_grande_volta_igual_em_varios_pedacos(arm, tenant):
    a = ArquivosNoBanco(arm)
    conteudo = secrets.token_bytes(PEDACO * 2 + 123)
    await a.salvar(tenant, "obra/c1.jpg", conteudo)
    assert await a.ler(tenant, "obra/c1.jpg") == conteudo
    indice = await arm.obter(COLECAO_INDICE, tenant, _id_arquivo(tenant, "obra/c1.jpg"))
    assert (indice["pedacos"], indice["tamanho"]) == (3, len(conteudo))


async def test_pequeno_vazio_e_inexistente(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/p.png", b"\x89PNG")
    assert await a.ler(tenant, "obra/p.png") == b"\x89PNG"
    await a.salvar(tenant, "obra/vazio.pdf", b"")
    assert await a.ler(tenant, "obra/vazio.pdf") == b""
    assert await a.ler(tenant, "obra/nao-existe.jpg") is None


async def test_outro_tenant_nao_le(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/c1.jpg", b"meu comprovante")
    assert await a.ler(f"{tenant}-outro", "obra/c1.jpg") is None


async def test_salvar_de_novo_nao_duplica_nem_falha(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/c1.jpg", b"x" * 10)
    await a.salvar(tenant, "obra/c1.jpg", b"x" * 10)
    assert await a.ler(tenant, "obra/c1.jpg") == b"x" * 10


async def test_pedaco_adulterado_nao_vira_outra_foto(arm, tenant):
    a = ArquivosNoBanco(arm)
    await a.salvar(tenant, "obra/c1.jpg", b"original")
    await arm.atualizar(COLECAO_PEDACOS, tenant, f"{_id_arquivo(tenant, 'obra/c1.jpg')}-000", {"dados": b"trocado!"})
    assert await a.ler(tenant, "obra/c1.jpg") is None
