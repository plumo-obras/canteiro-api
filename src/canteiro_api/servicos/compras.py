"""Aprovar o orçamento gera Compras: cada item vigente vira um insumo da obra, com a quantidade e o preço orçados.

Um orçamento de 2.000 linhas não cabe numa transação, então os insumos entram em lotes antes e só a última transação
vira o status e grava o registro. O ID de cada insumo sai do nome e o da aprovação sai das versões, de modo que repetir
depois de uma queda no meio continua de onde parou. O rastro para o Desfazer fica em cada insumo (`aprovacao`), e não
na receita: a lista inteira não caberia num documento.
"""
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import DESFAZENDO_APROVACAO, Insumo, Saldo, StatusPedido, TipoVersao, agora
from canteiro_dominio.regras import RegraViolada, chave_do_nome, insumo_ao_aprovar, itens_para_compras

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..modelos import doc
from ..nucleo import Conflito, executar_e_registrar, obra_do_tenant, recusar_se_desfazendo
from . import obras, orcamento, versoes
from .estoque import ativos, gravar_insumo_novo, id_derivado, id_insumo, mudou_desde_a_aprovacao

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
                # sobra de uma tentativa abandonada: a base é o que ele era antes dela, para o Desfazer voltar até lá
                a = lido.get("aprovacao")
                sobra = a is not None and a["antes"] is not None and not mudou_desde_a_aprovacao(lido)
                base = {**lido, **a["antes"]} if sobra else lido
                depois = insumo_ao_aprovar(base, c)
                tx.atualizar("insumos", u.tenant_id, x["id"], {**depois, "aprovacao": {
                    "id": aprovacao_id, "antes": {k: base.get(k) for k in depois}, "depois": depois}})
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


def _limpar_lote(u: UsuarioAtual, lote: list[dict]):
    async def corpo(tx):
        lidos = [await tx.obter("insumos", u.tenant_id, x["id"]) for x in lote]
        for i in lidos:
            a = i.get("aprovacao")
            if a is None or i.get("arquivado"):
                continue
            if a["antes"] is None:
                tx.atualizar("insumos", u.tenant_id, i["id"], {"arquivado": True})
            else:
                volta = {} if mudou_desde_a_aprovacao(i) else a["antes"]
                tx.atualizar("insumos", u.tenant_id, i["id"], {**volta, "aprovacao": None})
    return corpo


async def _limpar_abandonados(arm, u: UsuarioAtual, obra_id: str, aprovacao_id: str | None, plano: list[dict]) -> None:
    """O que uma tentativa abandonada gravou e esta aprovação não vai regravar (a pessoa desistiu e reimportou). Sem
    isto, assim que a obra saísse de `em_orcamento` essas sobras apareceriam em Compras."""
    no_plano = {x["id"] for x in plano}
    insumos = ativos(await arm.consultar(Consulta("insumos", u.tenant_id, [Filtro("obra_id", "==", obra_id)])))
    sobras = [i for i in insumos
              if i.get("aprovacao") and i["aprovacao"]["id"] != aprovacao_id and i["id"] not in no_plano]
    for k in range(0, len(sobras), INSUMOS_POR_LOTE):
        await arm.em_transacao(_limpar_lote(u, sobras[k:k + INSUMOS_POR_LOTE]))


def _id_aprovacao(tenant_id: str, obra_id: str, vigentes: list[dict]) -> str:
    return id_derivado(tenant_id, "aprovacao", obra_id,
                       *sorted(f"{v['id']}:{v.get('edicoes', 0)}" for v in vigentes))


async def aprovar(arm, u: UsuarioAtual, obra_id: str) -> dict:
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    recusar_se_desfazendo(o)
    if o["status"] != "em_orcamento":
        raise Conflito("O orçamento já foi aprovado.")
    insumos = await obras.insumos_da_obra(arm, u.tenant_id, obra_id)
    vigentes = [v for v in await versoes.versoes_da_obra(arm, u.tenant_id, obra_id) if v["ativa"]]
    aprovacao_id, plano = None, []
    if any(v["tipo"] == TipoVersao.CONTRATO for v in vigentes):
        itens = await versoes.itens_das_versoes(arm, u.tenant_id, obra_id, vigentes)
        plano = _plano(u, obra_id, itens, vigentes, insumos)
        aprovacao_id = _id_aprovacao(u.tenant_id, obra_id, vigentes)
    await _limpar_abandonados(arm, u, obra_id, aprovacao_id, plano)
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
#
# Também em lotes, e também repetível: a 1ª rodada marca a obra (`desfazendo_aprovacao`), e o registro só fica desfeito
# na transação final, que devolve a obra para `em_orcamento`. Se parar no meio, por queda ou por 409, o insumo já
# revertido sai da conta (arquivado, ou sem o rastro da aprovação) e rodar o Desfazer de novo continua dali.

