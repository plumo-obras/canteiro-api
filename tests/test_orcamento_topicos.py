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
