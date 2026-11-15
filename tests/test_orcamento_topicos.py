"""Tópicos do orçamento: o nível 2 da numeração da planilha entra na árvore da obra, junto com os tópicos manuais."""
import hashlib
import uuid

from test_gastos import _confirmacao
from test_gastos_orcamento import _gasto, _resumo
from test_obras_orcamento import nova_obra, xlsx
from test_orcamento_planilha_custo import MAPA, PLANILHA
from test_orcamento_versoes import CABECALHO, CONTRATO, mapa, orcamento, versao

from canteiro_api.esquemas import PedidoCorpo
from canteiro_api.servicos.pedidos import assinatura_do_pedido


async def _arvore(vila, obra):
    r = await vila.http.get(f"/obras/{obra}/arvore", headers=vila.h)
    assert r.status_code == 200, r.text
    return {e["chave"]: e for e in r.json()}


def _topicos(arvore, chave):
    return [(t["nome"], t["origem"]) for t in arvore[chave]["topicos"]]


async def _importar(vila, obra, conteudo=PLANILHA, m=None, **kw):
    r = await versao(vila, obra, conteudo, m or MAPA, aplicar=True, **kw)
    assert r.status_code == 201, r.text
    return r.json()


async def _desfazer(vila, registro_id, status=200):
    r = await vila.http.post(f"/registro/{registro_id}/desfazer", headers=vila.h)
    assert r.status_code == status, r.text
    return r.json()


async def test_obra_com_orcamento_mostra_os_topicos_dele(vila):
    # a sonda da integração: a planilha de custo entra como orçamento, e os tópicos dela ficam nas etapas DELA (nada
    # vai para a lista padrão, nem nasce etapa extra)
    obra = (await nova_obra(vila))["id"]
    previa = (await versao(vila, obra, PLANILHA, MAPA)).json()
    assert [(t["etapa"], t["nome"], t["total"]) for t in previa["topicos"]] == [
        ("7", "Materiasi diversos", 15485970), ("7", "Mão de Obra", 15796930), ("11", "Portas", 3464668),
        ("11", "Mão de Obra", 980400), ("12", "Mão de Obra", 18000000)]
    portas = {x["topico_id"] for x in previa["itens"] if x["descricao"] == "Portas"}
    assert len(portas) == 1 and next(iter(portas)).startswith("orc-")

    p = await _importar(vila, obra)
    a = await _arvore(vila, obra)
    assert list(a) == ["7", "11", "12"]
    assert _topicos(a, "7") == [("Materiasi diversos", "orcamento"), ("Mão de Obra", "orcamento")]
    assert _topicos(a, "11") == [("Portas", "orcamento"), ("Mão de Obra", "orcamento")]
    assert _topicos(a, "12") == [("Mão de Obra", "orcamento")]
    ids = [t["id"] for e in a.values() for t in e["topicos"]]
    assert len(set(ids)) == 5 and all(i.startswith("orc-") for i in ids)
    assert {t["id"] for t in previa["topicos"]} == set(ids)

    # reimportar a mesma planilha devolve os mesmos IDs (o gasto continua no tópico); o Desfazer volta os de antes
    p2 = await _importar(vila, obra)
    assert [t["id"] for e in (await _arvore(vila, obra)).values() for t in e["topicos"]] == ids
    await _desfazer(vila, p2["registro_id"])
    await _desfazer(vila, p["registro_id"])
    a = await _arvore(vila, obra)
    assert "7" not in a and all(e["topicos"] == [] for e in a.values())


