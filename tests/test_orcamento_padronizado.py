"""O orçamento padronizado: a planilha da pessoa entra encaixada na lista padrão de etapas (o nosso modelo), sempre em
custo, com orçado × gasto por etapa e por tópico (a etapa da planilha de onde cada linha veio)."""
import json

from test_gastos_orcamento import _gasto
from test_obras_orcamento import nova_obra, xlsx
from test_orcamento_planilha_custo import MAPA, PLANILHA
from test_orcamento_versoes import CABECALHO, mapa, orcamento, versao

from canteiro_dominio.encaixe import ordem_da_etapa


async def _padronizada(vila, obra, conteudo=PLANILHA, m=None, status=201, **kw):
    r = await versao(vila, obra, conteudo, m or MAPA, padronizar="true", **kw)
    assert r.status_code == status, r.text
    return r.json()


def _abertura(orc):
    return [(e["etapa"], e["descricao"], e["atual"], [(t["nome"], t["atual"]) for t in e["topicos"]])
            for e in orc["etapas"]]


async def test_planilha_feita_a_mao_entra_encaixada_na_lista_padrao(vila):
    # a sonda de 15/11: sem o título da etapa, "Mão de Obra" de paredes ia para "Outros serviços" e juntava com a das
    # esquadrias num tópico só
    obra = (await nova_obra(vila))["id"]
    previa = await _padronizada(vila, obra, status=200)
    assert previa["padronizada"] and len(previa["etapas_disponiveis"]) == 24
    assert [(e["etapa_planilha"], e["descricao"], e["destino"], e["total"]) for e in previa["encaixe"]] == [
        ("7", "PAREDES E PAINES", "alvenaria", 31282900),
        ("11", "ESQUADRIA MADEIRA", "esquadrias", 4445068),
        ("12", "ESQUADRIA METÁLICA", "esquadrias", 18000000)]
    assert [(e["etapa"], e["descricao"], e["total"]) for e in previa["etapas"]] == [
        ("alvenaria", "Alvenaria e vedações", 31282900), ("esquadrias", "Esquadrias", 22445068)]
    assert previa["total"] == 53727968

    p = await _padronizada(vila, obra, aplicar=True)
    orc = await orcamento(vila, obra)
    assert _abertura(orc) == [
        ("alvenaria", "Alvenaria e vedações", 31282900, [("PAREDES E PAINES", 31282900)]),
        ("esquadrias", "Esquadrias", 22445068, [("ESQUADRIA MADEIRA", 4445068), ("ESQUADRIA METÁLICA", 18000000)])]
    assert orc["totais"]["atual"] == 53727968 and orc["totais"]["estimado"] == 0
    assert next(v for v in orc["versoes"] if v["id"] == p["versao_id"])["padronizada"] is True
    # na árvore de lançamento, o nome da etapa padrão sem a chave na frente e os tópicos do orçamento travados
    r = await vila.http.get(f"/obras/{obra}/arvore", headers=vila.h)
    assert [(e["chave"], e["nome"], [(t["nome"], t["origem"]) for t in e["topicos"]]) for e in r.json()] == [
        ("alvenaria", "Alvenaria e vedações", [("PAREDES E PAINES", "orcamento")]),
        ("esquadrias", "Esquadrias", [("ESQUADRIA MADEIRA", "orcamento"), ("ESQUADRIA METÁLICA", "orcamento")])]

    # o gasto lançado no tópico conta nele e na fase
    metalica = next(t for t in orc["etapas"][1]["topicos"] if t["nome"] == "ESQUADRIA METÁLICA")
    await _gasto(vila, obra, valor=500000, etapa="esquadrias", topico_id=metalica["topico_id"])
    orc = await orcamento(vila, obra)
    t = next(t for t in orc["etapas"][1]["topicos"] if t["nome"] == "ESQUADRIA METÁLICA")
    assert (t["gasto"], t["saldo"]) == (500000, 17500000)
    assert orc["etapas"][1]["gasto"] == 500000


async def test_a_pessoa_troca_o_destino_da_etapa_ou_de_uma_linha(vila):
    obra = (await nova_obra(vila))["id"]
    previa = await _padronizada(vila, obra, status=200)
    linha_portas = next(x["linha"] for x in previa["itens"] if x["numero"] == "11.1")
    previa = await _padronizada(vila, obra, status=200,
                                  destinos=json.dumps({"12": "outros", f"linha:{linha_portas}": "pisos"}))
    encaixe = {e["etapa_planilha"]: e for e in previa["encaixe"]}
    assert (encaixe["12"]["destino"], encaixe["12"]["escolhida"]) == ("outros", True)
    assert [(d["etapa"], d["total"]) for d in encaixe["11"]["etapas"]] == [("esquadrias", 2749624),
                                                                            ("pisos", 1695444)]
    assert [e["descricao"] for e in previa["etapas"]] == ["Alvenaria e vedações", "Esquadrias", "Pisos", "Outros"]
    r = await versao(vila, obra, PLANILHA, MAPA, padronizar="true", destinos='{"12": "garagem"}')
    assert r.status_code == 422 and "Etapa desconhecida" in r.json()["detail"]


