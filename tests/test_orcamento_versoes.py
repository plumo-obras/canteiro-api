"""Orçamento da obra como EAP em versões: etapas e itens como na planilha, contrato e aditivos, desfazer."""
import json
import uuid

from test_obras_orcamento import XLSX, nova_obra, registro, xlsx

# Como a planilha de obra pública vem: capa, cabeçalho com ITEM/CÓDIGO/FONTE, etapa, itens numerados sem a linha da
# subetapa, subtotal, o mesmo serviço em duas etapas e unitário com 3 casas (o total da linha é o do contrato).
CABECALHO = ["ITEM", "CÓDIGO", "FONTE", "DESCRIÇÃO DOS SERVIÇOS", "UN.", "QUANT.", "PREÇO UNIT. (R$)", "TOTAL (R$)"]
CONTRATO = xlsx(
    ["PROPOSTA DE PREÇOS · ORÇAMENTO CONTRATADO"],
    ["Obra: Escola Teste · BDI 22,91% incluído"],
    [],
    CABECALHO,
    ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
    ["1.1", "74209/1", "SINAPI", "Placa de obra em chapa galvanizada", "m²", 10, 238.91, 2389.1],
    ["1.2", "93212", "SINAPI", "Sanitário em canteiro", "m²", 2.52, 562.218, 1416.79],
    [None, None, None, "Subtotal 1", None, None, None, 3805.89],
    ["2", None, None, "FUNDAÇÕES", None, None, None, None],
    ["2.1.1", "93358", "SINAPI", "Escavação manual de valas", "m³", 289.2, 57.12, 16519.1],
    ["2.2.1", "93358", "SINAPI", "Escavação manual de valas", "m³", 10.76, 57.12, 614.61],
    ["2.2.2", "92915", "SINAPI", "Armação de aço CA-60 5,0mm", "kg", 12.23, 11.15, 136.36],
    [None, None, None, "Subtotal 2", None, None, None, 17270.07],
    [None, None, None, "TOTAL GERAL", None, None, None, 21075.96],
)
LINHAS_ADITIVO = [
    ["1º termo aditivo"],
    CABECALHO,
    ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
    ["1.14", "74220/1", "SINAPI", "Tapume de chapa de madeira", "m²", 436, 45.23, 19720.28],
    ["2", None, None, "FUNDAÇÕES", None, None, None, None],
    ["2.2.1", "93358", "SINAPI", "Escavação manual de valas (supressão)", "m³", -10.76, 57.12, 614.61],
    ["2.9", "92915", "SINAPI", "Armação de aço CA-60 5,0mm", "m²", 1, 11.15, 11.15],
]
ADITIVO = xlsx(*LINHAS_ADITIVO)


def mapa(linha_cabecalho=4, **kw):
    return {"planilha": "Orçamento", "linha_cabecalho": linha_cabecalho, "item": 0, "codigo": 1, "fonte": 2,
            "nome": 3, "unidade": 4, "quantidade": 5, "valor_unitario": 6, "valor_total": 7, **kw}


async def versao(vila, obra, conteudo, m, *, tipo="contrato", aplicar=False, chave=None, **extra):
    data = {"mapeamento": json.dumps(m), "tipo": tipo, "aplicar": str(aplicar).lower(), **extra}
    if aplicar:
        data["chave_idempotencia"] = chave or uuid.uuid4().hex
    return await vila.http.post(f"/obras/{obra}/orcamento/versoes", headers=vila.h,
                                files={"arquivo": ("orcamento.xlsx", conteudo, XLSX)}, data=data)


async def orcamento(vila, obra):
    r = await vila.http.get(f"/obras/{obra}/orcamento", headers=vila.h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_ler_sugere_item_codigo_e_fonte(vila):
    o = await nova_obra(vila)
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/ler", headers=vila.h,
                             files={"arquivo": ("o.xlsx", CONTRATO, XLSX)})
    assert r.status_code == 200, r.text
    s = r.json()["sugestao"]
    assert r.json()["linha_cabecalho"] == 4
    assert {k: s[k] for k in ("item", "codigo", "fonte", "nome", "unidade", "quantidade", "valor_unitario",
                              "valor_total")} == {"item": 0, "codigo": 1, "fonte": 2, "nome": 3, "unidade": 4,
                                                  "quantidade": 5, "valor_unitario": 6, "valor_total": 7}