async def test_topico_do_orcamento_nao_se_edita_e_o_manual_reaproveitado_tambem_nao(vila):
    obra = (await nova_obra(vila))["id"]
    so_mao_de_obra = xlsx([None, None, None, None, None, None, "qtdade", "vlr unit orç", "x", "ORÇADO SET/23"],
                          [None, None, 11, "ESQUADRIA MADEIRA", None, None, None, None, None, None],
                          [None, None, 11.6, "Mão de Obra", None, None, 114, 86, 0, 9804])
    await _importar(vila, obra, so_mao_de_obra)
    # o tópico manual nasce antes de a planilha completa trazer "Portas" para a mesma etapa
    manual = await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h, json={"etapa": "11", "nome": "portas"})
    assert manual.status_code == 201, manual.text
    repetido = await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h,
                                    json={"etapa": "11", "nome": "Mão de obra"})
    assert repetido.status_code == 422 and "Já existe" in repetido.json()["detail"]
    gasto = await _gasto(vila, obra, etapa="11", topico_id=manual.json()["id"])

    await _importar(vila, obra)
    a = await _arvore(vila, obra)
    # o "Portas" da planilha reaproveita o ID do manual: aparece uma vez, como do orçamento, e o gasto continua nele
    assert _topicos(a, "11") == [("Portas", "orcamento"), ("Mão de Obra", "orcamento")]
    assert a["11"]["topicos"][0]["id"] == manual.json()["id"] == gasto["gasto"]["topico_id"]
    for alvo in (manual.json()["id"], a["11"]["topicos"][1]["id"]):
        r = await vila.http.patch(f"/topicos/{alvo}", headers=vila.h, json={"nome": "Outro"})
        assert r.status_code == 422 and "vem do orçamento" in r.json()["detail"], r.text
    r = await vila.http.patch(f"/topicos/{alvo}", headers=vila.h, json={"arquivado": True})
    assert r.status_code == 422


async def test_gasto_no_topico_do_orcamento_e_nome_depois_do_desfazer(vila):
    obra = (await nova_obra(vila))["id"]
    p = await _importar(vila, obra)
    portas = next(t for t in (await _arvore(vila, obra))["11"]["topicos"] if t["nome"] == "Portas")
    g = await _gasto(vila, obra, etapa="11", topico_id=portas["id"])
    assert g["gasto"]["topico_id"] == portas["id"]
    # tópico de outra etapa não vale
    sete = (await _arvore(vila, obra))["7"]["topicos"][0]["id"]
    c = await vila.http.post(f"/obras/{obra}/comprovantes", headers=vila.h,
                             files={"arquivo": ("pix.jpg", b"\xff\xd8\xff\xe0" + uuid.uuid4().bytes, "image/jpeg")})
    assert c.status_code == 201, c.text
    r = await vila.http.post(f"/comprovantes/{c.json()['id']}/confirmar", headers=vila.h,
                             json=_confirmacao(etapa="11", topico_id=sete))
    assert r.status_code == 422 and "não está na etapa" in r.json()["detail"]

    etapa = next(e for e in (await _resumo(vila, obra))["etapas"] if e["etapa"] == "11")
    assert etapa["topicos"] == [{"topico_id": portas["id"], "total": 35000, "nome": "Portas"}]
    # a versão sai, mas o gasto lançado no tópico dela continua com nome
    await _desfazer(vila, p["registro_id"])
    etapa = next(e for e in (await _resumo(vila, obra))["etapas"] if e["etapa"] == "11")
    assert etapa["topicos"][0]["nome"] == "Portas"


