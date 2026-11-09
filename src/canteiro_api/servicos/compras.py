"""Aprovar o orçamento gera Compras: cada item vigente vira um insumo da obra, com a quantidade e o preço orçados.

Um orçamento de 2.000 linhas não cabe numa transação, então os insumos entram em lotes antes e só a última transação
vira o status e grava o registro. O ID de cada insumo sai do nome e o da aprovação sai das versões, de modo que repetir
depois de uma queda no meio continua de onde parou. O rastro para o Desfazer fica em cada insumo (`aprovacao`), e não
na receita: a lista inteira não caberia num documento.
"""
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Insumo, Saldo, StatusPedido, TipoVersao, agora
from canteiro_dominio.regras import RegraViolada, chave_do_nome, insumo_ao_aprovar, itens_para_compras

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..modelos import doc
from ..nucleo import Conflito, executar_e_registrar, obra_do_tenant
from . import obras, orcamento, versoes
from .estoque import gravar_insumo_novo, id_derivado, id_insumo

# Até 2 escritas por insumo (ele e o saldo): folgado nas 500 da transação.
INSUMOS_POR_LOTE = 200


def _plano(u: UsuarioAtual, obra_id: str, itens: list[dict], vigentes: list[dict], insumos: list[dict]) -> list[dict]:
    do_projeto = {v["id"] for v in vigentes if (v.get("arquivo") or "").endswith(versoes.SUFIXO_PROJETO)}
    # item que veio das pranchas é material; planilha de orçamento é de serviços
    material = {chave_do_nome(x["descricao"]) for x in itens if x["tipo"] == "item" and x["versao_id"] in do_projeto}
    existentes = {chave_do_nome(i["nome"]): i for i in insumos}
    compras = [c for c in itens_para_compras(itens) if c.quantidade > 0]
    divergentes = []
    for c in compras:
        atual = existentes.get(chave_do_nome(c.nome))
        if atual and atual["unidade"].strip().casefold() != c.unidade.strip().casefold():
            divergentes.append(f"{c.nome} ({c.unidade} no orçamento, {atual['unidade']} em Compras)")
    if divergentes:
        # não dá para somar kg com m³, e dois insumos de mesmo nome não existem: quem decide a unidade é a pessoa
        raise RegraViolada(f"Unidade diferente da que já está cadastrada: {'; '.join(divergentes)}. Corrija e aprove "
                           "de novo.")
    pesos = [{"chave": chave_do_nome(c.nome), "total": c.total, "curva": None} for c in compras]
    orcamento._curva_abc(pesos)
    curvas = {x["chave"]: x["curva"] for x in pesos}
    plano = []
    for c in compras:
        chave = chave_do_nome(c.nome)
        atual = existentes.get(chave)
        plano.append({"compra": c, "existe": atual is not None, "curva": curvas[chave],
                      "tipo": "material" if chave in material else "servico",
                      "id": atual["id"] if atual else id_insumo(u.tenant_id, obra_id, c.nome)})
    return plano


def _gravar_lote(u: UsuarioAtual, obra_id: str, aprovacao_id: str, lote: list[dict]):
    async def corpo(tx):
        lidos = [await tx.obter("insumos", u.tenant_id, x["id"]) for x in lote]
        for x, lido in zip(lote, lidos, strict=True):
            c = x["compra"]
            if lido and not lido.get("arquivado") and (lido.get("aprovacao") or {}).get("id") == aprovacao_id:
                continue               # repetição depois de uma queda: este já entrou
            if x["existe"]:
                if not lido or lido.get("arquivado"):
                    raise Conflito(f"'{c.nome}' mudou agora há pouco; aprove de novo.")
                depois = insumo_ao_aprovar(lido, c)
                tx.atualizar("insumos", u.tenant_id, x["id"], {**depois, "aprovacao": {
                    "id": aprovacao_id, "antes": {k: lido.get(k) for k in depois}, "depois": depois}})
                continue
            depois = {"precisa_ate_o_fim": c.quantidade, "valor_unitario": c.valor_unitario, "etapa": c.etapa}
            d = doc(Insumo(id=x["id"], tenant_id=u.tenant_id, obra_id=obra_id, nome=c.nome, unidade=c.unidade,
                           tipo=x["tipo"], curva=x["curva"], **depois,
                           aprovacao={"id": aprovacao_id, "antes": None, "depois": depois}))
            gravar_insumo_novo(tx, d, lido)
            # o saldo nasce com o instante do cadastro: é por ele que o Desfazer sabe que nada se mexeu desde então
            tx.salvar("saldos", doc(Saldo(id=x["id"], tenant_id=u.tenant_id, obra_id=obra_id, saldo=0,
                                          atualizado_em=d["criado_em"])))
    return corpo


def _id_aprovacao(tenant_id: str, obra_id: str, vigentes: list[dict]) -> str:
    return id_derivado(tenant_id, "aprovacao", obra_id,
                       *sorted(f"{v['id']}:{v.get('edicoes', 0)}" for v in vigentes))


