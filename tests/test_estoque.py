"""Almoxarifado pelas rotas: Conta da falta com cotação, movimentos idempotentes, isolamento e papéis."""
import asyncio

from conftest import nova_vila

from canteiro_api.eventos import PublicadorEmMemoria, publicar_pendentes
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.tipos import qtd


async def test_conta_da_casa_vila_nova(vila):
    aco = await vila.conta("aço")
    # 3.940 − 140 − 3.200 a caminho − 600 em cotação = 0: não comprar de novo
    assert aco == {"precisa": qtd(3940), "saldo": qtd(140), "a_caminho": qtd(3200), "cotando": qtd(600),
                   "transferencia": 0, "falta": 0, "comprar": 0, "sobra": 0, "sem_previsao": False, "medido": 0,
                   "texto": "precisa 3.940 kg − saldo 140 kg − a caminho 3.200 kg − em cotação 600 kg − "
                            "transferência 0 kg = falta 0 kg"}
    cimento = await vila.conta("cimento")
    assert (cimento["a_caminho"], cimento["falta"]) == (qtd(120), qtd(34))
    assert (await vila.conta("bloco"))["saldo"] == qtd(2760)
    # areia: PED-0157 recebido (falta conferir) ainda conta como a caminho
    assert (await vila.conta("areia"))["a_caminho"] == qtd(40)


async def test_obras_lista_so_do_tenant(vila, http, arm, config):
    outra = await nova_vila(http, arm, config)
    r = await http.get("/obras", headers=vila.h)
    assert [o["id"] for o in r.json()] == [vila.obra]
    assert outra.obra != vila.obra


async def test_isolamento_entre_tenants(vila, http, arm, config):
    intruso = await nova_vila(http, arm, config)
    h = intruso.h
    assert (await http.get(f"/obras/{vila.obra}/insumos", headers=h)).status_code == 404
    assert (await http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['aço']}/movimentos", headers=h)).status_code == 404
    r = await http.post(f"/obras/{intruso.obra}/movimentos", headers=h, json={
        "insumo_id": vila.insumo["aço"], "tipo": "entrada", "quantidade": 1000, "chave_idempotencia": "invasao-001"})
    assert r.status_code == 404
    assert (await http.post(f"/pedidos/{vila.pedido['PED-0157']}/avancar", headers=h,
                            json={"para": "conferido"})).status_code == 404
    assert (await vila.conta("areia"))["saldo"] == qtd(12)


async def test_movimento_grava_e_atualiza_saldo(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": vila.insumo["bloco"], "tipo": "saida", "quantidade": qtd(60), "destino": "Muro",
        "chave_idempotencia": "saida-bloco-0001"})
    assert r.status_code == 201, r.text
    assert (await vila.conta("bloco"))["saldo"] == qtd(2700)
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['bloco']}/movimentos?limite=2",
                                headers=vila.h)).json()
    assert [m["id"] for m in movs][0] == r.json()["id"]            # mais novo primeiro
    assert len(movs) == 2


async def test_mesma_chave_mesmo_resultado(vila):
    corpo = {"insumo_id": vila.insumo["cimento"], "tipo": "entrada", "quantidade": qtd(10),
             "chave_idempotencia": "entrada-cimento-xyz"}
    a = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json=corpo)
    b = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json=corpo)
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json() == b.json()
    assert (await vila.conta("cimento"))["saldo"] == qtd(56)
    c = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={**corpo, "quantidade": 1})
    assert c.status_code == 409


async def test_movimento_traz_nome_de_quem_e_registro_para_desfazer(vila):
    corpo = {"insumo_id": vila.insumo["cimento"], "tipo": "saida", "quantidade": qtd(4), "destino": "Laje",
             "chave_idempotencia": "saida-cimento-nome"}
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json=corpo)
    m = r.json()
    assert (m["quem"], m["usuario_id"]) == ("Admin da Vila Nova", vila.sessao["usuario"]["id"])
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert m["registro_id"] == reg["id"] and reg["desfazer"]["op"] == "estornar_movimento"
    # a repetição da chave aponta para o mesmo registro (o Desfazer da tela continua valendo)
    assert (await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json=corpo)).json() == m
    assert (await vila.http.post(f"/registro/{m['registro_id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await vila.conta("cimento"))["saldo"] == qtd(46)
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['cimento']}/movimentos",
                                headers=vila.h)).json()
    assert movs[-1]["quem"] == "Joana (mestre)"                  # o seed também mostra um nome, não "seed"


async def test_mesma_chave_em_paralelo_grava_uma_vez(vila):
    corpo = {"insumo_id": vila.insumo["areia"], "tipo": "entrada", "quantidade": qtd(5),
             "chave_idempotencia": "paralelo-areia-1"}
    rs = await asyncio.gather(*(vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json=corpo)
                                for _ in range(4)))
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 201]
    assert (await vila.conta("areia"))["saldo"] == qtd(17)


