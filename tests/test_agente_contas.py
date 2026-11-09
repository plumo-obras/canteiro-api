"""Contas que o agente cita prontas (teste com o modelo real em 05/11): linha de conta honesta, preço da última
compra, proposta de entrega pelo mesmo serviço da tela, efeito da proposta, cronograma por etapa e resumo da obra."""
import uuid

from test_cronograma import _medir
from test_cronograma import _obra as _obra_com_cronograma
from test_pedidos_propostas import proposta

from canteiro_dominio.tipos import qtd


async def _itens(vila, obra=None):
    r = await vila.http.get(f"/obras/{obra or vila.obra}/insumos", headers=vila.h)
    assert r.status_code == 200, r.text
    return {x["insumo"]["id"]: x for x in r.json()}


# --- linha de conta ---

async def test_sem_previsao_diz_que_nao_sabe_e_nunca_falta_zero(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/insumos", headers=vila.h, json={"nome": "Brita 1", "unidade": "m³"})
    brita = r.json()["id"]
    await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": brita, "tipo": "entrada", "quantidade": qtd(18), "chave_idempotencia": uuid.uuid4().hex})
    c = (await _itens(vila))[brita]["conta"]
    assert (c["sem_previsao"], c["falta"], c["sobra"]) == (True, 0, 0)
    assert c["texto"].startswith("sem previsão lançada para a obra")
    assert "saldo 18 m³" in c["texto"] and "= falta" not in c["texto"] and "precisa 0" not in c["texto"]


async def test_sobra_aparece_como_soma_verdadeira(vila):
    # areia: precisa 60, saldo 12, a caminho 40 (falta 8); entram 20 → 32 + 40 = 72 para 60: sobra 12
    await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": vila.insumo["areia"], "tipo": "entrada", "quantidade": qtd(20),
        "chave_idempotencia": uuid.uuid4().hex})
    c = await vila.conta("areia")
    assert (c["falta"], c["sobra"], c["sem_previsao"]) == (0, qtd(12), False)
    assert c["texto"] == ("saldo 32 m³ + a caminho 40 m³ + em cotação 0 m³ + transferência 0 m³ = 72 m³ para precisa "
                          "60 m³: sobra 12 m³, falta 0")
    cimento = await vila.conta("cimento")
    assert cimento["texto"].endswith("= falta 34 sc")


# --- preço da última compra ---

async def test_ultimo_preco_vem_do_pedido_mais_novo_nao_cancelado(vila):
    cimento = vila.insumo["cimento"]
    p = (await _itens(vila))[cimento]["ultimo_preco"]
    assert (p["numero"], p["valor_unitario"], p["fornecedor"]) == ("PED-0160", 3800, "Depósito São Jorge")
    assert len(p["data"]) == 10
    novo = (await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json={
        "insumo_id": cimento, "quantidade": qtd(10), "fornecedor": "Casa do Cimento", "valor": 37500})).json()
    p = (await _itens(vila))[cimento]["ultimo_preco"]
    assert (p["numero"], p["valor_unitario"]) == (novo["numero"], 3750)
    await vila.http.post(f"/pedidos/{novo['id']}/avancar", headers=vila.h, json={"para": "cancelado"})
    assert (await _itens(vila))[cimento]["ultimo_preco"]["numero"] == "PED-0160"
    assert (await _itens(vila))[vila.insumo["bloco"]]["ultimo_preco"] is None


# --- proposta de entrega ---

def _entrega(vila, linhas, pedido="PED-0157", **extra):
    return proposta(vila, "registrar_entrega", {"pedido_id": vila.pedido[pedido],
                                                "linhas": [{"indice": i, "quantidade": q} for i, q in linhas],
                                                **extra})


async def test_entrega_proposta_mostra_a_conta_e_aprova_pelo_servico_da_tela(vila):
    r = await vila.http.post("/propostas", headers=vila.h, json=_entrega(
        vila, [(0, qtd(36))], nf_numero="4410", nf_data="2026-11-05", nf_valor=432000))
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["efeito"] == "entrada"
    assert p["conta"] == ("PED-0157 · Areia média: recebido 0 m³ + esta entrega 36 m³ = 36 de 40 m³ (ainda faltam "
                          "4 m³) · o pedido continua parcial")
    assert (await vila.conta("areia"))["saldo"] == qtd(12)              # proposta não executa
    a = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert a.status_code == 200, a.text
    reg = a.json()["registro"]
    assert reg["texto"] == "PED-0157 · 1ª entrega (NF 4410): 36 de 40 m³ de Areia média · faltam 4 m³ de Areia média"
    assert reg["desfazer"]["op"] == "desfazer_recebimento"
    assert (await vila.conta("areia"))["saldo"] == qtd(48)
    recs = (await vila.http.get(f"/pedidos/{vila.pedido['PED-0157']}/recebimentos", headers=vila.h)).json()
    assert [(x["nf_numero"], x["nf_valor"], x["nf_data"]) for x in recs] == [("4410", 432000, "2026-11-05")]
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("areia"))["saldo"] == qtd(12)


async def test_entrega_a_mais_so_com_confirmacao(vila):
    r = await vila.http.post("/propostas", headers=vila.h, json=_entrega(vila, [(0, qtd(45))]))
    assert r.status_code == 422 and "passa do que falta" in r.json()["detail"]
    r = await vila.http.post("/propostas", headers=vila.h, json=_entrega(vila, [(0, qtd(45))], confirmar_excesso=True))
    assert r.status_code == 201, r.text
    assert "(5 m³ a mais) · pedido completo" in r.json()["conta"]
    assert (await vila.http.post("/propostas", headers=vila.h, json=_entrega(vila, [(3, qtd(1))]))).status_code == 422
    # pedido em cotação não recebe entrega
    ruim = _entrega(vila, [(0, qtd(1))], pedido="PED-0167")
    assert (await vila.http.post("/propostas", headers=vila.h, json=ruim)).status_code == 422


