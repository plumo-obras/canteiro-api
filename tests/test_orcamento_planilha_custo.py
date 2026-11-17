"""A planilha de custo feita à mão como orçamento da obra: colunas sem título, data-base no título, complemento, verba e
subtotal de grupo."""
from test_obras_orcamento import XLSX, nova_obra, xlsx
from test_orcamento_versoes import CABECALHO, orcamento, versao

# Como a planilha de quem orça à mão vem: só os valores têm título; número e descrição ficam em colunas sem nome, a
# medida das portas numa coluna à parte, e quase tudo é verba (valor fechado, sem unidade).
CABECALHO_CUSTO = [None, None, None, None, None, None, "qtdade", "vlr unit orç", "vlr unit. Atual", "ORÇADO SET/23"]
PLANILHA = xlsx(
    CABECALHO_CUSTO,
    [None, "ok", 7, "PAREDES E PAINES", None, None, None, None, None, None],
    [None, None, 7.1, "Ceramica tijolo", None, None, None, None, None, 0],
    [None, None, 7.7, "Materiasi diversos", None, None, None, None, None, 154859.7],
    [None, None, 7.8, "Mão de Obra", None, None, None, None, None, 157969.3],
    [None, None, None, None, None, None, None, None, None, 312829],
    [None, "ok", 11, "ESQUADRIA MADEIRA", None, None, None, None, None, None],
    [None, None, 11.1, "Portas", "0.60x2,10", None, 34, 498.66, 0, 16954.44],
    [None, None, 11.2, "Portas", "0.70 x 2,10", None, 34, 520.36, 560, 17692.24],
    [None, None, 11.6, "Mão de Obra", None, None, 114, 86, 0, 9804],
    [None, None, None, None, None, None, None, None, None, 44450.68],
    [None, None, 12, "ESQUADRIA METÁLICA", None, None, None, None, None, None],
    [None, None, 12.5, "Mão de Obra", None, None, 0, 0, None, 180000],
    [None, None, None, None, "CUSTO TOTAL DE MATERIAIS", None, None, None, None, 1687462.78],
)
MAPA = {"planilha": "Orçamento", "linha_cabecalho": 1, "item": 2, "nome": 3, "complemento": 4, "quantidade": 6,
        "valor_unitario": 7, "valor_total": 9}


async def test_ler_acha_numero_e_descricao_sem_titulo_e_a_data_base(vila):
    o = await nova_obra(vila)
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/ler", headers=vila.h,
                             files={"arquivo": ("custo.xlsx", PLANILHA, XLSX)})
    assert r.status_code == 200, r.text
    d = r.json()
    s = d["sugestao"]
    assert (s["item"], s["nome"], s["quantidade"], s["valor_unitario"], s["valor_total"]) == (2, 3, 6, 7, 9)
    assert d["data_base"] == "2023-09"
    titulos = {c["indice"]: c["titulo"] for c in d["colunas"]}
    # coluna sem título só aparece se tem dado: a A (vazia) não, a B ("ok") sim
    assert 0 not in titulos and titulos[1] == "Coluna B"
    assert titulos[2] == "Coluna C" and titulos[3] == "Coluna D" and titulos[4] == "Coluna E"
    assert titulos[9] == "ORÇADO SET/23"


async def test_ler_sugere_complemento_pelo_titulo(vila):
    o = await nova_obra(vila)
    planilha = xlsx(["Item", "Descrição", "Complemento", "Un", "Qtd", "Total"],
                    ["1", "ESQUADRIAS", None, None, None, None],
                    ["1.1", "Porta", "0,60 x 2,10", "un", 2, 1000])
    r = await vila.http.post(f"/obras/{o['id']}/orcamento/ler", headers=vila.h,
                             files={"arquivo": ("o.xlsx", planilha, XLSX)})
    assert r.json()["sugestao"]["complemento"] == 2 and r.json()["data_base"] is None
    p = (await versao(vila, o["id"], planilha, {"planilha": "Orçamento", "linha_cabecalho": 1, "item": 0, "nome": 1,
                                                "complemento": 2, "unidade": 3, "quantidade": 4,
                                                "valor_total": 5})).json()
    assert [(x["descricao"], x["complemento"]) for x in p["itens"] if x["tipo"] == "item"] == [("Porta", "0,60 x 2,10")]