async def test_reordenar_continua_livre_com_topicos_do_orcamento(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    v = (await orcamento(vila, obra))["versoes"][0]["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/etapas/ordem", headers=vila.h,
                             json={"ordem": ["12", "7", "11"], "chave_idempotencia": uuid.uuid4().hex, "versao_id": v})
    assert r.status_code == 201, r.text
    a = await _arvore(vila, obra)
    # os tópicos foram junto com as etapas renumeradas
    assert _topicos(a, "1") == [("Mão de Obra", "orcamento")]
    assert _topicos(a, "3") == [("Portas", "orcamento"), ("Mão de Obra", "orcamento")]
    orc = await orcamento(vila, obra)
    validos = {t["id"] for e in a.values() for t in e["topicos"]}
    assert {x["topico_id"] for x in orc["itens"] if x["tipo"] == "item"} == validos

    # com gasto num tópico, a etapa dele não muda mais de número (como sempre foi com gasto na etapa)
    await _gasto(vila, obra, etapa="3", topico_id=a["3"]["topicos"][0]["id"])
    r = await vila.http.post(f"/obras/{obra}/orcamento/etapas/ordem", headers=vila.h,
                             json={"ordem": ["3", "1", "2"], "chave_idempotencia": uuid.uuid4().hex,
                                   "versao_id": orc["versoes"][0]["id"]})
    assert r.status_code == 409 and "Já há lançamento" in r.json()["detail"]


async def test_eap_comum_tem_topico_por_servico_de_nivel_2_e_etapa_longa_fica_sem(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra, CONTRATO, mapa())
    a = await _arvore(vila, obra)
    # "1.1" e "1.2" são serviços de nível 2: cada um é um tópico; "2.1.1" sem a linha "2.1" não tem tópico
    assert [n for n, _ in _topicos(a, "1")] == ["Placa de obra em chapa galvanizada", "Sanitário em canteiro"]
    assert a["2"]["topicos"] == []

    longa = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS", None, None, None, None],
                 *[[f"1.{n}", None, None, f"Serviço {n}", "un", 1, 10, 10] for n in range(1, 32)])
    obra2 = (await nova_obra(vila))["id"]
    previa = (await versao(vila, obra2, longa, mapa(linha_cabecalho=1))).json()
    assert previa["topicos"] == []
    assert "A etapa 1 SERVIÇOS tem 31 tópicos na numeração (o limite é 30)" in previa["avisos"][0]["mensagem"]
    await _importar(vila, obra2, longa, mapa(linha_cabecalho=1))
    assert (await _arvore(vila, obra2))["1"]["topicos"] == []


async def test_versao_gravada_antes_dos_topicos_monta_pela_numeracao(vila):
    obra = (await nova_obra(vila))["id"]
    p = await _importar(vila, obra)
    ids = [t["id"] for e in (await _arvore(vila, obra)).values() for t in e["topicos"]]
    # o documento como era antes do campo: sem `lista_topicos` na versão e sem `topico_id` nos itens
    arm, tenant = vila.arm, vila.tenant_id
    v = await arm.obter("versoes_orcamento", tenant, p["versao_id"])
    await arm.salvar("versoes_orcamento", {k: x for k, x in v.items() if k != "lista_topicos"})
    for x in (await orcamento(vila, obra))["itens"]:
        d = await arm.obter("itens_orcamento", tenant, x["id"])
        await arm.salvar("itens_orcamento", {k: y for k, y in d.items() if k != "topico_id"})
    assert [t["id"] for e in (await _arvore(vila, obra)).values() for t in e["topicos"]] == ids


