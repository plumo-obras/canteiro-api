"""Contas do orçamento da obra que moram no domínio."""
from canteiro_dominio.modelos import StatusPedido
from canteiro_dominio.orcamento import andamento_da_linha, recebido_da_linha


def linha(quantidade=10_000, total=100_00, recebida=0):
    return {"quantidade": quantidade, "total": total, "recebida": recebida}


def test_recebido_pelo_preco_do_pedido():
    assert recebido_da_linha(linha(recebida=2_500)) == 25_00
    assert recebido_da_linha(linha(quantidade=3_000, total=100_00, recebida=1_000)) == 33_33
    assert recebido_da_linha(linha(quantidade=0)) == 0


def test_andamento_da_linha():
    assert andamento_da_linha(StatusPedido.CANCELADO, linha()) == (0, 0, 0)
    assert andamento_da_linha(StatusPedido.EM_COTACAO, linha()) == (100_00, 0, 0)
    assert andamento_da_linha(StatusPedido.APROVADO, linha()) == (0, 100_00, 0)
    assert andamento_da_linha(StatusPedido.PARCIAL, linha(recebida=4_000)) == (0, 100_00, 40_00)
    # entrega a mais também custa: compromete o entregue
    assert andamento_da_linha(StatusPedido.CONFERIDO, linha(recebida=12_000)) == (0, 120_00, 120_00)
    # encerrado com sobra: só o que chegou
    assert andamento_da_linha(StatusPedido.CONFERIDO, linha(recebida=6_000)) == (0, 60_00, 60_00)
