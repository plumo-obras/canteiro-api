"""Regras de negócio. Contas ficam aqui (código determinístico), nunca no modelo de IA."""
from dataclasses import dataclass

from .modelos import Movimento, StatusPedido, TipoMovimento


class RegraViolada(ValueError):
    """Erro de negócio com mensagem pronta para mostrar ao usuário."""


@dataclass(frozen=True)
class Conta:
    """A linha de conta que aparece em toda tela e em toda resposta do agente."""
    precisa: int
    saldo: int
    a_caminho: int
    cotando: int
    transferencia: int
    falta: int        # o que falta depois de tudo que já está pedido, em cotação ou vindo de outra obra
    comprar: int      # = falta, nunca negativo


def conta_de_falta(*, precisa: int, saldo: int, a_caminho: int = 0, cotando: int = 0, transferencia: int = 0) -> Conta:
    """falta = precisa − saldo − a caminho − em cotação − transferência (em milésimos).

    Descontar o que está em cotação evita a compra em dobro (erro achado no protótipo: o pedido de compra incluía itens
    já cotados). Saldo negativo (baixa registrada antes da entrada) conta como zero para não inflar a falta.
    """
    falta = max(0, precisa - max(0, saldo) - a_caminho - cotando - transferencia)
    return Conta(precisa, saldo, a_caminho, cotando, transferencia, falta, falta)


_SINAL = {
    TipoMovimento.ENTRADA: 1,
    TipoMovimento.TRANSFERENCIA_ENTRADA: 1,
    TipoMovimento.SAIDA: -1,
    TipoMovimento.TRANSFERENCIA_SAIDA: -1,
}


def efeito_no_saldo(m: Movimento) -> int:
    """Quanto o movimento muda o saldo. Ajuste carrega o próprio sinal; os outros têm quantidade positiva."""
    if m.tipo == TipoMovimento.AJUSTE:
        return m.quantidade
    if m.quantidade <= 0:
        raise RegraViolada("A quantidade do movimento tem de ser maior que zero.")
    return _SINAL[m.tipo] * m.quantidade


def saldo_de(movimentos: list[Movimento]) -> int:
    """Recalcula o saldo do zero a partir do livro-razão (usado para conferir a projeção)."""
    return sum(efeito_no_saldo(m) for m in movimentos)


def validar_saida(*, curva: str, destino: str | None) -> None:
    """Item curva A (aço, concreto, bloco, cimento…) só sai com a frente de serviço informada."""
    if curva == "A" and not (destino and destino.strip()):
        raise RegraViolada("Saída de item curva A precisa da frente de serviço (para onde foi).")


# Transições permitidas do pedido. Cancelar vale até antes de receber.
TRANSICOES: dict[StatusPedido, set[StatusPedido]] = {
    StatusPedido.EM_COTACAO: {StatusPedido.APROVADO, StatusPedido.CANCELADO},
    StatusPedido.APROVADO: {StatusPedido.ENVIADO, StatusPedido.CANCELADO},
    StatusPedido.ENVIADO: {StatusPedido.EM_TRANSITO, StatusPedido.RECEBIDO, StatusPedido.CANCELADO},
    StatusPedido.EM_TRANSITO: {StatusPedido.RECEBIDO},
    StatusPedido.RECEBIDO: {StatusPedido.CONFERIDO},
    StatusPedido.CONFERIDO: set(),
    StatusPedido.CANCELADO: set(),
}

# O que conta como "a caminho" e como "em cotação" na conta de falta.
A_CAMINHO = {StatusPedido.APROVADO, StatusPedido.ENVIADO, StatusPedido.EM_TRANSITO, StatusPedido.RECEBIDO}
EM_COTACAO = {StatusPedido.EM_COTACAO}


def validar_transicao(de: StatusPedido, para: StatusPedido) -> None:
    if para not in TRANSICOES[de]:
        raise RegraViolada(f"O pedido não pode ir de '{de.value}' para '{para.value}'.")
