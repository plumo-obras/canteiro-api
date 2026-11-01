"""Paginação por `antes_de` (exclusivo) nas listagens por data desc, e status múltiplo nos pedidos.

A fronteira é o caso que importa: itens com o MESMO instante, um em cada lado do corte do `limite`. Com `<` puro o
resto do empate sumiria; com `<=` a página seguinte repetiria o empate para sempre quando ele é do tamanho dela. A
API resolve completando o grupo empatado na própria página.
"""
from datetime import UTC, datetime, timedelta

from canteiro_api.modelos import doc
from canteiro_dominio.modelos import EntradaRegistro, Movimento, Pedido, Proposta, StatusPedido, TipoMovimento

BASE = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def instantes():
    """Cinco instantes, o 2º e o 3º iguais: com limite 2 a fronteira cai bem no meio do par."""
    return [BASE, BASE - timedelta(minutes=1), BASE - timedelta(minutes=1), BASE - timedelta(minutes=2),
            BASE - timedelta(minutes=3)]


async def paginar(vila, url: str, limite: int, campo: str = "criado_em") -> list[str]:
    """Como a web faz: pede, segue com antes_de = o instante do último, para quando vier menos que o limite."""
    vistos: list[str] = []
    antes_de = None
    for _ in range(20):
        params = {"limite": limite, **({"antes_de": antes_de} if antes_de else {})}
        r = await vila.http.get(url, headers=vila.h, params=params)
        assert r.status_code == 200, r.text
        pagina = r.json()
        repetidos = [x["id"] for x in pagina if x["id"] in vistos]
        assert not repetidos, f"página repetiu {repetidos}"
        vistos += [x["id"] for x in pagina]
        if len(pagina) < limite:
            return vistos
        antes_de = pagina[-1][campo]
    raise AssertionError("paginação não terminou")


async def test_registro_pagina_sem_perder_o_empate_na_fronteira(vila):
    ids = []
    for i, t in enumerate(instantes()):
        e = EntradaRegistro(tenant_id=vila.tenant_id, obra_id=vila.obra, quem="Teste", origem="tela", texto=f"r{i}",
                            criado_em=t)
        await vila.arm.criar("registro", doc(e))
        ids.append(e.id)
    vistos = await paginar(vila, f"/registro?obra_id={vila.obra}", 2)
    assert set(ids) <= set(vistos)
    # a ordem continua desc: os criados aqui (todos em janeiro) vêm depois do que o seed gravou agora
    nossos = [x for x in vistos if x in ids]
    assert nossos[0] == ids[0] and nossos[-1] == ids[-1]


async def test_empate_maior_que_a_pagina_vem_inteiro_e_termina(vila):
    ids = []
    for i in range(3):
        e = EntradaRegistro(tenant_id=vila.tenant_id, obra_id=vila.obra, quem="T", origem="tela", texto=f"e{i}",
                            criado_em=BASE)
        await vila.arm.criar("registro", doc(e))
        ids.append(e.id)
    r = await vila.http.get("/registro", headers=vila.h, params={"obra_id": vila.obra, "limite": 2,
                                                                 "antes_de": (BASE + timedelta(seconds=1)).isoformat()})
    assert {x["id"] for x in r.json()} == set(ids)   # passou do limite para não partir o empate
    vistos = await paginar(vila, f"/registro?obra_id={vila.obra}", 2)
    assert set(ids) <= set(vistos)


async def test_antes_de_e_exclusivo_e_recusa_data_sem_fuso(vila):
    e = EntradaRegistro(tenant_id=vila.tenant_id, obra_id=vila.obra, quem="T", origem="tela", texto="x", criado_em=BASE)
    await vila.arm.criar("registro", doc(e))
    r = await vila.http.get("/registro", headers=vila.h, params={"obra_id": vila.obra, "antes_de": BASE.isoformat()})
    assert r.status_code == 200 and e.id not in [x["id"] for x in r.json()]
    depois = (BASE + timedelta(milliseconds=1)).isoformat()
    r = await vila.http.get("/registro", headers=vila.h, params={"obra_id": vila.obra, "antes_de": depois})
    assert r.json()[0]["id"] == e.id
    sem_fuso = await vila.http.get("/registro", headers=vila.h, params={"antes_de": "2026-01-10T12:00:00"})
    assert sem_fuso.status_code == 422 and "fuso" in sem_fuso.json()["detail"]


