"""Pedidos (número por tenant, transições, conferido dá entrada) e propostas (só aprovação executa; desfazer)."""
import asyncio

from conftest import nova_vila

from canteiro_dominio.tipos import qtd


def pedido_corpo(vila, insumo="cimento", q=30):
    return {"insumo_id": vila.insumo[insumo], "quantidade": qtd(q), "fornecedor": "Depósito São Jorge",
            "valor": 114000}


async def test_criar_pedido_numera_por_tenant_e_entra_em_cotando(vila, http, arm, config):
    r = await http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json=pedido_corpo(vila))
    assert r.status_code == 201, r.text
    assert (r.json()["numero"], r.json()["status"]) == ("PED-0168", "em_cotacao")
    assert (await vila.conta("cimento"))["cotando"] == qtd(30)
    outra = await nova_vila(http, arm, config)
    r2 = await http.post(f"/obras/{outra.obra}/pedidos", headers=outra.h, json=pedido_corpo(outra))
    assert r2.json()["numero"] == "PED-0168"           # sequência própria de cada cliente


async def test_numeros_unicos_com_pedidos_em_paralelo(vila):
    rs = await asyncio.gather(*(vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h,
                                               json=pedido_corpo(vila, q=1)) for _ in range(6)))
    assert all(r.status_code == 201 for r in rs), [r.text for r in rs]
    assert sorted(r.json()["numero"] for r in rs) == [f"PED-{n:04d}" for n in range(168, 174)]


async def test_listar_pedidos_por_status(vila):
    r = await vila.http.get(f"/obras/{vila.obra}/pedidos?status=em_cotacao", headers=vila.h)
    assert [p["numero"] for p in r.json()] == ["PED-0167"]
    assert len((await vila.http.get(f"/obras/{vila.obra}/pedidos", headers=vila.h)).json()) == 4


async def test_transicao_invalida_e_recusada(vila):
    r = await vila.http.post(f"/pedidos/{vila.pedido['PED-0167']}/avancar", headers=vila.h, json={"para": "conferido"})
    assert r.status_code == 422
    assert "não pode ir de 'em_cotacao' para 'conferido'" in r.json()["detail"]


async def test_conferido_da_entrada_na_mesma_transacao(vila):
    r = await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=vila.h, json={"para": "conferido"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "conferido"
    areia = await vila.conta("areia")
    assert (areia["saldo"], areia["a_caminho"]) == (qtd(52), 0)
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['areia']}/movimentos",
                                headers=vila.h)).json()
    assert (movs[0]["tipo"], movs[0]["origem_doc"], movs[0]["quantidade"]) == ("entrada", "PED-0157", qtd(40))


async def test_desfazer_conferido_volta_status_e_estorna(vila):
    await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=vila.h, json={"para": "conferido"})
    entrada = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert entrada["desfazer"]["op"] == "voltar_pedido"
    r = await vila.http.post(f"/registro/{entrada['id']}/desfazer", headers=vila.h)
    assert r.status_code == 200, r.text
    pedidos = (await vila.http.get(f"/obras/{vila.obra}/pedidos?status=recebido", headers=vila.h)).json()
    assert [p["numero"] for p in pedidos] == ["PED-0157"]
    assert (await vila.conta("areia"))["saldo"] == qtd(12)


async def test_viewer_nao_cria_pedido(vila):
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=h, json=pedido_corpo(vila))).status_code == 403


# --- propostas ---

def proposta(vila, tipo="criar_pedido", dados=None, **kw):
    return {"obra_id": vila.obra, "tipo": tipo, "titulo": "Comprar 34 sc de cimento",
            "conta": "precisa 200 − saldo 46 − a caminho 120 = falta 34", "fonte": "almoxarifado",
            "justificativa": "Concretagem da laje na semana que vem.",
            "dados": dados if dados is not None else pedido_corpo(vila, q=34), **kw}


async def test_proposta_so_guarda_nao_executa(vila):
    r = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["status"], p["sugerido_por"], p["justificativa"]) == ("pendente", "agente",
                                                                    "Concretagem da laje na semana que vem.")
    assert (await vila.conta("cimento"))["cotando"] == 0
    assert len((await vila.http.get(f"/obras/{vila.obra}/pedidos", headers=vila.h)).json()) == 4
    pend = (await vila.http.get(f"/propostas?status=pendente&obra_id={vila.obra}", headers=vila.h)).json()
    assert [x["id"] for x in pend] == [p["id"]]


async def test_viewer_pode_propor_mas_nao_aprovar(vila):
    h, _ = await vila.usuario("viewer")
    p = (await vila.http.post("/propostas", headers=h, json=proposta(vila))).json()
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=h)).status_code == 403
    assert (await vila.http.post(f"/propostas/{p['id']}/recusar", headers=h, json={"motivo": "x"})).status_code == 403


