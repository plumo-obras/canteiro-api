"""Onboarding: criar e corrigir a obra, ler a planilha de orçamento do jeito que ela vem e aprovar."""
import io
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
    assert "saldo" not in s and "curva" not in s
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


async def test_ler_xlsx_escolhe_a_aba_com_o_cabecalho_mais_completo(vila):
    # Planilha de licitação: antes do orçamento vem a tabela de encargos (CÓDIGO e DESCRIÇÃO bastavam para ela
    # ganhar), e depois vêm as composições (CPU), com quase o mesmo cabeçalho do orçamento.
    wb = Workbook()
    wb.active.title = "Capa"
    wb.active["A1"] = "Construtora Exemplo"
    encargos = wb.create_sheet("Encargos sociais")
    for linha in (["Encargos sociais"], ["CÓDIGO", "DESCRIÇÃO", "SEM DESONERAÇÃO"], ["A1", "INSS", 0.2]):
        encargos.append(linha)
    orcamento = wb.create_sheet("Sintético")
    for linha in (["Orçamento sintético"], [], CABECALHO, ["1.1", "Cimento", "sc", 10, 39, 390]):
        orcamento.append(linha)
    cpu = wb.create_sheet("CPU")
    for linha in (["Composições"], ["Código", "Descrição", "Unid.", "Coef.", "Total"],
                  ["88309", "Pedreiro", "h", 1, 30]):
        cpu.append(linha)
    b = io.BytesIO()
    wb.save(b)
    lp = (await ler(vila, b.getvalue())).json()
    assert (lp["planilha"], lp["linha_cabecalho"]) == ("Sintético", 3)
    assert lp["sugestao"]["quantidade"] == 3


async def test_ler_xlsx_ignora_aba_de_grafico(vila):
    # Aba de gráfico (a curva S, por exemplo) não tem células: ler todas as abas quebrava com 500. Ela não entra nem
    # na escolha nem na lista de abas da tela, antes ou depois do orçamento.
    from openpyxl.chart import BarChart, Reference

    wb = Workbook()
    ws = wb.active
    ws.title = "Orçamento"
    for linha in ([], CABECALHO, ["1.1", "Cimento", "sc", 10, 39, 390]):
        ws.append(linha)
    for nome, posicao in (("Curva S", None), ("Gráfico", 0)):
        g = BarChart()
        g.add_data(Reference(ws, min_col=6, min_row=2, max_row=3), titles_from_data=True)
        wb.create_chartsheet(nome, posicao).add_chart(g)
    b = io.BytesIO()
    wb.save(b)
    r = await ler(vila, b.getvalue())
    assert r.status_code == 200, r.text
    assert (r.json()["planilhas"], r.json()["planilha"], r.json()["linha_cabecalho"]) == (["Orçamento"], "Orçamento", 2)
    assert (await ler(vila, b.getvalue(), planilha="Curva S")).status_code == 422

    so_grafico = Workbook()
    g = BarChart()
    g.add_data(Reference(so_grafico.active, min_col=1, min_row=1, max_row=1))
    so_grafico.create_chartsheet("Curva S").add_chart(g)
    so_grafico.remove(so_grafico.active)
    b = io.BytesIO()
    so_grafico.save(b)
    r = await ler(vila, b.getvalue())
    assert r.status_code == 422 and "só gráficos" in r.json()["detail"]


async def test_preco_solto_ao_lado_do_total_e_o_unitario(vila):
    # Cabeçalho das planilhas SINAPI de licitação: "PREÇO(R$)" é o unitário, porque o total tem coluna própria.
    csv = ("ITEM;CÓDIGO;DESCRIÇÃO;UNIDADE;QUANT.;PREÇO(R$);PREÇO TOTAL (R$)\n"
           "1.1.1;;Mestre de obras;H;1056,00;35,10;37065,60\n").encode()
    s = (await ler(vila, csv, nome="sinapi.csv")).json()["sugestao"]
    campos = ("item", "codigo", "nome", "unidade", "quantidade", "valor_unitario", "valor_total")
    assert [s[c] for c in campos] == [0, 1, 2, 3, 4, 5, 6]
    # Sem total ao lado, "Preço" pode ser qualquer um dos dois: fica sem palpite.
    so_preco = (await ler(vila, "Descrição;Unidade;Quantidade;Preço\nCal;sc;30;18,50\n".encode(), nome="p.csv")).json()
    assert so_preco["sugestao"]["valor_unitario"] is None


async def test_ler_csv_com_titulo_e_formatos_recusados(vila):
    csv = ("Orçamento da obra\n\nDescrição;Unidade;Quantidade;Valor unitário;Estoque;Curva ABC\n"
           "Cal hidratada;sc;30;18,50;4;C\n").encode("cp1252")
    r = await ler(vila, csv, nome="orc.csv")
    assert r.status_code == 200, r.text
    lp = r.json()
    assert lp["planilhas"] == ["CSV"] and lp["linha_cabecalho"] == 3
    s = lp["sugestao"]
    assert (s["nome"], s["unidade"], s["quantidade"], s["valor_unitario"]) == (0, 1, 2, 3)
    assert lp["amostra"] == [["Cal hidratada", "sc", "30", "18,50", "4", "C"]]

    xls = await ler(vila, b"\xd0\xcf\x11\xe0" + b"\0" * 100, nome="velho.xls")
    assert xls.status_code == 422 and ".xlsx" in xls.json()["detail"]
    assert (await ler(vila, b"PK\x03\x04lixo", nome="quebrado.xlsx")).status_code == 422
    assert (await ler(vila, b"", nome="vazio.csv")).status_code == 422
    h_viewer, _ = await vila.usuario("viewer")
    r = await vila.http.post(f"/obras/{vila.obra}/orcamento/ler", headers=h_viewer,
                             files={"arquivo": ("orc.csv", csv, "text/csv")})
    assert r.status_code == 403


