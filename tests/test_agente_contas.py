"""Contas que o agente cita prontas (teste com o modelo real em 05/11): cronograma por etapa e resumo da obra."""
from test_cronograma import _medir
from test_cronograma import _obra as _obra_com_cronograma
from test_propostas import proposta

# --- cronograma por etapa ---

async def test_cronograma_por_etapa_situacao_e_medido_por_mes(vila):
    obra = await _obra_com_cronograma(vila)        # etapa 1: 100% em jan; etapa 2: 50% fev, 50% mar
    c = (await vila.http.get(f"/obras/{obra}/cronograma", headers=vila.h)).json()
    assert [(e["etapa"], e["situacao"], e["valores_mes"]) for e in c["etapas"]] == [
        ("1", "nao_comecou", [380589, 0, 0]), ("2", "nao_comecou", [0, 863504, 863503])]
    assert (await _medir(vila, obra, "2026-01", {"1": 5000})).status_code == 201
    e1, e2 = (await vila.http.get(f"/obras/{obra}/cronograma", headers=vila.h)).json()["etapas"]
    assert (e1["situacao"], e1["previsto_ate_referencia"], e1["atraso_pct"], e1["medido_mes"]) == (
        "atrasada", 10000, 5000, [5000, 0, 0])
    assert e2["situacao"] == "nao_comecou"
    assert (await _medir(vila, obra, "2026-02", {"1": 10000, "2": 6000})).status_code == 201
    e1, e2 = (await vila.http.get(f"/obras/{obra}/cronograma", headers=vila.h)).json()["etapas"]
    assert (e1["situacao"], e1["medido_mes"]) == ("concluida", [5000, 5000, 0])
    assert (e2["situacao"], e2["previsto_ate_referencia"], e2["medido_mes"]) == ("adiantada", 5000, [0, 6000, 0])


# --- resumo de uma obra ---

async def test_resumo_da_obra_com_cronograma(vila):
    obra = await _obra_com_cronograma(vila)
    await _medir(vila, obra, "2026-01", {"1": 5000})
    r = await vila.http.get(f"/obras/{obra}/resumo?hoje=2026-02-10", headers=vila.h)
    assert r.status_code == 200, r.text
    s = r.json()
    c = s["cronograma"]
    assert (c["cadastrado"], c["mes_corrente"], c["mes_ultima_medicao"], c["previsto_no_mes"]) == (
        True, "2026-02", "2026-01", 863504)
    assert [(e["etapa"], e["pct_no_mes"], e["valor_no_mes"]) for e in c["etapas_do_mes"]] == [("2", 5000, 863504)]
    assert [(e["etapa"], e["atraso_pct"]) for e in c["etapas_atrasadas"]] == [("1", 5000)]
    assert (c["proximo_mes_com_inicio"], c["proximas_etapas"]) == (None, [])
    antes = (await vila.http.get(f"/obras/{obra}/resumo?hoje=2025-12-20", headers=vila.h)).json()["cronograma"]
    # a etapa 1 já tem medição (jan): não "começa" de novo; a próxima a começar é a 2, em fevereiro
    assert (antes["etapas_do_mes"], antes["proximo_mes_com_inicio"]) == ([], "2026-02")
    assert [e["etapa"] for e in antes["proximas_etapas"]] == ["2"]
    assert antes["proximo_mes"] == "2026-01"
    assert [(e["etapa"], e["comeca"]) for e in antes["etapas_do_proximo_mes"]] == [("1", False)]
    f = s["financeiro"]
    assert (f["tem_orcamento"], f["orcado_atual"], f["comprometido"]) == (True, 2107596, 0)


async def test_resumo_da_obra_sem_cronograma_nem_orcamento(vila):
    r = await vila.http.get(f"/obras/{vila.obra}/resumo?hoje=2026-11-01", headers=vila.h)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["cronograma"]["cadastrado"] is False and s["financeiro"]["tem_orcamento"] is False
    assert "entregas" not in s and "almoxarifado" not in s
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{vila.obra}/resumo", headers=h)).status_code == 200


