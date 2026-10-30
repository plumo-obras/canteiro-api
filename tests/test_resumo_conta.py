"""Página inicial (resumo das obras) e conta (perfil, chave própria sem o segredo, créditos)."""
from conftest import nova_vila
from test_pedidos_propostas import pedido_corpo, proposta

from canteiro_dominio.tipos import novo_id


async def test_resumo_soma_na_api_a_partir_do_seed(vila):
    r = await vila.http.get("/obras/resumo", headers=vila.h)
    assert r.status_code == 200, r.text
    [res] = r.json()
    assert res["obra"]["id"] == vila.obra and res["obra"]["nome"] == "Casa Vila Nova"
    # bloco (falta 2.040), cimento (34) e areia (8) faltam; o aço fecha com o que está a caminho e cotando
    assert res["falta_comprar"] == 3
    assert res["pedidos_abertos"] == 4
    assert res["valor_pedidos_abertos"] == 480000 + 2080000 + 456000 + 396000
    assert res["nf_para_conferir"] == 1                                    # PED-0157 recebido
    assert res["pendencias"] == 0


async def test_resumo_bate_com_as_rotas_da_obra(vila):
    """O número da página inicial tem de ser o mesmo que a pessoa vê ao entrar na obra."""
    await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json=pedido_corpo(vila, q=34))
    [res] = (await vila.http.get("/obras/resumo", headers=vila.h)).json()

    insumos = (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()
    pedidos = (await vila.http.get(f"/obras/{vila.obra}/pedidos", headers=vila.h)).json()
    abertos = [p for p in pedidos if p["status"] not in ("conferido", "cancelado")]
    pendentes = (await vila.http.get(f"/propostas?status=pendente&obra_id={vila.obra}", headers=vila.h)).json()
    registro = (await vila.http.get(f"/registro?obra_id={vila.obra}&limite=1", headers=vila.h)).json()

    assert res["falta_comprar"] == sum(1 for x in insumos if x["conta"]["comprar"] > 0) == 2   # cimento coberto
    assert res["pedidos_abertos"] == len(abertos) == 5
    assert res["valor_pedidos_abertos"] == sum(p["valor"] for p in abertos)
    assert res["pendencias"] == len(pendentes) == 2
    assert res["ultima_atividade"] == registro[0]["criado_em"]


async def test_resumo_conferir_tira_da_lista(vila):
    await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=vila.h, json={"para": "conferido"})
    [res] = (await vila.http.get("/obras/resumo", headers=vila.h)).json()
    assert (res["nf_para_conferir"], res["pedidos_abertos"]) == (0, 3)
    assert res["valor_pedidos_abertos"] == 2080000 + 456000 + 396000


async def test_resumo_so_do_proprio_tenant(vila, http, arm, config):
    outra = await nova_vila(http, arm, config)
    await outra.http.post("/propostas", headers=outra.h, json=proposta(outra))
    [res] = (await vila.http.get("/obras/resumo", headers=vila.h)).json()
    assert res["obra"]["id"] == vila.obra and res["pendencias"] == 0
    [res_outra] = (await http.get("/obras/resumo", headers=outra.h)).json()
    assert res_outra["obra"]["id"] == outra.obra and res_outra["pendencias"] == 1


async def test_resumo_qualquer_papel_le_e_exige_sessao(vila):
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get("/obras/resumo", headers=h)).status_code == 200
    assert (await vila.http.get("/obras/resumo")).status_code == 401


async def test_resumo_cliente_sem_obra(vila, arm):
    h, _ = await vila.usuario("viewer", tenant_id=f"t-{novo_id()}")
    assert (await vila.http.get("/obras/resumo", headers=h)).json() == []


# --- conta ---

async def test_conta_traz_perfil_tenant_e_creditos(vila):
    r = await vila.http.get("/conta", headers=vila.h)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["usuario"] == {"id": vila.sessao["usuario"]["id"], "nome": vila.sessao["usuario"]["nome"],
                            "email": vila.email, "papel": "admin"}
    assert c["tenant"] == {"id": vila.tenant_id, "nome": "Construtora Exemplo"}
    assert c["chave_propria"] is None
    creditos = (await vila.http.get("/creditos", headers=vila.h)).json()
    assert c["creditos"] == creditos


async def test_conta_sem_saldo_nao_da_402(vila):
    """O /creditos responde 402 para o agente parar; a tela da conta precisa mostrar o saldo zerado."""
    await vila.arm.atualizar("tenants", vila.tenant_id, vila.tenant_id, {"saldo_microdolares": 0})
    r = await vila.http.get("/conta", headers=vila.h)
    assert r.status_code == 200 and r.json()["creditos"]["saldo_microdolares"] == 0


async def test_conta_papel_vem_do_banco(vila):
    h, u = await vila.usuario("viewer")
    assert (await vila.http.get("/conta", headers=h)).json()["usuario"]["papel"] == "viewer"
    await vila.arm.atualizar("usuarios", vila.tenant_id, u["id"], {"papel": "master"})
    assert (await vila.http.get("/conta", headers=h)).json()["usuario"]["papel"] == "master"


async def test_situacao_da_chave_sem_o_segredo(vila, http, arm, config):
    estado = await vila.http.get("/conta/chave-anthropic", headers=vila.h)
    assert estado.json() == {"ativa": False, "final": None, "cadastrada_em": None}

    chave = "sk-ant-teste-" + novo_id()
    h_master, _ = await vila.usuario("master")
    assert (await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=h_master)).status_code == 200

    h_viewer, _ = await vila.usuario("viewer")
    for h in (vila.h, h_master, h_viewer):                                  # qualquer papel vê a situação
        r = await vila.http.get("/conta/chave-anthropic", headers=h)
        assert r.status_code == 200
        assert r.json()["ativa"] is True and r.json()["final"] == chave[-4:] and r.json()["cadastrada_em"]
        assert chave not in r.text and "cifrado" not in r.text
        conta = await vila.http.get("/conta", headers=h)
        assert conta.json()["chave_propria"]["final"] == chave[-4:] and chave not in conta.text
        assert conta.json()["creditos"]["chave_propria"] is True

    # outro cliente não enxerga a chave deste
    outra = await nova_vila(http, arm, config)
    assert (await http.get("/conta/chave-anthropic", headers=outra.h)).json()["ativa"] is False
    assert (await http.get("/conta", headers=outra.h)).json()["chave_propria"] is None

    await vila.http.delete("/conta/chave-anthropic", headers=h_master)
    assert (await vila.http.get("/conta/chave-anthropic", headers=h_viewer)).json() == {
        "ativa": False, "final": None, "cadastrada_em": None}
    assert (await vila.http.get("/conta", headers=h_viewer)).json()["chave_propria"] is None


async def test_conta_exige_sessao(vila):
    assert (await vila.http.get("/conta")).status_code == 401
    assert (await vila.http.get("/conta/chave-anthropic")).status_code == 401
