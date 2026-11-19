"""Compras, pedidos e estoque saíram: as rotas respondem 410 com a frase, e o que sobrou no banco não quebra nada."""
from datetime import UTC, datetime

import pytest
from test_cronograma import _obra

from canteiro_api.nucleo import COMPRAS_SAIRAM
from canteiro_api.rotas.retiradas import RETIRADAS


@pytest.mark.parametrize(("metodo", "caminho"), RETIRADAS)
async def test_rotas_de_compra_respondem_410(vila, metodo, caminho):
    url = caminho.format(obra_id=vila.obra, insumo_id="i1", pedido_id="p1")
    r = await vila.http.request(metodo, url, headers=vila.h, json={})
    assert (r.status_code, r.json()["detail"]) == (410, COMPRAS_SAIRAM)


async def test_proposta_de_compra_nova_e_410(vila):
    for tipo in ("criar_pedido", "avancar_pedido", "ajuste_estoque", "registrar_entrega"):
        r = await vila.http.post("/propostas", headers=vila.h, json={
            "obra_id": vila.obra, "tipo": tipo, "titulo": "Comprar cimento", "dados": {}})
        assert r.status_code == 410, (tipo, r.text)


async def _gravar(vila, colecao: str, **campos) -> dict:
    d = {"tenant_id": vila.tenant_id, "criado_em": datetime(2026, 11, 1, tzinfo=UTC), **campos}
    await vila.arm.criar(colecao, d)
    return d


async def test_proposta_de_compra_que_ficou_pendente_so_se_recusa(vila):
    p = await _gravar(vila, "propostas", id="p-compra", obra_id=vila.obra, tipo="criar_pedido", titulo="Comprar",
                      dados={"insumo_id": "i1"}, status="pendente", sugerido_por="agente", efeito="compra")
    assert any(x["id"] == p["id"] for x in (await vila.http.get("/propostas", headers=vila.h)).json())
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert (r.status_code, r.json()["detail"]) == (410, COMPRAS_SAIRAM)
    r = await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={"motivo": "Compras saiu"})
    assert r.json()["status"] == "recusada"


async def test_desfazer_de_compra_que_ficou_no_historico_e_410(vila):
    e = await _gravar(vila, "registro", id="r-ped", obra_id=vila.obra, quem="Admin", origem="tela",
                      texto="PED-0001 aprovado", desfazer={"op": "voltar_pedido", "pedido_id": "p1"}, desfeito=False)
    r = await vila.http.post(f"/registro/{e['id']}/desfazer", headers=vila.h)
    assert (r.status_code, r.json()["detail"]) == (410, COMPRAS_SAIRAM)


async def test_pagamento_de_pedido_de_antes_conta_como_gasto(vila):
    """O gasto que era pagamento de pedido (`pedido_id`) passa a contar no orçamento, como a limpeza do banco faz."""
    obra = await _obra(vila, com_cronograma=False)
    await _gravar(vila, "gastos", id="g-ped", obra_id=obra, valor=50_000, data_pagamento="2026-10-10",
                  favorecido="Depósito", descricao="cimento", etapa="1", pedido_id="p1", estornado=False)
    orc = (await vila.http.get(f"/obras/{obra}/orcamento", headers=vila.h)).json()
    assert next(e for e in orc["etapas"] if e["etapa"] == "1")["gasto"] == 50_000
    assert orc["totais"]["gasto"] == 50_000
    g = (await vila.http.get(f"/obras/{obra}/gastos", headers=vila.h)).json()
    assert [x["id"] for x in g] == ["g-ped"] and "pedido_id" not in g[0]
