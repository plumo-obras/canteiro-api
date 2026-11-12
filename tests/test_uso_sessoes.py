"""Consumo de IA (token de serviço + usuário conferido), créditos, chave própria e sessões de conversa."""
import pytest
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
    h_viewer, _ = await vila.usuario("viewer")
    assert (await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=h_viewer)).status_code == 403
    assert (await vila.http.delete("/conta/chave-anthropic", headers=h_viewer)).status_code == 403
    h_admin = vila.h                                                      # admin já conecta, troca e remove
    r = await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=h_admin)
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

    assert (await vila.http.delete("/conta/chave-anthropic", headers=h_admin)).status_code == 200
    assert (await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))).status_code == 404


async def test_chave_propria_no_registro_sem_desfazer_e_sem_segredo(vila, config):
    """Exceção documentada em rotas/sessoes.py: o registro mostra conectar/desconectar, mas não guarda receita que
    precisaria do segredo: voltar atrás é desconectar ou colar a chave de novo."""
    chave = "sk-ant-teste-" + novo_id()
    h_master, _ = await vila.usuario("master")
    await vila.http.put("/conta/chave-anthropic", json={"chave": chave}, headers=h_master)
    cifrado = (await vila.arm.obter("segredos", vila.tenant_id, f"anthropic-{vila.tenant_id}"))["cifrado"]
    await vila.http.delete("/conta/chave-anthropic", headers=h_master)
    regs = (await vila.http.get("/registro", headers=h_master)).json()
    conectou = next(x for x in regs if x["texto"].startswith("Conectou a chave própria"))
    desconectou = next(x for x in regs if x["texto"] == "Desconectou a chave própria da Anthropic")
    assert conectou["texto"].endswith(f"(…{chave[-4:]})")
    assert conectou["desfazer"] is None and desconectou["desfazer"] is None
    # nem a chave nem o texto cifrado entram no registro, por nenhum caminho
    brutos = [await vila.arm.obter("registro", vila.tenant_id, x["id"]) for x in (conectou, desconectou)]
    assert chave not in str(brutos) and cifrado not in str(brutos)
    for x in (conectou, desconectou):
        r = await vila.http.post(f"/registro/{x['id']}/desfazer", headers=h_master)
        assert r.status_code == 422 and "não tem como ser desfeita" in r.json()["detail"]
    assert (await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))).status_code == 404


async def test_chave_recusada_pela_anthropic_nao_troca_a_que_funciona(vila, config):
    boa = "sk-ant-teste-" + novo_id()
    assert (await vila.http.put("/conta/chave-anthropic", json={"chave": boa}, headers=vila.h)).status_code == 200
    r = await vila.http.put("/conta/chave-anthropic", json={"chave": "sk-ant-teste-" + novo_id() + "-recusada"},
                            headers=vila.h)
    assert r.status_code == 422 and "recusou" in r.json()["detail"]
    assert (await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))).json() == {"chave": boa}
    regs = (await vila.http.get("/registro", headers=vila.h)).json()
    assert sum(x["texto"].startswith("Conectou a chave própria") for x in regs) == 1

    # Anthropic fora do ar: 503, e também nada muda
    r = await vila.http.put("/conta/chave-anthropic", json={"chave": "sk-ant-teste-" + novo_id() + "-fora"},
                            headers=vila.h)
    assert r.status_code == 503
    assert (await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))).json() == {"chave": boa}
    regs = (await vila.http.get("/registro", headers=vila.h)).json()
    assert sum(x["texto"].startswith("Conectou a chave própria") for x in regs) == 1


async def test_chave_colada_com_bordas_e_formato_fechado(vila, config):
    """Espaço ou quebra de linha nas bordas (colar do Console) é limpo; no meio, ou caractere fora do formato, é 422
    antes de qualquer chamada à Anthropic."""
    chave = "sk-ant-teste-" + novo_id()
    r = await vila.http.put("/conta/chave-anthropic", json={"chave": f"  {chave}\n"}, headers=vila.h)
    assert r.status_code == 200 and r.json()["final"] == chave[-4:]
    interna = await vila.http.get("/interno/chave-anthropic", headers=cab_servico(vila, config))
    assert interna.json() == {"chave": chave}
    for ruim in (chave[:15] + " " + chave[15:], chave[:15] + "\n" + chave[15:], chave + "é"):
        r = await vila.http.put("/conta/chave-anthropic", json={"chave": ruim}, headers=vila.h)
        assert r.status_code == 422 and ruim not in r.text


@pytest.mark.parametrize("status,erro", [(200, None), (429, None), (401, "ChaveRecusada"), (403, "ChaveRecusada"),
                                         (500, "AnthropicForaDoAr")])
async def test_conferir_chave_na_anthropic(status, erro):
    """A conferência de verdade, contra uma Anthropic falsa: só a lista de modelos, com a chave no cabeçalho."""
    import anthropic
    import httpx2

    from canteiro_api.servicos import chave_anthropic

    pedidos = []

    def responder(req: httpx2.Request) -> httpx2.Response:
        pedidos.append(req)
        return httpx2.Response(status, json={"data": [], "has_more": False, "first_id": None, "last_id": None}
                              if status == 200 else {"type": "error", "error": {"type": "x", "message": "x"}},
                              headers={"retry-after": "0"})

    async with anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(responder)) as http_client:
        if erro:
            with pytest.raises(getattr(chave_anthropic, erro)) as e:
                await chave_anthropic.conferir_chave("sk-ant-falsa-123456789012", http_client=http_client)
            assert "sk-ant-falsa" not in e.value.mensagem
        else:
            await chave_anthropic.conferir_chave("sk-ant-falsa-123456789012", http_client=http_client)
    assert all(p.method == "GET" and p.url.path == "/v1/models" for p in pedidos)
    h = pedidos[0].headers                       # a chave do cliente; o cabeçalho quem escolhe é o SDK
    assert "sk-ant-falsa-123456789012" in h.get("authorization", "") + h.get("x-api-key", "")


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


async def test_abrir_sessao_alheia_responde_como_inexistente(vila, http, arm, config):
    """POST /sessoes com o ID de outra pessoa (mesmo cliente) ou de outro cliente (o ID colide na coleção inteira)
    responde exatamente como uma sessão que não existe: nem o código nem a mensagem revelam que ela está lá."""
    sid = (await vila.http.post("/sessoes", headers=vila.h, json={})).json()["id"]
    colega, _ = await vila.usuario("admin")
    intruso = await nova_vila(http, arm, config)
    inexistente = await http.get(f"/sessoes/{novo_id()}", headers=vila.h)
    assert (inexistente.status_code, inexistente.json()) == (404, {"detail": "Conversa não encontrada."})
    for h in (colega, intruso.h):
        r = await http.post("/sessoes", headers=h, json={"id": sid})
        assert (r.status_code, r.json()) == (404, {"detail": "Conversa não encontrada."})
    # nada foi gravado no lugar: a sessão continua do dono e sem mensagens de ninguém
    s = (await vila.http.get(f"/sessoes/{sid}", headers=vila.h)).json()
    assert s["dono"] == vila.sessao["usuario"]["id"] and s["total_mensagens"] == 0
