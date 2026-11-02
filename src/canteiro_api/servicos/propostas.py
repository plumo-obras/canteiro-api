"""Propostas (o agente só propõe), aprovação que executa e o desfazer a partir do registro."""
from pydantic import ValidationError

from canteiro_dominio.modelos import Proposta, StatusPedido, StatusProposta, TipoMovimento, TipoProposta, agora
from canteiro_dominio.regras import validar_conferencia, validar_transicao
from canteiro_dominio.tipos import fmt_qtd, fmt_reais

from ..auth import UsuarioAtual
from ..esquemas import (
    AJUSTAVEIS,
    DADOS_POR_TIPO,
    AjusteEstoqueDados,
    AvancarPedidoDados,
    PedidoDados,
    PropostaCorpo,
)
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    insumo_da_obra,
    obra_do_tenant,
    registrar,
)
from . import cronograma, fornecedores, obras, pedidos, versoes
from .estoque import id_derivado, linhas_de, preparar_estorno, preparar_movimento
from .pedidos import preparar_avancar, preparar_cancelar_criado, preparar_criar_pedido, preparar_voltar


def _dados(tipo: TipoProposta, dados: dict):
    try:
        return DADOS_POR_TIPO[tipo].model_validate(dados)
    except ValidationError as e:
        campos = ", ".join(".".join(map(str, err["loc"])) or "dados" for err in e.errors())
        raise ErroNegocio(f"Dados da proposta '{tipo.value}' inválidos ({campos}).") from None


def _regras_dos_dados(dados) -> None:
    """Regras que não dependem do banco: valem ao criar e de novo depois de um ajuste na aprovação."""
    if isinstance(dados, AjusteEstoqueDados) and dados.quantidade == 0:
        raise ErroNegocio("Ajuste de zero não muda nada.")
    if isinstance(dados, AvancarPedidoDados):
        validar_conferencia(dados.para, quantidade_recebida=dados.quantidade_recebida, nf=dados.nf)


async def criar_proposta(arm, u: UsuarioAtual, corpo: PropostaCorpo) -> dict:
    """Só valida e guarda. Conferir as referências agora evita cartão de decisão que nunca poderia ser aprovado."""
    dados = _dados(corpo.tipo, corpo.dados)
    _regras_dos_dados(dados)
    await obra_do_tenant(arm, u.tenant_id, corpo.obra_id)
    match dados:
        case PedidoDados():
            for x in dados.linhas:
                await insumo_da_obra(arm, u.tenant_id, corpo.obra_id, x.insumo_id)
            await versoes.validar_etapa(arm, u.tenant_id, corpo.obra_id, [x.etapa for x in dados.linhas],
                                        exigir=False)
        case AjusteEstoqueDados():
            await insumo_da_obra(arm, u.tenant_id, corpo.obra_id, dados.insumo_id)
        case AvancarPedidoDados():
            p = await arm.obter("pedidos", u.tenant_id, dados.pedido_id)
            if not p or p["obra_id"] != corpo.obra_id:
                raise NaoEncontrado("Pedido não encontrado nesta obra.")
            validar_transicao(StatusPedido(p["status"]), dados.para)
    p = Proposta(tenant_id=u.tenant_id, obra_id=corpo.obra_id, tipo=corpo.tipo, titulo=corpo.titulo,
                 conta=corpo.conta, justificativa=corpo.justificativa, fonte=corpo.fonte,
                 dados=_guardado(corpo.tipo, dados.model_dump(mode="json")))
    d = doc(p)

    async def corpo_tx(tx):
        tx.criar("propostas", d)
        evento(tx, u.tenant_id, "proposta.criada", corpo.obra_id, {"proposta_id": p.id, "tipo": p.tipo.value})

    await arm.em_transacao(corpo_tx)
    return d