async def test_previa_guarda_etapas_itens_e_fecha_no_centavo(vila):
    o = await nova_obra(vila)
    r = await versao(vila, o["id"], CONTRATO, mapa())
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["aplicado"] is False and p["erros"] == [] and p["substitui"] is None
    assert p["n_etapas"] == 2 and p["n_itens"] == 5
    assert p["total"] == 2107596                     # = TOTAL GERAL da planilha, sem juntar a escavação repetida
    assert [(e["etapa"], e["descricao"], e["total"]) for e in p["etapas"]] == [
        ("1", "SERVIÇOS PRELIMINARES", 380589), ("2", "FUNDAÇÕES", 1727007)]
    sanitario = next(x for x in p["itens"] if x["codigo"] == "93212")
    assert sanitario["total"] == 141679 and sanitario["valor_unitario"] == 56222
    assert [sanitario[k] for k in ("numero", "fonte", "etapa", "nivel")] == ["1.2", "SINAPI", "1", 2]
    escav = [x for x in p["itens"] if x["codigo"] == "93358"]
    assert [(x["numero"], x["etapa"], x["nivel"]) for x in escav] == [("2.1.1", "2", 3), ("2.2.1", "2", 3)]
    assert sorted(i["motivo"] for i in p["ignoradas"]) == ["subtotal ou total"] * 3
    assert (await orcamento(vila, o["id"]))["versoes"] == []        # prévia não grava


