"""Suprimentos: fornecedores, pedido com várias linhas e etapa, entregas parciais com NF, serviço sem estoque."""
import uuid

from test_obras_orcamento import nova_obra
from test_orcamento_versoes import CONTRATO, mapa, versao

from canteiro_dominio.tipos import qtd


async def _insumo(vila, obra, nome, unidade, tipo="material"):
    r = await vila.http.post(f"/obras/{obra}/insumos", headers=vila.h,
                             json={"nome": nome, "unidade": unidade, "tipo": tipo})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _obra_com_orcamento(vila):
    o = await nova_obra(vila)
    r = await versao(vila, o["id"], CONTRATO, mapa(), aplicar=True)
    assert r.status_code == 201, r.text
    ids = {"cimento": await _insumo(vila, o["id"], "Cimento CP-II 50 kg", "sc"),
           "brita": await _insumo(vila, o["id"], "Brita 1", "m³"),
           "aterro": await _insumo(vila, o["id"], "Aterro apiloado", "m³", "servico")}
    return o["id"], ids


async def _pedido(vila, obra, linhas, **extra):
    corpo = {"fornecedor": "Depósito São Jorge", "aprovado": True, "linhas": linhas, **extra}
    return await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h, json=corpo)


async def _receber(vila, pedido_id, linhas, chave=None, **nf):
    corpo = {"linhas": [{"indice": i, "quantidade": q} for i, q in linhas],
             "chave_idempotencia": chave or uuid.uuid4().hex, **nf}
    return await vila.http.post(f"/pedidos/{pedido_id}/recebimentos", headers=vila.h, json=corpo)


async def _conta(vila, obra, insumo_id):
    itens = (await vila.http.get(f"/obras/{obra}/insumos", headers=vila.h)).json()
    return next(x["conta"] for x in itens if x["insumo"]["id"] == insumo_id)


async def test_etapas_da_obra_vem_do_orcamento(vila):
    obra, _ = await _obra_com_orcamento(vila)
    r = await vila.http.get(f"/obras/{obra}/etapas", headers=vila.h)
    assert r.json() == [{"etapa": "1", "descricao": "SERVIÇOS PRELIMINARES"},
                        {"etapa": "2", "descricao": "FUNDAÇÕES"}]
    sem = await nova_obra(vila)
    assert (await vila.http.get(f"/obras/{sem['id']}/etapas", headers=vila.h)).json() == []


async def test_fornecedor_cadastro_cnpj_e_repetido(vila):
    r = await vila.http.post("/fornecedores", headers=vila.h,
                             json={"nome": "Depósito  São Jorge", "cnpj": "11.222.333/0001-81", "telefone": " 51 9 "})
    assert r.status_code == 201, r.text
    f = r.json()
    assert (f["nome"], f["cnpj"], f["telefone"]) == ("Depósito São Jorge", "11222333000181", "51 9")
    assert (await vila.http.post("/fornecedores", headers=vila.h,
                                 json={"nome": "Outro", "cnpj": "11222333000181"})).status_code == 409
    ruim = await vila.http.post("/fornecedores", headers=vila.h, json={"nome": "X", "cnpj": "11222333000182"})
    assert ruim.status_code == 422 and "CNPJ inválido" in ruim.json()["detail"]
    r = await vila.http.patch(f"/fornecedores/{f['id']}", headers=vila.h, json={"contato": "Jorge"})
    assert (r.status_code, r.json()["contato"]) == (200, "Jorge")
    assert (await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    lista = (await vila.http.get("/fornecedores", headers=vila.h)).json()
    assert [(x["nome"], x["contato"]) for x in lista] == [("Depósito São Jorge", "")]
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.post("/fornecedores", headers=h, json={"nome": "Y"})).status_code == 403


async def test_pedido_com_varias_linhas_exige_etapa_valida(vila):
    obra, i = await _obra_com_orcamento(vila)
    sem_etapa = await _pedido(vila, obra, [{"insumo_id": i["cimento"], "quantidade": qtd(120), "valor_unitario": 3800}])
    assert sem_etapa.status_code == 422 and "etapa" in sem_etapa.json()["detail"]
    errada = await _pedido(vila, obra, [{"insumo_id": i["cimento"], "quantidade": qtd(1), "etapa": "9"}])
    assert errada.status_code == 422 and "etapa 9" in errada.json()["detail"]
    repetido = await _pedido(vila, obra, [{"insumo_id": i["cimento"], "quantidade": qtd(1), "etapa": "2"}] * 2)
    assert repetido.status_code == 422
    r = await _pedido(vila, obra, [
        {"insumo_id": i["cimento"], "quantidade": qtd(120), "valor_unitario": 3800, "etapa": "2"},
        {"insumo_id": i["brita"], "quantidade": qtd(25), "valor_unitario": 11990, "etapa": "2"},
    ], previsao="2026-11-10")
    assert r.status_code == 201, r.text
    p = r.json()
    assert [(x["nome"], x["unidade"], x["total"]) for x in p["linhas"]] == [
        ("Cimento CP-II 50 kg", "sc", 456000), ("Brita 1", "m³", 299750)]
    assert (p["valor"], p["status"]) == (755750, "aprovado")
    assert (await _conta(vila, obra, i["brita"]))["a_caminho"] == qtd(25)
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == ("PED-0168 criado: 2 itens (Cimento CP-II 50 kg e Brita 1) com Depósito São Jorge "
                            "(R$ 7.557,50) · aprovado")