async def _preparar_proposta(tx, u: UsuarioAtual, prop: dict) -> Operacao:
    tipo = TipoProposta(prop["tipo"])
    dados = _dados(tipo, prop["dados"])
    match tipo:
        case TipoProposta.CRIAR_PEDIDO:
            # aprovar a proposta É a aprovação da compra: o pedido nasce aprovado, sem pedir um segundo "sim"
            return await preparar_criar_pedido(tx, u, prop["obra_id"], dados, status=StatusPedido.APROVADO)
        case TipoProposta.AVANCAR_PEDIDO:
            op = await preparar_avancar(tx, u, dados.pedido_id, dados.para, origem="agente",
                                        quantidade_recebida=dados.quantidade_recebida, nf=dados.nf)
            if op.obra_id != prop["obra_id"]:
                raise NaoEncontrado("Pedido não encontrado nesta obra.")
            return op
        case TipoProposta.AJUSTE_ESTOQUE:
            op = await preparar_movimento(
                tx, u, obra_id=prop["obra_id"], insumo_id=dados.insumo_id, tipo=TipoMovimento.AJUSTE,
                quantidade=dados.quantidade, id=id_derivado(u.tenant_id, "proposta", prop["id"]), origem="agente")
            texto = op.texto
            op.texto = lambda d: f"{texto(d)}: {dados.motivo}"
            return op


async def _proposta_pendente(tx, u: UsuarioAtual, proposta_id: str) -> dict:
    prop = await tx.obter("propostas", u.tenant_id, proposta_id)
    if not prop:
        raise NaoEncontrado("Proposta não encontrada.")
    if prop["status"] != StatusProposta.PENDENTE.value:
        raise Conflito(f"Esta proposta já foi {prop['status']}.")
    return prop


def _mesclar_ajustes(prop: dict, ajustes: dict) -> tuple[dict, dict]:
    """Devolve (dados mesclados e validados, só o que mudou). Revalida com o esquema do tipo: um ajuste não pode
    produzir o que a criação recusaria."""
    tipo = TipoProposta(prop["tipo"])
    desconhecidas = sorted(set(ajustes) - AJUSTAVEIS[tipo])
    if desconhecidas:
        raise ErroNegocio(f"Não dá para ajustar {', '.join(desconhecidas)} numa proposta '{tipo.value}' "
                          f"(ajustáveis: {', '.join(sorted(AJUSTAVEIS[tipo]))}).")
    base = prop["dados"]
    if tipo == TipoProposta.CRIAR_PEDIDO:
        base = _pedido_plano(base)
    dados = _dados(tipo, {**base, **ajustes})
    _regras_dos_dados(dados)
    mesclados = _guardado(tipo, dados.model_dump(mode="json"))
    mudou = {k: (base.get(k), mesclados[k]) for k in ajustes if base.get(k) != mesclados[k]}
    return mesclados, mudou


def _guardado(tipo: TipoProposta, dados: dict) -> dict:
    """Como a proposta fica gravada: a de pedido com uma linha só fica no formato plano (insumo, quantidade, valor),
    que é o que o cartão de aprovação mostra e o que o ajuste edita."""
    if tipo == TipoProposta.CRIAR_PEDIDO and len(dados.get("linhas", [])) == 1:
        return _pedido_plano(dados)
    return dados


def _pedido_plano(dados: dict) -> dict:
    """A proposta de pedido do agente tem uma linha só: o ajuste fala de quantidade e valor como antes das linhas."""
    if "linhas" not in dados:
        return dados
    if len(dados["linhas"]) != 1:
        raise ErroNegocio("Esta proposta tem várias linhas; recuse e peça outra em vez de ajustar.")
    x = dados["linhas"][0]
    resto = {k: v for k, v in dados.items() if k != "linhas"}
    total = x.get("total")
    if total is None:
        total = (x["quantidade"] * x.get("valor_unitario", 0) + 500) // 1000
    return {**resto, "insumo_id": x["insumo_id"], "quantidade": x["quantidade"], "valor": total,
            "etapa": x.get("etapa")}


async def _unidade(tx, u: UsuarioAtual, prop: dict) -> str:
    """Só para escrever o ajuste no registro com a unidade ("34 → 30 sc")."""
    dados = prop["dados"]
    insumo_id = dados.get("insumo_id") or (dados.get("linhas") or [{}])[0].get("insumo_id")
    if not insumo_id and dados.get("pedido_id"):
        p = await tx.obter("pedidos", u.tenant_id, dados["pedido_id"])
        insumo_id = p and linhas_de(p)[0]["insumo_id"]
    i = await tx.obter("insumos", u.tenant_id, insumo_id) if insumo_id else None
    return i["unidade"] if i else ""


