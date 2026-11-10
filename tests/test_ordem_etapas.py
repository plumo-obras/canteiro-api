"""Ordem das etapas antes de aprovar: a etapa das pranchas vai para depois dos serviços preliminares ao somar a
planilha, e a pessoa reordena à mão (contrato novo no lugar do vigente, com Desfazer). E o gasto fora de pedido sai
marcado "fora do orçado"."""
import uuid

from conftest import nova_vila
from test_gastos_orcamento import _contrato, _gasto, _resumo
from test_obras_orcamento import nova_obra, registro
from test_orcamento_versoes import CABECALHO, mapa, orcamento, versao, xlsx

# A planilha do engenheiro, sem estrutura: as pranchas trazem a dela.
PLANILHA = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
                ["1.1", None, None, "Locação da obra", "m²", 530, 5, 2650],
                ["1.2", None, None, "Tapume", "m", 100, 40, 4000],
                ["2", None, None, "FUNDAÇÕES", None, None, None, None],
                ["2.1", None, None, "Escavação manual", "m³", 48, 65, 3120])


async def _pranchas(vila, obra):
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex, "itens": [
            {"nome": "Concreto usinado fck 25", "unidade": "m³", "quantidade": 4000, "valor_unitario": 60000},
            {"nome": "Aço CA-50", "unidade": "kg", "quantidade": 200000, "valor_unitario": 1000}]})
    assert r.status_code == 201, r.text
    return r.json()


def _numeros(orc):
    return [(x["numero"], x["descricao"]) for x in orc["itens"]]


async def _ordem(vila, obra, ordem, chave=None):
    return await vila.http.post(f"/obras/{obra}/orcamento/etapas/ordem", headers=vila.h,
                                json={"ordem": ordem, "chave_idempotencia": chave or uuid.uuid4().hex})