def _intacto(i: dict, saldo: dict | None, com_movimento: set[str]) -> bool:
    """Saldo no instante do cadastro: nada se mexeu. Se o instante mudou, ainda vale quando todo movimento foi
    estornado: é assim que a pessoa resolve o que travou o Desfazer. Entrada e saída que se anulam sem estorno não
    contam: o material passou pela obra."""
    if saldo is None:
        return True
    return saldo["saldo"] == 0 and (normalizar(saldo["atualizado_em"]) == normalizar(i["criado_em"])
                                    or i["id"] not in com_movimento)


def _reverter_lote(u: UsuarioAtual, lote: list[dict], vistos: dict[str, object]):
    """`vistos` = o `atualizado_em` de cada saldo na conferência: se mudou, entrou movimento depois dela."""
    async def corpo(tx):
        lidos = [(await tx.obter("insumos", u.tenant_id, x["id"]), await tx.obter("saldos", u.tenant_id, x["id"]))
                 for x in lote]
        for i, saldo in lidos:
            novo = i["aprovacao"]["antes"] is None
            mexeu = saldo is not None and (saldo["saldo"] != 0
                                           or normalizar(saldo["atualizado_em"]) != vistos.get(i["id"]))
            if mudou_desde_a_aprovacao(i) or (novo and mexeu):
                raise Conflito(f"{i['nome']} teve movimento ou mudou desde então; resolva e desfaça de novo.")
            if novo:
                tx.atualizar("insumos", u.tenant_id, i["id"], {"arquivado": True})
                if saldo is not None:
                    # o saldo foi lido para decidir: a transação grava nele (regra da porta)
                    tx.atualizar("saldos", u.tenant_id, i["id"], {"atualizado_em": agora()})
            else:
                tx.atualizar("insumos", u.tenant_id, i["id"], {**i["aprovacao"]["antes"], "aprovacao": None})
    return corpo


async def _conferir(arm, u: UsuarioAtual, obra_id: str, aprovacao_id: str) -> tuple[list[dict], dict]:
    """O que falta reverter e o `atualizado_em` de cada saldo, depois de conferir que tudo pode ser revertido."""
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
    com_movimento: set[str] = set()
    if any(s["id"] in novos and normalizar(s["atualizado_em"]) != normalizar(novos[s["id"]]["criado_em"])
           for s in saldos.values()):
        movimentos = await arm.consultar(Consulta("movimentos", u.tenant_id, [Filtro("obra_id", "==", obra_id)]))
        estornados = {m["desfaz"] for m in movimentos if m.get("desfaz")}
        com_movimento = {m["insumo_id"] for m in movimentos if not m.get("desfaz") and m["id"] not in estornados}
    for i in gerados:
        if mudou_desde_a_aprovacao(i):
            raise Conflito(f"{i['nome']} foi editado depois da aprovação; não dá para desfazer.")
        if i["id"] in novos and not _intacto(i, saldos.get(i["id"]), com_movimento):
            raise Conflito(f"{i['nome']} teve movimento depois da aprovação; estorne-o antes de desfazer a aprovação.")
    return gerados, {k: normalizar(s["atualizado_em"]) for k, s in saldos.items()}


def _marcar(u: UsuarioAtual, obra_id: str, entrada_id: str):
    async def corpo(tx):
        o = await tx.obter("obras", u.tenant_id, obra_id)
        if o["status"] not in ("em_andamento", DESFAZENDO_APROVACAO):
            raise Conflito("A obra já mudou de situação desde então; não dá para desfazer.")
        tx.atualizar("obras", u.tenant_id, obra_id,
                     {"status": DESFAZENDO_APROVACAO, "desfazendo_registro_id": entrada_id})
    return corpo


async def desfazer_aprovacao(arm, u: UsuarioAtual, entrada_id: str, receita: dict) -> None:
    """Fora da transação do Desfazer, porque consulta e pode não caber numa só. Quem devolve a obra para `em_orcamento`
    é a transação do Desfazer, por último."""
    obra_id, aprovacao_id = receita["obra_id"], receita["aprovacao_id"]
    o = await arm.obter("obras", u.tenant_id, obra_id)
    if not o or o["status"] not in ("em_andamento", DESFAZENDO_APROVACAO):
        return                         # a transação do Desfazer dá a resposta certa para isso
    # a 1ª conferência é antes de marcar: se já há o que trave, a obra continua aprovada, sem nada pela metade
    await _conferir(arm, u, obra_id, aprovacao_id)
    await arm.em_transacao(_marcar(u, obra_id, entrada_id))
    # a de agora pega o que foi gravado antes de a marca valer; daqui em diante nada novo entra em Compras
    gerados, vistos = await _conferir(arm, u, obra_id, aprovacao_id)
    for k in range(0, len(gerados), INSUMOS_POR_LOTE):
        await arm.em_transacao(_reverter_lote(u, gerados[k:k + INSUMOS_POR_LOTE], vistos))