def _descrever_ajuste(mudou: dict, unidade: str) -> str:
    def fmt(campo: str, v) -> str:
        if v is None:
            return "—"
        if campo in ("quantidade", "quantidade_recebida"):
            return fmt_qtd(v)
        if campo == "valor":
            return fmt_reais(v)
        return str(v)

    partes = []
    for campo, (antes, depois) in mudou.items():
        un = f" {unidade}" if campo in ("quantidade", "quantidade_recebida") and unidade else ""
        partes.append(f"{campo.replace('_', ' ')} {fmt(campo, antes)} → {fmt(campo, depois)}{un}")
    return "; ".join(partes)


async def aprovar(arm, u: UsuarioAtual, proposta_id: str, ajustes: dict | None = None) -> tuple[dict, dict]:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        campos: dict = {}
        if ajustes:
            dados, mudou = _mesclar_ajustes(prop, ajustes)
            if mudou:
                campos = {"dados": dados, "dados_originais": prop["dados"], "ajustada": True}
                unidade = await _unidade(tx, u, prop)
        op = await _preparar_proposta(tx, u, {**prop, **campos})
        if campos:
            texto = op.texto
            op.texto = lambda d: f"{texto(d)} · ajustado: {_descrever_ajuste(mudou, unidade)}"
        _, entrada = executar_e_registrar(tx, u, op, origem="agente", proposta_id=proposta_id)
        campos |= {"status": StatusProposta.APROVADA.value, "decidido_por": u.id,
                   "decidido_por_nome": u.nome, "decidido_em": agora(), "registro_id": entrada["id"]}
        # a proposta grava com os dados que foram executados, na mesma transação da execução
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        evento(tx, u.tenant_id, "proposta.aprovada", prop["obra_id"], {"proposta_id": proposta_id,
                                                                      "ajustada": bool(campos.get("ajustada"))})
        return {**prop, **campos}, entrada

    return await arm.em_transacao(corpo)


async def recusar(arm, u: UsuarioAtual, proposta_id: str, motivo: str) -> dict:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        campos = {"status": StatusProposta.RECUSADA.value, "decidido_por": u.id,
                  "decidido_por_nome": u.nome, "decidido_em": agora(),
                  "motivo_recusa": motivo}
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        registrar(tx, u, obra_id=prop["obra_id"], texto=f"Recusou: {prop['titulo']} ({motivo})", origem="tela",
                  proposta_id=proposta_id)
        evento(tx, u.tenant_id, "proposta.recusada", prop["obra_id"], {"proposta_id": proposta_id})
        return {**prop, **campos}

    return await arm.em_transacao(corpo)


_DESFAZER = {
    "estornar_movimento": lambda tx, u, r: preparar_estorno(tx, u, r["movimento_id"]),
    "voltar_pedido": preparar_voltar,
    "desfazer_recebimento": pedidos.preparar_desfazer_recebimento,
    "editar_pedido": pedidos.preparar_desfazer_edicao,
    "editar_fornecedor": fornecedores.preparar_desfazer_edicao,
    "restaurar_cronograma": cronograma.preparar_restaurar,
    "desfazer_medicao": cronograma.preparar_desfazer_medicao,
    "cancelar_pedido": preparar_cancelar_criado,
    "restaurar_obra": obras.preparar_restaurar,
    "reabrir_orcamento": obras.preparar_reabrir,
    "desligar_versao": versoes.preparar_desligar,
}


async def desfazer(arm, u: UsuarioAtual, entrada_id: str) -> dict:
    async def corpo(tx):
        e = await tx.obter("registro", u.tenant_id, entrada_id)
        if not e:
            raise NaoEncontrado("Entrada do registro não encontrada.")
        if e["desfeito"]:
            raise Conflito("Isto já foi desfeito.")
        if not e.get("desfazer"):
            raise ErroNegocio("Esta entrada não tem como ser desfeita.")
        prop = await tx.obter("propostas", u.tenant_id, e["proposta_id"]) if e.get("proposta_id") else None
        op = await _DESFAZER[e["desfazer"]["op"]](tx, u, e["desfazer"])
        _, nova = executar_e_registrar(tx, u, op, origem="tela")
        tx.atualizar("registro", u.tenant_id, entrada_id, {"desfeito": True})
        if prop and prop["status"] == StatusProposta.APROVADA.value:
            tx.atualizar("propostas", u.tenant_id, prop["id"], {"status": StatusProposta.DESFEITA.value})
        evento(tx, u.tenant_id, "registro.desfeito", e.get("obra_id"), {"registro_id": entrada_id})
        return nova

    return await arm.em_transacao(corpo)
