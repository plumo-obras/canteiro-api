"""Contas do orçamento da obra (EAP) que não dependem de banco: o andamento de cada linha de pedido contra o orçado.

A API soma por etapa (e por tópico) o que cada linha devolve aqui; a tela e o agente só mostram o resultado.
"""
from .modelos import StatusPedido
from .regras import EM_COTACAO


def recebido_da_linha(linha: dict) -> int:
    """O que a linha do pedido já recebeu, pelo preço do pedido (centavos): a mesma soma das entregas, sem ler cada
    uma."""
    q, recebida = linha["quantidade"], linha.get("recebida", 0)
    return (recebida * linha.get("total", 0) + q // 2) // q if q else 0


def andamento_da_linha(status: StatusPedido, linha: dict) -> tuple[int, int, int]:
    """(cotando, comprometido, realizado) de uma linha de pedido, em centavos.

    Realizado sai do que a linha já recebeu. Comprometido é o maior entre o pedido e o entregue (entrega a mais também
    custa), exceto no pedido encerrado com sobra, que só compromete o que chegou. Pedido cancelado não conta.
    """
    if status == StatusPedido.CANCELADO:
        return 0, 0, 0
    total = linha.get("total", 0)
    if status in EM_COTACAO:
        return total, 0, 0
    recebido = recebido_da_linha(linha)
    encerrado_com_sobra = status == StatusPedido.CONFERIDO and linha.get("recebida", 0) < linha["quantidade"]
    return 0, recebido if encerrado_com_sobra else max(total, recebido), recebido
