"""Regras do orçamento da obra (EAP) que não dependem de banco: os tópicos que saem da numeração e o andamento de
cada linha de pedido contra o orçado.

A API soma por etapa (e por tópico) o que cada linha devolve aqui; a tela e o agente só mostram o resultado.
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .etapas import MAX_TOPICOS_POR_ETAPA, normalizar_nome
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


# --- tópicos do orçamento: o nível 2 da numeração ---
#
# Na planilha numerada, "7 PAREDES" é a etapa, "7.1 Cerâmica tijolo" é o tópico e "7.1.1 …" é detalhe do tópico. O
# tópico não é documento: sai dos itens de cada versão (e a versão guarda a lista pronta, `lista_topicos`, como guarda
# `lista_etapas`). Assim reordenar e somar continuam livres antes de aprovar, e o Desfazer da versão leva os tópicos
# junto, sem receita própria.


@dataclass(frozen=True)
class TopicoDoOrcamento:
    etapa: str
    nome: str                          # como veio na 1ª linha dele
    chave: str                         # o nome normalizado: a mesma "Portas" duas vezes na etapa é um tópico só


@dataclass
class TopicosDosItens:
    topicos: list[TopicoDoOrcamento] = field(default_factory=list)
    # posição do item (na lista recebida) -> o tópico dele; item sem tópico não entra
    do_item: dict[int, TopicoDoOrcamento] = field(default_factory=dict)
    # etapa -> quantos tópicos ela teria, quando passa do limite: ela fica sem tópicos do orçamento
    cheias: dict[str, int] = field(default_factory=dict)


def numero_do_topico(numero: str) -> str | None:
    """"7.1.2" -> "7.1"; número de etapa ("7") ou sem número -> None."""
    partes = numero.split(".")
    if len(partes) < 2 or not partes[0] or not partes[1]:
        return None
    return f"{partes[0]}.{partes[1]}"


def topicos_dos_itens(itens: Sequence[dict], herdados: Mapping[str, str] | None = None) -> TopicosDosItens:
    """Os tópicos de uma versão do orçamento e o tópico de cada item (`tipo`, `numero`, `nivel`, `etapa`,
    `descricao`, `grupo`), com os itens na ordem da planilha.

    O nome do tópico é a descrição da linha de nível 2 ("7.1"), seja ela título do grupo ou o próprio item; o item
    "7.1.3" cai no tópico "7.1". O item sem número de nível 3 é o detalhe do título de nível 2 logo acima dele (o grupo
    que a planilha feita à mão escreve sem numerar) e cai no tópico dele. `herdados` = {"7.1": nome do tópico} do
    orçamento que vale (no aditivo): o item que guarda o número do contrato ("7.1.3") sem trazer a linha "7.1" cai no
    tópico do contrato; a linha do próprio aditivo vale sobre ele. Item sem linha de nível 2 que o nomeie (a EAP que
    numera "2.1.1" sem a linha "2.1") fica sem tópico. O item encaixado numa etapa padrão (`grupo` = a etapa da planilha
    de onde ele veio) cai no tópico com o nome dela: a etapa padrão junta etapas da planilha, e o tópico as mantém
    separadas. A etapa do tópico é a do item (`etapa`), não o prefixo do número: depois do encaixe nas etapas padrão os
    dois diferem. Etapa com mais de `MAX_TOPICOS_POR_ETAPA` tópicos fica sem nenhum (`cheias`): a árvore com um tópico
    por serviço de uma EAP longa não ajudaria ninguém a escolher, e o leitor de comprovantes recebe a lista.
    """
    nomes = dict(herdados or {})
    proprios: set[str] = set()
    for x in itens:
        n = x.get("numero") or ""
        if numero_do_topico(n) == n and n not in proprios:
            proprios.add(n)
            nomes[n] = " ".join(x["descricao"].split())
    r = TopicosDosItens()
    vistos: dict[tuple[str, str], TopicoDoOrcamento] = {}
    grupo: str | None = None
    for i, x in enumerate(itens):
        n = x.get("numero") or ""
        if x["tipo"] != "item":
            grupo = n if numero_do_topico(n) == n else None
            continue
        if n:
            grupo = None
        da_planilha = " ".join((x.get("grupo") or "").split())
        if da_planilha:
            nome = da_planilha
        else:
            tn = numero_do_topico(n) if n else grupo if x.get("nivel", 0) > 2 else None
            if tn is None or tn not in nomes:
                continue
            nome = nomes[tn]
        if not x.get("etapa"):
            continue
        chave = normalizar_nome(nome)
        t = vistos.get((x["etapa"], chave))
        if t is None:
            t = vistos[(x["etapa"], chave)] = TopicoDoOrcamento(etapa=x["etapa"], nome=nome, chave=chave)
            r.topicos.append(t)
        r.do_item[i] = t
    por_etapa: dict[str, int] = {}
    for t in r.topicos:
        por_etapa[t.etapa] = por_etapa.get(t.etapa, 0) + 1
    r.cheias = {e: n for e, n in por_etapa.items() if n > MAX_TOPICOS_POR_ETAPA}
    if r.cheias:
        r.topicos = [t for t in r.topicos if t.etapa not in r.cheias]
        r.do_item = {i: t for i, t in r.do_item.items() if t.etapa not in r.cheias}
    return r


# --- custo e venda ---

_CEM = 10000                           # 100% em centésimos de ponto percentual (BDI 22,91% = 2291)


def custo_da_venda(valor: int, bdi: int) -> int:
    """O custo de um preço de venda com BDI: preço ÷ (1 + BDI), em centavos, arredondado para o mais próximo (meio para
    longe do zero, para a supressão do aditivo dar o mesmo número com o sinal trocado). O orçado × realizado compara
    custo com custo: sem tirar o BDI, a margem apareceria como dinheiro que falta gastar."""
    if bdi <= 0:
        return valor
    d = _CEM + bdi
    sinal = -1 if valor < 0 else 1
    return sinal * ((abs(valor) * _CEM * 2 + d) // (2 * d))
