"""Projeção de desembolso: o que já saiu por mês e o que ainda vai sair até o fim (servicos/desembolso.py)."""
import uuid

from test_cronograma import _medir, _obra
from test_gastos_orcamento import _gasto

from canteiro_api.servicos.desembolso import EtapaBase, projetar, repartir, restante_do_plano

A = 100_000                                  # R$ 1.000,00
B = 200_000


def _etapas(**kw):
    a = EtapaBase("1", "Preliminares", A, kw.get("ra", 0), [5000, 5000, 0], kw.get("ma"))
    b = EtapaBase("2", "Fundações", B, kw.get("rb", 0), [0, 5000, 5000], kw.get("mb"))
    return [a, b]


def _proj(etapas, **kw):
    args = {"inicio": "2026-01", "atraso": 0, "mes_atual": "2026-02", "pago_por_mes": {}, "previsto_por_mes": {},
            "realizado_total": sum(e.realizado for e in etapas), **kw}
    return projetar(etapas, **args)


def _serie(d):
    return {m.mes: m.projetado for m in d.meses if m.projetado}


def test_restante_do_plano_consome_do_comeco():
    assert restante_do_plano([3000, 4000, 3000], 5000) == [0, 2000, 3000]
    assert restante_do_plano([3000, 4000, 3000], 0) == [3000, 4000, 3000]
    assert restante_do_plano([3000, 4000, 3000], 10000) == [0, 0, 0]


def test_repartir_fecha_no_centavo():
    r = repartir(100, {5: 1, 6: 1, 7: 1})
    assert sum(r.values()) == 100 and r == {5: 33, 6: 34, 7: 33}


