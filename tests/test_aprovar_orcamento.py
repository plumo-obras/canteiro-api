"""Aprovar o orçamento fixa o orçado; o Desfazer reabre. Editar a linha do orçamento, antes e depois da aprovação."""
from conftest import nova_vila
from test_obras_orcamento import nova_obra, registro, xlsx
from test_orcamento_versoes import CABECALHO, CONTRATO, mapa, orcamento, versao

from canteiro_dominio.modelos import DESFAZENDO_APROVACAO
from canteiro_dominio.tipos import qtd

ETAPA = ["1", None, None, "FUNDAÇÕES", None, None, None, None]
TAPUME = xlsx(CABECALHO, ["1", None, None, "Serviços preliminares", None, None, None, None],
              ["1.14", "74220/1", "SINAPI", "Tapume de chapa de madeira", "m²", 436, 45.23, 19720.28])
SEM_PRECO = xlsx(CABECALHO, ETAPA, ["1.1", "1", "S", "Areia média", "m³", 10, 0, 0],
                 ["1.2", "2", "S", "Brita", "m³", 5, 100, 500])


async def _contrato(vila, obra, conteudo=CONTRATO, m=None):
    r = await versao(vila, obra, conteudo, m or mapa(), aplicar=True)
    assert r.status_code == 201, r.text[:300]
    return r.json()


async def _aprovar(vila, obra):
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    return r.json()


async def _desfazer(vila, registro_id):
    return await vila.http.post(f"/registro/{registro_id}/desfazer", headers=vila.h)


async def _obra(vila, obra):
    return next(x for x in (await vila.http.get("/obras", headers=vila.h)).json() if x["id"] == obra)


# --- aprovar ---

async def test_aprovar_fixa_o_orcamento_e_o_desfazer_reabre(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    assert (a["status"], a["aprovada"]) == ("em_andamento", True)
    assert "insumos_criados" not in a
    reg = await registro(vila, a["registro_id"])
    assert reg["texto"].startswith("Aprovou o orçamento de ") and "itens, R$ 21.075,96)" in reg["texto"]
    assert reg["desfazer"] == {"op": "reabrir_orcamento", "obra_id": obra}
    assert (await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)).status_code == 409

    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    o = await _obra(vila, obra)
    assert (o["status"], o["aprovada"]) == ("em_orcamento", False)
    assert (await _aprovar(vila, obra))["aprovada"] is True


async def test_aprovar_sem_orcamento_e_recusado(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 422 and "Importe o orçamento" in r.json()["detail"]
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=h)).status_code == 403


async def test_obra_parada_no_meio_do_desfazer_antigo_volta_para_o_orcamento(vila):
    """O Desfazer da aprovação rodava em lotes e podia parar no meio; a obra que ficou assim termina com um Desfazer."""
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    await vila.arm.atualizar("obras", vila.tenant_id, obra,
                             {"status": DESFAZENDO_APROVACAO, "desfazendo_registro_id": a["registro_id"]})
    r = await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 409 and "parou no meio" in r.json()["detail"]
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    o = await _obra(vila, obra)
    assert (o["status"], o["desfazendo_registro_id"]) == ("em_orcamento", None)