async def test_organizar_em_fases_leva_os_topicos_junto_e_o_desfazer_volta(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    antes = await _arvore(vila, obra)
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    a = await _arvore(vila, obra)
    validos = {t["id"]: chave for chave, e in a.items() for t in e["topicos"]}
    assert validos and all(t.startswith("orc-") for t in validos)
    for x in (await orcamento(vila, obra))["itens"]:
        if x["tipo"] == "item" and x["topico_id"]:
            # o item fica num tópico da etapa (fase) para onde ele foi
            assert validos[x["topico_id"]] == x["etapa"], x
    await _desfazer(vila, r.json()["registro_id"])
    assert await _arvore(vila, obra) == antes


# --- tópico na linha do pedido ---

async def _insumo(vila, obra, nome="Porta 0,60"):
    r = await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h,
                             json={"nome": nome, "unidade": "un"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _pedido(vila, obra, *linhas, chave=None):
    return await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h,
                                json={"fornecedor": "Madeireira", "linhas": list(linhas),
                                      "chave_idempotencia": chave or uuid.uuid4().hex})


async def test_linha_do_pedido_com_topico_da_arvore(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    a = await _arvore(vila, obra)
    portas, mao_7 = a["11"]["topicos"][0]["id"], a["7"]["topicos"][1]["id"]
    insumo = await _insumo(vila, obra)
    linha = {"insumo_id": insumo, "quantidade": 2000, "valor_unitario": 49866, "etapa": "11"}

    r = await _pedido(vila, obra, {**linha, "topico_id": portas})
    assert r.status_code == 201, r.text
    assert r.json()["linhas"][0]["topico_id"] == portas
    for errada, msg in (({**linha, "topico_id": mao_7}, "não está na etapa"),
                        ({**linha, "topico_id": "orc-inventado"}, "não está na etapa"),
                        ({**linha, "etapa": None, "topico_id": portas}, "Diga de qual etapa")):
        r = await _pedido(vila, obra, errada)
        assert r.status_code == 422 and msg in r.json()["detail"], r.text

    # a proposta do agente confere do mesmo jeito, antes de guardar o cartão (nela a etapa é opcional, o tópico não
    # vem sem ela)
    for errada, msg in (({**linha, "topico_id": mao_7}, "não está na etapa"),
                        ({**linha, "etapa": None, "topico_id": portas}, "etapa do tópico")):
        r = await vila.http.post("/propostas", headers=vila.h, json={
            "obra_id": obra, "tipo": "criar_pedido", "chave_idempotencia": uuid.uuid4().hex, "titulo": "Portas",
            "dados": {"fornecedor": "Madeireira", "linhas": [errada]}})
        assert r.status_code == 422 and msg in r.json()["detail"], r.text


async def test_pedido_sem_topico_assina_como_antes_do_campo(vila):
    corpo = PedidoCorpo(fornecedor="X", linhas=[{"insumo_id": "i", "quantidade": 1000, "etapa": "1"}],
                        chave_idempotencia="abcdefgh12")
    # o corpo como era assinado antes de `topico_id` existir: a repetição de uma chave antiga continua 200
    antes = ('{"fornecedor":"X","fornecedor_id":null,"linhas":[{"insumo_id":"i","quantidade":1000,'
             '"valor_unitario":0,"total":null,"etapa":"1"}],"previsao":null,"observacao":"","aprovado":false}')
    assert assinatura_do_pedido(corpo) == hashlib.sha256(antes.encode()).hexdigest()
    com = corpo.model_copy(update={"linhas": [corpo.linhas[0].model_copy(update={"topico_id": "orc-x"})]})
    assert assinatura_do_pedido(com) != assinatura_do_pedido(corpo)

    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    insumo = await _insumo(vila, obra)
    chave = uuid.uuid4().hex
    linha = {"insumo_id": insumo, "quantidade": 1000, "valor_unitario": 100, "etapa": "11"}
    assert (await _pedido(vila, obra, linha, chave=chave)).status_code == 201
    assert (await _pedido(vila, obra, linha, chave=chave)).status_code == 200
    portas = (await _arvore(vila, obra))["11"]["topicos"][0]["id"]
    assert (await _pedido(vila, obra, {**linha, "topico_id": portas}, chave=chave)).status_code == 409


# --- orçado × realizado por tópico ---

def _abertura(orc, etapa):
    e = next(x for x in orc["etapas"] if x["etapa"] == etapa)
    return e, {t["nome"]: t for t in e["topicos"]}


async def test_orcado_e_realizado_por_topico(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    a = await _arvore(vila, obra)
    portas, mao_11 = (t["id"] for t in a["11"]["topicos"])
    insumo, outro = await _insumo(vila, obra), await _insumo(vila, obra, "Dobradiça")
    r = await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h, json={
        "fornecedor": "Madeireira", "aprovado": True, "chave_idempotencia": uuid.uuid4().hex, "linhas": [
            {"insumo_id": insumo, "quantidade": 34000, "valor_unitario": 50000, "etapa": "11", "topico_id": portas},
            {"insumo_id": outro, "quantidade": 10000, "valor_unitario": 1000, "etapa": "11"}]})
    assert r.status_code == 201, r.text
    # gasto avulso no tópico do orçamento, e num tópico manual criado na tela (que só aparece por ter lançamento)
    await _gasto(vila, obra, etapa="11", topico_id=mao_11, valor=1_000_000)
    telhado = (await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h,
                                    json={"etapa": "7", "nome": "Andaime"})).json()["id"]
    await _gasto(vila, obra, etapa="7", topico_id=telhado)

    orc = await orcamento(vila, obra)
    e11, t11 = _abertura(orc, "11")
    assert list(t11) == ["Portas", "Mão de Obra", "Sem tópico"]
    assert (t11["Portas"]["contratado"], t11["Portas"]["comprometido"], t11["Portas"]["origem"]) == (
        3464668, 1700000, "orcamento")
    assert t11["Portas"]["saldo"] == 3464668 - 1700000 and not t11["Portas"]["estourou"]
    # R$ 9.804,00 orçados e R$ 10.000,00 gastos: o tópico estoura, a etapa não
    m = t11["Mão de Obra"]
    assert (m["contratado"], m["gastos_avulsos"], m["realizado"], m["estourou"]) == (980400, 1_000_000, 1_000_000, True)
    assert not e11["estourou"]
    sem = t11["Sem tópico"]
    assert (sem["topico_id"], sem["origem"], sem["contratado"], sem["comprometido"]) == (None, None, 0, 10000)
    for k in ("contratado", "atual", "cotando", "comprometido", "realizado", "saldo", "gastos_avulsos"):
        assert sum(t[k] for t in e11["topicos"]) == e11[k], k

    _, t7 = _abertura(orc, "7")
    assert list(t7) == ["Materiasi diversos", "Mão de Obra", "Andaime"]
    assert (t7["Andaime"]["origem"], t7["Andaime"]["contratado"], t7["Andaime"]["realizado"]) == ("manual", 0, 35000)
    _, t12 = _abertura(orc, "12")
    assert list(t12) == ["Mão de Obra"]
    assert {x["topico_id"] for x in orc["itens"] if x["descricao"] == "Portas"} == {portas}


async def test_etapa_sem_topico_nao_abre(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra, CONTRATO, mapa())
    orc = await orcamento(vila, obra)
    e2, _ = _abertura(orc, "2")
    assert e2["topicos"] == []
    _, t1 = _abertura(orc, "1")
    assert [t["origem"] for t in t1.values()] == ["orcamento", "orcamento"]


async def test_proposta_de_pedido_aprovada_leva_o_topico(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    portas = (await _arvore(vila, obra))["11"]["topicos"][0]["id"]
    insumo, outro = await _insumo(vila, obra), await _insumo(vila, obra, "Dobradiça")
    linha = {"insumo_id": insumo, "quantidade": 2000, "valor_unitario": 49866, "etapa": "11"}

    async def propor(*linhas):
        r = await vila.http.post("/propostas", headers=vila.h, json={
            "obra_id": obra, "tipo": "criar_pedido", "chave_idempotencia": uuid.uuid4().hex, "titulo": "Portas",
            "dados": {"fornecedor": "Madeireira", "linhas": list(linhas)}})
        assert r.status_code == 201, r.text
        return r.json()

    # a proposta de uma linha (o formato do agente) guarda o tópico, e o pedido aprovado nasce com ele
    p = await propor({**linha, "topico_id": portas})
    assert p["dados"]["topico_id"] == portas
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    pedidos = (await vila.http.get(f"/obras/{obra}/pedidos", headers=vila.h)).json()
    assert [x["topico_id"] for p in pedidos for x in p["linhas"]] == [portas]

    # sem tópico, a proposta grava como antes do campo (a trava e a repetição da chave antiga continuam as mesmas)
    assert "topico_id" not in (await propor(linha))["dados"]
    varias = await propor(linha, {**linha, "insumo_id": outro})
    assert all("topico_id" not in x for x in varias["dados"]["linhas"])


async def test_desfazer_nao_arquiva_o_manual_que_o_orcamento_reaproveitou(vila):
    obra = (await nova_obra(vila))["id"]
    so_mao_de_obra = xlsx([None, None, None, None, None, None, "qtdade", "vlr unit orç", "x", "ORÇADO SET/23"],
                          [None, None, 11, "ESQUADRIA MADEIRA", None, None, None, None, None, None],
                          [None, None, 11.6, "Mão de Obra", None, None, 114, 86, 0, 9804])
    await _importar(vila, obra, so_mao_de_obra)
    manual = await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h, json={"etapa": "11", "nome": "Portas"})
    assert manual.status_code == 201, manual.text
    await _importar(vila, obra)
    assert (await _arvore(vila, obra))["11"]["topicos"][0]["id"] == manual.json()["id"]
    # o Desfazer da criação arquivaria o tópico que agora é do orçamento: a mesma recusa do PATCH
    r = await _desfazer(vila, manual.json()["registro_id"], status=422)
    assert "vem do orçamento" in r["detail"]
    assert (await vila.arm.obter("topicos", vila.tenant_id, manual.json()["id"])).get("arquivado") is not True


async def test_aditivo_com_o_numero_do_contrato_cai_no_topico_do_contrato(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra, CONTRATO, mapa())
    placa = next(t for t in (await _arvore(vila, obra))["1"]["topicos"]
                 if t["nome"] == "Placa de obra em chapa galvanizada")
    # o aditivo mexe no "1.1.1" do contrato sem trazer a linha "1.1"
    aditivo = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
                   ["1.1.1", None, None, "Placa adicional", "m²", 2, 100, 200])
    previa = (await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                           data="2026-11-20")).json()
    assert [(t["id"], t["total"]) for t in previa["topicos"]] == [(placa["id"], 20000)]
    r = await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                     data="2026-11-20", aplicar=True)
    assert r.status_code == 201, r.text
    _, t1 = _abertura(await orcamento(vila, obra), "1")
    assert (t1["Placa de obra em chapa galvanizada"]["contratado"],
            t1["Placa de obra em chapa galvanizada"]["aditivos"]) == (238910, 20000)
    assert "Sem tópico" not in t1