async def test_erros_e_avisos(vila):
    o = await nova_obra(vila)
    ruim = xlsx(CABECALHO, ["1", None, None, "ETAPA", None, None, None, None],
                ["1.1", "1", "SINAPI", "Negativo", "m²", -2, 10, None],
                ["1.2", "2", "SINAPI", "Ambíguo", "m²", "2.760", 10, None])
    p = (await versao(vila, o["id"], ruim, mapa(linha_cabecalho=1))).json()
    assert [(e["linha"], e["campo"]) for e in p["erros"]] == [(3, "quantidade"), (4, "quantidade")]
    gravar = await versao(vila, o["id"], ruim, mapa(linha_cabecalho=1), aplicar=True)
    assert gravar.status_code == 200 and gravar.json()["aplicado"] is False

    await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    p = (await versao(vila, o["id"], ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo")).json()
    assert p["erros"] == []
    # o aviso de unidade compara dentro da planilha: o aditivo tem o 92915 em m² só uma vez, então nada; o que sobra é
    # o de repetido (o 92915 já está no contrato)
    assert [a["linha"] for a in p["avisos"]] == [7] and "parece repetir" in p["avisos"][0]["mensagem"]
    dupla = xlsx(CABECALHO, ["1.1", "92915", "SINAPI", "Aço", "kg", 1, 10, None],
                 ["1.2", "92915", "SINAPI", "Aço", "m²", 1, 10, None])
    p = (await versao(vila, o["id"], dupla, mapa(linha_cabecalho=1))).json()
    assert p["avisos"] == [{"linha": 3, "mensagem": "O código 92915 aparece em kg (linha 2) e em m² (linha 3): "
                                                    "uma das duas unidades está errada."}]
    sem_col = await versao(vila, o["id"], CONTRATO, mapa(unidade=None))
    assert sem_col.status_code == 422


async def test_contrato_aditivo_e_desfazer(vila):
    o = await nova_obra(vila)
    sem_contrato = await versao(vila, o["id"], ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo")
    assert sem_contrato.status_code == 422 and "contratado" in sem_contrato.json()["detail"]

    chave = uuid.uuid4().hex
    r = await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True, chave=chave)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["aplicado"] and c["versao_id"]
    reg = await registro(vila, c["registro_id"])
    assert reg["texto"] == f"Importou o contrato de {o['nome']} (5 itens, R$ 21.075,96)"
    repete = await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True, chave=chave)
    assert repete.status_code == 200 and repete.json()["versao_id"] == c["versao_id"]
    outro = await versao(vila, o["id"], ADITIVO, mapa(linha_cabecalho=2), aplicar=True, chave=chave)
    assert outro.status_code == 409

    a = await versao(vila, o["id"], ADITIVO, mapa(linha_cabecalho=2), tipo="aditivo", aplicar=True,
                     descricao="tapume e aço", data="2020-04-15")
    assert a.status_code == 201, a.text
    reg_a = await registro(vila, a.json()["registro_id"])
    assert reg_a["texto"] == f"Importou o 1º aditivo de {o['nome']}: tapume e aço (+R$ 19.116,82)"

    orc = await orcamento(vila, o["id"])
    assert {k: orc["totais"][k] for k in ("contratado", "aditivos", "atual", "n_itens", "n_etapas")} == {
        "contratado": 2107596, "aditivos": 1911682, "atual": 4019278, "n_itens": 8, "n_etapas": 2}
    assert [(e["etapa"], e["contratado"], e["aditivos"]) for e in orc["etapas"]] == [
        ("1", 380589, 1972028), ("2", 1727007, -60346)]
    adit = next(v for v in orc["versoes"] if v["tipo"] == "aditivo")
    assert (adit["numero"], adit["descricao"], adit["data"], adit["ativa"]) == (1, "tapume e aço", "2020-04-15", True)
    # contrato intacto: os itens dele continuam com a quantidade original
    escav = [x for x in orc["itens"] if x["numero"] == "2.2.1"]
    assert [x["quantidade"] for x in escav] == [10760, -10760]
    assert orc["itens"][0]["tipo"] == "etapa" and orc["itens"][0]["curva"] is None
    assert {x["curva"] for x in orc["itens"] if x["tipo"] == "item"} <= {"A", "B", "C"}

    # contrato com aditivo valendo não se desfaz; o aditivo sim, e aí o contrato também
    trava = await vila.http.post(f"/registro/{c['registro_id']}/desfazer", headers=vila.h)
    assert trava.status_code == 409 and "aditivo" in trava.json()["detail"]
    d = await vila.http.post(f"/registro/{a.json()['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200, d.text
    assert (await orcamento(vila, o["id"]))["totais"]["aditivos"] == 0
    d = await vila.http.post(f"/registro/{c['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200, d.text
    vazio = await orcamento(vila, o["id"])
    assert vazio["itens"] == [] and vazio["totais"]["atual"] == 0 and len(vazio["versoes"]) == 2


async def test_reimportar_contrato_substitui_e_desfazer_volta_o_anterior(vila):
    o = await nova_obra(vila)
    c1 = (await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)).json()
    menor = xlsx(CABECALHO, ["1", None, None, "ÚNICA", None, None, None, None],
                 ["1.1", "1", "PRÓPRIO", "Serviço", "vb", 1, 100, None])
    p = (await versao(vila, o["id"], menor, mapa(linha_cabecalho=1))).json()
    assert p["substitui"] == {"versao_id": c1["versao_id"], "total": 2107596, "itens": 5}
    c2 = (await versao(vila, o["id"], menor, mapa(linha_cabecalho=1), aplicar=True)).json()
    assert (await registro(vila, c2["registro_id"]))["texto"].endswith("— substitui o anterior")
    assert (await orcamento(vila, o["id"]))["totais"]["atual"] == 10000

    # desfazer o primeiro já não vale: ele saiu de vigência
    assert (await vila.http.post(f"/registro/{c1['registro_id']}/desfazer", headers=vila.h)).status_code == 409
    d = await vila.http.post(f"/registro/{c2['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200 and "o anterior volta a valer" in d.json()["texto"]
    assert (await orcamento(vila, o["id"]))["totais"]["atual"] == 2107596


async def test_aprovar_pelo_contrato(vila):
    o = await nova_obra(vila)
    await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Aprovou o orçamento de {o['nome']} (5 itens, R$ 21.075,96)"


async def test_dados_do_contrato_na_obra(vila):
    o = await nova_obra(vila, numero_contrato=" 061/2019 ", bdi=2291, data_base="2017-01")
    assert (o["numero_contrato"], o["bdi"], o["data_base"]) == ("061/2019", 2291, "2017-01")
    ruim = await vila.http.post("/obras", headers=vila.h, json={"nome": "X", "chave_idempotencia": uuid.uuid4().hex,
                                                                 "data_base": "2017-13"})
    assert ruim.status_code == 422
    r = await vila.http.patch(f"/obras/{o['id']}", headers=vila.h, json={"bdi": 2500})
    assert (await registro(vila, r.json()["registro_id"]))["texto"].endswith(": BDI")


async def test_viewer_le_mas_nao_importa(vila):
    o = await nova_obra(vila)
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{o['id']}/orcamento", headers=h)).status_code == 200
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/versoes", headers=h,
                             files={"arquivo": ("o.xlsx", CONTRATO, XLSX)}, data={"mapeamento": json.dumps(mapa())})
    assert r.status_code == 403


async def test_aditivo_com_etapa_nova(vila):
    o = await nova_obra(vila)
    await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    nova = xlsx(CABECALHO, ["25", None, None, "AMPLIAÇÃO DO REFEITÓRIO", None, None, None, None],
                ["25.1", "1", "PRÓPRIO", "Parede", "m²", 10, 50, None])
    p = (await versao(vila, o["id"], nova, mapa(linha_cabecalho=1), tipo="aditivo")).json()
    assert p["n_etapas"] == 1
    assert p["etapas"] == [{"etapa": "25", "descricao": "AMPLIAÇÃO DO REFEITÓRIO", "total": 50000}]
    await versao(vila, o["id"], nova, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True, descricao="x")
    orc = await orcamento(vila, o["id"])
    assert orc["etapas"][-1]["descricao"] == "AMPLIAÇÃO DO REFEITÓRIO"
    assert all(v.get("registro_id") for v in orc["versoes"])


async def test_aprovar_soma_os_aditivos_vigentes(vila):
    o = await nova_obra(vila)
    await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    # o aço na mesma unidade do contrato: em duas unidades, a aprovação recusa (não dá para somar em Compras)
    aditivo = xlsx(*(["kg" if x[0] == "2.9" and c == "m²" else c for c in x] for x in LINHAS_ADITIVO))
    await versao(vila, o["id"], aditivo, mapa(linha_cabecalho=2), tipo="aditivo", aplicar=True)
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Aprovou o orçamento de {o['nome']} (8 itens, R$ 40.192,78)"


ADITIVO_A1 = xlsx(
    ["Aditivo 01"],
    CABECALHO,
    ["A1", None, None, "ACRÉSCIMOS", None, None, None, None],
    ["A1.1", "93382", "SINAPI", "Aterro apiloado", "m³", 100, 20.48, 2048],
    ["A1.2", "74209/1", "SINAPI", "Placa de obra (mais)", "m²", 2, 238.91, 477.82],
    ["A1.3", "92915", "SINAPI", "Armação CA-60 5,0mm", "kg", 10, 11.15, 111.5],
)


async def test_aditivo_cai_nas_etapas_do_contrato_pelo_codigo_ou_pela_escolha(vila):
    o = await nova_obra(vila)
    assert (await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)).status_code == 201
    m = mapa(linha_cabecalho=2)
    r = await versao(vila, o["id"], ADITIVO_A1, m, tipo="aditivo", descricao="Aditivo 01", data="2026-11-20")
    assert r.status_code == 200, r.text
    itens = [x for x in r.json()["itens"] if x["tipo"] == "item"]
    assert [(x["numero"], x["etapa"], x["etapa_origem"], x["etapa_planilha"]) for x in itens] == [
        ("A1.1", "A1", "planilha", "A1"), ("A1.2", "1", "codigo", "A1"), ("A1.3", "2", "codigo", "A1")]
    # o aterro não tem código no contrato: a pessoa aponta a etapa 2 (fundações) na prévia
    aterro = itens[0]["ordem"]
    r = await versao(vila, o["id"], ADITIVO_A1, m, tipo="aditivo", descricao="Aditivo 01", data="2026-11-20",
                     aplicar=True, etapas_itens=json.dumps({str(aterro): "2"}))
    assert r.status_code == 201, r.text
    assert [(e["etapa"], e["descricao"], e["total"]) for e in r.json()["etapas"]] == [
        ("2", "FUNDAÇÕES", 204800 + 11150), ("1", "SERVIÇOS PRELIMINARES", 47782)]
    assert not any(x["tipo"] == "etapa" for x in r.json()["itens"])        # o título "A1" ficou sem item
    orc = await orcamento(vila, o["id"])
    por = {e["etapa"]: e["aditivos"] for e in orc["etapas"]}
    assert (por["1"], por["2"], "A1" in por) == (47782, 215950, False)
    etapas = (await vila.http.get(f"/obras/{o['id']}/etapas", headers=vila.h)).json()
    assert [e["etapa"] for e in etapas] == ["1", "2"]
    ruim = await versao(vila, o["id"], ADITIVO_A1, m, tipo="aditivo", descricao="x", data="2026-11-20",
                        etapas_itens=json.dumps({str(aterro): "9"}))
    assert ruim.status_code == 422


# --- somar ao orçamento antes de aprovar, etapa pelo nome, item repetido ---

# Como as pranchas entram: uma etapa "Estrutura" numerada 1, com os materiais.
PRANCHAS = xlsx(CABECALHO, ["1", None, None, "Estrutura", None, None, None, None],
                ["1.1", None, None, "Concreto usinado fck 25", "m³", 20, 500, 10000],
                ["1.2", None, None, "Aço CA-50", "kg", 1000, 10, 10000],
                ["1.3", None, None, "Forma de madeira", "m²", 100, 50, 5000])
# A planilha do engenheiro: a etapa 1 dela é outra (serviços preliminares), e a estrutura vem como etapa 2.
PLANILHA = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
                ["1.1", None, None, "Locação da obra", "m²", 530, 5, 2650],
                ["1.2", None, None, "Tapume", "m", 100, 40, 4000],
                ["2", None, None, "ESTRUTURA", None, None, None, None],
                ["2.1", None, None, "Concreto fck 25 sapatas", "m³", 30, 600, 18000],
                ["2.2", None, None, "Aço CA-50 10 mm", "kg", 500, 12, 6000],
                ["2.3", None, None, "Porta de madeira", "m²", 4, 300, 1200])


