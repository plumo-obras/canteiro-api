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
                   "transferencia": 0, "falta": 0, "comprar": 0}
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
