"""Cronograma físico-financeiro por etapa, medição mensal e prazo projetado pela curva S."""
import uuid

from test_obras_orcamento import nova_obra
from test_orcamento_versoes import CONTRATO, mapa, versao

# CONTRATO: etapa 1 = R$ 3.805,89 e etapa 2 = R$ 17.270,07 (total R$ 21.075,96)
PLANO = {"inicio": "2026-01", "meses": 3,
         "etapas": [{"etapa": "1", "percentuais": [10000, 0, 0]}, {"etapa": "2", "percentuais": [0, 5000, 5000]}]}


async def _obra(vila, com_cronograma=True):
    o = await nova_obra(vila)
    assert (await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)).status_code == 201
    if com_cronograma:
        r = await vila.http.put(f"/obras/{o['id']}/cronograma", headers=vila.h, json=PLANO)
        assert r.status_code == 200, r.text
    return o["id"]


async def _medir(vila, obra, mes, etapas, chave=None):
    return await vila.http.post(f"/obras/{obra}/medicoes", headers=vila.h, json={
        "mes": mes, "etapas": [{"etapa": e, "pct_acumulado": p} for e, p in etapas.items()],
        "chave_idempotencia": chave or uuid.uuid4().hex})


async def _cron(vila, obra):
    return (await vila.http.get(f"/obras/{obra}/cronograma", headers=vila.h)).json()


async def test_cronograma_previsto_por_mes(vila):
    obra = await _obra(vila)
    c = await _cron(vila, obra)
    assert (c["inicio"], c["meses"], c["prazo"]["termino_contratual"]) == ("2026-01", 3, "2026-03")
    assert [(m["mes"], m["previsto"], m["pct_previsto_acumulado"]) for m in c["mensal"]] == [
        ("2026-01", 380589, 1806), ("2026-02", 863504, 5903), ("2026-03", 863504, 10000)]
    assert [(e["etapa"], e["percentuais"]) for e in c["etapas"]] == [("1", [10000, 0, 0]), ("2", [0, 5000, 5000])]
    textos = [x["texto"] for x in (await vila.http.get("/registro", headers=vila.h)).json()]
    assert "Montou o cronograma: 2 etapas, 3 meses a partir de 2026-01" in textos


async def test_cronograma_recusa_o_que_nao_fecha(vila):
    obra = await _obra(vila, com_cronograma=False)

    async def put(**kw):
        return await vila.http.put(f"/obras/{obra}/cronograma", headers=vila.h, json={**PLANO, **kw})

    r = await put(etapas=[{"etapa": "1", "percentuais": [5000, 0, 0]}])
    assert r.status_code == 422 and "soma 50,0%" in r.json()["detail"]
    assert (await put(etapas=[{"etapa": "1", "percentuais": [10000, 0]}])).status_code == 422
    assert (await put(etapas=[{"etapa": "9", "percentuais": [10000, 0, 0]}])).status_code == 422
    assert (await put(inicio="2026-13")).status_code == 422
    sem = await nova_obra(vila)
    r = await vila.http.put(f"/obras/{sem['id']}/cronograma", headers=vila.h, json=PLANO)
    assert r.status_code == 422 and "orçamento" in r.json()["detail"]
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.put(f"/obras/{obra}/cronograma", headers=h, json=PLANO)).status_code == 403
    assert (await _medir(vila, obra, "2026-01", {"1": 10000})).status_code == 422     # sem cronograma


async def test_medicoes_e_prazo_projetado(vila):
    obra = await _obra(vila)
    r = await _medir(vila, obra, "2026-01", {"1": 10000})
    assert r.status_code == 201, r.text
    assert (r.json()["medicao"]["valor"], r.json()["medicao"]["pct_obra_acumulado"]) == (380589, 1806)
    prazo = r.json()["cronograma"]["prazo"]
    assert (prazo["atraso_meses"], prazo["atraso_decimos"]) == (0, 0)
    await _medir(vila, obra, "2026-02", {"2": 2000})
    r = await _medir(vila, obra, "2026-03", {"2": 3000})
    med = r.json()["medicao"]
    assert (med["numero"], med["valor"], med["pct_obra_acumulado"]) == (3, 172701, 4264)
    assert med["etapas"] == [{"etapa": "2", "pct_acumulado": 3000, "pct_periodo": 1000, "valor_periodo": 172701}]
    p = r.json()["cronograma"]["prazo"]
    # 42,6% medido em março; o cronograma previa isso a 60% de fevereiro: 1,4 mês de atraso
    assert (p["atraso_meses"], p["atraso_decimos"], p["termino_projetado"], p["pct_previsto_na_medicao"],
            p["pct_medido"]) == (1, 14, "2026-04", 10000, 4264)
    mensal = r.json()["cronograma"]["mensal"]
    assert [m["medido_acumulado"] for m in mensal] == [380589, 380589 + 345401, 380589 + 345401 + 172701]
    regs = (await vila.http.get("/registro", headers=vila.h)).json()
    reg = next(x for x in regs if x["id"] == r.json()["registro_id"])
    assert reg["texto"] == "3ª medição (2026-03): R$ 1.727,01 no mês · 42,6% da obra executada"


