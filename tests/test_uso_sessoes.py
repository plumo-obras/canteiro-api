"""Consumo de IA (token de serviço + usuário conferido), créditos, chave própria e sessões de conversa."""
from conftest import nova_vila

from canteiro_dominio.tipos import novo_id


def linha_uso(vila, **kw):
    return {"id": novo_id(), "tenant_id": vila.tenant_id, "usuario_id": vila.sessao["usuario"]["id"],
            "obra_id": vila.obra, "rota": "conversa", "modelo": "claude-opus-5-5", "tokens_entrada": 1000,
            "tokens_saida": 200, "tokens_cache_leitura": 5000, "custo_microdolares": 9000, "dia": "2026-10-30", **kw}


def cab_servico(vila, config):
    return {"Authorization": f"Bearer {config.token_servico_agente}", "X-Canteiro-Usuario": vila.h["Authorization"]}


async def test_uso_exige_token_de_servico_e_usuario(vila, config):
    corpo = linha_uso(vila)
    assert (await vila.http.post("/uso", json=corpo, headers=vila.h)).status_code == 401
    so_servico = {"Authorization": f"Bearer {config.token_servico_agente}"}
    assert (await vila.http.post("/uso", json=corpo, headers=so_servico)).status_code == 401


async def test_uso_confere_corpo_contra_o_usuario(vila, http, arm, config):
    outra = await nova_vila(http, arm, config)
    r = await http.post("/uso", json=linha_uso(outra), headers=cab_servico(vila, config))
    assert r.status_code == 403
    r = await http.post("/uso", json=linha_uso(vila, usuario_id="outro"), headers=cab_servico(vila, config))
    assert r.status_code == 403


async def test_uso_grava_desconta_e_e_idempotente(vila, config):
    antes = (await vila.http.get("/creditos", headers=vila.h)).json()
    assert antes["usuario_id"] == vila.sessao["usuario"]["id"] and antes["tenant_id"] == vila.tenant_id
    corpo = linha_uso(vila)
    a = await vila.http.post("/uso", json=corpo, headers=cab_servico(vila, config))
    b = await vila.http.post("/uso", json=corpo, headers=cab_servico(vila, config))
    assert (a.status_code, b.status_code) == (201, 200)
    depois = (await vila.http.get("/creditos", headers=vila.h)).json()
    assert antes["saldo_microdolares"] - depois["saldo_microdolares"] == 9000


async def test_relatorio_soma_na_api(vila, config):
    cab = cab_servico(vila, config)
    await vila.http.post("/uso", json=linha_uso(vila), headers=cab)
    await vila.http.post("/uso", json=linha_uso(vila, rota="briefing", custo_microdolares=1000), headers=cab)
    await vila.http.post("/uso", json=linha_uso(vila, dia="2026-10-31", obra_id=None, tokens_cache_leitura=0),
                         headers=cab)
    await vila.http.post("/uso", json=linha_uso(vila, dia="2026-11-15"), headers=cab)   # fora do período
    r = (await vila.http.get("/uso?de=2026-10-01&ate=2026-10-31", headers=vila.h)).json()
    assert r["total"] == {"custo": 19000, "tokens": 6200 * 2 + 1200}
    assert r["por_dia"] == [{"dia": "2026-10-30", "custo": 10000, "tokens": 12400},
                            {"dia": "2026-10-31", "custo": 9000, "tokens": 1200}]
    assert r["por_rota"][0] == {"rota": "conversa", "custo": 18000, "tokens": 7400}
    assert {(x["obra_id"], x["custo"]) for x in r["por_obra"]} == {(vila.obra, 10000), (None, 9000)}
    assert (await vila.http.get("/uso?de=2026-10-31&ate=2026-10-01", headers=vila.h)).status_code == 422


async def test_sem_creditos_402(vila, config):
    await vila.arm.atualizar("tenants", vila.tenant_id, vila.tenant_id, {"saldo_microdolares": 0})
    r = await vila.http.get("/creditos", headers=vila.h)
    assert r.status_code == 402
    assert "créditos" in r.json()["detail"] and r.json()["saldo_microdolares"] == 0