async def test_resumo_da_obra_conta_as_propostas_pendentes(vila):
    obra = await _obra_com_cronograma(vila, com_cronograma=False)
    await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))
    p = (await vila.http.get(f"/obras/{obra}/resumo", headers=vila.h)).json()["pendencias"]
    assert (p["propostas"], p["titulos"]) == (1, ["Cronograma da obra"])


# --- resumo: o mês seguinte (segundo teste com o modelo real, 05/11/2026) ---

def _cronograma_emei():
    """A EMEI do teste: novembro medido, dezembro com cinco frentes. Fundações já têm 4,51% medidos (adiantadas)
    embora o primeiro % previsto delas seja dezembro."""
    from canteiro_api.esquemas import CronogramaObra, EtapaCronograma, MesCronograma, Prazo

    def etapa(n, nome, pcts, valores, medido, previsto, situacao):
        pcts, valores = pcts + [0] * (10 - len(pcts)), valores + [0] * (10 - len(valores))
        return EtapaCronograma(etapa=n, descricao=nome, atual=sum(valores), percentuais=pcts, valores_mes=valores,
                               medido_acumulado=medido, previsto_ate_referencia=previsto, situacao=situacao)

    etapas = [
        etapa("1", "SERVIÇOS PRELIMINARES", [10000], [6117367], 8886, 10000, "atrasada"),
        etapa("2", "MOVIMENTO DE TERRA", [1500, 8500], [999754, 5665273], 2483, 1500, "adiantada"),
        etapa("3", "FUNDAÇÕES", [0, 7000, 3000], [0, 7411960, 3176554], 451, 0, "adiantada"),
        etapa("4", "SUPERESTRUTURA", [0, 1000, 9000], [0, 1129568, 10166112], 0, 0, "nao_comecou"),
        etapa("21", "SPDA", [0, 1500, 8500], [0, 266679, 1511181], 0, 0, "nao_comecou"),
        etapa("23", "SERVIÇOS COMPLEMENTARES", [1500, 2500, 6000], [1078387, 1797312, 4313549], 1000, 1500,
              "atrasada"),
        etapa("9", "REVESTIMENTOS", [0, 0, 0, 10000], [0, 0, 0, 29974323], 0, 0, "nao_comecou"),
    ]
    mensal = [MesCronograma(mes=f"2026-{11 + i}" if i < 2 else f"2027-{i - 1:02d}", previsto=sum(
        e.valores_mes[i] for e in etapas), previsto_acumulado=0, pct_previsto_acumulado=0) for i in range(10)]
    return CronogramaObra(inicio="2026-11", meses=10, etapas=etapas, mensal=mensal,
                          prazo=Prazo(mes_referencia="2026-11", pct_medido=396, pct_previsto_na_medicao=391))


def test_resumo_lista_o_mes_seguinte_inteiro_e_nao_reinicia_etapa_ja_medida():
    from datetime import date

    from canteiro_api.servicos.resumo import resumo_cronograma

    r = resumo_cronograma(_cronograma_emei(), date(2026, 11, 5))
    # dezembro inteiro, maior valor primeiro: o movimento de terra (2ª frente) entra, e cada uma diz se começa
    assert r.proximo_mes == "2026-12" and r.previsto_no_proximo_mes == 16270792
    assert [(e.etapa, e.valor_no_mes, e.comeca) for e in r.etapas_do_proximo_mes] == [
        ("3", 7411960, False), ("2", 5665273, False), ("23", 1797312, False), ("4", 1129568, True),
        ("21", 266679, True)]
    # Fundações já foram medidas: não "começam em dezembro"
    assert r.proximo_mes_com_inicio == "2026-12"
    assert [e.etapa for e in r.proximas_etapas] == ["4", "21"]
    assert [e.etapa for e in r.etapas_do_mes] == ["1", "23", "2"]          # novembro, maior valor primeiro
    # depois de dezembro, a próxima a começar é a que não tem medição nenhuma
    jan = resumo_cronograma(_cronograma_emei(), date(2026, 12, 10))
    assert (jan.proximo_mes_com_inicio, [e.etapa for e in jan.proximas_etapas]) == ("2027-02", ["9"])
    # último mês do cronograma: não há mês seguinte
    fim = resumo_cronograma(_cronograma_emei(), date(2027, 8, 1))
    assert (fim.proximo_mes, fim.etapas_do_proximo_mes) == (None, [])
