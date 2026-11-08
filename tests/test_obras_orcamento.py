"""Onboarding: criar e corrigir a obra, ler a planilha de orçamento do jeito que ela vem, importar e aprovar."""
import io
import json
import uuid

from openpyxl import Workbook

from canteiro_dominio.tipos import qtd, reais

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def nova_obra(vila, **campos):
    corpo = {"nome": f"Obra {uuid.uuid4().hex[:6]}", "chave_idempotencia": uuid.uuid4().hex, **campos}
    r = await vila.http.post("/obras", headers=vila.h, json=corpo)
    assert r.status_code == 201, r.text
    return r.json()


def xlsx(*linhas, abas=("Orçamento",)) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = abas[0]
    for linha in linhas:
        ws.append(list(linha))
    for nome in abas[1:]:
        wb.create_sheet(nome).append(["outra coisa"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# Planilha como sai de verdade: título e dados da obra antes do cabeçalho, etapa, subtotal e material repetido.
CABECALHO = ["Item", "Descrição", "Unid.", "Qtde.", "Preço Unit. (R$)", "Total (R$)"]
PLANILHA = xlsx(
    ["Orçamento — Residência Teste"],
    [],
    ["Cliente: Fulano"],
    CABECALHO,
    ["1", "FUNDAÇÃO", None, None, None, None],
    ["1.1", "Cimento CP-II 50 kg", "sc", 40, 37.9, 1516],
    ["1.2", "Concreto usinado fck 25", "m³", 12.5, None, 6250],
    [None, "Subtotal fundação", None, None, None, 7766],
    ["2", "ALVENARIA", None, None, None, None],
    ["2.1", "Cimento CP-II 50 kg", "sc", 10, 40, 400],
    ["2.2", "Bloco de concreto 14x19x39", "un", "1.200,0", "3,10", None],
    abas=("Orçamento", "Resumo"))


async def registro(vila, id_):
    """A entrada pelo id, não pelo topo da lista: duas escritas no mesmo milissegundo empatam na ordem por criado_em."""
    return next(x for x in (await vila.http.get("/registro", headers=vila.h)).json() if x["id"] == id_)


async def ler(vila, conteudo, nome="orcamento.xlsx", obra=None, planilha=None):
    return await vila.http.post(f"/obras/{obra or vila.obra}/orcamento/ler", headers=vila.h,
                                files={"arquivo": (nome, conteudo, XLSX)},
                                data={"planilha": planilha} if planilha else {})


async def importar(vila, conteudo, mapeamento, aplicar=False, nome="orcamento.xlsx", obra=None):
    return await vila.http.post(f"/obras/{obra or vila.obra}/orcamento/importar", headers=vila.h,
                                files={"arquivo": (nome, conteudo, XLSX)},
                                data={"mapeamento": json.dumps(mapeamento), "aplicar": str(aplicar).lower()})


def mapa(**kw):
    base = {"planilha": "Orçamento", "linha_cabecalho": 4, "nome": 1, "unidade": 2, "quantidade": 3,
            "valor_unitario": 4, "valor_total": 5}
    return {**base, **kw}


# --- obra ---

async def test_criar_obra_nasce_em_orcamento_e_repete_pela_chave(vila):
    corpo = {"nome": "  Sobrado   Jardim ", "chave_idempotencia": uuid.uuid4().hex, "uf": "sp", "tipologia": "sobrado",
             "area_construida": qtd("182,5"), "pavimentos": 2, "padrao": "normal", "regime": "empreitada",
             "inicio": "2026-11-03", "prazo_meses": 10, "verba": reais("950.000,00")}
    r = await vila.http.post("/obras", headers=vila.h, json=corpo)
    assert r.status_code == 201, r.text
    o = r.json()
    assert (o["nome"], o["status"], o["uf"], o["area_construida"]) == ("Sobrado Jardim", "em_orcamento", "SP", 182500)
    reg = await registro(vila, o["registro_id"])
    assert reg["texto"] == "Criou a obra Sobrado Jardim" and reg["desfazer"] is None

    de_novo = await vila.http.post("/obras", headers=vila.h, json=corpo)
    assert de_novo.status_code == 200 and de_novo.json()["id"] == o["id"]
    assert de_novo.json()["registro_id"] == o["registro_id"]
    outra = await vila.http.post("/obras", headers=vila.h, json={**corpo, "nome": "Outra"})
    assert outra.status_code == 409
    assert o["id"] in [x["id"] for x in (await vila.http.get("/obras", headers=vila.h)).json()]


async def test_criar_obra_valida_e_exige_admin(vila):
    h_viewer, _ = await vila.usuario("viewer")
    corpo = {"nome": "Casa", "chave_idempotencia": uuid.uuid4().hex}
    assert (await vila.http.post("/obras", headers=h_viewer, json=corpo)).status_code == 403
    for ruim in ({"padrao": "luxo"}, {"inicio": "03/11/2026"}, {"nome": "   "}, {"pavimentos": -1}, {"x": 1}):
        r = await vila.http.post("/obras", headers=vila.h, json={**corpo, **ruim})
        assert r.status_code == 422, ruim


async def test_editar_obra_registra_so_o_que_mudou_e_desfaz(vila):
    o = await nova_obra(vila, padrao="baixo")
    url = f"/obras/{o['id']}"
    r = await vila.http.patch(url, headers=vila.h, json={"area_construida": qtd(120), "padrao": "alto", "uf": "SP",
                                                         "cidade": "", "pavimentos": 0})
    assert r.status_code == 200, r.text
    assert (r.json()["area_construida"], r.json()["padrao"]) == (120000, "alto")
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Atualizou a obra {o['nome']}: UF, área, padrão"

    igual = await vila.http.patch(url, headers=vila.h, json={"padrao": "alto"})
    assert igual.status_code == 200 and igual.json()["registro_id"] is None

    desfeito = await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)
    assert desfeito.status_code == 200, desfeito.text
    obra = next(x for x in (await vila.http.get("/obras", headers=vila.h)).json() if x["id"] == o["id"])
    assert (obra["padrao"], obra["area_construida"], obra["uf"]) == ("baixo", 0, "")


async def test_desfazer_edicao_recusa_se_o_campo_mudou_depois(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}"
    r1 = (await vila.http.patch(url, headers=vila.h, json={"verba": reais(500_000)})).json()
    await vila.http.patch(url, headers=vila.h, json={"verba": reais(600_000)})
    r = await vila.http.post(f"/registro/{r1['registro_id']}/desfazer", headers=vila.h)
    assert r.status_code == 409 and "mudaram" in r.json()["detail"]
    assert (await vila.http.patch(url, headers=vila.h, json={"nome": None})).status_code == 422
    assert (await vila.http.patch(f"/obras/{uuid.uuid4()}", headers=vila.h, json={"uf": "RJ"})).status_code == 404


# --- ler a planilha ---

async def test_ler_xlsx_acha_o_cabecalho_depois_do_titulo_e_sugere_colunas(vila):
    r = await ler(vila, PLANILHA)
    assert r.status_code == 200, r.text
    lp = r.json()
    assert lp["planilhas"] == ["Orçamento", "Resumo"] and lp["planilha"] == "Orçamento"
    assert lp["linha_cabecalho"] == 4
    assert [c["titulo"] for c in lp["colunas"]] == CABECALHO
    s = lp["sugestao"]
    assert (s["nome"], s["unidade"], s["quantidade"], s["valor_unitario"], s["valor_total"]) == (1, 2, 3, 4, 5)
    assert s["saldo"] is None and s["curva"] is None
    assert lp["amostra"][0][1] == "FUNDAÇÃO" and lp["amostra"][1][3:5] == ["40", "37,9"]
    assert len(lp["amostra"]) == 7

    outra = await ler(vila, PLANILHA, planilha="Resumo")
    assert outra.json()["planilha"] == "Resumo"
    assert (await ler(vila, PLANILHA, planilha="Não existe")).status_code == 422


async def test_ler_xlsx_pula_a_capa_e_vai_para_a_aba_do_orcamento(vila):
    wb = Workbook()
    wb.active.title = "Capa"
    wb.active["A1"] = "Construtora Exemplo"
    ws = wb.create_sheet("Orçamento")
    for linha in (["Orçamento"], [], CABECALHO, ["1.1", "Cimento", "sc", 10, 39, 390]):
        ws.append(linha)
    b = io.BytesIO()
    wb.save(b)
    lp = (await ler(vila, b.getvalue())).json()
    assert (lp["planilhas"], lp["planilha"], lp["linha_cabecalho"]) == (["Capa", "Orçamento"], "Orçamento", 3)
    capa = (await ler(vila, b.getvalue(), planilha="Capa")).json()
    assert capa["planilha"] == "Capa" and capa["sugestao"]["nome"] is None


async def test_ler_csv_com_titulo_e_formatos_recusados(vila):
    csv = ("Orçamento da obra\n\nDescrição;Unidade;Quantidade;Valor unitário;Estoque;Curva ABC\n"
           "Cal hidratada;sc;30;18,50;4;C\n").encode("cp1252")
    r = await ler(vila, csv, nome="orc.csv")
    assert r.status_code == 200, r.text
    lp = r.json()
    assert lp["planilhas"] == ["CSV"] and lp["linha_cabecalho"] == 3
    s = lp["sugestao"]
    assert (s["nome"], s["unidade"], s["quantidade"], s["valor_unitario"], s["saldo"], s["curva"]) == (0, 1, 2, 3, 4, 5)
    assert lp["amostra"] == [["Cal hidratada", "sc", "30", "18,50", "4", "C"]]

    xls = await ler(vila, b"\xd0\xcf\x11\xe0" + b"\0" * 100, nome="velho.xls")
    assert xls.status_code == 422 and ".xlsx" in xls.json()["detail"]
    assert (await ler(vila, b"PK\x03\x04lixo", nome="quebrado.xlsx")).status_code == 422
    assert (await ler(vila, b"", nome="vazio.csv")).status_code == 422
    h_viewer, _ = await vila.usuario("viewer")
    r = await vila.http.post(f"/obras/{vila.obra}/orcamento/ler", headers=h_viewer,
                             files={"arquivo": ("orc.csv", csv, "text/csv")})
    assert r.status_code == 403


# --- importar ---

async def test_previa_ignora_titulos_junta_repetidos_e_calcula_valores(vila):
    o = await nova_obra(vila)
    r = await importar(vila, PLANILHA, mapa(), obra=o["id"])
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["aplicado"] is False and p["erros"] == []
    assert [(x["linha"], x["motivo"]) for x in p["ignoradas"]] == [
        (5, "sem unidade (título ou subtotal?)"), (8, "sem unidade (título ou subtotal?)"),
        (9, "sem unidade (título ou subtotal?)")]
    por_nome = {x["nome"]: x for x in p["linhas"]}
    cimento = por_nome["Cimento CP-II 50 kg"]
    # 40 × 37,90 + 10 × 40,00 = 1.916,00 em 50 sc → 38,32
    assert cimento["linhas"] == [6, 10] and cimento["quantidade"] == qtd(50)
    assert cimento["total"] == reais("1916") and cimento["valor_unitario"] == reais("38,32")
    concreto = por_nome["Concreto usinado fck 25"]           # sem unitário: vem do total
    assert concreto["valor_unitario"] == reais(500) and concreto["total"] == reais(6250)
    bloco = por_nome["Bloco de concreto 14x19x39"]            # texto em formato brasileiro
    assert bloco["quantidade"] == qtd(1200) and bloco["total"] == reais(3720)
    assert p["total"] == reais("11886") and (p["novos"], p["atualizados"], p["juntadas"]) == (3, 0, 1)
    assert all(x["situacao"] == "novo" for x in p["linhas"])


async def test_curva_abc_pelo_peso_no_valor(vila):
    o = await nova_obra(vila)
    plan = xlsx(["Descrição", "Un", "Qtd", "Total", "Curva"],
                ["Aço", "kg", 1, 7000, None], ["Concreto", "m³", 1, 2000, None], ["Tinta", "lata", 1, 600, None],
                ["Prego", "kg", 1, 400, None], ["Lixa", "un", 1, None, None], ["Fio", "m", 1, 10, "a"])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2, "valor_total": 3,
         "curva": 4}
    p = (await importar(vila, plan, m, obra=o["id"])).json()
    assert {x["nome"]: x["curva"] for x in p["linhas"]} == {
        "Aço": "A", "Concreto": "A", "Tinta": "B", "Prego": "C", "Lixa": "C", "Fio": "A"}
    # sem preço nenhum não há o que pesar: fica B
    sem = xlsx(["Descrição", "Un", "Qtd"], ["Areia", "m³", 3])
    p = (await importar(vila, sem, {**m, "valor_total": None, "curva": None}, obra=o["id"])).json()
    assert p["linhas"][0]["curva"] == "B" and p["linhas"][0]["valor_unitario"] == 0


