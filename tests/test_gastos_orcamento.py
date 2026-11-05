"""Gastos × orçamento: com orçamento, a árvore de gasto são as etapas dele (+ extras) e o gasto avulso soma no
realizado da etapa; pagamento de pedido não soma de novo; gasto antigo da lista padrão vira "sem etapa do orçamento"
e se reclassifica com Desfazer."""
import secrets

from test_gastos import JPEG, _agente, _campo, _confirmacao, _desfazer, _leitura
from test_obras_orcamento import nova_obra, registro
from test_orcamento_versoes import CONTRATO, mapa, orcamento, versao


async def _contrato(vila, obra):
    r = await versao(vila, obra, CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 201, r.text[:300]


async def _gasto(vila, obra, **kw):
    c = await vila.http.post(f"/obras/{obra}/comprovantes", headers=vila.h,
                             files={"arquivo": ("pix.jpg", JPEG + secrets.token_bytes(16), "image/jpeg")})
    assert c.status_code == 201, c.text
    r = await vila.http.post(f"/comprovantes/{c.json()['id']}/confirmar", headers=vila.h, json=_confirmacao(**kw))
    assert r.status_code == 201, r.text
    return r.json()


async def _resumo(vila, obra):
    r = await vila.http.get(f"/obras/{obra}/gastos/resumo", headers=vila.h)
    assert r.status_code == 200, r.text
    return r.json()


def _etapa(orc, chave):
    return next(e for e in orc["etapas"] if e["etapa"] == chave)


async def test_com_orcamento_a_arvore_sao_as_etapas_dele_mais_as_extras(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    extra = (await vila.http.post(f"/obras/{obra}/etapas-extras", headers=vila.h, json={"nome": "Taxas"})).json()
    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=vila.h)).json()
    assert [(e["chave"], e["nome"], e["origem"], e["fixa"]) for e in a] == [
        ("1", "1 SERVIÇOS PRELIMINARES", "orcamento", True),
        ("2", "2 FUNDAÇÕES", "orcamento", True),
        (extra["chave"], "Taxas", "extra", False),
    ]
    # tópico preso ao código da etapa do orçamento; a lista padrão não vale mais nesta obra
    t = await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h, json={"etapa": "2", "nome": "Estacas"})
    assert t.status_code == 201, t.text
    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=vila.h)).json()
    assert a[1]["topicos"] == [{"id": t.json()["id"], "nome": "Estacas"}]
    r = await vila.http.post(f"/obras/{obra}/topicos", headers=vila.h, json={"etapa": "eletrica", "nome": "Quadro"})
    assert r.status_code == 422
    # extra não repete o nome de etapa do orçamento (com ou sem o código); o da lista padrão aqui é livre
    for nome in ("Fundações", "2 fundações"):
        r = await vila.http.post(f"/obras/{obra}/etapas-extras", headers=vila.h, json={"nome": nome})
        assert r.status_code == 422 and "Já existe" in r.json()["detail"], r.text
    r = await vila.http.post(f"/obras/{obra}/etapas-extras", headers=vila.h, json={"nome": "Instalações elétricas"})
    assert r.status_code == 201, r.text
    # desfazer e refazer a criação de um tópico de etapa do orçamento (a base é lida fora da transação)
    desfeito = await _desfazer(vila, t.json()["registro_id"])
    await _desfazer(vila, desfeito["id"])
    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=vila.h)).json()
    assert a[1]["topicos"] == [{"id": t.json()["id"], "nome": "Estacas"}]