async def test_propostas_paginam(vila):
    ids = []
    for i, t in enumerate(instantes()):
        p = Proposta(tenant_id=vila.tenant_id, obra_id=vila.obra, tipo="criar_pedido", titulo=f"p{i}", dados={},
                     criado_em=t)
        await vila.arm.criar("propostas", doc(p))
        ids.append(p.id)
    vistos = await paginar(vila, f"/propostas?obra_id={vila.obra}", 2)
    assert set(ids) <= set(vistos)


async def novos_pedidos(vila, status: list[StatusPedido]) -> list[str]:
    ids = []
    for i, (t, st) in enumerate(zip(instantes(), status, strict=True)):
        p = Pedido(tenant_id=vila.tenant_id, numero=f"PED-T{i}", obra_id=vila.obra, insumo_id=vila.insumo["areia"],
                   quantidade=1000, fornecedor="F", valor=100, status=st, criado_em=t)
        await vila.arm.criar("pedidos", doc(p))
        ids.append(p.id)
    return ids


async def test_pedidos_paginam_e_aceitam_status_multiplo(vila):
    sts = [StatusPedido.CONFERIDO, StatusPedido.CANCELADO, StatusPedido.CONFERIDO, StatusPedido.CONFERIDO,
           StatusPedido.CANCELADO]
    ids = await novos_pedidos(vila, sts)
    url = f"/obras/{vila.obra}/pedidos"
    vistos = await paginar(vila, f"{url}?status=conferido&status=cancelado", 2)
    assert set(ids) <= set(vistos)

    r = await vila.http.get(url, headers=vila.h, params=[("status", "em_cotacao"), ("status", "aprovado")])
    assert r.status_code == 200
    assert {p["status"] for p in r.json()} <= {"em_cotacao", "aprovado"} and r.json()

    # compatível com um status só, e status repetido igual não vira "in"
    so_um = await vila.http.get(url, headers=vila.h, params={"status": "cancelado"})
    assert {p["id"] for p in so_um.json()} == {ids[1], ids[4]} | {p["id"] for p in so_um.json()} - set(ids)
    assert all(p["status"] == "cancelado" for p in so_um.json())
    dobrado = await vila.http.get(url, headers=vila.h, params=[("status", "cancelado"), ("status", "cancelado")])
    assert dobrado.json() == so_um.json()
    assert (await vila.http.get(url, headers=vila.h, params={"status": "nao_existe"})).status_code == 422


async def test_movimentos_paginam(vila):
    ids = []
    for t in instantes():
        m = Movimento(tenant_id=vila.tenant_id, obra_id=vila.obra, insumo_id=vila.insumo["areia"],
                      tipo=TipoMovimento.AJUSTE, quantidade=1000, quem="T", criado_em=t)
        await vila.arm.criar("movimentos", doc(m))
        ids.append(m.id)
    vistos = await paginar(vila, f"/obras/{vila.obra}/insumos/{vila.insumo['areia']}/movimentos", 2)
    assert set(ids) <= set(vistos)


async def test_sessoes_paginam_pela_ultima_atividade(vila):
    ids = []
    for i, t in enumerate(instantes()):
        r = await vila.http.post("/sessoes", headers=vila.h, json={"obra_id": vila.obra, "titulo": f"s{i}"})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        await vila.arm.atualizar("sessoes", vila.tenant_id, sid, {"atualizado_em": t})
        ids.append(sid)
    vistos = await paginar(vila, f"/sessoes?obra_id={vila.obra}", 2, campo="atualizado_em")
    assert set(ids) <= set(vistos)
    assert [x for x in vistos if x in ids][0] == ids[0]