# --- aprovar ---

async def _contrato_de_um_item(vila, obra):
    from test_orcamento_versoes import versao

    plan = xlsx(["Item", "Descrição", "Un", "Qtd", "Unit"], ["1", "PRELIMINARES", None, None, None],
                ["1.1", "Cal", "sc", 10, 20])
    m = {"planilha": "Orçamento", "linha_cabecalho": 1, "item": 0, "nome": 1, "unidade": 2, "quantidade": 3,
         "valor_unitario": 4}
    r = await versao(vila, obra, plan, m, aplicar=True)
    assert r.status_code == 201, r.text


async def test_aprovar_orcamento_e_desfazer(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}/orcamento/aprovar"
    vazio = await vila.http.post(url, headers=vila.h)
    assert vazio.status_code == 422 and "Importe" in vazio.json()["detail"]

    await _contrato_de_um_item(vila, o["id"])
    r = await vila.http.post(url, headers=vila.h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "em_andamento"
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Aprovou o orçamento de {o['nome']} (1 item, R$ 200,00)"
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
    await _contrato_de_um_item(vila, o["id"])
    r = (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).json()
    await arm.atualizar("obras", vila.tenant_id, o["id"], {"status": "concluida"})
    d = await vila.http.post(f"/registro/{r['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 409


# --- materiais do projeto como versão de orçamento (etapa "Estrutura") ---

async def aplicar_estrutura(vila, obra_id, corpo):
    return await vila.http.post(f"/obras/{obra_id}/orcamento/estrutura", headers=vila.h,
                                json={**corpo, "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})


ESTRUTURA = {"etapa": "Estrutura", "itens": [
    {"nome": "Aço CA-50 Ø 10,0 mm (SINAPI 34)", "unidade": "kg", "quantidade": qtd(220.3), "valor_unitario": 669},
    {"nome": "Concreto usinado C30 (SINAPI 34494)", "unidade": "m³", "quantidade": qtd(21.65), "valor_unitario": 51263},
]}


async def test_estrutura_vira_contrato_com_etapa_e_desfaz(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}/orcamento/estrutura"
    # prévia
    p = (await vila.http.post(url, headers=vila.h, json=ESTRUTURA)).json()
    assert p["tipo"] == "contrato" and p["n_itens"] == 2 and p["n_etapas"] == 1
    assert p["etapas"][0]["descricao"] == "Estrutura"
    # 220,3 × 6,69 + 21,65 × 512,63 = 1.473,81 + 11.098,44
    assert p["total"] == round(220.3 * 669) + round(21.65 * 51263)

    r = await aplicar_estrutura(vila, o["id"], ESTRUTURA)
    assert r.status_code == 201, r.text
    orc = (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=vila.h)).json()
    assert orc["totais"]["contratado"] == p["total"]
    v = next(x for x in orc["versoes"] if x["tipo"] == "contrato")
    assert v["ativa"] and v["descricao"] == "Estrutura"
    reg = await registro(vila, v["registro_id"])
    assert "contrato" in reg["texto"].lower()

    desfeito = await vila.http.post(f"/registro/{v['registro_id']}/desfazer", headers=vila.h)
    assert desfeito.status_code == 200, desfeito.text
    # desfazer desativa a versão (fica no histórico): não sobra contrato ativo
    orc2 = (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=vila.h)).json()
    assert not any(v["ativa"] for v in orc2["versoes"]) and orc2["totais"]["atual"] == 0


async def test_estrutura_soma_antes_de_aprovar_e_vira_aditivo_depois(vila):
    o = await nova_obra(vila)
    url = f"/obras/{o['id']}/orcamento/estrutura"
    await aplicar_estrutura(vila, o["id"], ESTRUTURA)
    # de novo, antes de aprovar: soma ao contrato (o mesmo material volta marcado como repetido)
    p = (await vila.http.post(url, headers=vila.h, json=ESTRUTURA)).json()
    assert p["tipo"] == "contrato" and p["substitui"]
    assert all(x["repetido_de"] for x in p["itens"] if x["tipo"] == "item")
    r = await aplicar_estrutura(vila, o["id"], ESTRUTURA)
    assert r.status_code == 201, r.text
    orc = (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=vila.h)).json()
    assert [v["tipo"] for v in orc["versoes"] if v["ativa"]] == ["contrato"]
    assert orc["totais"]["aditivos"] == 0 and orc["totais"]["n_itens"] == 4 and orc["totais"]["n_etapas"] == 1
    assert [x["numero"] for x in orc["itens"]] == ["1", "1.1", "1.2", "1.3", "1.4"]
    # depois de aprovar: aditivo
    assert (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).status_code == 200
    p = (await vila.http.post(url, headers=vila.h, json=ESTRUTURA)).json()
    assert p["tipo"] == "aditivo"
    r = await aplicar_estrutura(vila, o["id"], ESTRUTURA)
    assert r.status_code == 201, r.text
    orc = (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=vila.h)).json()
    assert sum(1 for v in orc["versoes"] if v["ativa"]) == 2  # contrato + aditivo
