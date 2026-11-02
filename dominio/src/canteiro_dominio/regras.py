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
    # de parcial, "conferido" é encerrar: o fornecedor não manda o resto e ele sai do "a caminho"
    StatusPedido.PARCIAL: {StatusPedido.CONFERIDO},
    StatusPedido.CONFERIDO: set(),
    StatusPedido.CANCELADO: set(),
}

# O que conta como "a caminho" e como "em cotação" na conta de falta.
A_CAMINHO = {StatusPedido.APROVADO, StatusPedido.ENVIADO, StatusPedido.EM_TRANSITO, StatusPedido.RECEBIDO,
             StatusPedido.PARCIAL}
EM_COTACAO = {StatusPedido.EM_COTACAO}


# Onde uma entrega pode ser registrada. Aprovado entra: o caminhão chega antes de alguém marcar "enviado" na tela.
PODE_RECEBER = A_CAMINHO


def falta_na_linha(linha) -> int:
    """O que ainda está a caminho numa linha do pedido. Entrega a mais não vira falta negativa."""
    return max(0, linha["quantidade"] - linha.get("recebida", 0))


def cnpj_valido(cnpj: str) -> bool:
    """14 dígitos com os dois verificadores (módulo 11). Sequência repetida ("000…") passa na conta e é recusada."""
    if len(cnpj) != 14 or not cnpj.isdigit() or len(set(cnpj)) == 1:
        return False
    def dv(base: str) -> str:
        pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2][-len(base):]
        r = sum(int(c) * w for c, w in zip(base, pesos, strict=True)) % 11
        return "0" if r < 2 else str(11 - r)
    return cnpj[12] == dv(cnpj[:12]) and cnpj[13] == dv(cnpj[:13])


def validar_transicao(de: StatusPedido, para: StatusPedido) -> None:
    if para not in TRANSICOES[de]:
        raise RegraViolada(f"O pedido não pode ir de '{de.value}' para '{para.value}'.")


def validar_conferencia(para: StatusPedido, *, quantidade_recebida: int | None = None, nf: str | None = None) -> None:
    """Quantidade recebida e NF descrevem a chegada do material: fora da conferência não têm onde entrar, e aceitar
    calado faria a pessoa achar que registrou algo que foi descartado."""
    if para != StatusPedido.CONFERIDO and (quantidade_recebida is not None or nf is not None):
        raise RegraViolada("Quantidade recebida e NF só valem ao conferir o pedido.")
    if quantidade_recebida is not None and quantidade_recebida <= 0:
        raise RegraViolada("A quantidade recebida tem de ser maior que zero.")


def quantidade_conferida(*, pedida: int, recebida: int | None) -> int:
    """O que entra no almoxarifado ao conferir: o que chegou de fato, ou o pedido inteiro se ninguém informou.

    Entrega a mais é aceita (o fornecedor mandou e o material está no canteiro); entrega a menos não deixa o resto
    "a caminho": o pedido fecha em conferido e a diferença volta a aparecer como falta na conta.
    """
    return pedida if recebida is None else recebida
