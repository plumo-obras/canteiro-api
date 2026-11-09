"""Pedidos (número por tenant, transições, conferido dá entrada) e propostas (só aprovação executa; desfazer)."""
import asyncio
import uuid

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
    assert entrada["desfazer"]["op"] == "desfazer_recebimento"
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
    assert (prop["ajustada"], prop["dados_originais"]) == (False, None)
    # aprovar a proposta já é a aprovação da compra: o pedido nasce aprovado (a caminho), não em cotação
    cimento = await vila.conta("cimento")
    assert (cimento["cotando"], cimento["a_caminho"]) == (0, qtd(154))
    ped = (await vila.http.get(f"/obras/{vila.obra}/pedidos?status=aprovado", headers=vila.h)).json()
    assert [x["numero"] for x in ped] == ["PED-0168"]
    assert (await vila.http.get("/registro", headers=vila.h)).json()[0]["id"] == reg["id"]
    assert (reg["proposta_id"], reg["origem"]) == (p["id"], "agente")
    assert reg["desfazer"] == {"op": "cancelar_pedido", "pedido_id": ped[0]["id"], "status_criado": "aprovado"}
    assert reg["texto"] == ("PED-0168 criado: 34 sc de Cimento CP-II 50 kg com Depósito São Jorge (R$ 1.140,00)"
                            " · aprovado")
    # aprovar de novo não executa de novo
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 409
    assert (await vila.conta("cimento"))["a_caminho"] == qtd(154)
    # desfazer cancela o pedido e marca a proposta
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("cimento"))["a_caminho"] == qtd(120)
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
    assert reg["texto"] == "Ajuste de -40 un de Bloco cerâmico 14x19x29: contagem de sexta"
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


# --- sem proposta duplicada ---

async def _pendentes(vila):
    return (await vila.http.get(f"/propostas?status=pendente&obra_id={vila.obra}", headers=vila.h)).json()


async def test_mesma_chave_devolve_a_mesma_proposta(vila):
    """O agente manda (sessão, tool_use) como chave: a repetição da mesma chamada de ferramenta não duplica."""
    corpo = proposta(vila, chave_idempotencia="sessao-1:toolu_1")
    a = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    b = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["id"] == b.json()["id"] and (a.json()["ja_existia"], b.json()["ja_existia"]) == (False, True)
    assert [x["id"] for x in await _pendentes(vila)] == [a.json()["id"]]
    # a mesma chave com outro conteúdo é erro de quem chama, não uma proposta nova
    outra = proposta(vila, dados=pedido_corpo(vila, q=10), chave_idempotencia="sessao-1:toolu_1")
    assert (await vila.http.post("/propostas", headers=vila.h, json=outra)).status_code == 409
    # decidida, a mesma chave continua devolvendo ela (com o status de agora), sem criar outra
    aprovada = await vila.http.post(f"/propostas/{a.json()['id']}/aprovar", headers=vila.h)
    assert aprovada.status_code == 200
    de_novo = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    assert (de_novo.status_code, de_novo.json()["status"], de_novo.json()["ja_existia"]) == (200, "aprovada", True)


async def test_proposta_igual_pendente_volta_a_existente(vila):
    """Sem chave (outra volta da conversa, outra aba): a mesma proposta pendente na mesma obra não nasce de novo."""
    a = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    # título e justificativa são texto do agente; o que decide é tipo + dados
    b = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila, titulo="Repor cimento",
                                                                         justificativa="de novo"))
    assert (a.status_code, b.status_code, b.json()["ja_existia"]) == (201, 200, True)
    assert b.json()["id"] == a.json()["id"] and b.json()["titulo"] == "Comprar 34 sc de cimento"
    c = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila, dados=pedido_corpo(vila, q=30)))
    assert (c.status_code, c.json()["ja_existia"]) == (201, False)
    assert len(await _pendentes(vila)) == 2


async def test_decidir_solta_a_trava_e_a_mesma_proposta_pode_voltar(vila):
    a = (await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))).json()
    await vila.http.post(f"/propostas/{a['id']}/recusar", headers=vila.h, json={"motivo": "agora não"})
    b = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    assert (b.status_code, b.json()["ja_existia"]) == (201, False) and b.json()["id"] != a["id"]
    # aprovar com ajuste também solta: a trava é do que o agente propôs, não do que foi executado
    r = await vila.http.post(f"/propostas/{b.json()['id']}/aprovar", headers=vila.h,
                             json={"ajustes": {"quantidade": qtd(30)}})
    assert r.status_code == 200 and r.json()["proposta"]["ajustada"] is True
    c = await vila.http.post("/propostas", headers=vila.h, json=proposta(vila))
    assert (c.status_code, c.json()["ja_existia"]) == (201, False)
    assert [x["id"] for x in await _pendentes(vila)] == [c.json()["id"]]