async def test_chave_propria_cifrada_e_entregue_so_ao_agente(vila, config):
    chave = "sk-ant-teste-" + novo_id()
    assert (await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=vila.h)).status_code == 403
    h_master, _ = await vila.usuario("master")
    r = await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=h_master)
    assert r.json() == {"chave_propria": True, "final": chave[-4:]}
    guardado = await vila.arm.obter("segredos", vila.tenant_id, f"anthropic-{vila.tenant_id}")
    assert chave not in str(guardado)                                     # cifrada em repouso
    assert chave not in str((await vila.http.get("/registro", headers=vila.h)).json())

    # com chave própria não há 402, mesmo sem saldo
    await vila.arm.atualizar("tenants", vila.tenant_id, vila.tenant_id, {"saldo_microdolares": 0})
    assert (await vila.http.get("/creditos", headers=vila.h)).json()["chave_propria"] is True

    assert (await vila.http.get("/interno/chave-anthropic", headers=vila.h)).status_code == 401
    r = await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))
    assert r.status_code == 200 and r.json() == {"chave": chave}
    assert r.headers["cache-control"] == "no-store"

    # o mesmo texto cifrado copiado para outro cliente não decifra
    import pytest

    from canteiro_api.cofre import CofreAesGcm, SegredoIlegivel
    with pytest.raises(SegredoIlegivel):
        CofreAesGcm(config.chave_cifra).decifrar(guardado["cifrado"], tenant_id="outro")

    await vila.http.delete("/conta/chave-anthropic", headers=h_master)
    assert (await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))).status_code == 404


# --- sessões ---

async def test_sessao_criar_acrescentar_e_ler(vila):
    sid = novo_id()
    r = await vila.http.post("/sessoes", headers=vila.h, json={"id": sid, "obra_id": vila.obra, "titulo": "Aço"})
    assert r.status_code == 201 and r.json()["total_mensagens"] == 0
    assert (await vila.http.post("/sessoes", headers=vila.h, json={"id": sid})).status_code == 200

    lote1 = {"seq_inicial": 0, "mensagens": [{"papel": "user", "conteudo": "quanto falta de aço?"},
                                             {"papel": "assistant", "conteudo": [{"type": "text", "text": "Nada."}]}]}
    r = await vila.http.post(f"/sessoes/{sid}/mensagens", headers=vila.h, json=lote1)
    assert r.status_code == 201 and r.json()["total_mensagens"] == 2
    assert [m["id"] for m in r.json()["mensagens"]] == [f"{sid}-000000", f"{sid}-000001"]
    # reenvio do mesmo lote: nada novo
    r = await vila.http.post(f"/sessoes/{sid}/mensagens", headers=vila.h, json=lote1)
    assert r.status_code == 200 and r.json()["total_mensagens"] == 2
    # lote diferente na posição antiga, ou buraco na sequência: 409
    assert (await vila.http.post(f"/sessoes/{sid}/mensagens", headers=vila.h, json={
        "seq_inicial": 1, "mensagens": [{"papel": "user", "conteudo": "outra"}]})).status_code == 409
    assert (await vila.http.post(f"/sessoes/{sid}/mensagens", headers=vila.h, json={
        "seq_inicial": 5, "mensagens": [{"papel": "user", "conteudo": "x"}]})).status_code == 409
    await vila.http.post(f"/sessoes/{sid}/mensagens", headers=vila.h, json={
        "seq_inicial": 2, "mensagens": [{"papel": "user", "conteudo": "e cimento?"}]})

    msgs = (await vila.http.get(f"/sessoes/{sid}/mensagens", headers=vila.h)).json()
    assert [(m["seq"], m["papel"]) for m in msgs] == [(0, "user"), (1, "assistant"), (2, "user")]
    assert msgs[1]["conteudo"] == [{"type": "text", "text": "Nada."}]
    assert [m["seq"] for m in (await vila.http.get(f"/sessoes/{sid}/mensagens?depois_de=0&limite=1",
                                                   headers=vila.h)).json()] == [1]

    r = await vila.http.patch(f"/sessoes/{sid}", headers=vila.h, json={"abas": [{"tela": "estoque"}]})
    assert r.json()["abas"] == [{"tela": "estoque"}]
    lista = (await vila.http.get(f"/sessoes?obra_id={vila.obra}", headers=vila.h)).json()
    assert [s["id"] for s in lista] == [sid] and lista[0]["total_mensagens"] == 3


async def test_sessao_e_privada(vila, http, arm, config):
    sid = (await vila.http.post("/sessoes", headers=vila.h, json={})).json()["id"]
    colega, _ = await vila.usuario("admin")                 # mesmo cliente, outra pessoa
    intruso = await nova_vila(http, arm, config)          # outro cliente
    for h in (colega, intruso.h):
        assert (await http.get(f"/sessoes/{sid}/mensagens", headers=h)).status_code == 404
        assert (await http.post(f"/sessoes/{sid}/mensagens", headers=h, json={
            "seq_inicial": 0, "mensagens": [{"papel": "user", "conteudo": "oi"}]})).status_code == 404
        assert (await http.get("/sessoes", headers=h)).json() == []
    assert (await http.post("/sessoes", headers=colega, json={"id": sid})).status_code == 409