async def test_medicao_nao_volta_nem_repete_mes(vila):
    obra = await _obra(vila)
    await _medir(vila, obra, "2026-02", {"1": 10000, "2": 2000})
    r = await _medir(vila, obra, "2026-03", {"2": 1000})
    assert r.status_code == 422 and "não diminui" in r.json()["detail"]
    assert (await _medir(vila, obra, "2026-02", {"2": 3000})).status_code == 422
    assert (await _medir(vila, obra, "2025-12", {"2": 3000})).status_code == 422
    assert (await _medir(vila, obra, "2027-04", {"2": 3000})).status_code == 422       # 13 meses depois do fim


async def test_medicao_repetida_e_desfazer_so_a_ultima(vila):
    obra = await _obra(vila)
    a = await _medir(vila, obra, "2026-01", {"1": 10000}, chave="medicao-janeiro")
    b = await _medir(vila, obra, "2026-01", {"1": 10000}, chave="medicao-janeiro")
    assert (a.status_code, b.status_code, a.json()["registro_id"]) == (201, 200, b.json()["registro_id"])
    assert (await _medir(vila, obra, "2026-02", {"1": 10000}, chave="medicao-janeiro")).status_code == 409
    segunda = await _medir(vila, obra, "2026-02", {"2": 4000})
    assert (await vila.http.post(f"/registro/{a.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 409
    assert (await vila.http.post(f"/registro/{segunda.json()['registro_id']}/desfazer",
                                 headers=vila.h)).status_code == 200
    c = await _cron(vila, obra)
    assert ([m["numero"] for m in c["medicoes"]], c["prazo"]["mes_referencia"]) == ([1], "2026-01")
    assert {e["etapa"]: e["medido_acumulado"] for e in c["etapas"]} == {"1": 10000, "2": 0}
    # depois do desfazer, fevereiro pode ser medido de novo
    assert (await _medir(vila, obra, "2026-02", {"2": 1000})).status_code == 201


async def test_aditivo_de_prazo_estende_e_desfaz(vila):
    obra = await _obra(vila)
    await _medir(vila, obra, "2026-02", {"1": 10000})
    curto = {**PLANO, "inicio": "2026-03", "meses": 2, "etapas": [{"etapa": "1", "percentuais": [10000, 0]}]}
    r = await vila.http.put(f"/obras/{obra}/cronograma", headers=vila.h, json=curto)
    assert r.status_code == 422 and "2026-02" in r.json()["detail"]                    # deixaria a medição de fora
    longo = {"inicio": "2026-01", "meses": 5, "etapas": [{"etapa": "1", "percentuais": [10000, 0, 0, 0, 0]},
                                                         {"etapa": "2", "percentuais": [0, 2500, 2500, 2500, 2500]}]}
    r = await vila.http.put(f"/obras/{obra}/cronograma", headers=vila.h, json=longo)
    assert (r.status_code, r.json()["prazo"]["termino_contratual"]) == (200, "2026-05"), r.text
    regs = (await vila.http.get("/registro", headers=vila.h)).json()
    reg = next(x for x in regs if x["id"] == r.json()["registro_id"])
    assert reg["texto"] == "Refez o cronograma: 2 etapas, 5 meses a partir de 2026-01 (era 3)"
    assert (await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await _cron(vila, obra))["meses"] == 3


async def test_atraso_no_primeiro_mes_nao_fica_em_dia(vila):
    obra = await _obra(vila)
    r = await _medir(vila, obra, "2026-01", {"1": 1000})        # 1,8% medido contra 18,1% previsto
    p = r.json()["cronograma"]["prazo"]
    assert (p["atraso_decimos"], p["atraso_meses"], p["termino_projetado"]) == (9, 1, "2026-04")