async def test_erros_por_linha_impedem_gravar(vila):
    o = await nova_obra(vila)
    plan = xlsx(CABECALHO, ["1", "Areia", "m³", "abc", 1, None], ["2", "Brita", "m³", "2.760", None, None],
                ["3", "Cal", "sc", 5, -3, None], ["4", "Cal", "kg", 5, 2, None], ["5", "Tijolo", "un", 0, 1, None],
                ["6", "Telha", "un", 100, 2, None])
    p = (await importar(vila, plan, mapa(linha_cabecalho=1), aplicar=True, obra=o["id"])).json()
    assert p["aplicado"] is False
    assert [(e["linha"], e["campo"]) for e in p["erros"]] == [
        (2, "quantidade"), (3, "quantidade"), (4, "valor_unitario")]
    assert "ambíguo" in p["erros"][1]["mensagem"]
    assert [(x["linha"], x["motivo"]) for x in p["ignoradas"]] == [(6, "sem quantidade")]
    # "Cal" em kg depois de "Cal" com erro: a 1ª não entrou, então a 2ª vale sozinha
    assert {x["nome"] for x in p["linhas"]} == {"Cal", "Telha"}
    assert (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json() == []

    conflito = xlsx(CABECALHO, ["1", "Cal", "sc", 5, 2, None], ["2", "cal ", "kg", 5, 2, None])
    p = (await importar(vila, conflito, mapa(linha_cabecalho=1), obra=o["id"])).json()
    assert p["erros"][0]["linha"] == 3 and "em sc na linha 2 e em kg" in p["erros"][0]["mensagem"]


async def test_mapeamento_incompleto_ou_invalido(vila):
    r = await importar(vila, PLANILHA, mapa(quantidade=None))
    assert r.status_code == 422 and "quantidade" in r.json()["detail"]
    r = await vila.http.post(f"/obras/{vila.obra}/orcamento/importar", headers=vila.h,
                             files={"arquivo": ("o.xlsx", PLANILHA, XLSX)}, data={"mapeamento": "{nao é json"})
    assert r.status_code == 422


async def test_aplicar_grava_e_reimportar_atualiza(vila):
    o = await nova_obra(vila)
    plan = xlsx(["Descrição", "Un", "Qtd", "Unit", "Saldo"],
                ["Cal hidratada", "sc", 30, "18,50", 4], ["Fio 2,5 mm", "m", 500, "1,20", None])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2, "valor_unitario": 3,
         "saldo": 4}
    r = await importar(vila, plan, m, aplicar=True, obra=o["id"])
    assert r.status_code == 200 and r.json()["aplicado"] is True, r.text
    itens = {x["insumo"]["nome"]: x for x in
             (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json()}
    cal = itens["Cal hidratada"]
    assert (cal["insumo"]["valor_unitario"], cal["insumo"]["precisa_ate_o_fim"]) == (1850, qtd(30))
    assert cal["conta"]["saldo"] == qtd(4) and itens["Fio 2,5 mm"]["conta"]["saldo"] == 0
    entradas = (await vila.http.get("/registro", headers=vila.h)).json()
    reg = next(x for x in entradas if x["texto"].startswith("Importou"))
    assert reg["texto"] == "Importou o orçamento: 2 novos, 0 atualizados (R$ 1.155,00)"

    # o cadastro pela tela usa o mesmo ID: "Cal hidratada" de novo é conflito, não duplicata
    dup = await vila.http.post(f"/obras/{o['id']}/insumos", headers=vila.h,
                               json={"nome": "cal  hidratada", "unidade": "sc"})
    assert dup.status_code == 409

    plan2 = xlsx(["Descrição", "Un", "Qtd", "Unit", "Saldo"], ["CAL HIDRATADA", "sc", 36, "19", 99])
    p = (await importar(vila, plan2, m, obra=o["id"])).json()
    linha = p["linhas"][0]
    assert (linha["situacao"], linha["precisa_antes"], linha["valor_antes"]) == ("atualiza", qtd(30), 1850)
    assert (p["novos"], p["atualizados"]) == (0, 1)
    assert (await importar(vila, plan2, m, aplicar=True, obra=o["id"])).json()["aplicado"] is True
    cal = next(x for x in (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json()
               if x["insumo"]["id"] == cal["insumo"]["id"])
    # o saldo da planilha só vale para insumo novo: o existente tem livro-razão
    assert (cal["insumo"]["precisa_ate_o_fim"], cal["insumo"]["valor_unitario"], cal["conta"]["saldo"]) == (
        qtd(36), 1900, qtd(4))
    assert cal["insumo"]["nome"] == "Cal hidratada"


async def test_atualiza_insumo_existente_e_recusa_unidade_diferente(vila):
    plan = xlsx(["Descrição", "Un", "Qtd"], ["Cimento CP-II 50 kg", "SC", 250], ["Areia média", "kg", 10])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2}
    p = (await importar(vila, plan, m)).json()
    assert [(x["nome"], x["situacao"], x["precisa_antes"]) for x in p["linhas"]] == [
        ("Cimento CP-II 50 kg", "atualiza", qtd(200))]
    assert p["erros"][0]["linha"] == 3 and "já está cadastrado em m³" in p["erros"][0]["mensagem"]


async def test_desfazer_importacao_do_orcamento_arquiva_novos_e_volta_os_atualizados(vila):
    o = await nova_obra(vila)
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2, "valor_unitario": 3,
         "saldo": 4}
    url = f"/obras/{o['id']}/insumos"
    plan1 = xlsx(["Descrição", "Un", "Qtd", "Unit", "Saldo"], ["Cal hidratada", "sc", 30, "18,50", 4])
    r1 = await importar(vila, plan1, m, aplicar=True, obra=o["id"])
    cal = (await vila.http.get(url, headers=vila.h)).json()[0]["insumo"]
    # a segunda importação atualiza a cal e cria o fio
    plan2 = xlsx(["Descrição", "Un", "Qtd", "Unit", "Saldo"], ["cal hidratada", "sc", 36, "19", None],
                 ["Fio 2,5 mm", "m", 500, "1,20", 50])
    r2 = await importar(vila, plan2, m, aplicar=True, obra=o["id"])
    (reg2,) = r2.json()["registro_ids"]
    desfeito = await vila.http.post(f"/registro/{reg2}/desfazer", headers=vila.h)
    assert desfeito.status_code == 200, desfeito.text
    assert desfeito.json()["texto"] == "Desfeito: importação do orçamento"
    itens = {x["insumo"]["nome"]: x for x in (await vila.http.get(url, headers=vila.h)).json()}
    assert set(itens) == {"Cal hidratada"}
    c = itens["Cal hidratada"]
    assert (c["insumo"]["precisa_ate_o_fim"], c["insumo"]["valor_unitario"], c["conta"]["saldo"]) == (
        qtd(30), 1850, qtd(4))

    refeito = await vila.http.post(f"/registro/{desfeito.json()['id']}/desfazer", headers=vila.h)
    assert refeito.status_code == 200, refeito.text
    itens = {x["insumo"]["nome"]: x for x in (await vila.http.get(url, headers=vila.h)).json()}
    assert itens["Cal hidratada"]["insumo"]["precisa_ate_o_fim"] == qtd(36)
    assert itens["Fio 2,5 mm"]["conta"]["saldo"] == qtd(50)

    # a primeira importação não se desfaz mais: a cal foi atualizada depois (e esse refazer ainda vale)
    r = await vila.http.post(f"/registro/{r1.json()['registro_ids'][0]}/desfazer", headers=vila.h)
    assert r.status_code == 409
    assert cal["id"] in {x["insumo"]["id"] for x in (await vila.http.get(url, headers=vila.h)).json()}