def _por_desc(p):
    return {x["descricao"]: x for x in p["itens"] if x["tipo"] == "item"}


async def test_somar_ao_orcamento_antes_de_aprovar(vila):
    o = await nova_obra(vila)
    m = mapa(linha_cabecalho=1)
    sem = await versao(vila, o["id"], PLANILHA, m, somar="true")
    assert sem.status_code == 422 and "importe a planilha" in sem.json()["detail"]
    c = (await versao(vila, o["id"], PRANCHAS, m, aplicar=True)).json()
    forma = next(x for x in (await orcamento(vila, o["id"]))["itens"] if x["descricao"] == "Forma de madeira")
    # a edição feita no orçamento vigente vai junto para o somado
    e = await vila.http.patch(f"/obras/{o['id']}/orcamento/itens/{forma['id']}", headers=vila.h,
                              json={"valor_unitario": 4000})
    assert e.status_code == 200, e.text
    assert (await versao(vila, o["id"], PLANILHA, m, tipo="aditivo", somar="true")).status_code == 422

    p = (await versao(vila, o["id"], PLANILHA, m, somar="true")).json()
    assert p["tipo"] == "contrato" and p["erros"] == []
    itens = _por_desc(p)
    # Serviços preliminares não cai na Estrutura pelo número 1: vira a etapa 2, com os números trocados
    assert [(itens[d]["etapa"], itens[d]["numero"], itens[d]["etapa_origem"]) for d in ("Locação da obra", "Tapume")] \
        == [("2", "2.1", "planilha"), ("2", "2.2", "planilha")]
    # a ESTRUTURA da planilha casa com a Estrutura das pranchas pelo nome e continua a numeração dela
    assert [(itens[d]["etapa"], itens[d]["numero"], itens[d]["etapa_origem"])
            for d in ("Concreto fck 25 sapatas", "Aço CA-50 10 mm", "Porta de madeira")] == [
        ("1", "1.4", "nome"), ("1", "1.5", "nome"), ("1", "1.6", "nome")]
    assert itens["Concreto fck 25 sapatas"]["repetido_de"]["descricao"] == "Concreto usinado fck 25"
    assert itens["Aço CA-50 10 mm"]["repetido_de"] == {
        "item_id": next(x["id"] for x in (await orcamento(vila, o["id"]))["itens"] if x["descricao"] == "Aço CA-50"),
        "descricao": "Aço CA-50", "quantidade": 1000000, "unidade": "kg"}
    assert itens["Porta de madeira"]["repetido_de"] is None and itens["Tapume"]["repetido_de"] is None
    assert sorted(a["linha"] for a in p["avisos"]) == [6, 7]
    assert p["total"] == 3185000 and p["substitui"]["total"] == 2400000 and p["total_apos"] == 5585000
    assert [(e["etapa"], e["descricao"]) for e in p["etapas"]] == [("2", "SERVIÇOS PRELIMINARES"), ("1", "Estrutura")]

    concreto = itens["Concreto fck 25 sapatas"]["ordem"]
    chave = uuid.uuid4().hex
    extra = {"somar": "true", "ignorar_itens": json.dumps([concreto])}
    r = await versao(vila, o["id"], PLANILHA, m, aplicar=True, chave=chave, **extra)
    assert r.status_code == 201, r.text
    # o ignorado sai, e a numeração fecha sem buraco
    assert "Concreto fck 25 sapatas" not in _por_desc(r.json())
    reg = await registro(vila, r.json()["registro_id"])
    assert reg["texto"] == f"Somou 4 itens ao orçamento de {o['nome']} (+R$ 13.850,00)"
    repete = await versao(vila, o["id"], PLANILHA, m, aplicar=True, chave=chave, **extra)
    assert repete.status_code == 200 and repete.json()["versao_id"] == r.json()["versao_id"]
    outro = await versao(vila, o["id"], PLANILHA, m, aplicar=True, chave=chave, somar="true")
    assert outro.status_code == 409

    orc = await orcamento(vila, o["id"])
    assert [v["tipo"] for v in orc["versoes"] if v["ativa"]] == ["contrato"]
    assert [(x["numero"], x["descricao"]) for x in orc["itens"]] == [
        ("1", "Estrutura"), ("1.1", "Concreto usinado fck 25"), ("1.2", "Aço CA-50"), ("1.3", "Forma de madeira"),
        ("1.4", "Aço CA-50 10 mm"), ("1.5", "Porta de madeira"),
        ("2", "SERVIÇOS PRELIMINARES"), ("2.1", "Locação da obra"), ("2.2", "Tapume")]
    assert next(x for x in orc["itens"] if x["descricao"] == "Forma de madeira")["valor_unitario"] == 4000
    t = orc["totais"]
    assert (t["contratado"], t["aditivos"], t["aditivos_pct"], t["n_itens"]) == (2400000 + 1385000, 0, 0, 7)

    d = await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)
    assert d.status_code == 200, d.text
    assert d.json()["texto"] == (f"Desfeito: os itens somados ao orçamento de {o['nome']} saíram "
                                 "(o anterior volta a valer)")
    volta = await orcamento(vila, o["id"])
    assert volta["totais"]["contratado"] == 2400000 and volta["totais"]["n_itens"] == 3
    assert [v["id"] for v in volta["versoes"] if v["ativa"]] == [c["versao_id"]]

    # aprovado, somar é recusado: o caminho é o aditivo
    assert (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).status_code == 200
    depois = await versao(vila, o["id"], PLANILHA, m, somar="true")
    assert depois.status_code == 422 and "aditivo" in depois.json()["detail"]