async def test_gasto_avulso_soma_no_realizado_da_etapa_e_mexe_no_saldo(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    antes = await orcamento(vila, obra)
    fund = _etapa(antes, "2")
    # um gasto maior que o orçado da etapa: estoura
    g = await _gasto(vila, obra, etapa="2", valor=fund["atual"] + 100)
    depois = await orcamento(vila, obra)
    e = _etapa(depois, "2")
    assert (e["realizado"], e["comprometido"], e["gastos_avulsos"]) == (fund["atual"] + 100,) * 3
    assert (e["saldo"], e["estourou"]) == (-100, True)
    assert _etapa(depois, "1")["realizado"] == 0
    t = depois["totais"]
    assert t["realizado"] == antes["totais"]["realizado"] + fund["atual"] + 100
    assert t["saldo"] == antes["totais"]["saldo"] - fund["atual"] - 100
    assert t["gastos"] == {"total": fund["atual"] + 100, "nas_etapas": fund["atual"] + 100, "fora_do_orcamento": 0,
                           "na_lista_padrao": 0, "sem_etapa_do_orcamento": 0}
    fin = (await vila.http.get(f"/obras/{obra}/resumo", headers=vila.h)).json()["financeiro"]
    assert fin["realizado"] == t["realizado"] and fin["gastos"] == t["gastos"]
    assert [x["etapa"] for x in fin["etapas_estouradas"]] == ["2"]
    r = await _resumo(vila, obra)
    assert (r["total"], r["tem_orcamento"], r["etapas"][0]["nome"], r["etapas"][0]["origem"]) == (
        fund["atual"] + 100, True, "2 FUNDAÇÕES", "orcamento")
    # estornado sai do realizado
    await _desfazer(vila, g["registro_id"])
    assert _etapa(await orcamento(vila, obra), "2")["realizado"] == 0


async def test_pagamento_de_pedido_nao_soma_de_novo_no_realizado(vila):
    obra = vila.obra
    await _contrato(vila, obra)
    antes = await orcamento(vila, obra)
    pedido_id = vila.pedido["PED-0158"]
    await _gasto(vila, obra, etapa="2", valor=35000, pedido_id=pedido_id)
    depois = await orcamento(vila, obra)
    # o custo do pedido já conta pela entrega: o pagamento só marca o pedido como pago
    assert depois["totais"] == antes["totais"]
    assert depois["etapas"] == antes["etapas"]
    r = await _resumo(vila, obra)
    assert (r["total"], r["pagamentos_de_pedidos"]) == (0, 35000)


async def test_gasto_em_etapa_extra_fica_fora_do_orcamento(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    extra = (await vila.http.post(f"/obras/{obra}/etapas-extras", headers=vila.h, json={"nome": "Taxas"})).json()
    antes = await orcamento(vila, obra)
    await _gasto(vila, obra, etapa=extra["chave"], valor=5000)
    depois = await orcamento(vila, obra)
    assert depois["etapas"] == antes["etapas"]            # não engorda etapa nenhuma
    assert depois["totais"]["realizado"] == antes["totais"]["realizado"] + 5000
    assert depois["totais"]["gastos"]["fora_do_orcamento"] == 5000
    r = await _resumo(vila, obra)
    assert (r["fora_do_orcamento"], r["etapas"][0]["origem"]) == (5000, "extra")


async def test_gasto_da_lista_padrao_vira_sem_etapa_do_orcamento_e_reclassifica_com_desfazer(vila):
    obra = (await nova_obra(vila))["id"]
    eletrica = await _gasto(vila, obra, etapa="eletrica", valor=35000)
    fundacao = await _gasto(vila, obra, etapa="fundacao", valor=12000)
    o = await orcamento(vila, obra)
    assert o["totais"]["gastos"]["na_lista_padrao"] == 47000 and o["totais"]["realizado"] == 47000
    assert (await _resumo(vila, obra))["sem_etapa_do_orcamento"] == 0

    await _contrato(vila, obra)
    r = await _resumo(vila, obra)
    assert (r["total"], r["sem_etapa_do_orcamento"], r["tem_orcamento"]) == (47000, 47000, True)
    assert [(e["etapa"], e["nome"], e["origem"]) for e in r["etapas"]] == [
        ("eletrica", "Instalações elétricas", "sem_etapa_do_orcamento"),
        ("fundacao", "Fundação", "sem_etapa_do_orcamento")]
    lancados = (await vila.http.get(f"/obras/{obra}/gastos", headers=vila.h)).json()
    assert {g["etapa_origem"] for g in lancados} == {"sem_etapa_do_orcamento"}
    o = await orcamento(vila, obra)
    assert o["totais"]["gastos"]["sem_etapa_do_orcamento"] == 47000 and o["totais"]["realizado"] == 47000
    assert _etapa(o, "2")["realizado"] == 0

    gid = fundacao["gasto"]["id"]
    r = await vila.http.patch(f"/gastos/{gid}", headers=vila.h, json={"etapa": "2"})
    assert r.status_code == 200, r.text
    novo = r.json()
    assert (novo["etapa"], novo["etapa_nome"], novo["etapa_origem"]) == ("2", "2 FUNDAÇÕES", "orcamento")
    assert (await registro(vila, novo["registro_id"]))["texto"] == (
        "Mudou o gasto de R$ 120,00 (mão de obra do eletricista) de Fundação para 2 FUNDAÇÕES")
    o = await orcamento(vila, obra)
    assert _etapa(o, "2")["realizado"] == 12000 and o["totais"]["gastos"]["sem_etapa_do_orcamento"] == 35000
    assert o["totais"]["realizado"] == 47000

    # Desfazer volta para a etapa antiga (mesmo fora da árvore); Refazer leva de novo
    desfeito = await _desfazer(vila, novo["registro_id"])
    assert desfeito["texto"] == "Desfeito: o gasto de R$ 120,00 (mão de obra do eletricista) voltou para Fundação"
    assert _etapa(await orcamento(vila, obra), "2")["realizado"] == 0
    await _desfazer(vila, desfeito["id"])
    assert _etapa(await orcamento(vila, obra), "2")["realizado"] == 12000

    # erros: fora da árvore, nada mudou, viewer, inexistente, gasto estornado
    url = f"/gastos/{eletrica['gasto']['id']}"
    r = await vila.http.patch(url, headers=vila.h, json={"etapa": "eletrica"})
    assert r.status_code == 422 and "Nada mudou" in r.json()["detail"]
    r = await vila.http.patch(url, headers=vila.h, json={"etapa": "pintura"})
    assert r.status_code == 422 and "Escolha uma etapa" in r.json()["detail"]
    r = await vila.http.patch(url, headers=vila.h, json={"etapa": "1", "topico_id": "inventado"})
    assert r.status_code == 422
    h_viewer, _ = await vila.usuario("viewer")
    assert (await vila.http.patch(url, headers=h_viewer, json={"etapa": "1"})).status_code == 403
    assert (await vila.http.patch("/gastos/nao-existe", headers=vila.h, json={"etapa": "1"})).status_code == 404
    await _desfazer(vila, eletrica["registro_id"])
    assert (await vila.http.patch(url, headers=vila.h, json={"etapa": "1"})).status_code == 409


async def test_desfazer_reclassificacao_nao_passa_por_cima_de_outra(vila):
    obra = (await nova_obra(vila))["id"]
    g = await _gasto(vila, obra, etapa="fundacao")
    await _contrato(vila, obra)
    url = f"/gastos/{g['gasto']['id']}"
    primeira = (await vila.http.patch(url, headers=vila.h, json={"etapa": "2"})).json()
    assert (await vila.http.patch(url, headers=vila.h, json={"etapa": "1"})).status_code == 200
    r = await vila.http.post(f"/registro/{primeira['registro_id']}/desfazer", headers=vila.h)
    assert r.status_code == 409, r.text


async def test_revisao_aceita_etapa_do_orcamento_e_descarta_a_da_lista_padrao(vila, config):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    c = (await vila.http.post(f"/obras/{obra}/comprovantes", headers=vila.h,
                              files={"arquivo": ("pix.jpg", JPEG + secrets.token_bytes(16), "image/jpeg")})).json()
    leitura = _leitura(classificacao={"certeza": "baixa", "opcoes": [
        {"etapa": "eletrica", "topico_id": None, "motivo": "lista padrão"},
        {"etapa": "2", "topico_id": None, "motivo": "estacas"}]}, valor=_campo("alta", "350,00"))
    r = await vila.http.put(f"/interno/comprovantes/{c['id']}/leitura", headers=_agente(vila, config),
                            json={"leitura": leitura, "modelo": "teste"})
    assert r.status_code == 200, r.text
    cl = (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["revisao"]["classificacao"]
    assert [(o["etapa"], o["rotulo"]) for o in cl["opcoes"]] == [("2", "2 FUNDAÇÕES")]