async def test_topico_de_versao_desligada_com_gasto_continua_do_orcamento(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra)
    portas = next(t for t in (await _arvore(vila, obra))["11"]["topicos"] if t["nome"] == "Portas")
    await _gasto(vila, obra, etapa="11", topico_id=portas["id"])
    so_mao_de_obra = xlsx([None, None, None, None, None, None, "qtdade", "vlr unit orç", "x", "ORÇADO SET/23"],
                          [None, None, 11, "ESQUADRIA MADEIRA", None, None, None, None, None, None],
                          [None, None, 11.6, "Mão de Obra", None, None, 114, 86, 0, 9804])
    await _importar(vila, obra, so_mao_de_obra)
    _, t11 = _abertura(await orcamento(vila, obra), "11")
    # não é manual: o PATCH /topicos dele responde 422, e a web não oferece renomear
    assert (t11["Portas"]["origem"], t11["Portas"]["contratado"], t11["Portas"]["realizado"]) == (
        "orcamento", 0, 35000)


async def test_aditivo_depois_de_organizar_em_fases_cai_no_topico_do_contrato(vila):
    obra = (await nova_obra(vila))["id"]
    contrato = xlsx(CABECALHO, ["7", None, None, "PAREDES", None, None, None, None],
                    ["7.1", None, None, "Cerâmica tijolo", None, None, None, None],
                    ["7.1.1", "87495", "SINAPI", "Alvenaria de tijolo cerâmico", "m²", 10, 100, 1000])
    await _importar(vila, obra, contrato, mapa(linha_cabecalho=1))
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    # na fase, o tópico é a etapa da planilha de onde o item veio
    tijolo = next((k, t) for k, e in (await _arvore(vila, obra)).items() for t in e["topicos"])
    assert tijolo[1]["nome"] == "PAREDES"
    # o aditivo numa etapa própria, que vai pelo código para a fase do tijolo e guarda o número do contrato
    aditivo = xlsx(CABECALHO, ["7", None, None, "ACRÉSCIMOS", None, None, None, None],
                   ["7.1.3", "87495", "SINAPI", "Tijolo a mais", "m²", 2, 100, 200])
    previa = (await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                           data="2026-11-20")).json()
    assert [(x["numero"], x["etapa"], x["etapa_origem"]) for x in previa["itens"] if x["tipo"] == "item"] == [
        ("7.1.3", tijolo[0], "codigo")]
    assert [(t["id"], t["nome"], t["total"]) for t in previa["topicos"]] == [(tijolo[1]["id"], "PAREDES", 20000)]


