"""Pedidos de compra: número legível por tenant e transições validadas no domínio."""
from canteiro_dominio.modelos import Pedido, StatusPedido, TipoMovimento
from canteiro_dominio.regras import quantidade_conferida, validar_conferencia, validar_transicao
from canteiro_dominio.tipos import fmt_qtd, fmt_reais

from ..auth import UsuarioAtual
from ..esquemas import PedidoDados
from ..modelos import doc
from ..nucleo import Conflito, NaoEncontrado, Operacao, evento, insumo_da_obra, obra_do_tenant
from .estoque import id_derivado, preparar_estorno, preparar_movimento


def id_contador(tenant_id: str) -> str:
    # O Firestore não tem sequência nem índice único: o número sai de um documento-contador lido e gravado na transação.
    return f"contador-pedidos-{tenant_id}"


async def preparar_criar_pedido(tx, u: UsuarioAtual, obra_id: str, corpo: PedidoDados,
                                status: StatusPedido = StatusPedido.EM_COTACAO) -> Operacao:
    """`status` = aprovado quando a compra já foi decidida (proposta aprovada, ou a pessoa marcou na tela): pedir uma
    segunda aprovação do mesmo pedido seria burocracia sem informação nova."""
    await obra_do_tenant(tx, u.tenant_id, obra_id)
    insumo = await insumo_da_obra(tx, u.tenant_id, obra_id, corpo.insumo_id)
    contador = await tx.obter("contadores", u.tenant_id, id_contador(u.tenant_id))
    n = (contador["ultimo"] if contador else 0) + 1
    p = Pedido(tenant_id=u.tenant_id, numero=f"PED-{n:04d}", obra_id=obra_id, insumo_id=corpo.insumo_id,
               quantidade=corpo.quantidade, fornecedor=corpo.fornecedor, valor=corpo.valor, previsao=corpo.previsao,
               status=status)

    def aplicar() -> dict:
        tx.salvar("contadores", {"id": id_contador(u.tenant_id), "tenant_id": u.tenant_id, "ultimo": n})
        d = doc(p)
        tx.criar("pedidos", d)
        evento(tx, u.tenant_id, "pedido.criado", obra_id, {"pedido_id": p.id, "numero": p.numero,
                                                           "status": status.value})
        return d

    def texto(d: dict) -> str:
        aprovado = " · aprovado" if status == StatusPedido.APROVADO else ""
        return (f"{d['numero']} criado: {fmt_qtd(d['quantidade'])} {insumo['unidade']} de {insumo['nome']} "
                f"com {d['fornecedor']} ({fmt_reais(d['valor'])}){aprovado}")

    # o status de nascimento vai na receita: desfazer só cancela se o pedido não andou desde então
    return Operacao(obra_id=obra_id, aplicar=aplicar, texto=texto,
                    desfazer=lambda d: {"op": "cancelar_pedido", "pedido_id": d["id"], "status_criado": status.value})


async def _pedido(tx, tenant_id: str, pedido_id: str) -> dict:
    p = await tx.obter("pedidos", tenant_id, pedido_id)
    if not p:
        raise NaoEncontrado("Pedido não encontrado.")
    return p


async def preparar_avancar(tx, u: UsuarioAtual, pedido_id: str, para: StatusPedido, origem: str = "tela", *,
                           quantidade_recebida: int | None = None, nf: str | None = None) -> Operacao:
    p = await _pedido(tx, u.tenant_id, pedido_id)
    de = StatusPedido(p["status"])
    validar_transicao(de, para)
    validar_conferencia(para, quantidade_recebida=quantidade_recebida, nf=nf)
    entrada = insumo = None
    campos: dict = {"status": para.value}
    if para == StatusPedido.CONFERIDO:
        recebida = quantidade_conferida(pedida=p["quantidade"], recebida=quantidade_recebida)
        n = p.get("conferencias", 0)
        campos |= {"quantidade_recebida": recebida, "nf": nf, "conferencias": n + 1}
        insumo = await insumo_da_obra(tx, u.tenant_id, p["obra_id"], p["insumo_id"])
        # conferir = dar entrada no almoxarifado, na mesma transação (ID derivado do pedido e da vez: a mesma
        # conferência nunca entra duas vezes, e uma nova depois de desfazer não colide com a estornada)
        entrada = await preparar_movimento(
            tx, u, obra_id=p["obra_id"], insumo_id=p["insumo_id"], tipo=TipoMovimento.ENTRADA,
            quantidade=recebida, id=id_derivado(u.tenant_id, "conferido", pedido_id, str(n)),
            origem_doc=f"{p['numero']} · NF {nf}" if nf else p["numero"], origem=origem, insumo=insumo)

    def aplicar() -> dict:
        mov = entrada.aplicar() if entrada else None
        tx.atualizar("pedidos", u.tenant_id, pedido_id, campos)
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": pedido_id, "numero": p["numero"], "de": de.value, "para": para.value})
        return {**p, **campos, "_movimento_id": mov["id"] if mov else None}

    def texto(d: dict) -> str:
        t = f"{p['numero']}: {de.value} → {para.value}"
        if para != StatusPedido.CONFERIDO:
            return t
        # a divergência fica escrita no registro: é o que o comprador cobra do fornecedor depois
        if d["quantidade_recebida"] != p["quantidade"]:
            t += (f" · chegaram {fmt_qtd(d['quantidade_recebida'])} de {fmt_qtd(p['quantidade'])} "
                  f"{insumo['unidade']}")
        return t + (f" (NF {nf})" if nf else "")

    def desfazer(d: dict) -> dict:
        return {"op": "voltar_pedido", "pedido_id": pedido_id, "de": de.value, "para": para.value,
                "movimento_id": d["_movimento_id"]}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar, texto=texto, desfazer=desfazer)


async def preparar_voltar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz um avanço: volta o status (sem passar pelas transições, é a operação inversa) e estorna a entrada."""
    p = await _pedido(tx, u.tenant_id, receita["pedido_id"])
    if p["status"] != receita["para"]:
        raise Conflito(f"{p['numero']} já mudou de status desde então; não dá para desfazer.")
    estorno = await preparar_estorno(tx, u, receita["movimento_id"]) if receita.get("movimento_id") else None

    def aplicar() -> dict:
        if estorno:
            estorno.aplicar()
        campos = {"status": receita["de"]}
        if receita["para"] == StatusPedido.CONFERIDO.value:
            # o que a conferência anotou deixa de valer junto com a entrada estornada
            campos |= {"quantidade_recebida": None, "nf": None}
        tx.atualizar("pedidos", u.tenant_id, p["id"], campos)
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": p["id"], "numero": p["numero"], "de": receita["para"], "para": receita["de"]})
        return {**p, **campos}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {p['numero']} voltou para {receita['de']}", desfazer=lambda d: None)


async def preparar_cancelar_criado(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    p = await _pedido(tx, u.tenant_id, receita["pedido_id"])
    # receita antiga (de antes do pedido poder nascer aprovado) não tem o campo: nascia sempre em cotação
    if p["status"] != receita.get("status_criado", StatusPedido.EM_COTACAO.value):
        raise Conflito(f"{p['numero']} já andou ({p['status']}); cancele pelo fluxo do pedido.")

    def aplicar() -> dict:
        tx.atualizar("pedidos", u.tenant_id, p["id"], {"status": StatusPedido.CANCELADO.value})
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": p["id"], "numero": p["numero"], "de": p["status"], "para": "cancelado"})
        return {**p, "status": StatusPedido.CANCELADO.value}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {p['numero']} cancelado", desfazer=lambda d: None)
