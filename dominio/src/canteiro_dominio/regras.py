"""Regras de negócio. Contas ficam aqui (código determinístico), nunca no modelo de IA."""
from collections.abc import Iterable
from dataclasses import dataclass

from .modelos import Movimento, StatusPedido, TipoMovimento
from .tipos import fmt_qtd

CEM = 10000                       # 100% em centésimos de ponto percentual


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
    # O que já tem ou vem além do que a obra precisa. Separado da falta para a linha de conta poder dizer "sobra 15"
    # em vez de um "falta 0" que, escrito como resultado da subtração, é aritmética falsa.
    sobra: int = 0
    # Ninguém lançou quanto a obra precisa (precisa = 0). Não quer dizer que não precisa: quer dizer que o sistema não
    # sabe. Foi o "precisa 0 … = falta 0" que levou o agente a concluir "a obra não precisa de cimento".
    sem_previsao: bool = False


def conta_de_falta(*, precisa: int, saldo: int, a_caminho: int = 0, cotando: int = 0, transferencia: int = 0) -> Conta:
    """falta = precisa − saldo − a caminho − em cotação − transferência (em milésimos).

    Descontar o que está em cotação evita a compra em dobro (erro achado no protótipo: o pedido de compra incluía itens
    já cotados). Saldo negativo (baixa registrada antes da entrada) conta como zero para não inflar a falta.
    """
    disponivel = max(0, saldo) + a_caminho + cotando + transferencia
    sem_previsao = precisa <= 0
    falta = max(0, precisa - disponivel)
    sobra = 0 if sem_previsao else max(0, disponivel - precisa)
    return Conta(precisa, saldo, a_caminho, cotando, transferencia, falta, falta, sobra, sem_previsao)


def texto_da_conta(c: Conta, unidade: str) -> str:
    """A linha de conta que a tela e o agente citam, sempre aritmeticamente verdadeira.

    Três formas: com falta (ou exatamente coberta) é a subtração de sempre; com sobra, a soma do que tem e vem contra o
    que precisa; sem previsão, nenhuma conta, e diz por quê. Escrever "precisa 0 − saldo 60 = falta 0" afirmava uma
    conta que dá −60 e soava como "não precisa".
    """
    def f(m: int) -> str:
        return f"{fmt_qtd(m)} {unidade}".strip()

    saldo = f(max(0, c.saldo)) + (f" (o livro está em {f(c.saldo)}; conta como zero)" if c.saldo < 0 else "")
    if c.sem_previsao:
        return (f"sem previsão lançada para a obra (o sistema não sabe quanto ela precisa, então não calcula a falta)"
                f" · saldo {saldo} · a caminho {f(c.a_caminho)} · em cotação {f(c.cotando)}"
                f" · transferência {f(c.transferencia)}")
    if c.sobra:
        disponivel = max(0, c.saldo) + c.a_caminho + c.cotando + c.transferencia
        return (f"saldo {saldo} + a caminho {f(c.a_caminho)} + em cotação {f(c.cotando)}"
                f" + transferência {f(c.transferencia)} = {f(disponivel)} para precisa {f(c.precisa)}:"
                f" sobra {f(c.sobra)}, falta 0")
    return (f"precisa {f(c.precisa)} − saldo {saldo} − a caminho {f(c.a_caminho)} − em cotação {f(c.cotando)}"
            f" − transferência {f(c.transferencia)} = falta {f(c.falta)}")


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


# --- do orçamento para Compras ---

@dataclass(frozen=True)
class ItemParaCompras:
    """Um insumo que o orçamento pede: as linhas de mesmo nome juntas. Quantidade com sinal (supressão de aditivo)."""
    nome: str
    unidade: str
    quantidade: int
    valor_unitario: int
    total: int
    etapa: str | None


def chave_do_nome(nome: str) -> str:
    """"Areia  média" e "areia média" são o mesmo insumo."""
    return " ".join(nome.split()).casefold()