async def test_aditivo_casa_a_etapa_pelo_nome_e_nao_pelo_numero(vila):
    o = await nova_obra(vila)
    await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    adit = xlsx(CABECALHO, ["1", None, None, "ESQUADRIAS", None, None, None, None],
                ["1.1", "1", "PRÓPRIO", "Janela de alumínio", "m²", 4, 500, None],
                ["3", None, None, "Fundações", None, None, None, None],
                ["3.1", "2", "PRÓPRIO", "Viga baldrame", "m³", 2, 800, None])
    p = (await versao(vila, o["id"], adit, mapa(linha_cabecalho=1), tipo="aditivo")).json()
    itens = _por_desc(p)
    # ESQUADRIAS não é a etapa 1 do contrato (serviços preliminares): etapa nova, no próximo número livre
    assert [itens["Janela de alumínio"][k] for k in ("etapa", "numero", "etapa_origem", "etapa_planilha")] == [
        "3", "3.1", "planilha", "1"]
    # Fundações casa com a FUNDAÇÕES do contrato (etapa 2) e continua a numeração dela
    assert [itens["Viga baldrame"][k] for k in ("etapa", "numero", "etapa_origem", "etapa_planilha")] == [
        "2", "2.3", "nome", "3"]
    assert [(e["etapa"], e["descricao"]) for e in p["etapas"]] == [("3", "ESQUADRIAS"), ("2", "FUNDAÇÕES")]
    r = await versao(vila, o["id"], adit, mapa(linha_cabecalho=1), tipo="aditivo", aplicar=True)
    assert r.status_code == 201, r.text
    orc = await orcamento(vila, o["id"])
    assert [(e["etapa"], e["descricao"], e["aditivos"]) for e in orc["etapas"]] == [
        ("1", "SERVIÇOS PRELIMINARES", 0), ("2", "FUNDAÇÕES", 160000), ("3", "ESQUADRIAS", 200000)]


