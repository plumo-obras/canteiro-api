"""Resumo de cada obra para a página inicial. Lê o tenant inteiro coleção por coleção e soma aqui: agregação no
banco não é portável para o Firestore, e uma consulta por coleção custa menos que uma por obra."""
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import StatusPedido, StatusProposta

from ..esquemas import ResumoObra
from . import estoque


async def resumo_obras(arm, tenant_id: str) -> list[ResumoObra]:
    obras = await arm.consultar(Consulta("obras", tenant_id, ordem=[("nome", "asc")]))
    if not obras:
        return []
    insumos = await arm.consultar(Consulta("insumos", tenant_id))
    saldos = {s["id"]: s["saldo"] for s in await arm.consultar(Consulta("saldos", tenant_id))}
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("status", "in", estoque.PEDIDOS_ABERTOS)]))
    pendentes = await arm.consultar(Consulta("propostas", tenant_id,
                                             [Filtro("status", "==", StatusProposta.PENDENTE.value)]))

    def por_obra(docs: list[dict]) -> dict[str, list[dict]]:
        g: dict[str, list[dict]] = {}
        for d in docs:
            g.setdefault(d["obra_id"], []).append(d)
        return g

    insumos_de, pedidos_de, pendentes_de = por_obra(insumos), por_obra(pedidos), por_obra(pendentes)
    saida = []
    for o in obras:
        ped = pedidos_de.get(o["id"], [])
        contas = estoque.com_conta(insumos_de.get(o["id"], []), saldos, ped)
        # a última atividade é a entrada mais nova do registro: uma leitura de 1 documento por obra
        ultima = await arm.consultar(Consulta("registro", tenant_id, [Filtro("obra_id", "==", o["id"])],
                                              [("criado_em", "desc")], limite=1))
        saida.append(ResumoObra(
            obra=o,
            pendencias=len(pendentes_de.get(o["id"], [])),
            falta_comprar=sum(1 for c in contas if c["conta"]["comprar"] > 0),
            pedidos_abertos=len(ped),
            valor_pedidos_abertos=sum(p["valor"] for p in ped),
            nf_para_conferir=sum(1 for p in ped if p["status"] == StatusPedido.RECEBIDO.value),
            ultima_atividade=ultima[0]["criado_em"] if ultima else None))
    return saida