async def test_entregas_parciais_com_nf_e_desfazer_so_a_ultima(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [
        {"insumo_id": i["cimento"], "quantidade": qtd(120), "valor_unitario": 3800, "etapa": "2"},
        {"insumo_id": i["brita"], "quantidade": qtd(25), "valor_unitario": 11990, "etapa": "2"},
    ])).json()
    r = await _receber(vila, p["id"], [(0, qtd(120)), (1, qtd(18))], nf_numero="1187", nf_data="2026-11-09",
                       nf_valor=671820)
    assert r.status_code == 201, r.text
    rec, ped = r.json()["recebimento"], r.json()["pedido"]
    assert (ped["status"], [x["recebida"] for x in ped["linhas"]]) == ("parcial", [qtd(120), qtd(18)])
    assert (rec["numero"], rec["valor"], rec["nf_valor"]) == (1, 456000 + 215820, 671820)
    brita = await _conta(vila, obra, i["brita"])
    assert (brita["saldo"], brita["a_caminho"]) == (qtd(18), qtd(7))
    movs = (await vila.http.get(f"/obras/{obra}/insumos/{i['brita']}/movimentos", headers=vila.h)).json()
    assert (movs[0]["origem_doc"], movs[0]["etapa"]) == ("PED-0168 · NF 1187", "2")
    reg1 = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg1["texto"] == ("PED-0168 · 1ª entrega (NF 1187): 120 sc de Cimento CP-II 50 kg; 18 de 25 m³ de Brita 1 "
                             "· faltam 7 m³ de Brita 1")

    r2 = await _receber(vila, p["id"], [(1, qtd(7))], nf_numero="1201")
    assert r2.json()["pedido"]["status"] == "conferido"
    # só a última entrega se desfaz
    assert (await vila.http.post(f"/registro/{reg1['id']}/desfazer", headers=vila.h)).status_code == 409
    assert (await vila.http.post(f"/registro/{r2.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.http.post(f"/registro/{reg1['id']}/desfazer", headers=vila.h)).status_code == 200
    ped = (await vila.http.get(f"/obras/{obra}/pedidos", headers=vila.h)).json()[0]
    assert (ped["status"], ped["recebimentos"], [x["recebida"] for x in ped["linhas"]]) == ("aprovado", 0, [0, 0])
    assert (await _conta(vila, obra, i["brita"]))["saldo"] == 0
    assert (await vila.http.get(f"/pedidos/{p['id']}/recebimentos", headers=vila.h)).json() == []


async def test_recebimento_repetido_com_a_mesma_chave(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [{"insumo_id": i["brita"], "quantidade": qtd(25), "etapa": "2"}])).json()
    a = await _receber(vila, p["id"], [(0, qtd(18))], chave="entrega-brita-1")
    b = await _receber(vila, p["id"], [(0, qtd(18))], chave="entrega-brita-1")
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["registro_id"] == b.json()["registro_id"]
    assert (await _conta(vila, obra, i["brita"]))["saldo"] == qtd(18)
    assert (await _receber(vila, p["id"], [(0, qtd(5))], chave="entrega-brita-1")).status_code == 409
    assert (await _receber(vila, p["id"], [(3, qtd(1))])).status_code == 422
    sem_nota = await _receber(vila, p["id"], [(0, qtd(7))], nf_numero=None, nf_data=None, nf_valor=None)
    assert (sem_nota.status_code, sem_nota.json()["recebimento"]["nf_numero"]) == (201, ""), sem_nota.text


async def test_servico_mede_sem_estoque(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [{"insumo_id": i["aterro"], "quantidade": qtd(1646), "valor_unitario": 4500,
                                     "etapa": "2"}], fornecedor="Terraplenagem Vale")).json()
    r = await _receber(vila, p["id"], [(0, qtd(800))])
    assert r.status_code == 201, r.text
    assert (r.json()["recebimento"]["movimentos"], r.json()["pedido"]["status"]) == ([], "parcial")
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"].startswith("PED-0168 · 1ª medição: 800 de 1.646 m³ de Aterro apiloado")
    conta = await _conta(vila, obra, i["aterro"])
    assert (conta["saldo"], conta["a_caminho"]) == (0, qtd(846))
    mov = await vila.http.post(f"/obras/{obra}/movimentos", headers=vila.h,
                               json={"insumo_id": i["aterro"], "tipo": "entrada", "quantidade": qtd(1),
                                     "chave_idempotencia": uuid.uuid4().hex})
    assert mov.status_code == 422 and "serviço" in mov.json()["detail"]