async def test_planilha_de_custo_vira_orcamento_com_verbas(vila):
    o = await nova_obra(vila)
    r = await versao(vila, o["id"], PLANILHA, MAPA)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["erros"] == []
    itens = [(x["numero"], x["descricao"], x["complemento"], x["unidade"], x["quantidade"], x["total"])
             for x in p["itens"] if x["tipo"] == "item"]
    assert itens == [
        ("7.7", "Materiasi diversos", "", "vb", 1000, 15485970),
        ("7.8", "Mão de Obra", "", "vb", 1000, 15796930),
        ("11.1", "Portas", "0.60x2,10", "vb", 1000, 1695444),
        ("11.2", "Portas", "0.70 x 2,10", "vb", 1000, 1769224),
        ("11.6", "Mão de Obra", "", "vb", 1000, 980400),
        ("12.5", "Mão de Obra", "", "vb", 1000, 18000000),
    ]
    assert p["total"] == 53727968
    assert [(e["etapa"], e["descricao"], e["total"]) for e in p["etapas"]] == [
        ("7", "PAREDES E PAINES", 31282900), ("11", "ESQUADRIA MADEIRA", 4445068),
        ("12", "ESQUADRIA METÁLICA", 18000000)]
    # o tópico sem valor fica como título; a linha do subtotal sem descrição e o "CUSTO TOTAL" são ignorados
    assert ("7.1", "etapa", 2) in [(x["numero"], x["tipo"], x["nivel"]) for x in p["itens"]]
    assert sorted(i["motivo"] for i in p["ignoradas"]) == ["sem descrição"] * 3
    avisos = [a["mensagem"] for a in p["avisos"]]
    assert avisos[0].startswith("Sem a coluna da unidade")
    assert "11.1 Portas: sem unidade, entra como verba (1 vb de R$ 16.954,44). A quantidade 34 ficou de fora" in \
        avisos[3]
    assert avisos[6] == "12.5 Mão de Obra: sem unidade, entra como verba (1 vb de R$ 180.000,00)."

    gravado = await versao(vila, o["id"], PLANILHA, MAPA, aplicar=True)
    assert gravado.status_code == 201, gravado.text
    orc = await orcamento(vila, o["id"])
    assert orc["totais"]["contratado"] == 53727968
    assert {x["complemento"] for x in orc["itens"] if x["descricao"] == "Portas"} == {"0.60x2,10", "0.70 x 2,10"}


async def test_subtotal_de_grupo_nao_soma_duas_vezes(vila):
    o = await nova_obra(vila)
    planilha = xlsx(
        CABECALHO,
        # a etapa com o valor na própria linha e linhas embaixo: título (o valor confere, sem aviso)
        ["1", None, None, "ESQUADRIAS", None, None, None, 3000],
        # o tópico com unidade, quantidade e valor, e o detalhe embaixo: vale a soma do detalhe
        ["1.1", None, None, "Portas", "un", 2, 1250, 2500],
        ["1.1.1", None, None, "Porta 0,60", "un", 1, 1000, 1000],
        ["1.1.2", None, None, "Porta 0,70", "un", 1, 1100, 1100],
        # verba do grupo, sem unidade e sem detalhe
        ["1.2", None, None, "Ferragens", None, None, None, 900],
        # "Custo mão de obra" numerado é linha do orçamento, não subtotal
        ["1.3", None, None, "Custo mão de obra", "h", 10, 10, 100],
        [None, None, None, "CUSTO TOTAL", None, None, None, 2100],
        # etapa com valor e sem linha nenhuma: o valor não entra, com aviso
        ["2", None, None, "LIMPEZA", None, None, None, 500],
    )
    p = (await versao(vila, o["id"], planilha, {**_mapa_eap(), "linha_cabecalho": 1})).json()
    assert p["erros"] == []
    assert [(x["numero"], x["tipo"], x["total"]) for x in p["itens"]] == [
        ("1", "etapa", 0), ("1.1", "etapa", 0), ("1.1.1", "item", 100000), ("1.1.2", "item", 110000),
        ("1.2", "item", 90000), ("1.3", "item", 10000), ("2", "etapa", 0)]
    assert p["total"] == 310000
    assert [x["motivo"] for x in p["ignoradas"]] == ["subtotal ou total"]
    assert [a["mensagem"] for a in p["avisos"]] == [
        "1 ESQUADRIAS diz R$ 3.000,00 e as linhas dele somam R$ 3.100,00: vale a soma das linhas.",
        "1.1 Portas diz R$ 2.500,00 e as linhas dele somam R$ 2.100,00: vale a soma das linhas.",
        "A etapa 2 LIMPEZA tem R$ 500,00 e nenhuma linha: o valor não entra no orçamento. Ponha o valor numa "
        "linha 2.1.",
        "1.2 Ferragens: sem unidade, entra como verba (1 vb de R$ 900,00).",
    ]


def _mapa_eap():
    return {"planilha": "Orçamento", "item": 0, "codigo": 1, "fonte": 2, "nome": 3, "unidade": 4, "quantidade": 5,
            "valor_unitario": 6, "valor_total": 7}


def test_data_base_pelo_titulo():
    from canteiro_api.servicos.orcamento import _data_base, _letra

    assert _data_base(["qtd", "ORÇADO SET/23"]) == "2023-09"
    assert _data_base(["Preço 09/2023"]) == "2023-09"
    assert _data_base(["Valor de março 2024"]) == "2024-03"
    assert _data_base(["Total", "Unitário"]) is None
    assert [_letra(j) for j in (0, 2, 25, 26, 27)] == ["A", "C", "Z", "AA", "AB"]


