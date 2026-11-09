"""Aprovar o orçamento gera Compras; aditivo com a obra aprovada mexe nelas; editar a linha do orçamento."""
import uuid
from datetime import UTC, datetime

import pytest
from conftest import nova_vila
from test_obras_orcamento import importar, nova_obra, registro, xlsx
from test_orcamento_versoes import ADITIVO, CABECALHO, CONTRATO, mapa, orcamento, versao

from canteiro_api.nucleo import Conflito
from canteiro_api.servicos import compras, obras, versoes
from canteiro_dominio.tipos import qtd, reais

ETAPA = ["1", None, None, "FUNDAÇÕES", None, None, None, None]
TAPUME = xlsx(CABECALHO, ["1", None, None, "PRELIMINARES", None, None, None, None],
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


async def _insumos(vila, obra):
    r = await vila.http.get(f"/obras/{obra}/insumos", headers=vila.h)
    assert r.status_code == 200, r.text
    return {x["insumo"]["nome"]: x for x in r.json()}


async def _desfazer(vila, registro_id):
    return await vila.http.post(f"/registro/{registro_id}/desfazer", headers=vila.h)


async def _status(vila, obra):
    return next(x for x in (await vila.http.get("/obras", headers=vila.h)).json() if x["id"] == obra)["status"]


async def _pedido(vila, obra, insumo_id, etapa="2"):
    r = await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h, json={
        "fornecedor": "Depósito", "aprovado": True,
        "linhas": [{"insumo_id": insumo_id, "quantidade": qtd(1), "valor_unitario": 100, "etapa": etapa}]})
    assert r.status_code == 201, r.text
    return r.json()


# --- aprovar ---

