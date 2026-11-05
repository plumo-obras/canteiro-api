"""Regras do gasto: o que vale para confirmar um comprovante e o que entra nas contas da obra."""
from dataclasses import dataclass
from datetime import date

from .etapas import EtapaNaArvore, validar_classificacao
from .leitura import MAX_DESCRICAO, MAX_FAVORECIDO
from .regras import RegraViolada


@dataclass(frozen=True)
class GastoValidado:
    favorecido: str
    descricao: str


def validar_gasto(*, valor: int, data_pagamento: str, favorecido: str, descricao: str, etapa: str,
                  topico_id: str | None, arvore: list[EtapaNaArvore], hoje: date) -> GastoValidado:
    """O que a pessoa confirmou na revisão. Devolve os textos já arrumados (espaços), que é o que se grava."""
    if valor <= 0:
        raise RegraViolada("O valor tem de ser maior que zero.")
    try:
        dia = date.fromisoformat(data_pagamento)
    except ValueError:
        raise RegraViolada("Informe a data do pagamento.") from None
    if dia > hoje:
        # gasto é dinheiro que já saiu: Pix agendado e boleto para o mês que vem entram quando forem pagos
        raise RegraViolada("A data do pagamento não pode ser depois de hoje.")
    fav = " ".join(favorecido.split())
    if not fav:
        raise RegraViolada("Informe quem recebeu o pagamento.")
    if len(fav) > MAX_FAVORECIDO:
        raise RegraViolada(f"Quem recebeu: até {MAX_FAVORECIDO} caracteres.")
    desc = " ".join(descricao.split())
    if not desc:
        raise RegraViolada("Diga em poucas palavras o que foi pago.")
    if len(desc) > MAX_DESCRICAO:
        raise RegraViolada(f"Descrição: até {MAX_DESCRICAO} caracteres.")
    validar_classificacao(arvore, etapa, topico_id)
    return GastoValidado(favorecido=fav, descricao=desc)


def conta_no_realizado(gasto: dict) -> bool:
    """Gasto avulso soma no realizado (e no comprometido) da etapa. Gasto de um pedido não: o pedido já soma pela
    entrega, e o gasto só registra que ele foi pago. Estornado não conta em nada."""
    return not gasto.get("estornado") and not gasto.get("pedido_id")