async def test_verba_seguida_de_itens_sem_numero_e_titulo_do_grupo(vila):
    o = await nova_obra(vila)
    planilha = xlsx(
        CABECALHO,
        ["1", None, None, "FUNDAÇÃO", None, None, None, None],
        # sem unidade, com valor, e os itens dele vêm sem número: é o título do grupo, não verba
        ["1.1", None, None, "Estacas", None, None, None, 1000],
        [None, None, None, "Concreto", "m³", 1, 600, 600],
        [None, None, None, "Aço", "kg", 10, 50, 500],
        # com unidade, seguido de item sem número: continua item (como sempre foi)
        ["1.2", None, None, "Blocos", "un", 2, 100, 200],
        [None, None, None, "Forma", "m²", 1, 30, 30],
    )
    p = (await versao(vila, o["id"], planilha, {**_mapa_eap(), "linha_cabecalho": 1})).json()
    assert p["erros"] == []
    assert [(x["numero"], x["tipo"], x["total"]) for x in p["itens"]] == [
        ("1", "etapa", 0), ("1.1", "etapa", 0), ("", "item", 60000), ("", "item", 50000), ("1.2", "item", 20000),
        ("", "item", 3000)]
    assert p["total"] == 133000
    assert [a["mensagem"] for a in p["avisos"]] == [
        "1.1 Estacas diz R$ 1.000,00 e as linhas dele somam R$ 1.100,00: vale a soma das linhas."]


async def test_subtotal_da_etapa_conta_os_itens_sem_numero_e_o_grupo_vira_topico(vila):
    o = await nova_obra(vila)
    planilha = xlsx(
        CABECALHO,
        # 1.100 do grupo das estacas (itens sem número) + 200 dos blocos + 30 da forma: fecha com os 1.330 da etapa
        ["1", None, None, "FUNDAÇÃO", None, None, None, 1330],
        ["1.1", None, None, "Estacas", None, None, None, 1100],
        [None, None, None, "Concreto", "m³", 1, 600, 600],
        [None, None, None, "Aço", "kg", 10, 50, 500],
        ["1.2", None, None, "Blocos", "un", 2, 100, 200],
        [None, None, None, "Forma", "m²", 1, 30, 30],
        # a etapa com valor e só itens sem número: os itens entram no orçamento, e o valor confere com eles
        ["2", None, None, "LIMPEZA", None, None, None, 500],
        [None, None, None, "Caçamba", "un", 1, 500, 500],
    )
    p = (await versao(vila, o["id"], planilha, {**_mapa_eap(), "linha_cabecalho": 1})).json()
    assert p["erros"] == [] and p["avisos"] == []
    assert p["total"] == 183000
    # os itens sem número do grupo "1.1" caem no tópico dele; o item solto depois do item "1.2" continua sem tópico
    assert [(t["etapa"], t["nome"], t["total"]) for t in p["topicos"]] == [("1", "Estacas", 110000),
                                                                         ("1", "Blocos", 20000)]
    estacas = p["topicos"][0]["id"]
    assert [(x["descricao"], x["topico_id"] == estacas) for x in p["itens"] if x["tipo"] == "item"] == [
        ("Concreto", True), ("Aço", True), ("Blocos", False), ("Forma", False), ("Caçamba", False)]

    assert (await versao(vila, o["id"], planilha, {**_mapa_eap(), "linha_cabecalho": 1}, aplicar=True)).status_code \
        == 201
    orc = await orcamento(vila, o["id"])
    e1 = next(e for e in orc["etapas"] if e["etapa"] == "1")
    assert [(t["nome"], t["contratado"]) for t in e1["topicos"]] == [("Estacas", 110000), ("Blocos", 20000),
                                                                     ("Sem tópico", 3000)]


async def test_somar_nao_poe_a_linha_solta_da_planilha_no_grupo_do_contrato(vila):
    o = await nova_obra(vila)
    m = {**_mapa_eap(), "linha_cabecalho": 1}
    contrato = xlsx(CABECALHO, ["1", None, None, "FUNDAÇÃO", None, None, None, None],
                    ["1.1", None, None, "Estacas", None, None, None, 1100],
                    [None, None, None, "Concreto", "m³", 1, 600, 600],
                    [None, None, None, "Aço", "kg", 10, 50, 500])
    assert (await versao(vila, o["id"], contrato, m, aplicar=True)).status_code == 201
    # a "Forma" vem logo abaixo da etapa, fora de qualquer grupo; somada, ela entra depois do "Aço" do contrato
    planilha = xlsx(CABECALHO, ["1", None, None, "FUNDAÇÃO", None, None, None, None],
                    [None, None, None, "Forma", "m²", 1, 30, 30])
    p = (await versao(vila, o["id"], planilha, m, somar="true")).json()
    assert p["erros"] == [] and p["topicos"] == []
    assert [(x["descricao"], x["topico_id"]) for x in p["itens"] if x["tipo"] == "item"] == [("Forma", None)]
    assert (await versao(vila, o["id"], planilha, m, aplicar=True, somar="true")).status_code == 201
    orc = await orcamento(vila, o["id"])
    e1 = next(e for e in orc["etapas"] if e["etapa"] == "1")
    assert [(t["nome"], t["contratado"]) for t in e1["topicos"]] == [("Estacas", 110000), ("Sem tópico", 3000)]