async def test_aditivo_gravado_antes_dos_topicos_cai_no_topico_do_contrato(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra, CONTRATO, mapa())
    aditivo = xlsx(CABECALHO, ["1", None, None, "SERVIÇOS PRELIMINARES", None, None, None, None],
                   ["1.1.1", None, None, "Placa adicional", "m²", 2, 100, 200])
    await _importar(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                    data="2026-11-20")
    # as duas versões como eram antes do campo: sem `lista_topicos` e sem `topico_id` nos itens
    arm, tenant = vila.arm, vila.tenant_id
    for v in (await orcamento(vila, obra))["versoes"]:
        d = await arm.obter("versoes_orcamento", tenant, v["id"])
        await arm.salvar("versoes_orcamento", {k: x for k, x in d.items() if k != "lista_topicos"})
    for x in (await orcamento(vila, obra))["itens"]:
        d = await arm.obter("itens_orcamento", tenant, x["id"])
        await arm.salvar("itens_orcamento", {k: y for k, y in d.items() if k != "topico_id"})
    _, t1 = _abertura(await orcamento(vila, obra), "1")
    assert (t1["Placa de obra em chapa galvanizada"]["contratado"],
            t1["Placa de obra em chapa galvanizada"]["aditivos"]) == (238910, 20000)
    assert "Sem tópico" not in t1


