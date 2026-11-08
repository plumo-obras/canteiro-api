"""Tributos da obra: os dados fiscais pela edição da obra, a natureza fiscal do gasto com Desfazer e a estimativa do
INSS (aferição) e do IBS/CBS que sai de GET /obras/{id}/tributos."""
from test_gastos import _desfazer
from test_gastos_orcamento import _gasto
from test_obras_orcamento import nova_obra, registro

CASA = {"tipologia": "casa", "padrao": "normal", "area_construida": 150_000, "area_complementar": 20_000,
        "proprietario": "pf", "material": "alvenaria", "vau": 200_000, "vau_referencia": "2026-10",
        "cno": "90.012.34567/89", "finalidade": "uso_proprio", "verba": 48_000_000}


async def _tributos(vila, obra, h=None):
    r = await vila.http.get(f"/obras/{obra}/tributos", headers=h or vila.h)
    assert r.status_code == 200, r.text
    return r.json()


async def _fiscal(vila, gasto_id, natureza, ibs_cbs=0, h=None, **nota):
    return await vila.http.patch(f"/gastos/{gasto_id}/fiscal", headers=h or vila.h,
                                 json={"natureza_fiscal": natureza, "ibs_cbs": ibs_cbs, **nota})


async def test_obra_sem_dados_fiscais_diz_o_que_falta(vila):
    obra = (await nova_obra(vila))["id"]
    t = await _tributos(vila, obra)
    assert t["inss"]["situacao"] == "sem_dados" and len(t["inss"]["motivos"]) == 2
    assert t["ibs_cbs"]["situacao"] == "sem_dados"
    assert "contador" in t["aviso"]
    assert t["parametros"]["inss"]["aliquota"] == 3680


async def test_inss_da_casa_cai_quando_o_gasto_ganha_nota_e_desfaz(vila):
    obra = (await nova_obra(vila, **CASA))["id"]
    t = await _tributos(vila, obra)
    assert t["cno"] == "900123456789" and t["vau_referencia"] == "2026-10"
    # casa = residencial unifamiliar (da tipologia): 150 × 89% + 20 × 50% = 143,5 m²; PF → fator social de 170 m²
    assert (t["destinacao"], t["categoria"]) == ("residencial_unifamiliar", "obra_nova")
    assert t["inss"]["situacao"] == "calculado"
    assert (t["inss"]["custo_obra"], t["inss"]["rmt"], t["inss"]["fator_social"]) == (28_700_000, 2_296_000, 4000)
    assert t["inss"]["inss"] == 844_928
    assert (t["inss"]["fator_ajuste"], t["inss"]["falta_fator_ajuste"]) == ("nao_atingido", 1_148_000)
    assert t["ibs_cbs"]["situacao"] == "nao_incide"

    g = await _gasto(vila, obra, valor=1_000_000, descricao="empreiteiro, alvenaria")
    t = await _tributos(vila, obra)
    assert (t["a_classificar"], t["valor_a_classificar"]) == (1, 1_000_000)
    assert [p["id"] for p in t["pendentes"]] == [g["gasto"]["id"]]

    # pago sem nota: não abate, e a tela mostra quanto cairia com nota
    r = await _fiscal(vila, g["gasto"]["id"], "mao_de_obra_sem_nf")
    assert r.status_code == 200, r.text
    assert (await registro(vila, r.json()["registro_id"]))["texto"] == (
        "Classificou o gasto de R$ 10.000,00 (empreiteiro, alvenaria) como mão de obra sem nota")
    t = await _tributos(vila, obra)
    assert t["a_classificar"] == 0 and t["inss"]["inss"] == 844_928
    assert t["inss"]["economia_com_notas"] == 147_200 and len(t["sem_nota"]) == 1

    # nota sem o CNO da obra: a Receita não liga à obra, segue na lista do que falta comprovar
    r = await _fiscal(vila, g["gasto"]["id"], "mao_de_obra_nf")
    assert r.status_code == 200, r.text
    t = await _tributos(vila, obra)
    assert t["inss"]["inss"] == 844_928 and t["inss"]["notas_sem_cno"] == 1_000_000
    assert [x["nf_cno"] for x in t["sem_nota"]] == [None]

    # com o CNO na nota: abate 40% do valor; o IBS/CBS da nota vira crédito
    r = await _fiscal(vila, g["gasto"]["id"], "mao_de_obra_nf", ibs_cbs=50_000, nf_cno=True, retencao=110_000)
    assert r.status_code == 200, r.text
    assert (await registro(vila, r.json()["registro_id"]))["texto"] == (
        "Classificou o gasto de R$ 10.000,00 (empreiteiro, alvenaria) como mão de obra com nota (com o CNO da obra, "
        "retenção R$ 1.100,00, IBS/CBS R$ 500,00)")
    t = await _tributos(vila, obra)
    assert t["inss"]["comprovado"] == 400_000 and t["inss"]["inss"] == 697_728
    assert t["ibs_cbs"]["creditos"] == 50_000 and t["sem_nota"] == []

    desfeito = await _desfazer(vila, r.json()["registro_id"])
    assert desfeito["texto"] == ("Desfeito: voltou o gasto de R$ 10.000,00 (empreiteiro, alvenaria) como mão de obra "
                                 "com nota")
    assert (await _tributos(vila, obra))["inss"]["inss"] == 844_928
    await _desfazer(vila, desfeito["id"])
    assert (await _tributos(vila, obra))["inss"]["inss"] == 697_728
    # trocar para outra natureza limpa o CNO e a retenção, que só valem na nota de mão de obra
    r = await _fiscal(vila, g["gasto"]["id"], "folha", nf_cno=True, retencao=1)
    assert (await vila.arm.obter("gastos", vila.tenant_id, g["gasto"]["id"]))["nf_cno"] is None