async def test_dados_validados_pelo_tipo(vila):
    casos = [
        proposta(vila, dados={"insumo_id": vila.insumo["aço"]}),                                  # faltam campos
        proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0167"], "para": "voar"}),
        proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0167"], "para": "conferido"}),
        proposta(vila, "ajuste_estoque", {"insumo_id": vila.insumo["aço"], "quantidade": 0, "motivo": "x"}),
        proposta(vila, "ajuste_estoque", {"insumo_id": "nao-existe", "quantidade": 5, "motivo": "x"}),
        proposta(vila, dados={**pedido_corpo(vila), "extra": 1}),
    ]
    for corpo in casos:
        r = await vila.http.post("/propostas", headers=vila.h, json=corpo)
        assert r.status_code in (404, 422), (corpo["dados"], r.text)


async def test_aprovar_executa_e_registra_com_desfazer(vila):
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    prop, reg = r.json()["proposta"], r.json()["registro"]
    assert prop["status"] == "aprovada" and prop["decidido_por"] == vila.sessao["usuario"]["id"]
    assert prop["decidido_por_nome"] == reg["quem"] == reg["quem_nome"] == "Admin da Vila Nova"
    assert prop["registro_id"] == reg["id"] and p["registro_id"] is None
    assert (await vila.conta("cimento"))["cotando"] == qtd(34)
    assert (await vila.http.get("/registro", headers=vila.h)).json()[0]["id"] == reg["id"]
    assert (reg["proposta_id"], reg["origem"]) == (p["id"], "agente")
    assert reg["desfazer"]["op"] == "cancelar_pedido"
    assert reg["texto"].startswith("PED-0168 criado: 34 sc de Cimento")
    # aprovar de novo não executa de novo
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 409
    assert (await vila.conta("cimento"))["cotando"] == qtd(34)
    # desfazer cancela o pedido e marca a proposta
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("cimento"))["cotando"] == 0
    desfeitas = (await vila.http.get("/propostas?status=desfeita", headers=vila.h)).json()
    assert [x["id"] for x in desfeitas] == [p["id"]]
    assert desfeitas[0]["registro_id"] == reg["id"]          # continua apontando para a execução desfeita


async def test_proposta_de_avanco_conferido(vila):
    corpo = proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0157"], "para": "conferido"})
    p = (await vila.http.post("/propostas", headers=vila.h, json=corpo)).json()
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 200
    assert (await vila.conta("areia"))["saldo"] == qtd(52)


async def test_proposta_de_ajuste_e_desfazer(vila):
    corpo = proposta(vila, "ajuste_estoque", {"insumo_id": vila.insumo["bloco"], "quantidade": -qtd(40),
                                              "motivo": "contagem de sexta"})
    p = (await vila.http.post("/propostas", headers=vila.h, json=corpo)).json()
    await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert (await vila.conta("bloco"))["saldo"] == qtd(2720)
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == "Ajuste de 40 un de Bloco cerâmico 14x19x29: contagem de sexta"
    await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)
    assert (await vila.conta("bloco"))["saldo"] == qtd(2760)


async def test_falha_na_execucao_deixa_proposta_pendente(vila):
    corpo = proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0167"], "para": "aprovado"})
    p = (await vila.http.post("/propostas", headers=vila.h, json=corpo)).json()
    # alguém cancelou o pedido antes da aprovação
    await vila.http.post(f"/pedidos/{vila.pedido['PED-0167']}/avancar", headers=vila.h, json={"para": "cancelado"})
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 422
    pend = (await vila.http.get("/propostas?status=pendente", headers=vila.h)).json()
    assert [x["id"] for x in pend] == [p["id"]]


async def test_recusar_exige_motivo(vila):
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    assert (await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={})).status_code == 422
    r = await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={"motivo": "já compramos"})
    assert (r.json()["status"], r.json()["motivo_recusa"]) == ("recusada", "já compramos")
    assert r.json()["decidido_por_nome"] == "Admin da Vila Nova"
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 409


async def test_proposta_de_outro_tenant_e_invisivel(vila, http, arm, config):
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    intruso = await nova_vila(http, arm, config)
    assert (await http.post(f"/propostas/{p['id']}/aprovar", headers=intruso.h)).status_code == 404
    assert (await http.get("/propostas", headers=intruso.h)).json() == []
    assert (await http.post("/propostas", headers=intruso.h, json=proposta(vila))).status_code == 404
    reg = (await vila.http.get("/registro", headers=vila.h)).json()
    if reg:
        assert (await http.post(f"/registro/{reg[0]['id']}/desfazer", headers=intruso.h)).status_code == 404


async def test_registro_sem_desfazer(vila):
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={"motivo": "não"})
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 422
