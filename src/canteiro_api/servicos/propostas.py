"""Propostas (o agente só propõe), aprovação que executa e o desfazer a partir do registro."""
from pydantic import ValidationError

from canteiro_dominio.modelos import Proposta, StatusPedido, StatusProposta, TipoMovimento, TipoProposta, agora
from canteiro_dominio.regras import validar_transicao

from ..auth import UsuarioAtual
from ..esquemas import DADOS_POR_TIPO, AjusteEstoqueDados, AvancarPedidoDados, PedidoCorpo, PropostaCorpo
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
from .estoque import id_derivado, preparar_estorno, preparar_movimento
from .pedidos import preparar_avancar, preparar_cancelar_criado, preparar_criar_pedido, preparar_voltar


def _dados(tipo: TipoProposta, dados: dict):
    try:
        return DADOS_POR_TIPO[tipo].model_validate(dados)
    except ValidationError as e:
        campos = ", ".join(".".join(map(str, err["loc"])) or "dados" for err in e.errors())
        raise ErroNegocio(f"Dados da proposta '{tipo.value}' inválidos ({campos}).") from None


async def criar_proposta(arm, u: UsuarioAtual, corpo: PropostaCorpo) -> dict:
    """Só valida e guarda. Conferir as referências agora evita cartão de decisão que nunca poderia ser aprovado."""
    dados = _dados(corpo.tipo, corpo.dados)
    await obra_do_tenant(arm, u.tenant_id, corpo.obra_id)
    match dados:
        case PedidoCorpo() | AjusteEstoqueDados():
            await insumo_da_obra(arm, u.tenant_id, corpo.obra_id, dados.insumo_id)
        case AvancarPedidoDados():
            p = await arm.obter("pedidos", u.tenant_id, dados.pedido_id)
            if not p or p["obra_id"] != corpo.obra_id:
                raise NaoEncontrado("Pedido não encontrado nesta obra.")
            validar_transicao(StatusPedido(p["status"]), dados.para)
    if isinstance(dados, AjusteEstoqueDados) and dados.quantidade == 0:
        raise ErroNegocio("Ajuste de zero não muda nada.")
    p = Proposta(tenant_id=u.tenant_id, obra_id=corpo.obra_id, tipo=corpo.tipo, titulo=corpo.titulo,
                 conta=corpo.conta, justificativa=corpo.justificativa, fonte=corpo.fonte,
                 dados=dados.model_dump(mode="json"))
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
            return await preparar_criar_pedido(tx, u, prop["obra_id"], dados)
        case TipoProposta.AVANCAR_PEDIDO:
            op = await preparar_avancar(tx, u, dados.pedido_id, dados.para, origem="agente")
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


async def aprovar(arm, u: UsuarioAtual, proposta_id: str) -> tuple[dict, dict]:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        op = await _preparar_proposta(tx, u, prop)
        _, entrada = executar_e_registrar(tx, u, op, origem="agente", proposta_id=proposta_id)
        campos = {"status": StatusProposta.APROVADA.value, "decidido_por": u.id,
                  "decidido_por_nome": u.nome, "decidido_em": agora(), "registro_id": entrada["id"]}
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        evento(tx, u.tenant_id, "proposta.aprovada", prop["obra_id"], {"proposta_id": proposta_id})
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
    "cancelar_pedido": preparar_cancelar_criado,
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