async def test_planilha_de_venda_vira_custo_pelo_bdi(vila):
    obra = (await nova_obra(vila))["id"]
    eap = xlsx(CABECALHO, ["1", None, None, "FUNDAÇÕES", None, None, None, None],
               ["1.1", "94966", "SINAPI", "Concreto fck 25 MPa", "m³", 10, 1250, 12500],
               ["2", None, None, "ALVENARIA", None, None, None, None],
               ["2.1", "87905", "SINAPI", "Alvenaria de blocos cerâmicos", "m²", 100, 100, 10000])
    # sem o BDI (a obra não tem), não há como tirar o custo
    r = await versao(vila, obra, eap, mapa(linha_cabecalho=1), padronizar="true", natureza="venda")
    assert r.status_code == 422 and "BDI" in r.json()["detail"]
    p = await _padronizada(vila, obra, eap, mapa(linha_cabecalho=1), aplicar=True, natureza="venda", bdi="2500")
    assert (p["natureza"], p["bdi"], p["total"], p["total_venda"]) == ("venda", 2500, 1800000, 2250000)
    concreto = next(x for x in p["itens"] if x["numero"] == "1.1")
    # o concreto da etapa de fundações é fundação; o custo é o preço ÷ 1,25 e o preço fica guardado
    assert (concreto["etapa"], concreto["valor_unitario"], concreto["total"], concreto["total_venda"]) == (
        "fundacao", 100000, 1000000, 1250000)
    orc = await orcamento(vila, obra)
    assert [(e["descricao"], e["atual"], e["venda"]) for e in orc["etapas"]] == [
        ("Fundação", 1000000, 1250000), ("Alvenaria e vedações", 800000, 1000000)]
    # "FUNDAÇÕES" já é a etapa padrão: o tópico continua saindo da numeração (o nível 2), sem repetir o nome da etapa
    assert [t["nome"] for e in orc["etapas"] for t in e["topicos"]] == ["Concreto fck 25 MPa",
                                                                         "Alvenaria de blocos cerâmicos"]
    assert (orc["totais"]["atual"], orc["totais"]["venda"]) == (1800000, 2250000)


async def test_previsao_da_obra_fica_fora_do_orcado(vila):
    obra = (await nova_obra(vila))["id"]
    await _padronizada(vila, obra, aplicar=True)
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    assert r.json()["estimadas"] > 0
    orc = await orcamento(vila, obra)
    # o orçado continua o da planilha; a fase estimada aparece com o estimado, na ordem da obra
    assert orc["totais"]["atual"] == 53727968 and orc["totais"]["estimado"] > 0
    assert [e["etapa"] for e in orc["etapas"]] == sorted((e["etapa"] for e in orc["etapas"]), key=ordem_da_etapa)
    fundacao = next(e for e in orc["etapas"] if e["descricao"] == "Fundação")
    assert (fundacao["atual"], fundacao["estimado"] > 0) == (0, True)
    # a versão não engorda com o chute
    assert next(v for v in orc["versoes"] if v["ativa"])["total"] == 53727968
    # aprovar fixa o orçado da planilha, sem o estimado
    r = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    assert (await orcamento(vila, obra))["totais"]["atual"] == 53727968


async def test_aditivo_de_orcamento_padronizado_cai_nas_etapas_e_nos_topicos_dele(vila):
    obra = (await nova_obra(vila))["id"]
    contrato = xlsx(CABECALHO, ["7", None, None, "PAREDES", None, None, None, None],
                    ["7.1", "87495", "SINAPI", "Alvenaria de tijolo cerâmico", "m²", 10, 100, 1000])
    await _padronizada(vila, obra, contrato, mapa(linha_cabecalho=1), aplicar=True)
    aditivo = xlsx(CABECALHO, ["A", None, None, "ACRÉSCIMOS", None, None, None, None],
                   ["A.1", "87495", "SINAPI", "Tijolo a mais", "m²", 2, 100, 200],
                   ["A.2", None, None, "Pintura látex do muro", "m²", 5, 20, 100])
    r = await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                     data="2026-11-20")
    assert r.status_code == 200, r.text
    itens = [(x["descricao"], x["etapa"], x["grupo"]) for x in r.json()["itens"] if x["tipo"] == "item"]
    # pelo código, o tijolo cai na etapa e no tópico do contrato; a pintura, que não casou, vai para a etapa dela
    assert itens == [("Tijolo a mais", "alvenaria", "PAREDES"), ("Pintura látex do muro", "pintura", "ACRÉSCIMOS")]
    assert [t["nome"] for t in r.json()["topicos"]] == ["PAREDES", "ACRÉSCIMOS"]
    # somar não cabe: a numeração da planilha cairia nas chaves das fases
    r = await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), somar="true")
    assert r.status_code == 422 and "etapas padrão" in r.json()["detail"]


async def test_padronizar_depois_recusa_quando_ha_gasto_na_etapa_que_muda_de_sentido(vila):
    obra = (await nova_obra(vila))["id"]
    r = await versao(vila, obra, PLANILHA, MAPA, aplicar=True)
    assert r.status_code == 201, r.text
    await _gasto(vila, obra, valor=10000, etapa="7")
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 409 and "etapas 7" in r.json()["detail"], r.text


async def test_gasto_lancado_na_lista_padrao_antes_da_planilha_conta_no_orcado(vila):
    # na obra sem orçamento o gasto entra na lista padrão; a planilha padronizada usa as mesmas chaves, e o gasto não
    # vira "sem etapa do orçamento"
    obra = (await nova_obra(vila))["id"]
    await _gasto(vila, obra, valor=120000, etapa="alvenaria")
    await _padronizada(vila, obra, aplicar=True)
    orc = await orcamento(vila, obra)
    alvenaria = next(e for e in orc["etapas"] if e["etapa"] == "alvenaria")
    assert (alvenaria["gasto"], alvenaria["saldo"]) == (120000, 31282900 - 120000)
    assert orc["totais"]["gastos"]["sem_etapa_do_orcamento"] == 0