async def aprovar(arm, u: UsuarioAtual, obra_id: str) -> dict:
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    if o["status"] != "em_orcamento":
        raise Conflito("O orçamento já foi aprovado.")
    insumos = await obras.insumos_da_obra(arm, u.tenant_id, obra_id)
    vigentes = [v for v in await versoes.versoes_da_obra(arm, u.tenant_id, obra_id) if v["ativa"]]
    aprovacao_id, plano = None, []
    if any(v["tipo"] == TipoVersao.CONTRATO for v in vigentes):
        itens = await versoes.itens_das_versoes(arm, u.tenant_id, obra_id, vigentes)
        plano = _plano(u, obra_id, itens, vigentes, insumos)
        aprovacao_id = _id_aprovacao(u.tenant_id, obra_id, vigentes)
        for i in range(0, len(plano), INSUMOS_POR_LOTE):
            await arm.em_transacao(_gravar_lote(u, obra_id, aprovacao_id, plano[i:i + INSUMOS_POR_LOTE]))

    async def tx_corpo(tx):
        op = await obras.preparar_aprovar_orcamento(tx, u, obra_id, insumos, vigentes, aprovacao_id)
        obra, entrada = executar_e_registrar(tx, u, op)
        return {**obra, "registro_id": entrada["id"]}

    aprovada = await arm.em_transacao(tx_corpo)
    atualizados = sum(x["existe"] for x in plano)
    return {**aprovada, "insumos_criados": len(plano) - atualizados, "insumos_atualizados": atualizados}


# --- desfazer ---

def _intacto(i: dict, saldo: dict | None) -> bool:
    """Saldo zerado e no instante do cadastro: qualquer movimento troca o `atualizado_em`, mesmo entrada e saída que se
    anulam."""
    return saldo is None or (saldo["saldo"] == 0 and normalizar(saldo["atualizado_em"]) == normalizar(i["criado_em"]))


def _mudou(i: dict) -> bool:
    return any(i.get(k) != x for k, x in i["aprovacao"]["depois"].items())


def _reverter_lote(u: UsuarioAtual, lote: list[dict]):
    async def corpo(tx):
        lidos = [(await tx.obter("insumos", u.tenant_id, x["id"]), await tx.obter("saldos", u.tenant_id, x["id"]))
                 for x in lote]
        for i, saldo in lidos:
            if _mudou(i) or (i["aprovacao"]["antes"] is None and not _intacto(i, saldo)):
                raise Conflito(f"{i['nome']} teve movimento ou mudou desde então; não dá para desfazer.")
            if i["aprovacao"]["antes"] is None:
                tx.atualizar("insumos", u.tenant_id, i["id"], {"arquivado": True})
                if saldo is not None:
                    # o saldo foi lido para decidir: a transação grava nele (regra da porta)
                    tx.atualizar("saldos", u.tenant_id, i["id"], {"atualizado_em": agora()})
            else:
                tx.atualizar("insumos", u.tenant_id, i["id"], {**i["aprovacao"]["antes"], "aprovacao": None})
    return corpo


async def desfazer_aprovacao(arm, u: UsuarioAtual, receita: dict) -> None:
    """Fora da transação do Desfazer, porque consulta e pode não caber numa só: confere tudo antes de mexer em qualquer
    insumo, e só então reverte em lotes. Quem vira o status de volta é a transação do Desfazer, por último.

    Sobra a janela de alguém dar entrada num insumo entre a conferência e o lote dele: o lote relê cada insumo para não
    apagar o movimento e o Desfazer para com 409, com os lotes anteriores já fora de Compras."""
    obra_id, aprovacao_id = receita["obra_id"], receita["aprovacao_id"]
    o = await arm.obter("obras", u.tenant_id, obra_id)
    if not o or o["status"] != "em_andamento":
        return                         # a transação do Desfazer dá a resposta certa para isso
    if any(v["ativa"] and v.get("gerou_compras") for v in await versoes.versoes_da_obra(arm, u.tenant_id, obra_id)):
        raise Conflito("Há aditivo lançado depois da aprovação; desfaça o aditivo primeiro.")
    gerados = [i for i in await obras.insumos_da_obra(arm, u.tenant_id, obra_id)
               if (i.get("aprovacao") or {}).get("id") == aprovacao_id]
    novos = {i["id"]: i for i in gerados if i["aprovacao"]["antes"] is None}
    pedidos = await arm.consultar(Consulta("pedidos", u.tenant_id, [Filtro("obra_id", "==", obra_id)]))
    for p in pedidos:
        if p["status"] != StatusPedido.CANCELADO.value:
            for x in p.get("linhas", []):
                if x["insumo_id"] in novos:
                    raise Conflito(f"{novos[x['insumo_id']]['nome']} já tem pedido ({p['numero']}); cancele o pedido "
                                   "antes de desfazer a aprovação.")
    saldos = {s["id"]: s for s in await arm.consultar(Consulta("saldos", u.tenant_id,
                                                               [Filtro("obra_id", "==", obra_id)]))}
    for i in gerados:
        if _mudou(i):
            raise Conflito(f"{i['nome']} foi editado depois da aprovação; não dá para desfazer.")
        if i["id"] in novos and not _intacto(i, saldos.get(i["id"])):
            raise Conflito(f"{i['nome']} teve movimento depois da aprovação; não dá para desfazer.")
    for k in range(0, len(gerados), INSUMOS_POR_LOTE):
        await arm.em_transacao(_reverter_lote(u, gerados[k:k + INSUMOS_POR_LOTE]))