def test_sem_medicao_o_que_ja_devia_ter_saido_cai_no_mes_corrente():
    d = _proj(_etapas())
    # A era para jan e fev: tudo em fev (o mês corrente); B segue o plano
    assert _serie(d) == {"2026-02": A + B // 2, "2026-03": B // 2}
    assert (d.base, d.termino_projetado, d.totais.falta, d.totais.custo_final, d.totais.desvio) == (
        "gastos", "2026-03", A + B, A + B, 0)


def test_sem_medicao_o_gasto_conta_como_avanco():
    d = _proj(_etapas(ra=50_000), pago_por_mes={"2026-01": 50_000})
    # A gastou metade: o resto é a parte de fevereiro do plano dela
    assert _serie(d) == {"2026-02": 50_000 + B // 2, "2026-03": B // 2}
    assert [m.pago for m in d.meses if m.pago] == [50_000]
    assert d.etapas[0].executado_pct == 5000


def test_medicao_executado_sem_pagar_e_agora_e_o_resto_vem_empurrado_pelo_atraso():
    d = _proj(_etapas(ra=30_000, ma=5000, mb=0), atraso=1, mes_referencia="2026-01",
              pago_por_mes={"2026-01": 30_000})
    # A: mediu 50% (R$ 500) e realizou R$ 300 → R$ 200 devidos agora; os R$ 500 restantes eram de fev, vão para mar.
    # B: nada medido, plano fev/mar empurrado um mês → mar/abr.
    assert _serie(d) == {"2026-02": 20_000, "2026-03": 50_000 + B // 2, "2026-04": B // 2}
    a = d.etapas[0]
    assert (a.falta, a.devido_agora, a.executado_pct) == (70_000, 20_000, 5000)
    assert (d.base, d.atraso_meses, d.termino_projetado) == ("medicao", 1, "2026-04")
    assert d.totais.falta == 70_000 + B and d.totais.pago + d.totais.falta == d.totais.custo_final


def test_obra_adiantada_nao_puxa_desembolso_para_antes():
    d = _proj(_etapas(ma=10000, mb=5000, ra=A, rb=B // 2), atraso=-1, mes_referencia="2026-02",
              pago_por_mes={"2026-01": A + B // 2})
    assert _serie(d) == {"2026-03": B // 2} and d.atraso_meses == 0


def test_estouro_ja_havido_aparece_e_o_resto_custa_o_orcamento():
    d = _proj(_etapas(ra=A + 30_000), pago_por_mes={"2026-01": A + 30_000})
    assert d.etapas[0].falta == 0
    assert (d.totais.custo_final, d.totais.desvio) == (A + 30_000 + B, 30_000)


def test_material_comprado_antes_de_executar_nao_vira_estouro():
    # B comprou metade do orçado e não mediu nada: o que falta é a outra metade, não o orçado inteiro
    d = _proj(_etapas(rb=B // 2, ma=0, mb=0), mes_referencia="2026-01", pago_por_mes={"2026-01": B // 2})
    assert d.etapas[1].falta == B // 2 and d.totais.desvio == 0
    assert sum(_serie(d).values()) == A + B // 2


def test_entregue_sem_pagamento_e_pago_adiantado():
    d = _proj(_etapas(rb=40_000), pago_por_mes={"2026-01": 10_000})
    assert d.totais.entregue_sem_pagamento == 30_000
    sem = _proj(_etapas(rb=40_000), pago_por_mes={"2026-01": 40_000})
    assert _serie(d)["2026-02"] == _serie(sem)["2026-02"] + 30_000
    # pagou R$ 500 a mais do que recebeu: sai dos primeiros meses, sem mês negativo
    adiantado = _proj(_etapas(), pago_por_mes={"2026-01": 50_000}, realizado_total=0)
    assert _serie(adiantado) == {"2026-02": A + B // 2 - 50_000, "2026-03": B // 2}
    assert all(m.projetado >= 0 for m in adiantado.meses)


def test_etapa_fora_do_cronograma_fica_sem_mes():
    etapas = [*_etapas(), EtapaBase("3", "Pintura", 50_000, 0, [])]
    d = _proj(etapas)
    assert (d.totais.sem_mes, d.etapas[2].sem_cronograma) == (50_000, True)
    assert d.totais.falta == A + B + 50_000
    sem = projetar(etapas, inicio=None, atraso=0, mes_atual="2026-02", pago_por_mes={}, previsto_por_mes={},
                   realizado_total=0)
    assert (sem.base, sem.totais.sem_mes, _serie(sem)) == ("sem_cronograma", A + B + 50_000, {})


def test_meses_vao_do_primeiro_pagamento_ou_do_plano_ao_ultimo_projetado():
    d = _proj(_etapas(), pago_por_mes={"2025-12": 0, "2025-11": 5_000}, realizado_total=5_000,
              previsto_por_mes={"2026-01": 150_000, "2026-02": 100_000, "2026-03": 100_000})
    assert [m.mes for m in d.meses] == ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03"]
    assert d.meses[2].previsto == 150_000


# --- pela API ---

async def test_desembolso_pela_api(vila):
    obra = await _obra(vila)                           # etapa 1 = R$ 3.805,89 (jan); 2 = R$ 17.270,07 (fev/mar)
    await _gasto(vila, obra, valor=100_000, etapa="2", data_pagamento="2026-01-20")
    r = await vila.http.get(f"/obras/{obra}/desembolso", headers=vila.h, params={"hoje": "2026-02-10"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["base"] == "gastos" and d["mes_atual"] == "2026-02"
    assert [(m["mes"], m["pago"]) for m in d["meses"] if m["pago"]] == [("2026-01", 100_000)]
    t = d["totais"]
    assert (t["orcado"], t["realizado"], t["pago"]) == (2_107_596, 100_000, 100_000)
    assert t["falta"] == 2_107_596 - 100_000 and t["desvio"] == 0
    assert [m["previsto"] for m in d["meses"]] == [p["previsto"] for p in
                                                   (await vila.http.get(f"/obras/{obra}/cronograma",
                                                                        headers=vila.h)).json()["mensal"]]
    # mediu a etapa 1 inteira em janeiro sem gasto nela: os R$ 3.805,89 são devidos agora
    assert (await _medir(vila, obra, "2026-01", {"1": 10000})).status_code == 201
    d = (await vila.http.get(f"/obras/{obra}/desembolso", headers=vila.h, params={"hoje": "2026-02-10"})).json()
    assert d["base"] == "medicao" and d["mes_referencia"] == "2026-01"
    e1 = next(e for e in d["etapas"] if e["etapa"] == "1")
    assert (e1["devido_agora"], e1["falta"]) == (380_589, 380_589)
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{obra}/desembolso", headers=h)).status_code == 200


async def test_desembolso_de_obra_de_outro_tenant_nao_abre(vila):
    obra = await _obra(vila, com_cronograma=False)
    h, _ = await vila.usuario("admin", tenant_id=f"t-outro-{uuid.uuid4().hex[:8]}")
    assert (await vila.http.get(f"/obras/{obra}/desembolso", headers=h)).status_code == 404


def _ritmo(d):
    return {m.mes: m.projetado_no_ritmo for m in d.meses if m.projetado_no_ritmo}


def test_no_ritmo_atual_o_resto_se_estica():
    # mediu 25% quando o plano previa 50%: ritmo de 50%, cada mês de plano vira dois
    e = [EtapaBase("1", "Obra", 400_000, 100_000, [2500, 2500, 2500, 2500], 2500)]
    d = projetar(e, inicio="2026-01", atraso=1, mes_atual="2026-03", pago_por_mes={"2026-01": 100_000},
                 previsto_por_mes={}, realizado_total=100_000, mes_referencia="2026-02", ritmo=5000)
    # pelo cronograma: os 3 meses restantes (fev..abr) empurrados 1 → mar..mai
    assert _serie(d) == {"2026-03": 100_000, "2026-04": 100_000, "2026-05": 100_000}
    # no ritmo: os mesmos 3 meses de plano em 6 meses de calendário
    assert _ritmo(d) == {f"2026-{m:02d}": 50_000 for m in range(3, 9)}
    assert (d.ritmo_pct, d.termino_projetado, d.termino_no_ritmo) == (5000, "2026-05", "2026-08")
    assert sum(_ritmo(d).values()) == sum(_serie(d).values()) == d.totais.falta


def test_ritmo_so_vale_com_medicao_e_tem_piso():
    d = _proj(_etapas(), ritmo=3000)                   # sem medição: ignora o ritmo
    assert _ritmo(d) == _serie(d) and d.ritmo_pct == 10000
    parada = _proj(_etapas(ma=0, mb=0), mes_referencia="2026-01", ritmo=0)
    assert parada.ritmo_pct == 1000                    # obra parada: 10%, não infinito
    assert sum(_ritmo(parada).values()) == A + B
    # o devido agora e o entregue sem pagamento caem no mês corrente nos dois cenários
    d = _proj(_etapas(ra=30_000, ma=5000, mb=0, rb=10_000), mes_referencia="2026-01", ritmo=5000,
              pago_por_mes={"2026-01": 30_000})
    assert _serie(d)["2026-02"] >= 30_000 and _ritmo(d)["2026-02"] >= 30_000


def test_no_ritmo_resto_pequeno_de_etapa_antiga_nao_empurra_o_fim():
    # B (R$ 1.000) de jul a dez, nada feito; hoje jul, ritmo 90%. Sozinha, termina em jan/27.
    b = EtapaBase("2", "Estrutura", 100_000, 0, [0] * 6 + [1667] * 5 + [1665], 0)
    args = {"inicio": "2026-01", "atraso": 0, "mes_atual": "2026-07", "pago_por_mes": {}, "previsto_por_mes": {},
            "mes_referencia": "2026-06", "ritmo": 9000}
    so_b = projetar([b], realizado_total=0, **args)
    # A (R$ 1.000 em jan) medida 99% e paga: sobram R$ 10, que entram na fila em poucos dias e não mexem no fim
    a = EtapaBase("1", "Preliminares", 100_000, 99_000, [10000] + [0] * 11, 9900)
    com_a = projetar([a, b], realizado_total=99_000, pago_por_mes={"2026-01": 99_000},
                     **{k: v for k, v in args.items() if k != "pago_por_mes"})
    assert so_b.termino_no_ritmo == com_a.termino_no_ritmo == "2027-01"
    assert all(m.projetado_no_ritmo > 0 for m in com_a.meses if "2026-07" <= m.mes <= "2027-01")   # sem buraco


def test_no_ritmo_espera_hoje_sem_esticar_o_vazio_ate_o_plano():
    # o plano (já empurrado) começa daqui a 2 meses: o vazio até lá não é trabalho, não se estica pelo ritmo
    e = [EtapaBase("1", "Obra", 200_000, 0, [0, 0, 0, 0, 5000, 5000], 0)]
    d = projetar(e, inicio="2026-01", atraso=0, mes_atual="2026-03", pago_por_mes={}, previsto_por_mes={},
                 realizado_total=0, mes_referencia="2026-02", ritmo=5000)
    assert _ritmo(d) == {"2026-05": 50_000, "2026-06": 50_000, "2026-07": 50_000, "2026-08": 50_000}


def test_sem_medicao_quase_tudo_gasto_continua_no_cronograma():
    e = [EtapaBase("1", "Obra", 1_000_000, 999_960, [5000, 5000])]
    d = projetar(e, inicio="2026-01", atraso=0, mes_atual="2026-01", pago_por_mes={"2026-01": 999_960},
                 previsto_por_mes={}, realizado_total=999_960)
    assert (d.etapas[0].sem_cronograma, d.totais.sem_mes, d.totais.falta) == (False, 0, 40)
    assert _serie(d) == {"2026-02": 40}


async def test_desembolso_recusa_data_absurda(vila):
    obra = await _obra(vila)
    r = await vila.http.get(f"/obras/{obra}/desembolso", headers=vila.h, params={"hoje": "9999-12-31"})
    assert r.status_code == 422 and "10 anos" in r.json()["detail"]


def test_no_ritmo_o_atrasado_entra_em_fila_e_nao_empilha():
    # nada executado e já em março: pelo cronograma, jan..mar caem todos agora; no ritmo de 50%, em fila
    d = _proj(_etapas(ma=0, mb=0), mes_atual="2026-03", mes_referencia="2026-01", ritmo=5000)
    assert _serie(d) == {"2026-03": A + B}
    assert _ritmo(d) == {"2026-03": 25_000, "2026-04": 25_000, "2026-05": 75_000, "2026-06": 75_000,
                         "2026-07": 50_000, "2026-08": 50_000}