async def test_propostas_iguais_ao_mesmo_tempo_viram_uma(vila):
    rs = await asyncio.gather(*[vila.http.post("/propostas", headers=vila.h, json=proposta(vila)) for _ in range(4)])
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 201], [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert len(await _pendentes(vila)) == 1


# --- conferir com o que chegou de fato ---

async def _conferir(vila, **extra):
    return await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=vila.h,
                                json={"para": "conferido", **extra})


async def _movs_areia(vila):
    return (await vila.http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['areia']}/movimentos",
                                headers=vila.h)).json()


async def test_conferir_com_menos_que_o_pedido_deixa_o_resto_a_caminho(vila):
    assert (await vila.conta("areia"))["falta"] == qtd(8)          # 60 − 12 − 40 a caminho
    r = await _conferir(vila, quantidade_recebida=qtd(36), nf=" 4471 ")
    assert r.status_code == 200, r.text
    ped = r.json()
    linha = ped["linhas"][0]
    assert (ped["status"], linha["quantidade"], linha["recebida"], ped["recebimentos"]) == (
        "parcial", qtd(40), qtd(36), 1)
    # entrega parcial: os 4 m³ que não vieram continuam a caminho (a falta não muda)
    areia = await vila.conta("areia")
    assert (areia["saldo"], areia["a_caminho"], areia["falta"]) == (qtd(48), qtd(4), qtd(8))
    # e encerrar o resto (o fornecedor não manda) tira os 4 m³ do caminho: voltam como falta
    r = await vila.http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=vila.h,
                             json={"para": "conferido"})
    assert (r.status_code, r.json()["status"]) == (200, "conferido"), r.text
    areia = await vila.conta("areia")
    assert (areia["a_caminho"], areia["falta"]) == (0, qtd(12))
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == "PED-0157 encerrado: o resto não vem (4 m³ de Areia média)"
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("areia"))["a_caminho"] == qtd(4)
    m = (await _movs_areia(vila))[0]
    assert (m["tipo"], m["quantidade"], m["origem_doc"]) == ("entrada", qtd(36), "PED-0157 · NF 4471")
    assert (m["quem"], m["usuario_id"]) == ("Admin da Vila Nova", vila.sessao["usuario"]["id"])
    reg = next(x for x in (await vila.http.get("/registro", headers=vila.h)).json() if x["id"] == ped["registro_id"])
    assert reg["texto"] == "PED-0157 · 1ª entrega (NF 4471): 36 de 40 m³ de Areia média · faltam 4 m³ de Areia média"


async def test_conferir_com_mais_que_o_pedido(vila):
    ped = (await _conferir(vila, quantidade_recebida=qtd(45))).json()
    assert (ped["status"], ped["linhas"][0]["recebida"]) == ("conferido", qtd(45))
    assert (await vila.conta("areia"))["saldo"] == qtd(57)
    assert (await _movs_areia(vila))[0]["origem_doc"] == "PED-0157"
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == "PED-0157 · 1ª entrega: 45 de 40 m³ de Areia média · pedido completo"


async def test_conferir_sem_divergencia_so_cita_a_nf(vila):
    ped = (await _conferir(vila, nf="4471")).json()
    assert ped["linhas"][0]["recebida"] == qtd(40)
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["texto"] == "PED-0157 · 1ª entrega (NF 4471): 40 m³ de Areia média · pedido completo"


async def test_desfazer_conferencia_parcial_limpa_e_estorna_o_que_entrou(vila):
    ped = (await _conferir(vila, quantidade_recebida=qtd(36), nf="4471")).json()
    r = await vila.http.post(f"/registro/{ped['registro_id']}/desfazer", headers=vila.h)
    assert r.status_code == 200, r.text
    volta = (await vila.http.get(f"/obras/{vila.obra}/pedidos?status=recebido", headers=vila.h)).json()
    assert [(p["numero"], p["linhas"][0]["recebida"], p["recebimentos"]) for p in volta] == [("PED-0157", 0, 0)]
    areia = await vila.conta("areia")
    assert (areia["saldo"], areia["a_caminho"]) == (qtd(12), qtd(40))
    estorno = (await _movs_areia(vila))[0]
    assert (estorno["tipo"], estorno["quantidade"]) == ("saida", qtd(36))