def preco_medio(total: int, quantidade: int) -> int:
    """Unitário em centavos que dá `total` em `quantidade` milésimos, arredondado ao centavo."""
    return (total * 1000 + quantidade // 2) // quantidade if quantidade > 0 else 0


def itens_para_compras(itens: Iterable[dict]) -> list[ItemParaCompras]:
    """Junta as linhas do orçamento pelo nome. A mesma coisa em duas etapas é um insumo só, para o almoxarifado ter um
    saldo só; a soma é das quantidades, o preço é o médio ponderado e a etapa é a de maior valor (a de maior quantidade
    no empate, como num orçamento ainda sem preços; depois, a primeira)."""
    grupos: dict[str, dict] = {}
    unidades: dict[str, list[str]] = {}
    for x in itens:
        if x["tipo"] != "item":
            continue
        chave = chave_do_nome(x["descricao"])
        g = grupos.setdefault(chave, {"nome": " ".join(x["descricao"].split()), "unidade": x["unidade"],
                                      "quantidade": 0, "total": 0, "etapas": {}})
        if g["unidade"].strip().casefold() != x["unidade"].strip().casefold():
            unidades.setdefault(g["nome"], [g["unidade"]]).append(x["unidade"])
            continue
        g["quantidade"] += x["quantidade"]
        g["total"] += x["total"]
        por_etapa = g["etapas"].setdefault(x.get("etapa") or None, [0, 0])
        por_etapa[0] += x["total"]
        por_etapa[1] += x["quantidade"]
    if unidades:
        lista = "; ".join(f"{n} ({' e '.join(dict.fromkeys(u))})" for n, u in unidades.items())
        raise RegraViolada(f"Mesmo item com unidades diferentes no orçamento: {lista}. Corrija antes de continuar.")
    return [ItemParaCompras(nome=g["nome"], unidade=g["unidade"], quantidade=g["quantidade"],
                            valor_unitario=preco_medio(g["total"], g["quantidade"]), total=g["total"],
                            etapa=max(g["etapas"], key=lambda e: tuple(g["etapas"][e])))
            for g in grupos.values()]


def insumo_ao_aprovar(insumo: dict, item: ItemParaCompras) -> dict:
    """O que muda num insumo que já existia quando o orçamento é aprovado. Não soma: quem cadastrou antes já estava
    prevendo o mesmo material, e somar dobraria a compra."""
    return {"precisa_ate_o_fim": max(insumo.get("precisa_ate_o_fim", 0), item.quantidade),
            "valor_unitario": insumo.get("valor_unitario", 0) or item.valor_unitario,
            "etapa": insumo.get("etapa") or item.etapa}


def insumo_com_aditivo(insumo: dict, item: ItemParaCompras) -> dict:
    """O que muda num insumo quando entra um aditivo com a obra aprovada. Aqui soma, porque aditivo é acréscimo ao que
    já foi aprovado; supressão reduz, sem passar de zero."""
    return {"precisa_ate_o_fim": max(0, insumo.get("precisa_ate_o_fim", 0) + item.quantidade),
            "valor_unitario": insumo.get("valor_unitario", 0) or item.valor_unitario,
            "etapa": insumo.get("etapa") or item.etapa}


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


def validar_conferencia(para: StatusPedido, *, quantidade_recebida: int | None = None, nf: str | None = None,
                        de: StatusPedido | None = None) -> None:
    """Quantidade recebida e NF descrevem a chegada do material: fora da conferência não têm onde entrar, e aceitar
    calado faria a pessoa achar que registrou algo que foi descartado.

    De `parcial`, conferir é ENCERRAR o resto sem entrada: quantidade e NF ali eram descartadas em silêncio (o agente
    propôs "chegaram 7 m³ (NF 4410)" e a aprovação só fecharia o pedido). O que chegou entra por uma entrega."""
    if para != StatusPedido.CONFERIDO and (quantidade_recebida is not None or nf is not None):
        raise RegraViolada("Quantidade recebida e NF só valem ao conferir o pedido.")
    if de == StatusPedido.PARCIAL and (quantidade_recebida is not None or nf is not None):
        raise RegraViolada("De parcial, conferir só encerra o resto, sem entrada no almoxarifado. O que chegou entra "
                           "registrando a entrega (com quantidade e NF).")
    if quantidade_recebida is not None and quantidade_recebida <= 0:
        raise RegraViolada("A quantidade recebida tem de ser maior que zero.")


def quantidade_conferida(*, pedida: int, recebida: int | None) -> int:
    """O que entra no almoxarifado ao conferir: o que chegou de fato, ou o pedido inteiro se ninguém informou.

    Entrega a mais é aceita (o fornecedor mandou e o material está no canteiro); entrega a menos não deixa o resto
    "a caminho": o pedido fecha em conferido e a diferença volta a aparecer como falta na conta.
    """
    return pedida if recebida is None else recebida


@dataclass(frozen=True)
class LinhaEntregue:
    """Uma linha do pedido numa entrega: antes, esta entrega e depois. É o que a proposta de entrega mostra."""
    indice: int
    nome: str
    unidade: str
    pedida: int
    recebida_antes: int
    quantidade: int
    recebida_depois: int
    falta_depois: int
    excesso: int                  # o que passa do que faltava (0 quando cabe)


@dataclass(frozen=True)
class PreviaEntrega:
    linhas: list[LinhaEntregue]
    completa: bool                # com esta entrega nada mais falta no pedido (ele fecha em conferido)


def previa_entrega(linhas_pedido: list[dict], entregas: list[tuple[int, int]]) -> PreviaEntrega:
    """O que uma entrega faz com o pedido, sem gravar: `entregas` = [(índice da linha, quantidade em milésimos)]."""
    if not entregas:
        raise RegraViolada("A entrega precisa de pelo menos uma linha com quantidade.")
    vistos: set[int] = set()
    depois = [x.get("recebida", 0) for x in linhas_pedido]
    saida = []
    for indice, q in entregas:
        if not 0 <= indice < len(linhas_pedido):
            raise RegraViolada(f"O pedido não tem a linha {indice + 1}.")
        if indice in vistos:
            raise RegraViolada("A mesma linha do pedido aparece duas vezes na entrega.")
        if q <= 0:
            raise RegraViolada("A quantidade entregue tem de ser maior que zero.")
        vistos.add(indice)
        x = linhas_pedido[indice]
        antes = x.get("recebida", 0)
        depois[indice] = antes + q
        faltava = falta_na_linha(x)
        saida.append(LinhaEntregue(indice=indice, nome=x.get("nome", ""), unidade=x.get("unidade", ""),
                                   pedida=x["quantidade"], recebida_antes=antes, quantidade=q,
                                   recebida_depois=antes + q, falta_depois=max(0, x["quantidade"] - antes - q),
                                   excesso=max(0, q - faltava)))
    completa = all(r >= x["quantidade"] for r, x in zip(depois, linhas_pedido, strict=True))
    return PreviaEntrega(saida, completa)


def validar_entrega(status: StatusPedido, linhas_pedido: list[dict], entregas: list[tuple[int, int]], *,
                    confirmar_excesso: bool = False) -> PreviaEntrega:
    """Regras de uma entrega proposta: o pedido recebe neste status, as linhas existem e, se passa do que falta, a
    pessoa confirmou (é o "chegou a mais" da tela: aceito, mas nunca calado, porque costuma ser erro de digitação ou a
    entrega de outro pedido)."""
    if status not in PODE_RECEBER:
        raise RegraViolada(f"O pedido está {status.value}: não recebe entrega.")
    previa = previa_entrega(linhas_pedido, entregas)
    acima = [x for x in previa.linhas if x.excesso]
    if acima and not confirmar_excesso:
        # o que faltava = esta entrega − o que passou dela (a prévia já calculou os dois)
        partes = "; ".join(f"{x.nome}: faltavam {fmt_qtd(x.quantidade - x.excesso)} {x.unidade} e a entrega diz "
                           f"{fmt_qtd(x.quantidade)} {x.unidade}" for x in acima)
        raise RegraViolada(f"A entrega passa do que falta ({partes}). Confirme com a pessoa que chegou mesmo a mais "
                           "antes de propor (confirmar_excesso).")
    return previa


def texto_da_entrega(previa: PreviaEntrega) -> str:
    """A conta da entrega, linha a linha: "Brita 1: recebido 18 m³ + esta entrega 7 m³ = 25 de 25 m³ · pedido
    completo". Os números vêm da prévia; quem mostra não soma."""
    partes = []
    for x in previa.linhas:
        un = f" {x.unidade}" if x.unidade else ""
        t = (f"{x.nome}: recebido {fmt_qtd(x.recebida_antes)}{un} + esta entrega {fmt_qtd(x.quantidade)}{un}"
             f" = {fmt_qtd(x.recebida_depois)} de {fmt_qtd(x.pedida)}{un}")
        if x.excesso:
            t += f" ({fmt_qtd(x.excesso)}{un} a mais)"
        elif x.falta_depois:
            t += f" (ainda faltam {fmt_qtd(x.falta_depois)}{un})"
        partes.append(t)
    return "; ".join(partes) + (" · pedido completo" if previa.completa else " · o pedido continua parcial")


@dataclass(frozen=True)
class SituacaoEtapa:
    situacao: str                 # concluida | atrasada | adiantada | em_dia | nao_comecou
    previsto: int                 # % previsto acumulado até o mês de referência (centésimos)
    falta: int                    # quanto o medido está abaixo do previsto (centésimos; 0 se não está atrasada)


def situacao_etapa(percentuais: list[int], medido: int, ref: int | None) -> SituacaoEtapa:
    """Situação de uma etapa no mês de referência (o da última medição; `ref` é o índice dele no cronograma).

    É a regra que a tela do cronograma mostrava calculando no navegador; aqui ela é a mesma para a tela e o agente.
    Sem medição (`ref` None) o previsto até a referência é zero. "Adiantada" é o medido acima do previsto, que a tela
    contava como "em dia".
    """
    previsto = 0 if ref is None or ref < 0 else sum(percentuais[: ref + 1])
    if medido >= CEM:
        return SituacaoEtapa("concluida", previsto, 0)
    if medido < previsto:
        return SituacaoEtapa("atrasada", previsto, previsto - medido)
    if not medido and not previsto:
        return SituacaoEtapa("nao_comecou", previsto, 0)
    if medido > previsto:
        return SituacaoEtapa("adiantada", previsto, 0)
    return SituacaoEtapa("em_dia", previsto, 0)