async def test_saida_curva_a_exige_destino(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": vila.insumo["aço"], "tipo": "saida", "quantidade": qtd(10), "chave_idempotencia": "aco-sem-dest"})
    assert r.status_code == 422
    assert "frente de serviço" in r.json()["detail"]
    assert (await vila.conta("aço"))["saldo"] == qtd(140)


async def test_quantidade_invalida(vila):
    for tipo, q in [("entrada", 0), ("saida", -5), ("ajuste", 0)]:
        r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
            "insumo_id": vila.insumo["areia"], "tipo": tipo, "quantidade": q, "destino": "x",
            "chave_idempotencia": f"invalida-{tipo}-{q}"})
        assert r.status_code == 422, (tipo, q)


async def test_viewer_le_mas_nao_movimenta(vila):
    h, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=h)).status_code == 200
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=h, json={
        "insumo_id": vila.insumo["areia"], "tipo": "entrada", "quantidade": 1000, "chave_idempotencia": "viewer-01"})
    assert r.status_code == 403


async def test_movimento_vai_para_outbox_e_registro(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": vila.insumo["areia"], "tipo": "entrada", "quantidade": qtd(3), "chave_idempotencia": "outbox-01"})
    eventos = await vila.arm.consultar(Consulta("outbox", vila.tenant_id, [Filtro("tipo", "==", "estoque.movimento")]))
    assert [e["dados"]["movimento_id"] for e in eventos] == [r.json()["id"]]
    assert eventos[0]["chave_ordenacao"] == vila.obra and not eventos[0]["publicado"]
    reg = (await vila.http.get(f"/registro?obra_id={vila.obra}", headers=vila.h)).json()
    assert reg[0]["texto"] == "Entrada de 3 m³ de Areia média"
    assert reg[0]["desfazer"] == {"op": "estornar_movimento", "movimento_id": r.json()["id"]}

    pub = PublicadorEmMemoria()
    assert await publicar_pendentes(vila.arm, pub, vila.tenant_id) == 1
    assert pub.publicados[0][0] == "dominio.eventos" and pub.publicados[0][2] == vila.obra
    assert await publicar_pendentes(vila.arm, pub, vila.tenant_id) == 0


async def test_desfazer_movimento_estorna(vila):
    await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h, json={
        "insumo_id": vila.insumo["bloco"], "tipo": "saida", "quantidade": qtd(100), "destino": "Muro",
        "chave_idempotencia": "desfazer-01"})
    entrada = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    r = await vila.http.post(f"/registro/{entrada['id']}/desfazer", headers=vila.h)
    assert r.status_code == 200, r.text
    assert r.json()["texto"].startswith("Estorno: Entrada de 100")
    assert (await vila.conta("bloco"))["saldo"] == qtd(2760)
    assert (await vila.http.post(f"/registro/{entrada['id']}/desfazer", headers=vila.h)).status_code == 409
    # o livro-razão não apaga: o original e o estorno continuam lá
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{vila.insumo['bloco']}/movimentos",
                                headers=vila.h)).json()
    assert movs[0]["desfaz"] == movs[1]["id"]


# --- insumo cadastrado pela tela ---
async def test_cadastrar_insumo_novo_e_lancar_entrada(vila):
    r = await vila.http.post(f"/obras/{vila.obra}/insumos", headers=vila.h,
                             json={"nome": "  Cal  hidratada ", "unidade": "sc"})
    assert r.status_code == 201, r.text
    ins = r.json()
    assert (ins["nome"], ins["unidade"], ins["curva"], ins["precisa_ate_o_fim"]) == ("Cal hidratada", "sc", "C", 0)
    assert ins["registro_id"]
    lista = (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()
    cal = next(i for i in lista if i["insumo"]["id"] == ins["id"])
    assert (cal["conta"]["saldo"], cal["conta"]["falta"]) == (0, 0)       # fora do orçamento: nada a comprar
    m = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h,
                             json={"insumo_id": ins["id"], "tipo": "entrada", "quantidade": 10_000,
                                   "chave_idempotencia": "cal-entrada-1"})
    assert m.status_code == 201, m.text
    reg = (await vila.http.get("/registro", headers=vila.h)).json()
    assert reg[1]["texto"] == "Cadastrou o insumo Cal hidratada (sc, curva C)"