async def test_conferir_desfazer_e_conferir_de_novo(vila):
    # a 2ª conferência é outra entrada: colidir com o ID da 1ª (já estornada) dava 409 e travava o pedido
    ped = (await _conferir(vila, quantidade_recebida=qtd(36), nf="4471")).json()
    assert (await vila.http.post(f"/registro/{ped['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    r = await _conferir(vila, quantidade_recebida=qtd(40))
    assert r.status_code == 200, r.text
    assert (await vila.conta("areia"))["saldo"] == qtd(52)
    # e o desfazer da 2ª estorna a 2ª, não a 1ª
    assert (await vila.http.post(f"/registro/{r.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("areia"))["saldo"] == qtd(12)


async def test_quantidade_recebida_e_nf_so_ao_conferir(vila):
    ped = vila.pedido["PED-0158"]                                    # em trânsito
    for extra in ({"quantidade_recebida": qtd(10)}, {"nf": "123"}):
        r = await vila.http.post(f"/pedidos/{ped}/avancar", headers=vila.h, json={"para": "recebido", **extra})
        assert r.status_code == 422, r.text
        assert "só valem ao conferir" in r.json()["detail"]
    assert (await _conferir(vila, quantidade_recebida=0)).status_code == 422
    assert (await _conferir(vila, nf="9" * 61)).status_code == 422
    assert (await vila.conta("areia"))["saldo"] == qtd(12)


async def test_avancar_devolve_registro_id(vila):
    r = await vila.http.post(f"/pedidos/{vila.pedido['PED-0158']}/avancar", headers=vila.h, json={"para": "recebido"})
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert r.json()["registro_id"] == reg["id"]
    assert r.json()["recebimentos"] == 0


# --- pedido manual que já nasce aprovado ---

async def test_pedido_manual_aprovado_e_desfazer(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h,
                             json={**pedido_corpo(vila), "aprovado": True})
    assert r.status_code == 201, r.text
    ped = r.json()
    assert ped["status"] == "aprovado" and "aprovado" not in ped        # o corpo não vira campo do pedido
    assert (await vila.conta("cimento"))["a_caminho"] == qtd(150)
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert ped["registro_id"] == reg["id"]
    assert reg["desfazer"]["status_criado"] == "aprovado"
    assert (await vila.http.post(f"/registro/{ped['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    cancelados = (await vila.http.get(f"/obras/{vila.obra}/pedidos?status=cancelado", headers=vila.h)).json()
    assert [p["id"] for p in cancelados] == [ped["id"]]


async def test_pedido_manual_padrao_continua_em_cotacao(vila):
    ped = (await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json=pedido_corpo(vila))).json()
    assert ped["status"] == "em_cotacao" and ped["registro_id"]
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert reg["desfazer"]["status_criado"] == "em_cotacao"


async def test_desfazer_pedido_que_ja_andou_e_conflito(vila):
    ped = (await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h,
                                json={**pedido_corpo(vila), "aprovado": True})).json()
    await vila.http.post(f"/pedidos/{ped['id']}/avancar", headers=vila.h, json={"para": "enviado"})
    r = await vila.http.post(f"/registro/{ped['registro_id']}/desfazer", headers=vila.h)
    assert r.status_code == 409
    assert "já andou" in r.json()["detail"]


async def test_desfazer_receita_antiga_sem_status_criado(vila):
    """Registro gravado antes do campo existir: o pedido nascia em cotação."""
    ped = (await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json=pedido_corpo(vila))).json()
    receita = {"op": "cancelar_pedido", "pedido_id": ped["id"]}

    async def tirar_campo(tx):
        tx.atualizar("registro", vila.tenant_id, ped["registro_id"], {"desfazer": receita})
        return None

    await vila.arm.em_transacao(tirar_campo)
    assert (await vila.http.post(f"/registro/{ped['registro_id']}/desfazer", headers=vila.h)).status_code == 200


# --- aprovar com ajuste ---

async def _aprovar(vila, p, ajustes=None):
    corpo = None if ajustes is None else {"ajustes": ajustes}
    return await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h, json=corpo)


async def _pendente(vila, corpo):
    r = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    assert r.status_code == 201, r.text
    return r.json()


async def test_ajustar_pedido_antes_de_aprovar(vila):
    p = await _pendente(vila, proposta(vila))
    r = await _aprovar(vila, p, {"quantidade": qtd(30), "fornecedor": "Casa do Cimento"})
    assert r.status_code == 200, r.text
    prop, reg = r.json()["proposta"], r.json()["registro"]
    assert prop["ajustada"] is True
    assert (prop["dados"]["quantidade"], prop["dados"]["fornecedor"]) == (qtd(30), "Casa do Cimento")
    assert prop["dados_originais"] == p["dados"]
    assert reg["texto"].endswith("· ajustado: quantidade 34 → 30 sc; fornecedor Depósito São Jorge → Casa do Cimento")
    ped = (await vila.http.get(f"/obras/{vila.obra}/pedidos?status=aprovado", headers=vila.h)).json()[0]
    assert (ped["linhas"][0]["quantidade"], ped["fornecedor"]) == (qtd(30), "Casa do Cimento")
    # o que foi gravado é o que a lista devolve depois
    salvo = (await vila.http.get("/propostas?status=aprovada", headers=vila.h)).json()[0]
    assert (salvo["ajustada"], salvo["dados"]["quantidade"], salvo["dados_originais"]["quantidade"]) == (
        True, qtd(30), qtd(34))


async def test_ajuste_com_campo_nao_ajustavel_e_recusado(vila):
    p = await _pendente(vila, proposta(vila))
    for ajustes in ({"insumo_id": vila.insumo["areia"]}, {"aprovado": True}, {"quantidade": 0},
                    {"quantidade": None}, {"valor": -1}):
        r = await _aprovar(vila, p, ajustes)
        assert r.status_code == 422, (ajustes, r.text)
    assert "insumo_id" in (await _aprovar(vila, p, {"insumo_id": "x"})).json()["detail"]
    pend = (await vila.http.get("/propostas?status=pendente", headers=vila.h)).json()
    assert [x["id"] for x in pend] == [p["id"]]
    assert (await vila.conta("cimento"))["a_caminho"] == qtd(120)


async def test_ajuste_vazio_ou_igual_aprova_como_proposto(vila):
    for ajustes in ({}, {"quantidade": qtd(34)}):
        p = await _pendente(vila, proposta(vila))
        prop = (await _aprovar(vila, p, ajustes)).json()["proposta"]
        assert (prop["ajustada"], prop["dados_originais"], prop["dados"]) == (False, None, p["dados"])


async def test_ajustar_ajuste_de_estoque(vila):
    dados = {"insumo_id": vila.insumo["bloco"], "quantidade": -qtd(40), "motivo": "contagem de sexta"}
    p = await _pendente(vila, proposta(vila, "ajuste_estoque", dados))
    assert (await _aprovar(vila, p, {"quantidade": 0})).status_code == 422
    assert (await _aprovar(vila, p, {"destino": "x"})).status_code == 422
    r = await _aprovar(vila, p, {"quantidade": -qtd(30), "motivo": "recontagem"})
    assert r.status_code == 200, r.text
    assert (await vila.conta("bloco"))["saldo"] == qtd(2730)
    assert r.json()["registro"]["texto"] == ("Ajuste de -30 un de Bloco cerâmico 14x19x29: recontagem · ajustado: "
                                             "quantidade -40 → -30 un; motivo contagem de sexta → recontagem")


async def test_ajustar_conferencia_proposta(vila):
    p = await _pendente(vila, proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0157"],
                                                                "para": "conferido"}))
    r = await _aprovar(vila, p, {"quantidade_recebida": qtd(36), "nf": "4471"})
    assert r.status_code == 200, r.text
    assert r.json()["registro"]["texto"] == ("PED-0157 · 1ª entrega (NF 4471): 36 de 40 m³ de Areia média · faltam "
                                             "4 m³ de Areia média · ajustado: quantidade recebida — → 36 m³; "
                                             "nf — → 4471")
    assert r.json()["proposta"]["dados_originais"] == {"pedido_id": vila.pedido["PED-0157"], "para": "conferido",
                                                       "quantidade_recebida": None, "nf": None}
    assert (await vila.conta("areia"))["saldo"] == qtd(48)