async def test_fator_de_ajuste_e_selic_pela_api(vila):
    from canteiro_api import selic
    from canteiro_api.modelos import TENANT_SISTEMA
    r = await selic.importar(vila.arm, {"2026-01": 100, "2026-02": 99}, "teste")
    assert r == {"meses": 2, "novos": 2, "mudaram": 0}
    assert (await selic.importar(vila.arm, {"2026-02": 98}, "teste"))["mudaram"] == 1
    assert (await vila.arm.obter("indices", TENANT_SISTEMA, "selic:2026"))["meses"] == {"2026-01": 100,
                                                                                         "2026-02": 98}
    obra = (await nova_obra(vila, **CASA))["id"]
    g = await _gasto(vila, obra, valor=2_000_000, descricao="folha de outubro")
    await _fiscal(vila, g["gasto"]["id"], "folha")
    t = await _tributos(vila, obra)
    assert t["inss"]["fator_ajuste"] == "atingido" and t["inss"]["inss"] > 0    # 20.000 ≥ 50% de 22.960
    r = await vila.http.patch(f"/obras/{obra}", headers=vila.h, json={"dctfweb_em_dia": True})
    assert r.status_code == 200, r.text
    t = await _tributos(vila, obra)
    assert (t["inss"]["fator_ajuste"], t["inss"]["inss"]) == ("aplicado", 0)
    # reforma pela tipologia: 35% da RMT
    obra = (await nova_obra(vila, **{**CASA, "tipologia": "reforma", "destinacao": "residencial_unifamiliar"}))["id"]
    t = await _tributos(vila, obra)
    assert (t["categoria"], t["inss"]["rmt"]) == ("reforma", 803_600)


async def test_ibs_cbs_da_venda_e_edicao_fiscal_da_obra_com_desfazer(vila):
    obra = (await nova_obra(vila, **{**CASA, "finalidade": "venda", "verba": 80_000_000}))["id"]
    r = await vila.http.patch(f"/obras/{obra}", headers=vila.h, json={"valor_terreno": 20_000_000, "unidades": 1})
    assert r.status_code == 200, r.text
    assert (await registro(vila, r.json()["registro_id"]))["texto"].endswith("valor do terreno, unidades")
    t = await _tributos(vila, obra)
    assert (t["ibs_cbs"]["base"], t["ibs_cbs"]["debito"]) == (50_000_000, 6_625_000)
    await _desfazer(vila, r.json()["registro_id"])
    assert (await _tributos(vila, obra))["ibs_cbs"]["base"] == 80_000_000   # terreno e unidades voltam a 0


async def test_erros_da_classificacao_fiscal_e_papeis(vila):
    obra = (await nova_obra(vila, **CASA))["id"]
    g = await _gasto(vila, obra, valor=10_000)
    gid = g["gasto"]["id"]
    r = await _fiscal(vila, gid, None)
    assert r.status_code == 422 and "Nada mudou" in r.json()["detail"]
    r = await _fiscal(vila, gid, "folha", ibs_cbs=10_001)
    assert r.status_code == 422 and "IBS/CBS" in r.json()["detail"]
    assert (await _fiscal(vila, gid, "inventada")).status_code == 422
    assert (await _fiscal(vila, "nao-existe", "folha")).status_code == 404
    h_viewer, _ = await vila.usuario("viewer")
    assert (await _fiscal(vila, gid, "folha", h=h_viewer)).status_code == 403
    assert (await _tributos(vila, obra, h=h_viewer))["inss"]["situacao"] == "calculado"
    h_outro, _ = await vila.usuario("admin", tenant_id="outro-tenant")
    assert (await vila.http.get(f"/obras/{obra}/tributos", headers=h_outro)).status_code == 404
    await _desfazer(vila, g["registro_id"])
    assert (await _fiscal(vila, gid, "folha")).status_code == 409


async def test_cno_invalido_e_recusado(vila):
    r = await vila.http.patch(f"/obras/{vila.obra}", headers=vila.h, json={"cno": "123"})
    assert r.status_code == 422
    r = await vila.http.patch(f"/obras/{vila.obra}", headers=vila.h, json={"cno": "900123456789"})
    assert r.status_code == 200 and r.json()["cno"] == "900123456789"