async def test_cadastrar_insumo_com_nome_que_ja_existe(vila):
    # o bloco veio do seed (ID aleatório): a checagem é pelo nome, sem diferenciar maiúsculas e espaços
    nome = (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()[0]["insumo"]["nome"]
    url = f"/obras/{vila.obra}/insumos"
    r = await vila.http.post(url, headers=vila.h, json={"nome": nome.upper(), "unidade": "un"})
    assert r.status_code == 409
    primeira = await vila.http.post(url, headers=vila.h, json={"nome": "Brita 1", "unidade": "m³"})
    assert primeira.status_code == 201
    r2 = await vila.http.post(url, headers=vila.h, json={"nome": "brita  1", "unidade": "m³"})
    assert r2.status_code == 409


async def test_cadastrar_insumo_exige_admin_e_valida(vila):
    h, _ = await vila.usuario("viewer")
    url = f"/obras/{vila.obra}/insumos"
    assert (await vila.http.post(url, headers=h, json={"nome": "X", "unidade": "un"})).status_code == 403
    for ruim in ({"nome": "", "unidade": "un"}, {"nome": "X", "unidade": ""},
                 {"nome": "X", "unidade": "un", "curva": "D"}, {"nome": "X", "unidade": "un", "precisa_ate_o_fim": -1}):
        assert (await vila.http.post(url, headers=vila.h, json=ruim)).status_code == 422


async def _desfazer(vila, registro_id):
    return await vila.http.post(f"/registro/{registro_id}/desfazer", headers=vila.h)


async def _ids_na_lista(vila):
    return {x["insumo"]["id"] for x in (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()}


async def test_desfazer_cadastro_arquiva_refazer_volta_e_o_nome_fica_livre(vila):
    url = f"/obras/{vila.obra}/insumos"
    ins = (await vila.http.post(url, headers=vila.h, json={"nome": "Cal hidratada", "unidade": "sc"})).json()
    desfeito = await _desfazer(vila, ins["registro_id"])
    assert desfeito.status_code == 200, desfeito.text
    assert desfeito.json()["texto"] == "Desfeito: cadastro do insumo Cal hidratada"
    assert ins["id"] not in await _ids_na_lista(vila)
    # arquivado não recebe movimento: para quem lança, ele não existe
    m = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h,
                             json={"insumo_id": ins["id"], "tipo": "entrada", "quantidade": 1000,
                                   "chave_idempotencia": "cal-arquivada-1"})
    assert m.status_code == 404

    refeito = await _desfazer(vila, desfeito.json()["id"])
    assert refeito.status_code == 200, refeito.text
    assert refeito.json()["texto"] == "Refeito: Cadastrou o insumo Cal hidratada (sc, curva C)"
    assert ins["id"] in await _ids_na_lista(vila)

    # desfeito de novo, o mesmo nome (com outra grafia) cadastra outra vez no mesmo documento
    assert (await _desfazer(vila, refeito.json()["id"])).status_code == 200
    de_novo = await vila.http.post(url, headers=vila.h, json={"nome": "CAL  hidratada", "unidade": "kg"})
    assert de_novo.status_code == 201, de_novo.text
    assert de_novo.json()["id"] == ins["id"] and de_novo.json()["unidade"] == "kg"
    conta = next(x for x in (await vila.http.get(url, headers=vila.h)).json() if x["insumo"]["id"] == ins["id"])
    assert conta["insumo"]["arquivado"] is False and conta["conta"]["saldo"] == 0


async def test_desfazer_cadastro_recusa_depois_de_movimento(vila):
    url = f"/obras/{vila.obra}/insumos"
    ins = (await vila.http.post(url, headers=vila.h, json={"nome": "Brita 2", "unidade": "m³"})).json()
    for chave, tipo in (("brita-entrada", "entrada"), ("brita-saida", "saida")):
        r = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h,
                                 json={"insumo_id": ins["id"], "tipo": tipo, "quantidade": 1000,
                                       "chave_idempotencia": chave})
        assert r.status_code == 201, r.text
    # saldo voltou a zero, mas houve movimento: desfazer o cadastro apagaria o livro-razão da tela
    r = await _desfazer(vila, ins["registro_id"])
    assert r.status_code == 409 and "teve movimento" in r.json()["detail"]
    assert ins["id"] in await _ids_na_lista(vila)


async def test_desfazer_cadastro_recusa_com_pedido_aberto(vila):
    """Arquivado, o insumo some para quem lança: o pedido aberto ficaria sem ter onde dar entrada."""
    url = f"/obras/{vila.obra}/insumos"
    ins = (await vila.http.post(url, headers=vila.h, json={"nome": "Tela soldada", "unidade": "m²"})).json()
    p = await vila.http.post(f"/obras/{vila.obra}/pedidos", headers=vila.h,
                             json={"insumo_id": ins["id"], "quantidade": 10_000, "fornecedor": "Gerdau",
                                   "valor": 50_000})
    assert p.status_code == 201, p.text
    r = await _desfazer(vila, ins["registro_id"])
    assert r.status_code == 409 and p.json()["numero"] in r.json()["detail"]
    assert ins["id"] in await _ids_na_lista(vila)