async def test_ajuste_de_conferencia_so_em_proposta_de_conferir(vila):
    p = await _pendente(vila, proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0158"],
                                                                "para": "recebido"}))
    r = await _aprovar(vila, p, {"nf": "4471"})
    assert r.status_code == 422 and "só valem ao conferir" in r.json()["detail"]


async def test_proposta_de_conferir_com_quantidade_recebida(vila):
    dados = {"pedido_id": vila.pedido["PED-0157"], "para": "conferido", "quantidade_recebida": qtd(38), "nf": "88"}
    p = await _pendente(vila, proposta(vila, "avancar_pedido", dados))
    assert (await _aprovar(vila, p)).status_code == 200
    assert (await vila.conta("areia"))["saldo"] == qtd(50)
    assert (await _movs_areia(vila))[0]["origem_doc"] == "PED-0157 · NF 88"
    # fora da conferência, nem a criação aceita
    ruim = proposta(vila, "avancar_pedido", {"pedido_id": vila.pedido["PED-0158"], "para": "recebido", "nf": "1"})
    assert (await vila.http.post("/propostas", headers=vila.h, json=ruim)).status_code == 422
    # e a proposta de compra não aceita `aprovado` (aprovar a proposta já é a aprovação)
    com_aprovado = proposta(vila, dados={**pedido_corpo(vila), "aprovado": True})
    assert (await vila.http.post("/propostas", headers=vila.h, json=com_aprovado)).status_code == 422