async def test_aprovar_gera_um_insumo_por_item_com_etapa_e_preco(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    assert await _insumos(vila, obra) == {}           # antes de aprovar, Compras está vazia
    r = await _aprovar(vila, obra)
    assert (r["status"], r["insumos_criados"], r["insumos_atualizados"]) == ("em_andamento", 4, 0)
    ins = await _insumos(vila, obra)
    # "Escavação" está em duas subetapas da etapa 2: um insumo só, somado
    esc = ins["Escavação manual de valas"]
    assert esc["insumo"]["precisa_ate_o_fim"] == qtd("299,96")
    assert (esc["insumo"]["etapa"], esc["insumo"]["tipo"]) == ("2", "servico")
    assert esc["insumo"]["valor_unitario"] == reais("57,12")
    assert esc["conta"]["comprar"] == qtd("299,96")
    assert ins["Placa de obra em chapa galvanizada"]["insumo"]["etapa"] == "1"
    assert {x["insumo"]["curva"] for x in ins.values()} <= {"A", "B", "C"}


async def test_o_que_veio_das_pranchas_e_material_e_o_pedido_sai_na_etapa_do_insumo(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "itens": [{"nome": "Aço CA-50", "unidade": "kg", "quantidade": qtd(100), "valor_unitario": 669}],
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    await _aprovar(vila, obra)
    aco = (await _insumos(vila, obra))["Aço CA-50"]["insumo"]
    assert aco["tipo"] == "material" and aco["etapa"]
    p = await _pedido(vila, obra, aco["id"], etapa=aco["etapa"])
    assert p["linhas"][0]["etapa"] == aco["etapa"]


async def test_insumo_ja_cadastrado_nao_duplica(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h, json={
        "nome": "brita", "unidade": "M³", "precisa_ate_o_fim": qtd(20), "valor_unitario": 7700})
    assert r.status_code == 201, r.text
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    a = await _aprovar(vila, obra)
    assert (a["insumos_criados"], a["insumos_atualizados"]) == (1, 1)
    ins = await _insumos(vila, obra)
    assert set(ins) == {"brita", "Areia média"}
    # fica o maior precisa e o preço que já estava; a etapa, que ele não tinha, vem do orçamento
    assert ins["brita"]["insumo"]["precisa_ate_o_fim"] == qtd(20)
    assert (ins["brita"]["insumo"]["valor_unitario"], ins["brita"]["insumo"]["etapa"]) == (7700, "1")
    # o Desfazer devolve o que ele era e arquiva só o gerado
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    ins = await _insumos(vila, obra)
    assert set(ins) == {"brita"} and ins["brita"]["insumo"]["etapa"] is None


async def test_unidade_diferente_recusa_e_nao_aprova(vila):
    obra = (await nova_obra(vila))["id"]
    await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h, json={"nome": "Brita", "unidade": "kg"})
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 422 and "Brita" in r.json()["detail"]
    assert await _status(vila, obra) == "em_orcamento"
    assert set(await _insumos(vila, obra)) == {"Brita"}


async def test_orcamento_grande_entra_em_lotes(vila, monkeypatch):
    monkeypatch.setattr(compras, "INSUMOS_POR_LOTE", 1)
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    assert a["insumos_criados"] == 4 and len(await _insumos(vila, obra)) == 4
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert await _insumos(vila, obra) == {}


async def test_queda_antes_de_virar_o_status_e_repetivel(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    original = obras.preparar_aprovar_orcamento

    async def falha(*a, **k):
        raise RuntimeError("queda simulada")

    monkeypatch.setattr(obras, "preparar_aprovar_orcamento", falha)
    with pytest.raises(RuntimeError):
        await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert await _status(vila, obra) == "em_orcamento"
    monkeypatch.setattr(obras, "preparar_aprovar_orcamento", original)
    a = await _aprovar(vila, obra)
    # a repetição reconhece o que o lote já gravou: nada duplica, e o Desfazer os leva todos
    assert len(await _insumos(vila, obra)) == 4
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert await _insumos(vila, obra) == {}


# --- desfazer a aprovação ---

async def test_desfazer_arquiva_e_reaprovar_reaproveita(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert await _status(vila, obra) == "em_orcamento" and await _insumos(vila, obra) == {}
    assert (await _desfazer(vila, a["registro_id"])).status_code == 409
    b = await _aprovar(vila, obra)
    assert b["insumos_criados"] == 4 and len(await _insumos(vila, obra)) == 4


async def test_desfazer_recusa_se_algum_teve_pedido(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    esc = (await _insumos(vila, obra))["Escavação manual de valas"]["insumo"]
    p = await _pedido(vila, obra, esc["id"])
    r = await _desfazer(vila, a["registro_id"])
    assert r.status_code == 409 and "Escavação" in r.json()["detail"] and p["numero"] in r.json()["detail"]
    assert await _status(vila, obra) == "em_andamento" and len(await _insumos(vila, obra)) == 4
    # pedido cancelado não segura
    c = await vila.http.post(f"/pedidos/{p['id']}/avancar", headers=vila.h, json={"para": "cancelado"})
    assert c.status_code == 200, c.text
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200


async def test_desfazer_recusa_se_algum_teve_movimento(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "itens": [{"nome": "Aço", "unidade": "kg", "quantidade": qtd(100), "valor_unitario": 669}],
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    a = await _aprovar(vila, obra)
    aco = (await _insumos(vila, obra))["Aço"]["insumo"]
    m = await vila.http.post(f"/obras/{obra}/movimentos", headers=vila.h, json={
        "insumo_id": aco["id"], "tipo": "entrada", "quantidade": qtd(1), "chave_idempotencia": uuid.uuid4().hex})
    assert m.status_code == 201, m.text
    d = await _desfazer(vila, a["registro_id"])
    assert d.status_code == 409 and "Aço" in d.json()["detail"]
    assert await _status(vila, obra) == "em_andamento"


# --- depois de aprovado: aditivo e troca de contrato ---

async def test_aditivo_com_a_obra_aprovada_mexe_em_compras_e_o_desfazer_volta(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    antes = await _insumos(vila, obra)
    r = await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text[:300]
    ins = await _insumos(vila, obra)
    assert ins["Tapume de chapa de madeira"]["insumo"]["precisa_ate_o_fim"] == qtd(436)
    assert ins["Tapume de chapa de madeira"]["insumo"]["etapa"] == "1"

    # o Desfazer da aprovação espera o do aditivo
    d = await _desfazer(vila, a["registro_id"])
    assert d.status_code == 409 and "aditivo" in d.json()["detail"]

    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 200
    assert set(await _insumos(vila, obra)) == set(antes)
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200


async def test_aditivo_com_unidade_diferente_e_recusado_sem_gravar(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await _aprovar(vila, obra)
    antes = await orcamento(vila, obra)
    # o ADITIVO traz o aço em m², e no contrato ele está em kg
    r = await versao(vila, obra, ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo", aplicar=True)
    assert r.status_code == 422 and "Armação" in r.json()["detail"]
    assert (await orcamento(vila, obra))["totais"]["atual"] == antes["totais"]["atual"]


async def test_mesmo_item_em_duas_unidades_no_orcamento_trava_a_aprovacao(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await versao(vila, obra, ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo", aplicar=True)
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 422 and "Armação" in r.json()["detail"]
    assert await _status(vila, obra) == "em_orcamento" and await _insumos(vila, obra) == {}


async def test_aditivo_soma_ao_que_ja_esta_em_compras_e_supressao_reduz(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    await _aprovar(vila, obra)
    plan = xlsx(CABECALHO, ETAPA, ["1.3", "2", "S", "Brita", "m³", 2, 100, 200],
                ["1.4", "1", "S", "Areia média", "m³", -4, 50, 200])
    r = await versao(vila, obra, plan, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text[:300]
    ins = await _insumos(vila, obra)
    assert ins["Brita"]["insumo"]["precisa_ate_o_fim"] == qtd(7)
    assert ins["Areia média"]["insumo"]["precisa_ate_o_fim"] == qtd(6)
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 200
    ins = await _insumos(vila, obra)
    assert ins["Brita"]["insumo"]["precisa_ate_o_fim"] == qtd(5)
    assert ins["Areia média"]["insumo"]["precisa_ate_o_fim"] == qtd(10)


async def test_aditivo_antes_de_aprovar_so_entra_na_aprovacao(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    r = await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text[:300]
    assert await _insumos(vila, obra) == {}
    await _aprovar(vila, obra)
    assert "Tapume de chapa de madeira" in await _insumos(vila, obra)


async def test_trocar_contrato_depois_de_aprovado_e_recusado(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    r = await versao(vila, obra, CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 409 and "aditivo" in r.json()["detail"]
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert (await versao(vila, obra, CONTRATO, mapa(), aplicar=True)).status_code == 201


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

    d = await _desfazer(vila, r.json()["registro_id"])
    assert d.status_code == 200, d.text
    assert (await orcamento(vila, obra))["totais"]["atual"] == 50000
    item = await _item(vila, obra, "Areia média")
    assert (item["quantidade"], item["valor_unitario"], item["total"]) == (qtd(10), 0, 0)
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 409
    # refazer é desfazer o desfazer
    assert (await _desfazer(vila, d.json()["id"])).status_code == 200
    assert (await orcamento(vila, obra))["totais"]["atual"] == 74000


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
    await _aprovar(vila, obra)
    areia, brita = await _item(vila, obra, "Areia média"), await _item(vila, obra, "Brita")
    # item com preço e quantidade: é aditivo
    assert (await vila.http.patch(_url(obra, brita["id"]), headers=vila.h,
                                  json={"valor_unitario": 200})).status_code == 409
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"quantidade": qtd(1)})
    assert r.status_code == 409 and "aditivo" in r.json()["detail"]

    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 9000})
    assert r.status_code == 200, r.text
    assert (await _insumos(vila, obra))["Areia média"]["insumo"]["valor_unitario"] == 9000
    assert (await orcamento(vila, obra))["totais"]["atual"] == 140000
    # agora tem preço: a exceção era só para o que estava em branco
    assert (await vila.http.patch(_url(obra, areia["id"]), headers=vila.h,
                                  json={"valor_unitario": 9100})).status_code == 409
    assert (await _desfazer(vila, r.json()["registro_id"])).status_code == 200
    assert (await _insumos(vila, obra))["Areia média"]["insumo"]["valor_unitario"] == 0


async def test_completar_preco_nao_atrapalha_o_desfazer_da_aprovacao(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    a = await _aprovar(vila, obra)
    areia = await _item(vila, obra, "Areia média")
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 9000})
    assert r.status_code == 200, r.text
    d = await _desfazer(vila, a["registro_id"])
    assert d.status_code == 200, d.text
    assert await _insumos(vila, obra) == {}


async def test_edicao_de_antes_da_aprovacao_nao_se_desfaz_depois(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    areia = await _item(vila, obra, "Areia média")
    e = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 500})
    await _aprovar(vila, obra)
    assert (await _desfazer(vila, e.json()["registro_id"])).status_code == 409


async def test_preco_de_insumo_cadastrado_a_mao_fica(vila):
    obra = (await nova_obra(vila))["id"]
    await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h, json={
        "nome": "Areia média", "unidade": "m³", "precisa_ate_o_fim": qtd(1), "valor_unitario": 7000})
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    await _aprovar(vila, obra)
    areia = await _item(vila, obra, "Areia média")
    r = await vila.http.patch(_url(obra, areia["id"]), headers=vila.h, json={"valor_unitario": 9000})
    assert r.status_code == 200, r.text
    assert (await _insumos(vila, obra))["Areia média"]["insumo"]["valor_unitario"] == 7000


async def test_servico_entregue_nao_volta_para_falta_comprar(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await _aprovar(vila, obra)
    esc = (await _insumos(vila, obra))["Escavação manual de valas"]["insumo"]
    assert esc["tipo"] == "servico"
    linha = {"insumo_id": esc["id"], "quantidade": esc["precisa_ate_o_fim"], "valor_unitario": 5712, "etapa": "2"}
    r = await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h, json={
        "fornecedor": "Empreiteira", "aprovado": True, "linhas": [linha]})
    assert r.status_code == 201, r.text
    p = r.json()
    e = await vila.http.post(f"/pedidos/{p['id']}/recebimentos", headers=vila.h, json={
        "linhas": [{"indice": 0, "quantidade": esc["precisa_ate_o_fim"]}], "chave_idempotencia": uuid.uuid4().hex})
    assert e.status_code == 201, e.text
    assert e.json()["pedido"]["status"] == "conferido"
    conta = (await _insumos(vila, obra))["Escavação manual de valas"]["conta"]
    assert (conta["comprar"], conta["saldo"], conta["medido"]) == (0, 0, esc["precisa_ate_o_fim"])
    assert "medido" in conta["texto"]
    lista = (await vila.http.get("/obras/resumo", headers=vila.h)).json()
    # as outras 3 linhas seguem faltando; a entregue não
    assert next(x for x in lista if x["obra"]["id"] == obra)["falta_comprar"] == 3


# --- Desfazer da aprovação pela metade ---

async def _obra(vila, obra):
    return next(x for x in (await vila.http.get("/obras", headers=vila.h)).json() if x["id"] == obra)


async def _para_no_segundo_lote(vila, obra, monkeypatch):
    """Aprova e derruba o Desfazer depois do 1º lote: um insumo revertido, três ainda em Compras."""
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    monkeypatch.setattr(compras, "INSUMOS_POR_LOTE", 1)
    original, lotes = compras._reverter_lote, []

    def quebra(*args):
        lotes.append(1)
        if len(lotes) == 2:
            raise RuntimeError("queda simulada")
        return original(*args)

    monkeypatch.setattr(compras, "_reverter_lote", quebra)
    with pytest.raises(RuntimeError):
        await _desfazer(vila, a["registro_id"])
    monkeypatch.setattr(compras, "_reverter_lote", original)
    return a


async def test_queda_no_meio_do_desfazer_fica_marcada_e_continua(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    a = await _para_no_segundo_lote(vila, obra, monkeypatch)
    o = await _obra(vila, obra)
    assert (o["status"], o["desfazendo_registro_id"]) == ("desfazendo_aprovacao", a["registro_id"])
    assert len(await _insumos(vila, obra)) == 3
    assert (await registro(vila, a["registro_id"]))["desfeito"] is False

    r = await _desfazer(vila, a["registro_id"])
    assert r.status_code == 200, r.text
    o = await _obra(vila, obra)
    assert (o["status"], o["desfazendo_registro_id"]) == ("em_orcamento", None)
    assert await _insumos(vila, obra) == {}
    assert (await _desfazer(vila, a["registro_id"])).status_code == 409
    # e a obra volta a ser aprovável, com tudo de novo em Compras
    assert (await _aprovar(vila, obra))["insumos_criados"] == 4


async def test_pela_metade_nada_novo_entra_em_compras(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await _para_no_segundo_lote(vila, obra, monkeypatch)
    ins = await _insumos(vila, obra)
    i = next(iter(ins.values()))["insumo"]
    item = (await orcamento(vila, obra))["itens"][1]
    h = vila.h
    for r in [
        await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=h),
        await vila.http.post(f"/obras/{obra}/pedidos", headers=h, json={"fornecedor": "F", "aprovado": True, "linhas": [
            {"insumo_id": i["id"], "quantidade": qtd(1), "valor_unitario": 1, "etapa": "1"}]}),
        await vila.http.post(f"/obras/{obra}/movimentos", headers=h, json={
            "insumo_id": i["id"], "tipo": "ajuste", "quantidade": qtd(1), "chave_idempotencia": uuid.uuid4().hex}),
        await vila.http.post(f"/obras/{obra}/insumos", headers=h, json={"nome": "Novo", "unidade": "un"}),
        await vila.http.patch(_url(obra, item["id"]), headers=h, json={"valor_unitario": 1}),
        await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True),
    ]:
        assert r.status_code == 409 and "parou no meio" in r.json()["detail"], r.text
    assert len(await _insumos(vila, obra)) == 3


async def test_409_no_meio_do_desfazer_se_resolve_e_continua(vila, arm, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    a = await _para_no_segundo_lote(vila, obra, monkeypatch)
    resta = next(iter((await _insumos(vila, obra)).values()))["insumo"]
    # um movimento que escapou da marca
    await arm.atualizar("saldos", vila.tenant_id, resta["id"], {"saldo": qtd(1), "atualizado_em": datetime.now(UTC)})
    r = await _desfazer(vila, a["registro_id"])
    assert r.status_code == 409 and resta["nome"] in r.json()["detail"]
    assert (await _obra(vila, obra))["status"] == "desfazendo_aprovacao"
    assert (await registro(vila, a["registro_id"]))["desfeito"] is False

    await arm.atualizar("saldos", vila.tenant_id, resta["id"], {"saldo": 0})    # resolvido
    r = await _desfazer(vila, a["registro_id"])
    assert r.status_code == 200, r.text
    assert (await _obra(vila, obra))["status"] == "em_orcamento" and await _insumos(vila, obra) == {}


async def test_conferencia_que_recusa_na_primeira_rodada_nao_marca_a_obra(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    a = await _aprovar(vila, obra)
    esc = (await _insumos(vila, obra))["Escavação manual de valas"]["insumo"]
    await _pedido(vila, obra, esc["id"])
    assert (await _desfazer(vila, a["registro_id"])).status_code == 409
    o = await _obra(vila, obra)
    assert (o["status"], o["desfazendo_registro_id"]) == ("em_andamento", None)
    assert len(await _insumos(vila, obra)) == 4


async def test_estornar_o_movimento_libera_o_desfazer(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "itens": [{"nome": "Aço", "unidade": "kg", "quantidade": qtd(100), "valor_unitario": 669}],
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    a = await _aprovar(vila, obra)
    aco = (await _insumos(vila, obra))["Aço"]["insumo"]
    m = await vila.http.post(f"/obras/{obra}/movimentos", headers=vila.h, json={
        "insumo_id": aco["id"], "tipo": "entrada", "quantidade": qtd(1), "chave_idempotencia": uuid.uuid4().hex})
    assert m.status_code == 201, m.text
    assert (await _desfazer(vila, a["registro_id"])).status_code == 409
    assert (await _desfazer(vila, m.json()["registro_id"])).status_code == 200
    d = await _desfazer(vila, a["registro_id"])
    assert d.status_code == 200, d.text
    assert await _insumos(vila, obra) == {}


async def test_entrada_e_saida_que_se_anulam_sem_estorno_seguram_o_desfazer(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "itens": [{"nome": "Aço", "unidade": "kg", "quantidade": qtd(100), "valor_unitario": 669}],
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    a = await _aprovar(vila, obra)
    aco = (await _insumos(vila, obra))["Aço"]["insumo"]
    for tipo, extra in (("entrada", {}), ("saida", {"destino": "bloco A", "etapa": aco["etapa"]})):
        corpo = {"insumo_id": aco["id"], "tipo": tipo, "quantidade": qtd(1), "chave_idempotencia": uuid.uuid4().hex}
        m = await vila.http.post(f"/obras/{obra}/movimentos", headers=vila.h, json={**corpo, **extra})
        assert m.status_code == 201, m.text
    assert (await _desfazer(vila, a["registro_id"])).status_code == 409


# --- achados da revisão: troca de contrato, aprovação abandonada e corrida com aditivo ---

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


async def test_importar_contrato_com_a_obra_aprovada_sem_vigente_e_recusado(vila):
    obra = (await nova_obra(vila))["id"]
    plan = xlsx(["Descrição", "Un", "Qtd", "Unit"], ["Cal", "sc", 10, 20])
    await importar(vila, plan, {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2,
                                "valor_unitario": 3}, aplicar=True, obra=obra)
    await _aprovar(vila, obra)
    r = await versao(vila, obra, CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 409 and "desfaça a aprovação" in r.json()["detail"]


async def test_obra_antiga_que_nunca_foi_aprovada_ainda_importa_e_retira_o_contrato(vila):
    # o seed fica em_andamento sem ter passado pelo Aprovar, como as obras de antes desse fluxo
    assert (await _obra(vila, vila.obra))["aprovada"] is False
    c = await _contrato(vila, vila.obra)
    assert (await _desfazer(vila, c["registro_id"])).status_code == 200


async def _falha_na_final(vila, obra, monkeypatch):
    """Os lotes gravam, mas a transação final não vira o status: a aprovação fica abandonada."""
    original = obras.preparar_aprovar_orcamento

    async def falha(*a, **k):
        raise Conflito("O orçamento mudou agora há pouco; confira e aprove de novo.")

    monkeypatch.setattr(obras, "preparar_aprovar_orcamento", falha)
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 409, r.text
    monkeypatch.setattr(obras, "preparar_aprovar_orcamento", original)


async def _tem_orcamento(vila, obra):
    lista = (await vila.http.get("/obras/resumo", headers=vila.h)).json()
    return next(x for x in lista if x["obra"]["id"] == obra)


async def test_aprovacao_abandonada_nao_aparece_em_compras(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    c = await _contrato(vila, obra)
    await _falha_na_final(vila, obra, monkeypatch)
    assert await _insumos(vila, obra) == {}
    assert (await _tem_orcamento(vila, obra))["falta_comprar"] == 0
    # sem o contrato, as sobras não contam como orçamento nem deixam aprovar
    assert (await _desfazer(vila, c["registro_id"])).status_code == 200
    assert (await _tem_orcamento(vila, obra))["tem_orcamento"] is False
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 422


async def test_reaprovar_depois_de_edicao_nao_deixa_orfaos(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await _falha_na_final(vila, obra, monkeypatch)
    item = await _item(vila, obra, "Sanitário em canteiro")
    r = await vila.http.patch(_url(obra, item["id"]), headers=vila.h, json={"quantidade": qtd(3)})
    assert r.status_code == 200, r.text
    a = await _aprovar(vila, obra)            # outra aprovação: a edição mudou o ID dela
    assert (a["insumos_criados"], a["insumos_atualizados"]) == (4, 0)
    assert (await _insumos(vila, obra))["Sanitário em canteiro"]["insumo"]["precisa_ate_o_fim"] == qtd(3)
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert await _insumos(vila, obra) == {}


async def test_desistir_e_reimportar_nao_deixa_sobra_em_compras(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    c = await _contrato(vila, obra)
    await _falha_na_final(vila, obra, monkeypatch)
    assert (await _desfazer(vila, c["registro_id"])).status_code == 200
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    a = await _aprovar(vila, obra)
    assert (a["insumos_criados"], a["insumos_atualizados"]) == (2, 0)
    assert set(await _insumos(vila, obra)) == {"Areia média", "Brita"}
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    assert await _insumos(vila, obra) == {}


async def test_insumo_ja_cadastrado_volta_ao_original_depois_de_tentativa_abandonada(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h, json={
        "nome": "Brita", "unidade": "m³", "precisa_ate_o_fim": qtd(1), "valor_unitario": 0})
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    await _falha_na_final(vila, obra, monkeypatch)
    # enquanto a tentativa está abandonada, ele aparece como era
    assert (await _insumos(vila, obra))["Brita"]["insumo"]["precisa_ate_o_fim"] == qtd(1)
    item = await _item(vila, obra, "Areia média")
    await vila.http.patch(_url(obra, item["id"]), headers=vila.h, json={"valor_unitario": 500})
    a = await _aprovar(vila, obra)
    assert (a["insumos_criados"], a["insumos_atualizados"]) == (1, 1)
    assert (await _insumos(vila, obra))["Brita"]["insumo"]["precisa_ate_o_fim"] == qtd(5)
    assert (await _desfazer(vila, a["registro_id"])).status_code == 200
    brita = (await _insumos(vila, obra))["Brita"]["insumo"]
    assert (brita["precisa_ate_o_fim"], brita["valor_unitario"]) == (qtd(1), 0)


async def test_cadastrar_o_mesmo_nome_de_uma_sobra_funciona(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    await _falha_na_final(vila, obra, monkeypatch)
    r = await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h, json={"nome": "Brita", "unidade": "m³"})
    assert r.status_code == 201, r.text
    assert set(await _insumos(vila, obra)) == {"Brita"}


async def test_aditivo_que_entra_durante_a_aprovacao_da_conflito(vila, monkeypatch):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    original = versoes.itens_das_versoes
    feito = []

    async def com_aditivo(*a, **k):
        # depois de a aprovação ler as versões, um aditivo é gravado por outra pessoa
        if not feito:
            feito.append(1)
            r = await versao(vila, obra, TAPUME, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
            assert r.status_code == 201, r.text[:300]
        return await original(*a, **k)

    monkeypatch.setattr(versoes, "itens_das_versoes", com_aditivo)
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 409 and "mudou" in r.json()["detail"]
    assert await _status(vila, obra) == "em_orcamento"
    # aprovando de novo, o aditivo entra
    monkeypatch.undo()
    await _aprovar(vila, obra)
    assert "Tapume de chapa de madeira" in await _insumos(vila, obra)
