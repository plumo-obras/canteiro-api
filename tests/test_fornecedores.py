"""Fornecedores do cliente, as etapas da obra e as contas do orçamento que a API entrega prontas."""
from test_gastos_orcamento import _gasto
from test_obras_orcamento import nova_obra
from test_orcamento_versoes import ADITIVO, CONTRATO, mapa, versao


async def _obra_com_orcamento(vila):
    o = await nova_obra(vila)
    r = await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 201, r.text
    return o["id"]


async def test_etapas_da_obra_vem_do_orcamento(vila):
    obra = await _obra_com_orcamento(vila)
    r = await vila.http.get(f"/obras/{obra}/etapas", headers=vila.h)
    assert r.json() == [{"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES"},
                        {"etapa": "2", "descricao": "FUNDAÇÕES"}]
    sem = await nova_obra(vila)
    assert (await vila.http.get(f"/obras/{sem['id']}/etapas", headers=vila.h)).json() == []


async def test_fornecedor_cadastro_cnpj_e_repetido(vila):
    r = await vila.http.post("/fornecedores", headers=vila.h,
                             json={"nome": "Depósito  São Jorge", "cnpj": "11.222.333/0001-81", "telefone": " 51 9 "})
    assert r.status_code == 201, r.text
    f = r.json()
    assert (f["nome"], f["cnpj"], f["telefone"]) == ("Depósito São Jorge", "11222333000181", "51 9")
    assert (await vila.http.post("/fornecedores", headers=vila.h,
                                 json={"nome": "Outro", "cnpj": "11222333000181"})).status_code == 409
    ruim = await vila.http.post("/fornecedores", headers=vila.h, json={"nome": "X", "cnpj": "11222333000182"})
    assert ruim.status_code == 422 and "CNPJ inválido" in ruim.json()["detail"]
    r = await vila.http.patch(f"/fornecedores/{f['id']}", headers=vila.h, json={"contato": "Jorge"})
    assert (r.status_code, r.json()["contato"]) == (200, "Jorge")
    assert (await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    lista = (await vila.http.get("/fornecedores", headers=vila.h)).json()
    assert [(x["nome"], x["contato"]) for x in lista] == [("Depósito São Jorge", "")]
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.post("/fornecedores", headers=h, json={"nome": "Y"})).status_code == 403


async def test_saldo_estouro_e_aditivo_vem_prontos_da_api(vila):
    """O agente só cita: falta gastar, estouro da etapa e % do aditivo saem calculados de GET …/orcamento."""
    obra = await _obra_com_orcamento(vila)
    r = await versao(vila, obra, ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text
    await _gasto(vila, obra, valor=2500000, etapa="1")
    await _gasto(vila, obra, valor=325000, etapa="2")
    o = (await vila.http.get(f"/obras/{obra}/orcamento", headers=vila.h)).json()
    por = {e["etapa"]: e for e in o["etapas"]}
    assert (por["1"]["atual"], por["1"]["saldo"], por["1"]["estourou"]) == (2352617, 2352617 - 2500000, True)
    assert (por["2"]["atual"], por["2"]["saldo"], por["2"]["estourou"]) == (1666661, 1666661 - 325000, False)
    assert (por["2"]["cotando"], por["2"]["comprometido"], por["2"]["realizado"]) == (0, 325000, 325000)
    t = o["totais"]
    assert (t["saldo"], t["aditivos_pct"]) == (t["atual"] - 2825000, 9070)     # 90,70% do contrato


async def test_orcamento_vazio_nao_tem_percentual_de_aditivo(vila):
    o = await nova_obra(vila)
    t = (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=vila.h)).json()["totais"]
    assert (t["saldo"], t["aditivos_pct"]) == (0, None)


async def test_gasto_sai_com_o_nome_novo_ao_lado_dos_antigos(vila):
    """`gasto` é o nome que fica; `comprometido` e `realizado` (o mesmo número) e `cotando` (0) saem depois."""
    obra = await _obra_com_orcamento(vila)
    await _gasto(vila, obra, valor=100000, etapa="1")
    await _gasto(vila, obra, valor=7000, etapa=None)
    o = (await vila.http.get(f"/obras/{obra}/orcamento", headers=vila.h)).json()
    e1 = next(e for e in o["etapas"] if e["etapa"] == "1")
    assert (e1["gasto"], e1["comprometido"], e1["realizado"], e1["cotando"]) == (100000, 100000, 100000, 0)
    assert all(t["gasto"] == t["realizado"] for t in e1["topicos"])
    t = o["totais"]
    assert (t["gasto"], t["realizado"], t["sem_etapa"]["gasto"]) == (107000, 107000, 7000)
    fin = (await vila.http.get(f"/obras/{obra}/resumo", headers=vila.h)).json()["financeiro"]
    assert (fin["gasto"], fin["falta_gastar"], fin["pago"]) == (107000, t["atual"] - 107000, 107000)
    des = (await vila.http.get(f"/obras/{obra}/desembolso", headers=vila.h)).json()
    assert des["totais"]["gasto"] == des["totais"]["realizado"] == 107000
    assert next(e for e in des["etapas"] if e["etapa"] == "1")["gasto"] == 100000
