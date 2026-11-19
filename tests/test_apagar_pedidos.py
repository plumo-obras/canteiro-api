"""A limpeza do que sobrou de compras: simula sem gravar, copia antes de mudar e só mexe no que é de compra."""
import json
from datetime import UTC, datetime

import pytest

from canteiro_api import apagar_pedidos
from canteiro_api.armazenamento import ArmazenamentoMemoria


def _doc(tenant: str, id_: str, **campos) -> dict:
    return {"id": id_, "tenant_id": tenant, "criado_em": datetime(2026, 11, 1, tzinfo=UTC), **campos}


async def _semear(arm, t: str) -> None:
    await arm.criar("pedidos", _doc(t, f"{t}-ped", obra_id="o1", numero="PED-0001", valor=50_000, status="conferido"))
    await arm.criar("recebimentos", _doc(t, f"{t}-rec", obra_id="o1", pedido_id=f"{t}-ped", numero=1))
    await arm.criar("insumos", _doc(t, f"{t}-ins", obra_id="o1", nome="Cimento", unidade="sc"))
    await arm.criar("movimentos", _doc(t, f"{t}-mov", obra_id="o1", insumo_id=f"{t}-ins", tipo="entrada",
                                       quantidade=10_000))
    await arm.criar("saldos", _doc(t, f"{t}-ins", obra_id="o1", saldo=10_000))
    await arm.criar("contadores", {"id": f"contador-pedidos-{t}", "tenant_id": t, "ultimo": 1})
    await arm.criar("gastos", _doc(t, f"{t}-g-ped", obra_id="o1", valor=50_000, pedido_id=f"{t}-ped", etapa="1"))
    await arm.criar("gastos", _doc(t, f"{t}-g", obra_id="o1", valor=7_000, pedido_id=None, etapa="2"))
    await arm.criar("propostas", _doc(t, f"{t}-p-compra", obra_id="o1", tipo="criar_pedido", status="pendente"))
    await arm.criar("propostas", _doc(t, f"{t}-p-crono", obra_id="o1", tipo="cronograma", status="pendente"))
    await arm.criar("propostas_pendentes", {"id": f"{t}-trava", "tenant_id": t, "proposta_id": f"{t}-p-compra"})
    await arm.criar("registro", _doc(t, f"{t}-r-ped", obra_id="o1", texto="Pedido PED-0001 aprovado",
                                     desfazer={"op": "voltar_pedido", "pedido_id": f"{t}-ped"}, desfeito=False))
    await arm.criar("registro", _doc(t, f"{t}-r-adit", obra_id="o1", texto="1º aditivo", desfeito=False,
                                     desfazer={"op": "desligar_versao", "versao_id": "v2", "compras": {"x": 1}}))
    await arm.criar("registro", _doc(t, f"{t}-r-gasto", obra_id="o1", texto="Gasto", desfeito=False,
                                     desfazer={"op": "estornar_gasto", "gasto_id": f"{t}-g"}))
    await arm.criar("registro", _doc(t, f"{t}-r-velho", obra_id="o1", texto="Entrada desfeita", desfeito=True,
                                     desfazer={"op": "estornar_movimento", "movimento_id": f"{t}-mov"}))


async def test_simular_conta_e_nao_grava(arm, tenant, tmp_path):
    await _semear(arm, tenant)
    rel = await apagar_pedidos.limpar(arm, gravar=False, tenants=[tenant])
    assert rel.clientes == 1
    assert rel.apagados == {"pedidos": 1, "recebimentos": 1, "insumos": 1, "movimentos": 1, "saldos": 1,
                            "contadores": 1}
    assert (rel.gastos_convertidos, rel.propostas_recusadas, rel.desfazer_tirados) == (1, 1, 2)
    assert rel.copia is None
    assert await arm.obter("pedidos", tenant, f"{tenant}-ped")
    assert (await arm.obter("gastos", tenant, f"{tenant}-g-ped"))["pedido_id"] == f"{tenant}-ped"
    assert list(tmp_path.iterdir()) == []


async def test_aplicar_copia_antes_e_limpa_so_o_de_compra(arm, tenant, tmp_path):
    await _semear(arm, tenant)
    avisos = []
    rel = await apagar_pedidos.limpar(arm, gravar=True, pasta_copia=tmp_path, tenants=[tenant], avisar=avisos.append)
    assert avisos == [f"ok {tenant}"]

    copia = json.loads((tmp_path / rel.copia.rsplit("/", 1)[1]).read_text())[tenant]
    assert [d["id"] for d in copia["pedidos"]] == [f"{tenant}-ped"]
    assert copia["gastos"][0]["pedido_id"] == f"{tenant}-ped"           # como estava, antes de mudar
    assert copia["registro"][0]["desfazer"]["op"] in ("voltar_pedido", "desligar_versao")
    assert sorted(copia) == ["contadores", "gastos", "insumos", "movimentos", "pedidos", "propostas",
                             "propostas_pendentes", "recebimentos", "registro", "saldos"]

    for colecao in apagar_pedidos.APAGADAS:
        assert await arm.consultar(apagar_pedidos.Consulta(colecao, tenant)) == []
    assert await arm.obter("contadores", tenant, f"contador-pedidos-{tenant}") is None
    assert (await arm.obter("gastos", tenant, f"{tenant}-g-ped"))["pedido_id"] is None
    assert (await arm.obter("gastos", tenant, f"{tenant}-g"))["valor"] == 7_000
    compra = await arm.obter("propostas", tenant, f"{tenant}-p-compra")
    assert (compra["status"], compra["motivo_recusa"]) == ("recusada", apagar_pedidos.MOTIVO)
    assert (await arm.obter("propostas", tenant, f"{tenant}-p-crono"))["status"] == "pendente"
    assert (await arm.obter("propostas_pendentes", tenant, f"{tenant}-trava"))["proposta_id"] is None
    assert (await arm.obter("registro", tenant, f"{tenant}-r-ped"))["desfazer"] is None
    adit = await arm.obter("registro", tenant, f"{tenant}-r-adit")
    assert adit["desfazer"] == {"op": "desligar_versao", "versao_id": "v2"}
    assert (await arm.obter("registro", tenant, f"{tenant}-r-gasto"))["desfazer"]["op"] == "estornar_gasto"
    # a desfeita não se desfaz de novo: fica como está
    assert (await arm.obter("registro", tenant, f"{tenant}-r-velho"))["desfazer"]["op"] == "estornar_movimento"

    # rodar de novo não acha mais nada nem grava outra cópia
    de_novo = await apagar_pedidos.limpar(arm, gravar=True, pasta_copia=tmp_path, tenants=[tenant])
    assert (de_novo.clientes, de_novo.copia) == (0, None)
    assert len(list(tmp_path.iterdir())) == 1


async def test_acha_os_clientes_sozinho_e_nao_toca_quem_nao_tem_compra(tmp_path):
    arm = ArmazenamentoMemoria()
    await _semear(arm, "t-a")
    await arm.criar("gastos", _doc("t-b", "t-b-g", obra_id="o9", valor=1_000, pedido_id=None, etapa="1"))
    rel = await apagar_pedidos.limpar(arm, gravar=True, pasta_copia=tmp_path)
    assert rel.clientes == 1
    assert (await arm.obter("gastos", "t-b", "t-b-g"))["valor"] == 1_000
    assert await arm.obter("pedidos", "t-a", "t-a-ped") is None


async def test_aplicar_exige_pasta_da_copia():
    with pytest.raises(ValueError):
        await apagar_pedidos.limpar(ArmazenamentoMemoria(), gravar=True)
