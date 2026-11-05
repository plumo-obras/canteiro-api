"""Regras do gasto: o que vale para confirmar um comprovante e o que entra nas contas da obra."""
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import date

from .etapas import EtapaNaArvore, origem_da_etapa, validar_classificacao
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


def soma_nos_gastos_avulsos(gasto: dict) -> bool:
    """Se o gasto entra no total da árvore de gastos e no realizado do orçamento. Gasto de um pedido não: o custo do
    pedido já conta pela entrega (`realizado` do pedido), e o gasto só registra que ele foi pago (`Pedido.pago`);
    somar de novo contaria duas vezes. Estornado não conta em nada."""
    return not gasto.get("estornado") and not gasto.get("pedido_id")


@dataclass
class GastosNoOrcamento:
    """Os gastos avulsos de uma obra, do jeito que entram no orçamento (centavos).

    - `por_etapa`: os que estão numa etapa do orçamento vigente; somam no comprometido e no realizado dela (e mexem
      no saldo e no estouro dela).
    - `fora_do_orcamento`: os das etapas extras da obra; dinheiro que saiu numa coisa que o orçamento não prevê. Não
      engordam etapa nenhuma, mas contam nos totais da obra.
    - `na_lista_padrao`: obra sem orçamento, gastos na lista padrão. Não há etapa do orçamento para eles ainda.
    - `sem_etapa_do_orcamento`: obra com orçamento, gastos numa chave que não é etapa dele nem extra (lançados na
      lista padrão antes de a obra importar o orçamento, ou numa etapa de versão desfeita). A pessoa reclassifica
      para levá-los a uma etapa; até lá contam só nos totais.

    Nos totais da obra entram todos: é tudo dinheiro que saiu.
    """
    por_etapa: dict[str, int] = field(default_factory=dict)
    fora_do_orcamento: int = 0
    na_lista_padrao: int = 0
    sem_etapa_do_orcamento: int = 0

    @property
    def nas_etapas(self) -> int:
        return sum(self.por_etapa.values())

    @property
    def total(self) -> int:
        return self.nas_etapas + self.fora_do_orcamento + self.na_lista_padrao + self.sem_etapa_do_orcamento

    @property
    def fora_das_etapas(self) -> int:
        """O que conta nos totais da obra sem estar em etapa nenhuma do orçamento."""
        return self.fora_do_orcamento + self.na_lista_padrao + self.sem_etapa_do_orcamento


def gastos_no_orcamento(gastos: Iterable[dict], etapas_orcamento: Collection[str]) -> GastosNoOrcamento:
    """Reparte os gastos da obra pela origem da etapa (`etapas_orcamento` = as chaves vigentes; vazio = obra sem
    orçamento). Só gasto avulso confirmado (`soma_nos_gastos_avulsos`)."""
    r = GastosNoOrcamento()
    for g in gastos:
        if not soma_nos_gastos_avulsos(g):
            continue
        origem = origem_da_etapa(g["etapa"], etapas_orcamento)
        if origem == "orcamento":
            r.por_etapa[g["etapa"]] = r.por_etapa.get(g["etapa"], 0) + g["valor"]
        elif origem == "extra":
            r.fora_do_orcamento += g["valor"]
        elif origem == "padrao":
            r.na_lista_padrao += g["valor"]
        else:
            r.sem_etapa_do_orcamento += g["valor"]
    return r


def validar_reclassificacao(gasto: dict, *, etapa: str, topico_id: str | None,
                            arvore: list[EtapaNaArvore]) -> None:
    """Mudar a etapa (e o tópico) de um gasto já confirmado: o destino tem de estar na árvore de hoje, como numa
    confirmação. É o caminho do gasto "sem etapa do orçamento" até uma etapa do orçamento."""
    if gasto["etapa"] == etapa and gasto.get("topico_id") == topico_id:
        raise RegraViolada("Nada mudou.")
    validar_classificacao(arvore, etapa, topico_id)