async def test_saida_com_orcamento_exige_etapa(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [{"insumo_id": i["cimento"], "quantidade": qtd(120), "etapa": "2"}])).json()
    await _receber(vila, p["id"], [(0, qtd(120))])

    def saida(**kw):
        return vila.http.post(f"/obras/{obra}/movimentos", headers=vila.h,
                              json={"insumo_id": i["cimento"], "tipo": "saida", "quantidade": qtd(60),
                                    "chave_idempotencia": uuid.uuid4().hex, **kw})

    assert (await saida()).status_code == 422
    r = await saida(etapa="2", destino="sapatas")
    assert r.status_code == 201, r.text
    assert r.json()["etapa"] == "2"
    assert (await _conta(vila, obra, i["cimento"]))["saldo"] == qtd(60)


async def test_editar_previsao_e_desfazer(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [{"insumo_id": i["brita"], "quantidade": qtd(25), "etapa": "2"}],
                       previsao="2026-11-10")).json()
    r = await vila.http.patch(f"/pedidos/{p['id']}", headers=vila.h, json={"previsao": "2026-11-24"})
    assert (r.status_code, r.json()["previsao"]) == (200, "2026-11-24"), r.text
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == "PED-0168: previsão 2026-11-10 → 2026-11-24"
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.http.get(f"/obras/{obra}/pedidos", headers=vila.h)).json()[0]["previsao"] == "2026-11-10"


async def test_resumo_conta_so_o_que_falta_entregar(vila):
    obra, i = await _obra_com_orcamento(vila)
    p = (await _pedido(vila, obra, [{"insumo_id": i["brita"], "quantidade": qtd(25), "total": 325000,
                                     "etapa": "2"}])).json()
    await _receber(vila, p["id"], [(0, qtd(18))])
    resumo = (await vila.http.get("/obras/resumo", headers=vila.h)).json()
    assert next(x for x in resumo if x["obra"]["id"] == obra)["valor_pedidos_abertos"] == 91000   # 7/25 de 3.250


async def test_codigo_generico_nao_gera_aviso_de_unidade(vila):
    from test_obras_orcamento import xlsx
    from test_orcamento_versoes import CABECALHO
    planilha = xlsx(["x"], CABECALHO, ["1", None, None, "ETAPA", None, None, None, None],
                    ["1.1", "CPU", "PRÓPRIO", "Serviço A", "m²", 1, 10, 10],
                    ["1.2", "CPU", "PRÓPRIO", "Serviço B", "kg", 1, 10, 10])
    o = await nova_obra(vila)
    r = await versao(vila, o["id"], planilha, mapa(linha_cabecalho=2))
    assert r.status_code == 200, r.text
    assert r.json()["avisos"] == []


async def test_previsto_realizado_por_etapa(vila):
    obra, i = await _obra_com_orcamento(vila)
    brita = (await _pedido(vila, obra, [{"insumo_id": i["brita"], "quantidade": qtd(25), "total": 325000,
                                         "etapa": "2"}])).json()
    await _receber(vila, brita["id"], [(0, qtd(18))])                   # parcial: compromete tudo, realiza 18/25
    cimento = (await _pedido(vila, obra, [{"insumo_id": i["cimento"], "quantidade": qtd(10), "total": 40000,
                                           "etapa": "1"}])).json()
    await _receber(vila, cimento["id"], [(0, qtd(6))])
    await vila.http.post(f"/pedidos/{cimento['id']}/avancar", headers=vila.h, json={"para": "conferido"})
    await vila.http.post(f"/obras/{obra}/pedidos", headers=vila.h, json={
        "fornecedor": "Cotação", "linhas": [{"insumo_id": i["aterro"], "quantidade": qtd(100), "total": 450000,
                                            "etapa": "2"}]})                   # em cotação
    o = (await vila.http.get(f"/obras/{obra}/orcamento", headers=vila.h)).json()
    por = {e["etapa"]: (e["cotando"], e["comprometido"], e["realizado"]) for e in o["etapas"]}
    assert por["2"] == (450000, 325000, 234000)
    assert por["1"] == (0, 24000, 24000)                                 # encerrado: só o que chegou
    t = o["totais"]
    assert (t["cotando"], t["comprometido"], t["realizado"], t["sem_etapa"]["realizado"]) == (
        450000, 349000, 258000, 0)