# --- aprovar ---

async def test_aprovar_orcamento_e_desfazer(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}/orcamento/aprovar"
    vazio = await vila.http.post(url, headers=vila.h)
    assert vazio.status_code == 422 and "Importe" in vazio.json()["detail"]

    plan = xlsx(["Descrição", "Un", "Qtd", "Unit"], ["Cal", "sc", 10, 20])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2, "valor_unitario": 3}
    await importar(vila, plan, m, aplicar=True, obra=o["id"])
    r = await vila.http.post(url, headers=vila.h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "em_andamento"
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Aprovou o orçamento de {o['nome']} (1 insumo, R$ 200,00)"
    de_novo = await vila.http.post(url, headers=vila.h)
    assert de_novo.status_code == 409

    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    obra = next(x for x in (await vila.http.get("/obras", headers=vila.h)).json() if x["id"] == o["id"])
    assert obra["status"] == "em_orcamento"
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 409
    h_viewer, _ = await vila.usuario("viewer")
    assert (await vila.http.post(url, headers=h_viewer)).status_code == 403


async def test_reabrir_recusa_se_a_obra_ja_mudou(vila, arm):
    """A receita só reabre obra em andamento (ex.: concluída depois não volta para orçamento)."""
    o = await nova_obra(vila)
    plan = xlsx(["Descrição", "Un", "Qtd"], ["Cal", "sc", 10])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "nome": 0, "unidade": 1, "quantidade": 2}
    await importar(vila, plan, m, aplicar=True, obra=o["id"])
    r = (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).json()
    await arm.atualizar("obras", vila.tenant_id, o["id"], {"status": "concluida"})
    d = await vila.http.post(f"/registro/{r['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 409


# --- materiais do projeto (a lista que o tradutor tira das pranchas) ---

MATERIAIS = {"origem": "projeto estrutural", "itens": [
    {"nome": "ACO CA-50, 10,0 MM, VERGALHAO", "unidade": "KG", "quantidade": qtd(220.3), "valor_unitario": 669},
    {"nome": "CONCRETO USINADO BOMBEAVEL C30", "unidade": "m3", "quantidade": qtd(21.65), "valor_unitario": 51263},
    {"nome": "Bloco EPS B8/30/125", "unidade": "un", "quantidade": qtd(382)},
]}


async def test_materiais_do_projeto_previa_grava_e_desfaz(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}/orcamento/materiais"
    p = (await vila.http.post(url, headers=vila.h, json=MATERIAIS)).json()
    assert p["aplicado"] is False and (p["novos"], p["atualizados"]) == (3, 0)
    concreto = next(x for x in p["linhas"] if x["nome"].startswith("CONCRETO"))
    assert concreto["unidade"] == "m³" and concreto["total"] == round(21.65 * 51263)  # m3 do tradutor vira m³
    assert concreto["curva"] == "A"  # o maior valor da lista
    assert (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json() == []  # prévia não grava

    r = await vila.http.post(url, headers=vila.h, json={**MATERIAIS, "aplicar": True})
    assert r.status_code == 200 and r.json()["aplicado"] is True, r.text
    (reg,) = r.json()["registro_ids"]
    entrada = await registro(vila, reg)
    assert entrada["texto"] == "Importou os materiais do projeto estrutural: 3 novos, 0 atualizados (R$ 12.572,25)"
    itens = {x["insumo"]["nome"]: x for x in (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json()}
    aco = itens["ACO CA-50, 10,0 MM, VERGALHAO"]
    # o projeto vira o "precisa": a conta do que falta comprar é a de sempre (nada em estoque, nada a caminho)
    assert aco["insumo"]["precisa_ate_o_fim"] == qtd(220.3) and aco["conta"]["falta"] == qtd(220.3)

    # mandar de novo atualiza (prévia mostra o antes), não duplica
    p2 = (await vila.http.post(url, headers=vila.h, json=MATERIAIS)).json()
    assert (p2["novos"], p2["atualizados"]) == (0, 3)

    desfeito = await vila.http.post(f"/registro/{reg}/desfazer", headers=vila.h)
    assert desfeito.status_code == 200, desfeito.text
    assert desfeito.json()["texto"] == "Desfeito: importação dos materiais do projeto estrutural"
    assert (await vila.http.get(f"/obras/{o['id']}/insumos", headers=vila.h)).json() == []


async def test_materiais_do_projeto_recusa_unidade_diferente_e_corpo_invalido(vila):
    url = f"/obras/{vila.obra}/orcamento/materiais"
    # a Casa Vila Nova já tem o cimento em sc: em kg é erro, e com erro nada é gravado
    corpo = {"origem": "projeto estrutural", "aplicar": True,
             "itens": [{"nome": "Cimento CP-II 50 kg", "unidade": "kg", "quantidade": qtd(10)}]}
    p = (await vila.http.post(url, headers=vila.h, json=corpo)).json()
    assert p["aplicado"] is False and "já está cadastrado em" in p["erros"][0]["mensagem"]
    vazio = await vila.http.post(url, headers=vila.h, json={"origem": "projeto estrutural", "itens": []})
    assert vazio.status_code == 422
    zero = {"origem": "x", "itens": [{"nome": "Areia", "unidade": "m³", "quantidade": 0}]}
    assert (await vila.http.post(url, headers=vila.h, json=zero)).status_code == 422