async def test_detalhe_do_aditivo_que_vai_para_outra_etapa_nao_tira_o_resto_do_grupo(vila):
    obra = (await nova_obra(vila))["id"]
    await _importar(vila, obra, CONTRATO, mapa())
    # o grupo "5.1" do aditivo: a placa vai pelo código para a etapa 1 do contrato, a argamassa fica no grupo
    aditivo = xlsx(CABECALHO, ["5", None, None, "ACRÉSCIMOS", None, None, None, None],
                   ["5.1", None, None, "Alvenaria extra", None, None, None, 500],
                   [None, "74209/1", "SINAPI", "Placa de obra", "m²", 1, 300, 300],
                   [None, None, None, "Argamassa", "m³", 1, 200, 200])
    previa = (await versao(vila, obra, aditivo, mapa(linha_cabecalho=1), tipo="aditivo", descricao="Aditivo 01",
                           data="2026-11-20")).json()
    assert previa["erros"] == []
    assert [(t["etapa"], t["nome"], t["total"]) for t in previa["topicos"]] == [("5", "Alvenaria extra", 20000)]
    grupo = previa["topicos"][0]["id"]
    assert [(x["descricao"], x["etapa"], x["topico_id"]) for x in previa["itens"] if x["tipo"] == "item"] == [
        ("Placa de obra", "1", None), ("Argamassa", "5", grupo)]