async def test_percentual_de_aditivo_sobre_o_orcamento_somado(vila):
    o = await nova_obra(vila)
    m = mapa(linha_cabecalho=1)
    await versao(vila, o["id"], PRANCHAS, m, aplicar=True)
    s = await versao(vila, o["id"], PLANILHA, m, aplicar=True, somar="true")
    assert s.status_code == 201, s.text
    assert (await vila.http.post(f"/obras/{o['id']}/orcamento/aprovar", headers=vila.h)).status_code == 200
    extra = xlsx(CABECALHO, ["9", None, None, "PAISAGISMO", None, None, None, None],
                 ["9.1", None, None, "Grama", "m²", 100, 10, 1000])
    a = await versao(vila, o["id"], extra, m, tipo="aditivo", aplicar=True, descricao="grama")
    assert a.status_code == 201, a.text
    t = (await orcamento(vila, o["id"]))["totais"]
    # a base é o orçamento aprovado inteiro (pranchas + planilha = R$ 56.850), não só as pranchas (R$ 25.000)
    assert (t["contratado"], t["aditivos"], t["aditivos_pct"]) == (5685000, 100000, 176)


def test_parece_repetido():
    from canteiro_api.servicos.versoes import _parecidos, _termos

    def par(a, b):
        return _parecidos(_termos(a), _termos(b))

    assert par("Concreto fck 25 sapatas", "Concreto usinado fck 25")
    assert par("Aço CA-50 10 mm", "Aço CA-50")
    assert par("Tapume de madeira", "TAPUME DE MADEIRA")
    assert not par("Forma de madeira", "Porta de madeira")
    assert not par("Concreto fck 25", "Concreto fck 30")
    assert not par("Aço CA-50 10 mm", "Aço CA-50 8 mm")
    assert not par("Pintura látex PVA paredes", "Pintura látex acrílica teto")


async def test_item_escolhido_para_etapa_que_trocou_de_numero_nao_repete_numero(vila):
    # a etapa 1 da planilha vira a 2 (a 1 é Estrutura) e a 2 vira a 3; a Escavação é escolhida para a nova 2
    planilha = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
                    ["1.1", None, None, "Locação da obra", "m²", 530, 5, None],
                    ["1.2", None, None, "Tapume", "m", 100, 40, None],
                    ["2", None, None, "FUNDAÇÃO", None, None, None, None],
                    ["2.1", None, None, "Escavação", "m³", 50, 60, None])
    m = mapa(linha_cabecalho=1)
    for extra in ({"somar": "true"}, {"tipo": "aditivo"}):
        o = await nova_obra(vila)
        await versao(vila, o["id"], PRANCHAS, m, aplicar=True)
        p = (await versao(vila, o["id"], planilha, m, etapas_itens=json.dumps({"4": "2"}), **extra)).json()
        itens = _por_desc(p)
        assert [(itens[d]["etapa"], itens[d]["numero"]) for d in ("Locação da obra", "Tapume", "Escavação")] == [
            ("2", "2.1"), ("2", "2.2"), ("2", "2.3")], extra