# --- criar pedido com chave de idempotência (a resposta se perde e a tela reenvia) ---

def _corpo_com_chave(vila, chave, quantidade=10_000):
    return {"fornecedor": "Depósito", "aprovado": True, "chave_idempotencia": chave,
            "linhas": [{"insumo_id": vila.insumo["cimento"], "quantidade": quantidade, "valor_unitario": 3790}]}


async def test_repetir_a_chave_devolve_o_mesmo_pedido(vila):
    url, chave = f"/obras/{vila.obra}/pedidos", uuid.uuid4().hex
    antes = len((await vila.http.get(url, headers=vila.h)).json())
    a = await vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave))
    assert a.status_code == 201, a.text
    b = await vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave))
    assert b.status_code == 200, b.text
    assert (a.json()["id"], a.json()["numero"], a.json()["registro_id"]) == (
        b.json()["id"], b.json()["numero"], b.json()["registro_id"])
    assert len((await vila.http.get(url, headers=vila.h)).json()) == antes + 1
    # o pedido andou (desfazer cancela) e a repetição ainda é a mesma
    assert (await vila.http.post(f"/registro/{a.json()['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    c = await vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave))
    assert (c.status_code, c.json()["status"]) == (200, "cancelado")


async def test_mesma_chave_com_outro_conteudo_e_409(vila):
    url, chave = f"/obras/{vila.obra}/pedidos", uuid.uuid4().hex
    assert (await vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave))).status_code == 201
    r = await vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave, quantidade=20_000))
    assert r.status_code == 409 and "chave" in r.json()["detail"]


async def test_dois_envios_ao_mesmo_tempo_criam_um_pedido(vila):
    url, chave = f"/obras/{vila.obra}/pedidos", uuid.uuid4().hex
    antes = len((await vila.http.get(url, headers=vila.h)).json())
    rs = await asyncio.gather(*(vila.http.post(url, headers=vila.h, json=_corpo_com_chave(vila, chave))
                                for _ in range(2)))
    assert sorted(r.status_code for r in rs) == [200, 201], [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert len((await vila.http.get(url, headers=vila.h)).json()) == antes + 1


async def test_sem_chave_continua_criando_e_chave_invalida_e_422(vila):
    url = f"/obras/{vila.obra}/pedidos"
    corpo = _corpo_com_chave(vila, "x")
    assert (await vila.http.post(url, headers=vila.h, json=corpo)).status_code == 422
    del corpo["chave_idempotencia"]
    a = await vila.http.post(url, headers=vila.h, json=corpo)
    b = await vila.http.post(url, headers=vila.h, json=corpo)
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]


async def test_a_mesma_chave_em_outro_tenant_e_outro_pedido(vila, http, arm, config):
    chave = uuid.uuid4().hex
    a = await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h, json=_corpo_com_chave(vila, chave))
    intruso = await nova_vila(http, arm, config)
    b = await intruso.http.post(f"/obras/{intruso.obra}/pedidos", headers=intruso.h,
                                json=_corpo_com_chave(intruso, chave))
    assert (a.status_code, b.status_code) == (201, 201) and a.json()["id"] != b.json()["id"]
    outra_obra = await intruso.http.post(f"/obras/{vila.obra}/pedidos", headers=intruso.h,
                                         json=_corpo_com_chave(intruso, chave))
    assert outra_obra.status_code == 404