async def test_somar_a_planilha_poe_as_pranchas_depois_dos_preliminares(vila):
    o = await nova_obra(vila)
    p = await _pranchas(vila, o["id"])
    orc = await orcamento(vila, o["id"])
    # o nome de sistema do arquivo não aparece para a pessoa
    assert [(v["rotulo"], v["origem"], v["desfazer_texto"]) for v in orc["versoes"]] == [
        ("Materiais das pranchas", None, None)]

    m = mapa(linha_cabecalho=1)
    previa = (await versao(vila, o["id"], PLANILHA, m, somar="true")).json()
    # a prévia segue com os números de antes (são os da escolha de etapa) e avisa da ordem
    assert [(e["etapa"], e["descricao"]) for e in previa["etapas"]] == [("2", "SERVIÇOS PRELIMINARES"),
                                                                       ("3", "FUNDAÇÕES")]
    assert [a["mensagem"] for a in previa["avisos"] if a["linha"] == 0] == [
        "Ao somar, Estrutura (das pranchas) vai para depois de SERVIÇOS PRELIMINARES, e as etapas são renumeradas "
        "nessa ordem."]

    s = await versao(vila, o["id"], PLANILHA, m, aplicar=True, somar="true")
    assert s.status_code == 201, s.text
    orc = await orcamento(vila, o["id"])
    assert _numeros(orc) == [
        ("1", "SERVIÇOS PRELIMINARES"), ("1.1", "Locação da obra"), ("1.2", "Tapume"),
        ("2", "Estrutura"), ("2.1", "Concreto usinado fck 25"), ("2.2", "Aço CA-50"),
        ("3", "FUNDAÇÕES"), ("3.1", "Escavação manual")]
    assert [(e["etapa"], e["descricao"]) for e in orc["etapas"]] == [
        ("1", "SERVIÇOS PRELIMINARES"), ("2", "Estrutura"), ("3", "FUNDAÇÕES")]
    somada = next(v for v in orc["versoes"] if v["ativa"])
    assert (somada["origem"], somada["rotulo"], somada["desfazer_texto"]) == (
        "somar", "Orçamento com a planilha somada",
        "Volta o orçamento para antes de somar a planilha (R$ 4.400,00, 2 itens).")

    d = await vila.http.post(f"/registro/{s.json()['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200, d.text
    volta = await orcamento(vila, o["id"])
    assert [v["id"] for v in volta["versoes"] if v["ativa"]] == [p["versao_id"]]
    assert _numeros(volta) == [("1", "Estrutura"), ("1.1", "Concreto usinado fck 25"), ("1.2", "Aço CA-50")]


async def test_sem_preliminares_ou_com_lancamento_as_pranchas_ficam_onde_estao(vila):
    m = mapa(linha_cabecalho=1)
    so_fundacao = xlsx(CABECALHO, ["1", None, None, "FUNDAÇÕES", None, None, None, None],
                       ["1.1", None, None, "Escavação manual", "m³", 48, 65, 3120])
    o = await nova_obra(vila)
    await _pranchas(vila, o["id"])
    assert (await versao(vila, o["id"], so_fundacao, m, aplicar=True, somar="true")).status_code == 201
    assert [e["descricao"] for e in (await orcamento(vila, o["id"]))["etapas"]] == ["Estrutura", "FUNDAÇÕES"]

    # um tópico já lançado na Estrutura (etapa 1): renumerar o levaria para os preliminares
    o = await nova_obra(vila)
    await _pranchas(vila, o["id"])
    t = await vila.http.post(f"/obras/{o['id']}/topicos", headers=vila.h, json={"etapa": "1", "nome": "Pilares"})
    assert t.status_code == 201, t.text
    previa = (await versao(vila, o["id"], PLANILHA, m, somar="true")).json()
    assert not [a for a in previa["avisos"] if a["linha"] == 0]
    assert (await versao(vila, o["id"], PLANILHA, m, aplicar=True, somar="true")).status_code == 201
    assert [(e["etapa"], e["descricao"]) for e in (await orcamento(vila, o["id"]))["etapas"]] == [
        ("1", "Estrutura"), ("2", "SERVIÇOS PRELIMINARES"), ("3", "FUNDAÇÕES")]


async def test_reordenar_as_etapas_antes_de_aprovar(vila, http, arm, config):
    o = await nova_obra(vila)
    assert (await _ordem(vila, o["id"], ["1"])).status_code == 422          # sem orçamento
    await _pranchas(vila, o["id"])
    assert (await versao(vila, o["id"], PLANILHA, mapa(linha_cabecalho=1), aplicar=True,
                         somar="true")).status_code == 201
    antes = await orcamento(vila, o["id"])
    tapume = next(x for x in antes["itens"] if x["descricao"] == "Tapume")
    e = await vila.http.patch(f"/obras/{o['id']}/orcamento/itens/{tapume['id']}", headers=vila.h,
                              json={"valor_unitario": 5000})
    assert e.status_code == 200, e.text

    for ruim in (["1", "2"], ["1", "2", "2"], ["1", "2", "4"], ["1", "2", "3"]):
        r = await _ordem(vila, o["id"], ruim)
        assert r.status_code == 422, (ruim, r.text)
    outra = await nova_vila(http, arm, config)
    assert (await http.post(f"/obras/{o['id']}/orcamento/etapas/ordem", headers=outra.h,
                            json={"ordem": ["3", "1", "2"], "chave_idempotencia": uuid.uuid4().hex})
            ).status_code == 404

    chave = uuid.uuid4().hex
    r = await _ordem(vila, o["id"], ["3", "1", "2"], chave)
    assert r.status_code == 201, r.text
    assert r.json()["etapas"] == [{"etapa": "1", "descricao": "FUNDAÇÕES"},
                                  {"etapa": "2", "descricao": "SERVIÇOS PRELIMINARES"},
                                  {"etapa": "3", "descricao": "Estrutura"}]
    assert (await registro(vila, r.json()["registro_id"]))["texto"] == \
        f"Reordenou as etapas do orçamento de {o['nome']}"
    repete = await _ordem(vila, o["id"], ["3", "1", "2"], chave)
    assert repete.status_code == 200 and repete.json() == r.json()
    assert (await _ordem(vila, o["id"], ["2", "1", "3"], chave)).status_code == 409

    orc = await orcamento(vila, o["id"])
    assert _numeros(orc) == [
        ("1", "FUNDAÇÕES"), ("1.1", "Escavação manual"),
        ("2", "SERVIÇOS PRELIMINARES"), ("2.1", "Locação da obra"), ("2.2", "Tapume"),
        ("3", "Estrutura"), ("3.1", "Concreto usinado fck 25"), ("3.2", "Aço CA-50")]
    # a edição de preço vem junto, e o total não muda
    assert next(x for x in orc["itens"] if x["descricao"] == "Tapume")["valor_unitario"] == 5000
    assert orc["totais"]["contratado"] == antes["totais"]["contratado"] + 100 * (5000 - 4000)
    v = next(v for v in orc["versoes"] if v["ativa"])
    assert (v["origem"], v["rotulo"], v["desfazer_texto"]) == (
        "reordenar", "Etapas reordenadas", "Volta as etapas do orçamento para a ordem de antes.")

    d = await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200, d.text
    assert d.json()["texto"] == f"Desfeito: as etapas do orçamento de {o['nome']} voltaram à ordem de antes"
    assert [x["descricao"] for x in (await orcamento(vila, o["id"]))["etapas"]] == [
        "SERVIÇOS PRELIMINARES", "Estrutura", "FUNDAÇÕES"]

    # lançamento numa etapa que mudaria de número: recusa, dizendo qual
    t = await vila.http.post(f"/obras/{o['id']}/topicos", headers=vila.h, json={"etapa": "3", "nome": "Sapatas"})
    assert t.status_code == 201, t.text
    presa = await _ordem(vila, o["id"], ["1", "3", "2"])
    assert presa.status_code == 409 and "3 FUNDAÇÕES" in presa.json()["detail"], presa.text

    assert (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).status_code == 200
    assert (await _ordem(vila, o["id"], ["2", "1", "3"])).status_code == 409


async def test_viewer_nao_reordena(vila):
    o = await nova_obra(vila)
    await _pranchas(vila, o["id"])
    hv, _ = await vila.usuario("viewer")
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/etapas/ordem", headers=hv,
                             json={"ordem": ["1"], "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 403


async def test_gasto_fora_de_pedido_sai_marcado_fora_do_orcado(vila):
    obra = vila.obra
    await _contrato(vila, obra)
    avulso = await _gasto(vila, obra, etapa="2", valor=64000)
    pago = await _gasto(vila, obra, etapa="2", valor=35000, pedido_id=vila.pedido["PED-0158"])
    assert (avulso["gasto"]["fora_do_orcado"], pago["gasto"]["fora_do_orcado"]) == (True, False)
    lista = (await vila.http.get(f"/obras/{obra}/gastos", headers=vila.h)).json()
    assert {g["id"]: g["fora_do_orcado"] for g in lista} == {avulso["gasto"]["id"]: True, pago["gasto"]["id"]: False}
    r = await _resumo(vila, obra)
    assert (r["total"], r["fora_do_orcado"], r["pagamentos_de_pedidos"]) == (64000, 64000, 35000)
    assert [(e["etapa"], e["total"], e["fora_do_orcado"]) for e in r["etapas"] if e["total"]] == [("2", 64000, 64000)]
    # só marca: o realizado do orçamento continua contando o gasto na etapa
    assert next(e for e in (await orcamento(vila, obra))["etapas"] if e["etapa"] == "2")["gastos_avulsos"] == 64000