async def test_organizar_leva_o_titulo_de_nivel_2_com_o_item_e_trava_a_ordem(vila):
    obra = (await nova_obra(vila))["id"]
    contrato = xlsx(CABECALHO, ["7", None, None, "PAREDES", None, None, None, None],
                    ["7.1", None, None, "Cerâmica tijolo", None, None, None, None],
                    ["7.1.1", "87495", "SINAPI", "Alvenaria de tijolo cerâmico", "m²", 10, 100, 1000])
    await _importar(vila, obra, contrato, mapa(linha_cabecalho=1))
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    orc = await orcamento(vila, obra)
    tijolo = next(x for x in orc["itens"] if x["numero"] == "7.1.1")
    # a linha "7.1" (o nome do tópico) não vira título de fase: vai junto com o item dela
    titulo = next(x for x in orc["itens"] if x["numero"] == "7.1")
    assert (titulo["descricao"], titulo["etapa"]) == ("Cerâmica tijolo", tijolo["etapa"])
    assert [t["nome"] for e in (await _arvore(vila, obra)).values() for t in e["topicos"]] == ["PAREDES"]
    # nas etapas padrão a ordem é a da lista: reordenar renumeraria as chaves e tiraria a obra da lista
    ordem = [e["etapa"] for e in orc["etapas"]][::-1]
    r = await vila.http.post(f"/obras/{obra}/orcamento/etapas/ordem", headers=vila.h,
                             json={"ordem": ordem, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 422 and "etapas padrão" in r.json()["detail"], r.text


async def test_organizar_de_novo_nao_muda_nada_e_a_organizacao_antiga_vai_para_as_etapas_padrao(vila):
    obra = (await nova_obra(vila))["id"]
    # sem preço, nenhuma fase é estimada: organizar de novo não tem o que mudar
    contrato = xlsx(CABECALHO, ["7", None, None, "PAREDES", None, None, None, None],
                    ["7.1", None, None, "Cerâmica tijolo", None, None, None, None],
                    ["7.1.1", None, None, "Tapume de obra", "m", 10, 0, 0],
                    ["7.1.2", None, None, "Alvenaria de tijolo cerâmico", "m²", 10, 0, 0],
                    ["7.1.3", None, None, "Pintura látex (2 demãos)", "m²", 10, 0, 0])
    await _importar(vila, obra, contrato, mapa(linha_cabecalho=1))
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    assert (r.json()["fases"], r.json()["estimadas"]) == (
        ["Serviços preliminares", "Alvenaria e vedações", "Pintura"], 0)
    # a chave é a da lista padrão, a mesma em toda obra
    orc = await orcamento(vila, obra)
    assert [(e["etapa"], e["descricao"]) for e in orc["etapas"]] == [
        ("preliminares", "Serviços preliminares"), ("alvenaria", "Alvenaria e vedações"), ("pintura", "Pintura")]
    antes = orc["itens"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 422 and "já está nas etapas padrão" in r.json()["detail"], r.text
    assert (await orcamento(vila, obra))["itens"] == antes

    # a organização em fases de antes da lista padrão numerava as fases presentes (1, 2, 3): organizar de novo leva
    # para as etapas padrão, e o tópico continua o da etapa da planilha
    arm, tenant = vila.arm, vila.tenant_id
    velha = {"preliminares": "1", "alvenaria": "2", "pintura": "3"}
    for x in antes:
        await arm.atualizar("itens_orcamento", tenant, x["id"], {"etapa": velha[x["etapa"]]})
    v = next(v for v in orc["versoes"] if v["ativa"])
    await arm.atualizar("versoes_orcamento", tenant, v["id"], {"padronizada": False, "lista_etapas": [
        {"etapa": velha[e["etapa"]], "descricao": e["descricao"]} for e in orc["etapas"]]})
    r = await vila.http.post(f"/obras/{obra}/orcamento/organizar-fases", headers=vila.h)
    assert r.status_code == 200, r.text
    assert [e["etapa"] for e in (await orcamento(vila, obra))["etapas"]] == ["preliminares", "alvenaria", "pintura"]
    assert {t["nome"] for e in (await _arvore(vila, obra)).values() for t in e["topicos"]} == {"PAREDES"}