async def test_aditivo_com_a_obra_aprovada_soma_e_o_desfazer_tira(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await _aprovar(vila, obra)
    r = await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text
    assert (await orcamento(vila, obra))["totais"]["aditivos"] == 1972028
    reg = await registro(vila, r.json()["registro_id"])
    assert "compras" not in reg["desfazer"]
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 200
    assert (await orcamento(vila, obra))["totais"]["aditivos"] == 0


async def test_trocar_contrato_depois_de_aprovado_e_recusado(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    r = await versao(vila, obra, CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 409 and "aditivo" in r.json()["detail"]
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert (await versao(vila, obra, CONTRATO, mapa(), aplicar=True)).status_code == 201


async def test_retirar_o_contrato_com_a_obra_aprovada_e_recusado(vila):
    obra = (await nova_obra(vila))["id"]
    c = await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    r = await _desfazer(vila, c["registro_id"])
    assert r.status_code == 409 and "aprovação" in r.json()["detail"]
    assert (await orcamento(vila, obra))["totais"]["contratado"] > 0
    # desfeita a aprovação, o contrato sai
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert (await _desfazer(vila, c["registro_id"])).status_code == 200


async def test_obra_antiga_que_nunca_foi_aprovada_ainda_importa_e_retira_o_contrato(vila):
    # o seed fica em_andamento sem ter passado pelo Aprovar, como as obras de antes desse fluxo
    assert (await _obra(vila, vila.obra))["aprovada"] is False
    c = await _contrato(vila, vila.obra)
    assert (await _desfazer(vila, c["registro_id"])).status_code == 200


# --- editar a linha do orçamento ---

async def _item(vila, obra, descricao):
    return next(x for x in (await orcamento(vila, obra))["itens"] if x["descricao"] == descricao)


def _url(obra, item_id):
    return f"/obras/{obra}/orcamento/itens/{item_id}"


async def test_editar_preco_e_quantidade_com_desfazer_e_refazer(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    corpo = {"valor_unitario": 12000, "quantidade": qtd(2)}
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json=corpo)
    assert r.status_code == 200, r.text
    assert (r.json()["item"]["total"], r.json()["total_versao"]) == (24000, 74000)
    orc = await orcamento(vila, obra)
    assert orc["totais"]["atual"] == 74000 and orc["versoes"][0]["total"] == 74000
    reg = await registro(vila, r.json()["registro_id"])
    assert "Areia média" in reg["texto"] and "R$ 120,00" in reg["texto"]
    assert "insumo" not in reg["desfazer"]

    d = await _desfazer(vila, r.json()["registro_id"])
    assert d.status_code == 200, d.text
    assert (await orcamento(vila, obra))["totais"]["atual"] == 50000
    item = await _item(vila, obra, "Areia média")
    assert (item["quantidade"], item["valor_unitario"], item["total"]) == (qtd(10), 0, 0)
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 409
    # refazer é desfazer o desfazer
    assert (await _desfazer(vila, d.json()["id"])).status_code == 200
    assert (await orcamento(vila, obra))["totais"]["atual"] == 74000


async def test_desfazer_edicao_gravada_com_o_preco_do_insumo(vila):
    """A receita de antes de Compras sair guardava o preço do insumo: o Desfazer volta só a linha do orçamento."""
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 9000})
    reg = await registro(vila, r.json()["registro_id"])
    await vila.arm.atualizar("registro", vila.tenant_id, reg["id"], {"desfazer": {
        **reg["desfazer"], "insumo": {"id": "insumo-que-nao-existe-mais", "antes": 0, "depois": 9000}}})
    d = await _desfazer(vila, reg["id"])
    assert d.status_code == 200, d.text
    assert (await _item(vila, obra, "Areia média"))["valor_unitario"] == 0
    assert "insumo" not in (await registro(vila, d.json()["id"]))["desfazer"]


async def test_editar_recusa_o_que_nao_vale(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    titulo = next(x for x in (await orcamento(vila, obra))["itens"] if x["tipo"] == "etapa")
    for item_id, corpo, status in [
        (areia["id"], {}, 422), (areia["id"], {"quantidade": 0}, 422), (areia["id"], {"quantidade": -1000}, 422),
        (areia["id"], {"valor_unitario": -1}, 422), (areia["id"], {"valor_unitario": 0}, 422),
        (areia["id"], {"total": 5}, 422), (titulo["id"], {"valor_unitario": 100}, 422),
        ("nao-existe", {"valor_unitario": 100}, 404),
    ]:
        assert (await vila.http.patch(_url(obra, item_id), headers=vila.h, json=corpo)).status_code == status, corpo
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.patch(_url(obra, areia["id"]), headers=h, json={"valor_unitario": 1})).status_code == 403


async def test_editar_outro_tenant_e_404(vila, http, arm, config):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    intruso = await nova_vila(http, arm, config)
    r = await intruso.http.patch(_url(obra, areia["id"]), headers=intruso.h, json={"valor_unitario": 1})
    assert r.status_code == 404
    r = await intruso.http.patch(_url(intruso.obra, areia["id"]), headers=intruso.h, json={"valor_unitario": 1})
    assert r.status_code == 404
    assert (await _item(vila, obra, "Areia média"))["valor_unitario"] == 0


async def test_aditivo_aceita_supressao_na_edicao(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    tapume = await _item(vila, obra, "Tapume de chapa de madeira")
    r = await vila.http.patch(_url(obra, tapume["id"]), headers=vila.h, json={"quantidade": -qtd(10)})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["total"] == -45230


async def test_depois_de_aprovado_so_completa_preco_em_branco(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    a = await _aprovar(vila, obra)
    areia, brita = await _item(vila, obra, "Areia média"), await _item(vila, obra, "Brita")
    # item com preço e quantidade: é aditivo
    assert (await vila.http.patch(_url(obra, brita["id"]), headers=vila.h,
                                  json={"valor_unitario": 200})).status_code == 409
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"quantidade": qtd(1)})
    assert r.status_code == 409 and "aditivo" in r.json()["detail"]

    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 9000})
    assert r.status_code == 200, r.text
    assert (await orcamento(vila, obra))["totais"]["atual"] == 140000
    # agora tem preço: a exceção era só para o que estava em branco
    assert (await vila.http.patch(_url(obra, areia["id"]), headers=vila.h,
                                  json={"valor_unitario": 9100})).status_code == 409
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 200
    assert (await _item(vila, obra, "Areia média"))["valor_unitario"] == 0
    # completar o preço não atrapalha o Desfazer da aprovação
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200


async def test_edicao_de_antes_da_aprovacao_nao_se_desfaz_depois(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    e = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 500})
    await _aprovar(vila, obra)
    assert (await _desfazer(vila, e.json()["registro_id"])).status_code == 409