async def test_entrega_revalida_na_aprovacao(vila):
    p = (await vila.http.post("/propostas", headers=vila.h, json=_entrega(vila, [(0, qtd(30))]))).json()
    # alguém registrou 20 pela tela no meio: os 30 agora passam do que falta
    await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/recebimentos", headers=vila.h, json={
        "linhas": [{"indice": 0, "quantidade": qtd(20)}], "chave_idempotencia": uuid.uuid4().hex})
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 422 and "passa do que falta" in r.json()["detail"]
    pend = (await vila.http.get("/propostas?status=pendente", headers=vila.h)).json()
    assert [x["id"] for x in pend] == [p["id"]]


# --- avanço: efeito e o encerramento que não aceita entrada ---

async def test_encerrar_parcial_nao_aceita_quantidade_nem_nf(vila):
    ped = vila.pedido["PED-0157"]
    await vila.http.post(f"/pedidos/{ped}/recebimentos", headers=vila.h, json={
        "linhas": [{"indice": 0, "quantidade": qtd(18)}], "chave_idempotencia": uuid.uuid4().hex})
    for extra in ({"nf": "4410"}, {"quantidade_recebida": qtd(7)}):
        r = await vila.http.post("/propostas", headers=vila.h, json=proposta(
            vila, "avancar_pedido", {"pedido_id": ped, "para": "conferido", **extra}))
        assert r.status_code == 422 and "só encerra o resto" in r.json()["detail"], r.text
        r = await vila.http.post(f"/pedidos/{ped}/avancar", headers=vila.h, json={"para": "conferido", **extra})
        assert r.status_code == 422
    r = await vila.http.post("/propostas", headers=vila.h, json=proposta(
        vila, "avancar_pedido", {"pedido_id": ped, "para": "conferido"}))
    assert r.json()["efeito"] == "encerramento"


async def test_proposta_de_conferir_que_mudou_de_sentido_nao_aprova(vila):
    ped = vila.pedido["PED-0157"]                                   # recebido: conferir = dar entrada de tudo
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(
        vila, "avancar_pedido", {"pedido_id": ped, "para": "conferido"}))).json()
    assert p["efeito"] == "entrada"
    await vila.http.post(f"/pedidos/{ped}/recebimentos", headers=vila.h, json={
        "linhas": [{"indice": 0, "quantidade": qtd(18)}], "chave_idempotencia": uuid.uuid4().hex})
    # agora parcial: aprovar ENCERRARIA, que não é o que o cartão mostrou
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 409 and "mudou desde a proposta" in r.json()["detail"]


async def test_efeito_das_outras_propostas(vila):
    compra = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    ajuste = (await vila.http.post("/propostas", headers=vila.h, json=proposta(
        vila, "ajuste_estoque", {"insumo_id": vila.insumo["bloco"], "quantidade": -qtd(2), "motivo": "quebra"}))).json()
    status = (await vila.http.post("/propostas", headers=vila.h, json=proposta(
        vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0158"], "para": "recebido"}))).json()
    assert (compra["efeito"], ajuste["efeito"], status["efeito"]) == ("compra", "ajuste", "status")


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
    e = s["entregas"]
    # PED-0160 com previsão 01/11 (hoje); PED-0157 (28/10) e o aço (03/11) não: atrasado / próximos dias
    assert [x["numero"] for x in e["hoje"]] == ["PED-0160"]
    assert [x["numero"] for x in e["atrasadas"]] == ["PED-0157"]
    assert [x["numero"] for x in e["proximos_7_dias"]] == ["PED-0158"]
    assert e["nf_para_conferir"] == 1
    assert [(x["nome"], x["falta"]) for x in s["almoxarifado"]["falta_comprar"]] == [
        ("Areia média", qtd(8)), ("Bloco cerâmico 14x19x29", qtd(2040)), ("Cimento CP-II 50 kg", qtd(34))]
    await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    p = (await vila.http.get(f"/obras/{vila.obra}/resumo", headers=vila.h)).json()["pendencias"]
    assert (p["propostas"], p["titulos"]) == (1, ["Comprar 34 sc de cimento"])
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{vila.obra}/resumo", headers=h)).status_code == 200


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


async def test_historico_da_obra_traz_todos_os_insumos_mais_novos_primeiro(vila):
    brita = (await vila.http.post(f"/obras/{vila.obra}/insumos", headers=vila.h,
                                  json={"nome": "Brita 1", "unidade": "m³"})).json()["id"]
    await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": brita, "tipo": "entrada", "quantidade": qtd(18), "chave_idempotencia": uuid.uuid4().hex})
    r = await vila.http.get(f"/obras/{vila.obra}/movimentos?limite=200", headers=vila.h)
    assert r.status_code == 200, r.text
    movs = r.json()
    assert movs[0]["insumo_id"] == brita                                   # o mais novo primeiro
    assert len({m["insumo_id"] for m in movs}) > 1                         # não só um insumo
    assert [m["criado_em"] for m in movs] == sorted((m["criado_em"] for m in movs), reverse=True)
    pagina = (await vila.http.get(f"/obras/{vila.obra}/movimentos?limite=1", headers=vila.h)).json()
    assert [m["id"] for m in pagina] == [movs[0]["id"]]
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{vila.obra}/movimentos", headers=h)).status_code == 200
    assert (await vila.http.get("/obras/nao-existe/movimentos", headers=vila.h)).status_code == 404
