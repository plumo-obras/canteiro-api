"""Orçamento da obra como EAP (etapa → item), em versões: o contrato e os aditivos.

A planilha de orçamento de obra é de serviços, não de materiais: "Pintura látex 3.119 m²" é um item da etapa
Revestimentos. Por isso cada linha vira um item com o número, o código e a fonte dela, e nada é juntado (o mesmo serviço
em duas etapas são dois itens, e o total fecha no centavo com a planilha).

Cada importação é uma versão. Reimportar o contrato substitui a versão inteira, e o aditivo soma sem tocar no contrato:
é o que o engenheiro precisa para responder "quanto custou o aditivo". Desfazer é desligar a versão, porque a porta de
armazenamento não apaga.
"""
import hashlib
import re
from decimal import Decimal

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.cronograma import estimar_fases_faltantes, ordem_da_fase
from canteiro_dominio.encaixe import (
    etapa_da_estimativa,
    etapa_no_contexto,
    etapa_pelo_nome,
    fase_da_etapa,
    nome_da_etapa,
    ordem_da_etapa,
)
from canteiro_dominio.etapas import (
    CHAVES_FIXAS,
    ETAPAS_FIXAS,
    MAX_NOME,
    MAX_TOPICOS_POR_ETAPA,
    OUTROS,
    normalizar_nome,
)
from canteiro_dominio.gastos import gastos_no_orcamento
from canteiro_dominio.modelos import ItemOrcamento, TipoVersao, VersaoOrcamento
from canteiro_dominio.orcamento import custo_da_venda, numero_do_topico, topicos_dos_itens
from canteiro_dominio.regras import RegraViolada
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

from ..auth import UsuarioAtual
from ..esquemas import (
    Aviso,
    DestinoDoEncaixe,
    EncaixeEtapa,
    ErroLinha,
    EtapaOrcamento,
    EtapaRef,
    GastosNoOrcamentoSaida,
    ItemPrevia,
    LinhaIgnorada,
    Mapeamento,
    OrcamentoObra,
    PreviaVersao,
    RepetidoDe,
    Substituida,
    TopicoOrcamento,
    TopicoPrevia,
    TotaisOrcamento,
    TotalEtapa,
)
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    id_derivado,
    obra_do_tenant,
    receita_inversa,
    recusar_se_desfazendo,
    verbo_desfazer,
)
from .orcamento import MAX_LINHAS, _arredonda, _grade, _normal, _numero, _texto, _vazio

# Itens por transação: uma escrita cada, folgado nas 500 da transação portável.
ITENS_POR_TRANSACAO = 400
# Linha de subtotal ou total no meio da planilha: soma o que já está nos itens, contar de novo dobraria a etapa.
SUBTOTAL = re.compile(r"^(sub ?total|total|soma)\b")
# Os da planilha de custo feita à mão ("CUSTO TOTAL", "CUSTO MÃO DE OBRA" no pé da etapa). Só em linha sem número:
# "7.2 Custo mão de obra" numerado é linha do orçamento.
SUBTOTAL_SEM_NUMERO = re.compile(r"^custo (total|geral|mao de ?obra)\b")
MAX_COMPLEMENTO = 80
# Linha numerada sem unidade, com valor e sem linhas embaixo dela: é verba ("Materiais diversos R$ 154.859,70"), um
# valor fechado. Entra como 1 vb para somar na etapa e poder ser comprado/contratado como qualquer item.
VERBA = "vb"
UMA_VERBA = 1000                       # 1 vb em milésimos
NUMERO_ITEM = re.compile(r"^\d+(\.\d+)*\.?$")
# O nome que `importar_estrutura` grava: é por ele que se sabe que a versão veio das pranchas (material, não serviço).
SUFIXO_PROJETO = " (projeto).csv"

def _ordinal(n: int) -> str:
    return f"{n}º"


def _numero_item(v) -> str:
    """'3.2.1.' perde o ponto final; o resto fica como veio ("A1.2" de aditivo também dá etapa e nível pelos pontos).

    Número digitado como número no Excel chega float (1.1): vai por Decimal para dar "1.1" e não "1,1", que é como o
    `_texto` escreve a quantidade para o brasileiro ler. Com vírgula, o nível sairia 1 (o item viraria etapa nova) e a
    reordenação não acharia o prefixo. "1.10" digitado como número já chega 1.1 do Excel: a prévia mostra para conferir.
    """
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return format(Decimal(str(v)).normalize(), "f")
    t = _texto(v)
    return t[:-1] if t.endswith(".") and NUMERO_ITEM.match(t) else t


def _ler(grade: list[list], m: Mapeamento, tipo: TipoVersao):
    itens: list[ItemPrevia] = []
    ignoradas: list[LinhaIgnorada] = []
    erros: list[ErroLinha] = []
    avisos: list[Aviso] = []
    unidade_do_codigo: dict[str, tuple[str, int]] = {}
    etapa_atual = ""
    verbas: dict[int, str] = {}                # posição em `itens` -> a quantidade sem unidade que veio na linha
    valor_titulo: dict[int, int] = {}          # posição em `itens` -> o valor escrito na própria linha do título

    def cel(row, idx):
        return row[idx] if idx is not None and idx < len(row) else None

    dados = 0
    for n, row in enumerate(grade[m.linha_cabecalho:], start=m.linha_cabecalho + 1):
        textos = [_texto(c) for c in row]
        if not any(textos):
            continue
        dados += 1
        if dados > MAX_LINHAS:
            erros.append(ErroLinha(linha=n, mensagem=f"Mais de {MAX_LINHAS} linhas; divida o arquivo."))
            break
        descricao = _texto(cel(row, m.nome))
        texto = descricao or next(t for t in textos if t)
        if not descricao:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem descrição"))
            continue
        numero = _numero_item(cel(row, m.item))
        if SUBTOTAL.match(_normal(descricao)) or (not numero and SUBTOTAL_SEM_NUMERO.match(_normal(descricao))):
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="subtotal ou total"))
            continue
        complemento = _texto(cel(row, m.complemento))
        codigo = _texto(cel(row, m.codigo))
        fonte = _texto(cel(row, m.fonte))
        unidade = _texto(cel(row, m.unidade))
        q_crua = cel(row, m.quantidade)
        erros_linha: list[ErroLinha] = []
        for campo, valor in (("item", numero), ("codigo", codigo), ("fonte", fonte)):
            if len(valor) > 40:
                erros_linha.append(ErroLinha(linha=n, campo=campo, mensagem="Passa de 40 caracteres."))
        if len(descricao) > 300:
            erros_linha.append(ErroLinha(linha=n, campo="nome", mensagem="Descrição passa de 300 caracteres."))
        if len(complemento) > MAX_COMPLEMENTO:
            erros_linha.append(ErroLinha(linha=n, campo="complemento",
                                         mensagem=f"Complemento passa de {MAX_COMPLEMENTO} caracteres."))
        if not unidade and "." in numero and not erros_linha and (
                valor := _valor_sem_unidade(cel(row, m.valor_total), cel(row, m.valor_unitario), q_crua)):
            # verba ou subtotal de grupo: só se sabe qual depois de ver se há linha embaixo dela (`_subtotais_e_verbas`)
            verbas[len(itens)] = _texto(q_crua) if _valor_sem_unidade(q_crua, None, None) else ""
            itens.append(ItemPrevia(ordem=len(itens), tipo="item", numero=numero, nivel=numero.count(".") + 1,
                                    etapa=numero.split(".")[0], codigo=codigo, fonte=fonte, descricao=descricao,
                                    unidade=VERBA, quantidade=UMA_VERBA, valor_unitario=valor, total=valor, linha=n,
                                    complemento=complemento))
            continue
        if not unidade and _vazio(q_crua):
            # título de etapa: sem unidade e sem quantidade
            if erros_linha:
                erros.extend(erros_linha)
                continue
            if numero and (valor := _valor_sem_unidade(cel(row, m.valor_total), None, None)):
                valor_titulo[len(itens)] = valor
            if numero:
                nivel = numero.count(".") + 1
                if nivel == 1:
                    etapa_atual = numero
            else:
                # sem número, a etapa é reconhecida pelo nome: é o que casa a "FUNDAÇÕES" do aditivo com a do contrato
                nivel = 1
                etapa_atual = _normal(descricao)[:60]
            itens.append(ItemPrevia(ordem=len(itens), tipo="etapa", numero=numero, nivel=nivel,
                                    etapa=numero.split(".")[0] if numero else etapa_atual,
                                    codigo=codigo, fonte=fonte, descricao=descricao, unidade="", quantidade=0,
                                    valor_unitario=0, total=0, linha=n))
            continue
        if not unidade:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem unidade"))
            continue
        if _vazio(q_crua):
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem quantidade"))
            continue
        if len(unidade) > 12:
            erros_linha.append(ErroLinha(linha=n, campo="unidade", mensagem="Unidade com mais de 12 caracteres."))
        quantidade = 0
        try:
            quantidade = _numero(q_crua, qtd)
        except (ValueError, ArithmeticError) as e:
            msg = str(e) if "ambíguo" in str(e) else f"'{_texto(q_crua)}' não é um número."
            erros_linha.append(ErroLinha(linha=n, campo="quantidade", mensagem=msg))
        if quantidade < 0 and tipo == TipoVersao.CONTRATO:
            erros_linha.append(ErroLinha(linha=n, campo="quantidade",
                                         mensagem="Quantidade negativa (supressão só vale em aditivo)."))
        unit = _preco(cel(row, m.valor_unitario), "valor_unitario", n, erros_linha)
        total_cel = _preco(cel(row, m.valor_total), "valor_total", n, erros_linha)
        if erros_linha:
            erros.extend(erros_linha)
            continue
        if quantidade == 0:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem quantidade"))
            continue
        # O total da linha manda: é o número que está no contrato, e recalcular quantidade × unitário arredondado
        # (unitário com 3 casas, como no SINAPI) faria a soma não bater com a planilha.
        if total_cel is not None:
            total = total_cel
            if quantidade < 0 and total > 0:
                total = -total            # supressão com total positivo na coluna: o sinal vem da quantidade
            if unit is None:
                unit = _arredonda(Decimal(abs(total)) * 1000 / abs(quantidade))
        elif unit is not None:
            total = _arredonda(Decimal(quantidade) * unit / 1000)
        else:
            unit, total = 0, 0
        # "CPU", "MERCADO", "COMPOSIÇÃO": código genérico (sem número) não identifica um serviço, e comparar as
        # unidades deles só gera aviso falso
        if codigo and any(c.isdigit() for c in codigo):
            chave = f"{fonte.casefold()}|{codigo.casefold()}"
            vista = unidade_do_codigo.get(chave)
            if vista is None:
                unidade_do_codigo[chave] = (unidade, n)
            elif vista[0].casefold() != unidade.casefold():
                # não dá para saber qual das duas está certa: o aviso aponta as duas linhas
                avisos.append(Aviso(linha=n, mensagem=f"O código {codigo} aparece em {vista[0]} (linha {vista[1]}) e "
                                                      f"em {unidade} (linha {n}): uma das duas unidades está errada."))
        nivel = numero.count(".") + 1 if numero else 2
        etapa = numero.split(".")[0] if "." in numero else etapa_atual
        itens.append(ItemPrevia(ordem=len(itens), tipo="item", numero=numero, nivel=nivel, etapa=etapa, codigo=codigo,
                                fonte=fonte, descricao=descricao, unidade=unidade, quantidade=quantidade,
                                valor_unitario=unit, total=total, linha=n, complemento=complemento))
    _subtotais_e_verbas(itens, verbas, valor_titulo, avisos)
    _detalhe_dos_grupos(itens)
    return itens, ignoradas, erros, avisos


def _detalhe_dos_grupos(itens: list[ItemPrevia]) -> None:
    """O item sem número logo abaixo do título de nível 2 ("1.1 Estacas" e os itens dele, sem numerar) é o detalhe do
    grupo: nível 3, e é o nível que o põe no tópico do título (`topicos_dos_itens`). O grupo fica decidido na leitura:
    ao somar, a linha solta da planilha que entra logo depois de um grupo do contrato não vira detalhe dele."""
    titulo = False
    for x in itens:
        if x.tipo != "item":
            titulo = bool(x.numero) and numero_do_topico(x.numero) == x.numero
        elif x.numero:
            titulo = False
        elif titulo:
            x.nivel = 3


def _valor_sem_unidade(total_cel, unit_cel, q_crua) -> int | None:
    """O valor de uma linha sem unidade (total da coluna; sem ela, quantidade × unitário), ou None. Célula que não é
    número não vira erro aqui: na linha de título ela sempre foi ignorada, e continua."""
    def ler(v, leitor):
        try:
            return None if _vazio(v) else _numero(v, leitor)
        except (ValueError, ArithmeticError):
            return None
    total = ler(total_cel, reais)
    if total is None and (unit := ler(unit_cel, reais)) is not None and (q := ler(q_crua, qtd)) is not None:
        total = _arredonda(Decimal(q) * unit / 1000)
    return total if total and total > 0 else None


def _subtotais_e_verbas(itens: list[ItemPrevia], verbas: dict[int, str], valor_titulo: dict[int, int],
                        avisos: list[Aviso]) -> None:
    """Linha numerada com linhas embaixo dela ("7.1" com "7.1.1") é título do grupo: o valor dela é subtotal, e somar
    junto com o das linhas contaria duas vezes. A que não tem linha embaixo e veio sem unidade é verba. Os dois casos
    ganham aviso na prévia, para a pessoa conferir."""
    com_filhos = {".".join(p[:k]) for x in itens if x.numero for p in [x.numero.split(".")] for k in range(1, len(p))}
    # a verba seguida de item sem número também é grupo (os itens dela é que não trazem a numeração): {posição: a soma
    # desses itens}. Só a linha sem unidade: o item com unidade seguido de item solto continua item, como sempre foi.
    soltos: dict[int, int] = {}
    for i in verbas:
        j, soma = i + 1, None
        while j < len(itens) and itens[j].tipo == "item" and not itens[j].numero:
            soma = (soma or 0) + itens[j].total
            j += 1
        if soma is not None:
            soltos[i] = soma
    for i, x in enumerate(itens):
        if i in soltos:
            valor_titulo[i] = x.total
            verbas.pop(i)
            itens[i] = x.model_copy(update={"tipo": "etapa", "etapa": x.numero.split(".")[0], "unidade": "",
                                            "quantidade": 0, "valor_unitario": 0, "total": 0})
            continue
        if x.tipo == "item" and x.numero in com_filhos:
            if x.total:
                valor_titulo[i] = x.total
            verbas.pop(i, None)
            itens[i] = x.model_copy(update={"tipo": "etapa", "etapa": x.numero.split(".")[0], "unidade": "",
                                            "quantidade": 0, "valor_unitario": 0, "total": 0})
    for i, valor in sorted(valor_titulo.items()):
        x = itens[i]
        nome = f"{x.numero} {x.descricao}"
        if "." in x.numero:
            linhas = [y.total for y in itens if y.tipo == "item" and y.numero.startswith(x.numero + ".")]
            linhas += [soltos[i]] if i in soltos else []
        else:
            linhas = [y.total for y in itens if y.tipo == "item" and y.etapa == x.etapa]
        if linhas:
            soma = sum(linhas)
            if soma != valor:
                avisos.append(Aviso(linha=x.linha, mensagem=f"{nome} diz {fmt_reais(valor)} e as linhas dele somam "
                                                            f"{fmt_reais(soma)}: vale a soma das linhas."))
        else:
            avisos.append(Aviso(linha=x.linha, mensagem=f"A etapa {nome} tem {fmt_reais(valor)} e nenhuma linha: o "
                                                        f"valor não entra no orçamento. Ponha o valor numa linha "
                                                        f"{x.numero}.1."))
    for i, q in sorted(verbas.items()):
        x = itens[i]
        texto = f"{x.numero} {x.descricao}: sem unidade, entra como verba (1 vb de {fmt_reais(x.total)})."
        if q:
            texto += f" A quantidade {q} ficou de fora: ponha a unidade na planilha para entrar por quantidade."
        avisos.append(Aviso(linha=x.linha, mensagem=texto))


def _preco(v, campo: str, n: int, erros: list[ErroLinha]) -> int | None:
    if _vazio(v):
        return None
    try:
        x = _numero(v, reais)
    except (ValueError, ArithmeticError) as e:
        msg = str(e) if "ambíguo" in str(e) else f"'{_texto(v)}' não é um número."
        erros.append(ErroLinha(linha=n, campo=campo, mensagem=msg))
        return None
    if x < 0:
        erros.append(ErroLinha(linha=n, campo=campo, mensagem="Preço negativo (supressão vai na quantidade)."))
        return None
    return x


def _totais_por_etapa(itens, descricoes_contrato: dict[str, str] | None = None) -> list[TotalEtapa]:
    descricoes: dict[str, str] = dict(descricoes_contrato or {})
    somas: dict[str, int] = {}
    for x in itens:
        if x.tipo == "etapa" and x.nivel == 1:
            descricoes.setdefault(x.etapa, x.descricao)
            if not descricoes_contrato:
                somas.setdefault(x.etapa, 0)
        elif x.tipo == "item":
            somas[x.etapa] = somas.get(x.etapa, 0) + x.total
    return [TotalEtapa(etapa=e, descricao=descricoes.get(e, "Sem etapa" if not e else f"Etapa {e}"), total=t)
            for e, t in somas.items()]


async def versoes_da_obra(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    vs = await leitor.consultar(Consulta("versoes_orcamento", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    return sorted(vs, key=lambda v: v["criado_em"], reverse=True)


def _contrato_vigente(versoes: list[dict]) -> dict | None:
    return next((v for v in versoes if v["ativa"] and v["tipo"] == TipoVersao.CONTRATO), None)


def id_versao(tenant_id: str, obra_id: str, chave: str) -> str:
    return id_derivado(tenant_id, "versao", obra_id, chave)


def _proximo_livre(tomadas: set[str]) -> str:
    return str(max((int(k) for k in tomadas if k.isdigit()), default=0) + 1)


def _encaixar(itens: list[ItemPrevia], existentes: list[dict], etapas_vigentes: dict[str, str],
              escolhidas: dict[int, str], *, somar: bool) -> dict[str, str]:
    """Põe cada linha da planilha (aditivo ou somar) na etapa do orçamento vigente onde ela conta, e devolve
    {etapa: descrição} das etapas vigentes, para os totais.

    A etapa casa pelo NOME: "1 SERVIÇOS PRELIMINARES" da planilha não é a "1 Estrutura" das pranchas só porque
    as duas são a etapa 1 (foi assim que Locação e Tapume caíram em Estrutura). Nome que não existe vira etapa nova,
    com o número da planilha se ele está livre, senão o próximo livre. No aditivo, item de etapa que não casou vai
    para a etapa do contrato onde está o mesmo código (a planilha de aditivo costuma vir numa etapa só, "A1
    ACRÉSCIMOS", e somar o tapume e o aço à parte esconderia o estouro da fundação); a pessoa pode apontar outra
    etapa na prévia."""
    descricoes = dict(etapas_vigentes)
    por_nome: dict[str, str] = {}
    for x in existentes:
        if x["tipo"] == "etapa" and x["nivel"] == 1:
            descricoes.setdefault(x["etapa"], x["descricao"])
            por_nome.setdefault(_normal(x["descricao"]), x["etapa"])
    usadas = set(descricoes) | {x["etapa"] for x in existentes}
    etapa_do_codigo: dict[str, str] = {}
    if not somar:
        for x in existentes:
            if x["tipo"] == "item" and x.get("codigo") and any(c.isdigit() for c in x["codigo"]):
                etapa_do_codigo.setdefault(f"{x.get('fonte', '').casefold()}|{x['codigo'].casefold()}", x["etapa"])

    titulos: dict[str, str] = {}
    for x in itens:
        if x.tipo == "etapa" and x.nivel == 1:
            titulos.setdefault(x.etapa, x.descricao)
    destino: dict[str, tuple[str, bool]] = {}          # etapa da planilha -> (etapa final, casou pelo nome?)
    tomadas = set(usadas)
    for x in itens:
        k = x.etapa
        if k in destino:
            continue
        nome = titulos.get(k)
        if nome and (alvo := por_nome.get(_normal(nome))) is not None:
            destino[k] = (alvo, True)
        elif not nome or not k:
            # sem título não há nome para comparar: a linha fica na chave que veio ("2.9" solto soma na etapa 2)
            destino[k] = (k, False)
        else:
            alvo = _proximo_livre(tomadas) if k in tomadas else k
            tomadas.add(alvo)
            destino[k] = (alvo, False)

    validas = usadas | {alvo for alvo, _ in destino.values()}
    for x in itens:
        alvo, pelo_nome = destino[x.etapa]
        x.etapa_planilha = x.etapa
        if x.tipo != "item":
            x.etapa = alvo
            continue
        if x.ordem in escolhidas:
            e = escolhidas[x.ordem]
            if e not in validas:
                if e not in destino:
                    raise ErroNegocio(f"A etapa {e} não existe no orçamento nem na planilha.")
                e = destino[e][0]
            x.etapa, x.etapa_origem = e, "escolhida"
        elif pelo_nome:
            x.etapa, x.etapa_origem = alvo, "nome" if alvo != x.etapa else "planilha"
        elif alvo not in usadas and (e := etapa_do_codigo.get(f"{x.fonte.casefold()}|{x.codigo.casefold()}")):
            x.etapa, x.etapa_origem = e, "codigo"
        else:
            x.etapa = alvo
        if not x.numero and x.nivel > 2 and x.etapa != alvo:
            x.nivel = 2

    # título de etapa que ficou sem item (todos foram para outras etapas) não vira etapa vazia; no somar, o título de
    # etapa que já existe também sai, porque o novo contrato já tem o dele
    com_item = {x.etapa for x in itens if x.tipo == "item"}
    # a `ordem` fica a da leitura (com buracos): é a chave da escolha na prévia (e do ID do item, fora do somar)
    itens[:] = [x for x in itens if x.tipo == "item" or x.nivel > 1
                or (x.etapa in com_item and not (somar and x.etapa in usadas))]
    _renumerar(itens, existentes, usadas, destino, somar=somar)
    return descricoes


def _renumerar(itens: list[ItemPrevia], existentes: list[dict], usadas: set[str],
               destino: dict[str, tuple[str, bool]], *, somar: bool) -> None:
    """Numera as linhas que mudaram de etapa na sequência da etapa nova, sem repetir número que já está nela.

    Etapa que mudou de número inteira só troca o prefixo ("1.2" -> "4.2"). No somar, linha que entra em etapa que já
    existe continua a numeração dela (2.3, 2.4…), porque o resultado é um contrato só. No aditivo, quem fica na mesma
    etapa ou foi pelo código/escolha guarda o número da planilha: ele aponta para o item do contrato que o aditivo
    acresce ou suprime."""
    numeradas = {x["etapa"] for x in existentes if x.get("numero")}
    maior: dict[str, int] = {}

    def conta(etapa: str, numero: str) -> None:
        p = numero.split(".")
        if len(p) > 1 and p[1].isdigit():
            maior[etapa] = max(maior.get(etapa, 0), int(p[1]))

    for x in existentes:
        if x.get("numero"):
            conta(x["etapa"], x["numero"])
    prefixo: list[ItemPrevia] = []                     # a etapa inteira veio para cá com outro número
    seguir: list[ItemPrevia] = []                      # entra numa etapa que não é a dela: continua a numeração
    for x in itens:
        origem, alvo = x.etapa_planilha, x.etapa
        da_etapa = alvo not in usadas and destino.get(origem, ("",))[0] == alvo
        if x.numero and da_etapa:
            numeradas.add(alvo)
        if not x.numero:
            continue
        if da_etapa:
            if alvo != origem:
                prefixo.append(x)
            else:
                conta(alvo, x.numero)
        elif alvo not in usadas or somar:
            # etapa nova de outra linha da planilha (escolha) ou, no somar, qualquer etapa que já existe
            seguir.append(x)
        elif alvo != origem and x.etapa_origem in ("planilha", "nome"):
            seguir.append(x)
        else:
            conta(alvo, x.numero)          # aditivo na etapa do contrato: o número aponta o item que ele mexe
    # os números trocados por prefixo ocupam a etapa antes da sequência: senão o item escolhido para ela repetiria
    for x in prefixo:
        if _sem_numero(x, numeradas):
            continue
        p = x.numero.split(".")
        x.numero = x.etapa if x.tipo == "etapa" and x.nivel == 1 else ".".join([x.etapa, *p[1:]])
        conta(x.etapa, x.numero)
    sequencia: dict[tuple[str, str, str], str] = {}
    for x in seguir:
        if _sem_numero(x, numeradas):
            continue
        origem, alvo = x.etapa_planilha, x.etapa
        if x.tipo == "etapa" and x.nivel == 1:
            x.numero = alvo
            continue
        p = x.numero.split(".")
        seg, resto = (p[1], p[2:]) if len(p) > 1 else (p[0], [])
        chave = (alvo, origem, seg)
        if chave not in sequencia:
            maior[alvo] = maior.get(alvo, 0) + 1
            sequencia[chave] = str(maior[alvo])
        novo = [alvo, sequencia[chave], *resto]
        x.numero = ".".join(novo)
        x.nivel = len(novo)


def _sem_numero(x: ItemPrevia, numeradas: set[str]) -> bool:
    """Etapa sem numeração (chave pelo nome): o número da planilha só confundiria, e a linha fica sem."""
    if x.etapa in numeradas:
        return False
    x.numero = ""
    return True


_TERMOS = re.compile(r"[a-z]+|\d+")


def _termos(descricao: str) -> list[str]:
    return _TERMOS.findall(_normal(descricao))


def _parecidos(a: list[str], b: list[str]) -> bool:
    """O mesmo serviço escrito de outro jeito: "Concreto fck 25 sapatas" × "Concreto usinado fck 25", "Aço CA-50 10 mm"
    × "Aço CA-50". Pede o mesmo termo principal (a 1ª palavra) e um número em comum, sem número que desdiga ("fck 25" ×
    "fck 30" não). Melhor deixar passar um repetido do que acusar "Forma de madeira" × "Porta de madeira": o aviso que
    erra muito deixa de ser lido."""
    if not a or not b:
        return False
    if a == b:
        return True
    if a[0] != b[0] or len(a[0]) < 3 or a[0].isdigit():
        return False
    na, nb = {t for t in a if t.isdigit()}, {t for t in b if t.isdigit()}
    return bool(na & nb) and (na <= nb or nb <= na)


def _marcar_repetidos(itens: list[ItemPrevia], existentes: list[dict], avisos: list[Aviso]) -> None:
    """Item da planilha que parece já estar no orçamento vigente ganha `repetido_de` e um aviso: somar o concreto da
    planilha com o das pranchas conta duas vezes, e o orçado (e o cronograma) dobram. Só avisa; quem
    decide é a pessoa (`ignorar_itens`)."""
    vigentes = [x for x in existentes if x["tipo"] == "item" and x["quantidade"] > 0]
    por_codigo: dict[str, dict] = {}
    for x in vigentes:
        if x.get("codigo") and any(c.isdigit() for c in x["codigo"]):
            por_codigo.setdefault(f"{x.get('fonte', '').casefold()}|{x['codigo'].casefold()}", x)
    termos = [(x, _termos(x["descricao"]), x["unidade"].strip().casefold()) for x in vigentes]
    for x in itens:
        if x.tipo != "item" or x.quantidade < 0:
            continue                       # supressão aponta de propósito para um item que já existe
        achado = por_codigo.get(f"{x.fonte.casefold()}|{x.codigo.casefold()}") if x.codigo else None
        if achado is None:
            meus, un = _termos(x.descricao), x.unidade.strip().casefold()
            achado = next((e for e, t, u in termos if u == un and _parecidos(meus, t)), None)
        if achado is None:
            continue
        x.repetido_de = RepetidoDe(item_id=achado["id"], descricao=achado["descricao"],
                                   quantidade=achado["quantidade"], unidade=achado["unidade"])
        avisos.append(Aviso(linha=x.linha, mensagem=f"{x.descricao} parece repetir {achado['descricao']} "
                                                    f"({fmt_qtd(achado['quantidade'])} {achado['unidade']}), que já "
                                                    "está no orçamento."))


# --- custo × venda e o encaixe na lista padrão de etapas, na importação ---

# o serviço que a previsão da obra estimou para uma fase sem orçamento: conta no cronograma, não no orçado
FONTE_ESTIMATIVA = "estimativa"


def _para_custo(itens: list[ItemPrevia], bdi: int) -> None:
    """Planilha de venda: cada item guarda o preço do cliente à parte e fica com o custo (a conta é do domínio)."""
    for x in itens:
        if x.tipo == "item":
            x.valor_unitario_venda, x.total_venda = x.valor_unitario, x.total
            x.valor_unitario, x.total = custo_da_venda(x.valor_unitario, bdi), custo_da_venda(x.total, bdi)


def _mesmo_nome(titulo: str, chave: str) -> bool:
    """O título da planilha é o nome da etapa padrão ("ALVENARIA" é "Alvenaria e vedações", "FUNDAÇÕES" é "Fundação"):
    compara sem acento, o nome inteiro ou o pedaço antes do "e", no singular."""
    def sing(t: str) -> str:
        t = normalizar_nome(t)
        return t[:-3] + "ao" if t.endswith("oes") else t[:-1] if t.endswith("s") else t
    nome = nome_da_etapa(chave)
    return sing(titulo) in {sing(nome), sing(re.split(r"\s+e\s+|,", normalizar_nome(nome))[0])}


def _grupo(titulo: str, chave: str) -> str:
    """A etapa da planilha vira o tópico do item dentro da etapa padrão ("Esquadrias › ESQUADRIA MADEIRA"), a não ser
    que ela já seja a etapa padrão ("ALVENARIA" em Alvenaria): aí o tópico continua saindo da numeração, como na
    planilha que não precisou de encaixe."""
    t = " ".join(titulo.split())
    return "" if not t or _mesmo_nome(t, chave) else t[:MAX_NOME]


def _titulo_da_etapa(chave: str, ordem: int = 0) -> ItemPrevia:
    return ItemPrevia(ordem=ordem, tipo="etapa", numero=str(ordem_da_etapa(chave) + 1), nivel=1, etapa=chave,
                      codigo="", fonte="", descricao=nome_da_etapa(chave), unidade="", quantidade=0,
                      valor_unitario=0, total=0, linha=0)


def _padronizar_previa(itens: list[ItemPrevia], escolhas: dict[str, str]) -> tuple[list[ItemPrevia],
                                                                                   list[EncaixeEtapa]]:
    """Encaixa cada linha da planilha numa etapa da lista padrão (o nosso modelo de orçamento) e devolve as linhas já
    na ordem da lista, com o título de cada etapa, e o encaixe de cada etapa da planilha para a prévia.

    A etapa de cada item vem do domínio, sabendo a etapa da planilha em que ele está (`etapa_no_contexto`). `escolhas`
    é o que a pessoa trocou na prévia: {etapa da planilha: etapa padrão} para a etapa inteira, {"linha:N": etapa
    padrão} para uma linha (N = a linha da planilha); a linha vale sobre a etapa."""
    for k in escolhas.values():
        if k not in CHAVES_FIXAS:
            raise ErroNegocio(f"Etapa desconhecida: {k}. Use uma da lista padrão.")
    titulo = {x.etapa: x.descricao for x in itens if x.tipo == "etapa" and x.nivel == 1}
    destino: dict[int, str] = {}
    for i, x in enumerate(itens):
        if x.tipo == "item":
            destino[i] = (escolhas.get(f"linha:{x.linha}") or escolhas.get(x.etapa)
                          or etapa_no_contexto(x.descricao, titulo.get(x.etapa, "")))
    # o subtítulo (nível 2 em diante) vai com o primeiro item abaixo dele na mesma etapa da planilha
    for i, x in enumerate(itens):
        if x.tipo == "etapa" and x.nivel > 1:
            abaixo = next((destino[j] for j in range(i + 1, len(itens))
                           if j in destino and itens[j].etapa == x.etapa), None)
            destino[i] = (abaixo or escolhas.get(x.etapa) or etapa_pelo_nome(titulo.get(x.etapa, "")) or OUTROS)
    por_etapa: dict[str, dict[str, list[int]]] = {}
    for i, x in enumerate(itens):
        if x.tipo == "item":
            conta = por_etapa.setdefault(x.etapa, {}).setdefault(destino[i], [0, 0])
            conta[0] += x.total
            conta[1] += 1
    encaixe = []
    for e, por in por_etapa.items():
        # a etapa padrão com mais valor; no empate, a que vem antes na lista
        principal = min(por, key=lambda k: (-por[k][0], ordem_da_etapa(k)))
        encaixe.append(EncaixeEtapa(
            etapa_planilha=e, descricao=titulo.get(e) or (f"Etapa {e}" if e else "Sem etapa"), destino=principal,
            destino_nome=nome_da_etapa(principal), escolhida=e in escolhas, total=sum(v[0] for v in por.values()),
            etapas=[DestinoDoEncaixe(etapa=k, nome=nome_da_etapa(k), total=v[0], itens=v[1])
                    for k, v in sorted(por.items(), key=lambda kv: ordem_da_etapa(kv[0]))]))
    linhas: list[ItemPrevia] = []
    for k in sorted(set(destino.values()), key=ordem_da_etapa):
        linhas.append(_titulo_da_etapa(k))
        linhas.extend(x.model_copy(update={"etapa": k, "etapa_planilha": x.etapa,
                                           "grupo": _grupo(titulo.get(x.etapa, ""), k)})
                      for i, x in enumerate(itens) if destino.get(i) == k)
    for n, x in enumerate(linhas):
        x.ordem = n
    return linhas, encaixe


def _aditivo_padronizado(itens: list[ItemPrevia], existentes: list[dict],
                         etapas_vigentes: dict[str, str]) -> list[ItemPrevia]:
    """O aditivo de um orçamento na lista padrão, depois de `_encaixar`: a linha que casou com o contrato (pelo código,
    pelo nome ou pela escolha) fica na etapa dele e no mesmo tópico (o `grupo` do item do contrato com o mesmo código);
    a que não casou é encaixada na lista como na importação. As linhas saem na ordem da lista, cada etapa com o seu
    título."""
    grupo_do_codigo: dict[str, str] = {}
    for x in existentes:
        if x["tipo"] == "item" and x.get("codigo"):
            grupo_do_codigo.setdefault(f"{x.get('fonte', '').casefold()}|{x['codigo'].casefold()}", x.get("grupo", ""))
    titulo = {x.etapa_planilha: x.descricao for x in itens if x.tipo == "etapa" and x.nivel == 1}
    for x in itens:
        if x.tipo != "item":
            continue
        da_planilha = titulo.get(x.etapa_planilha, "")
        if x.etapa in etapas_vigentes and x.etapa in CHAVES_FIXAS:
            x.grupo = (grupo_do_codigo.get(f"{x.fonte.casefold()}|{x.codigo.casefold()}", "")
                       if x.etapa_origem == "codigo" else _grupo(da_planilha, x.etapa))
        else:
            k = etapa_no_contexto(x.descricao, da_planilha)
            x.etapa, x.grupo = k, _grupo(da_planilha, k)
    # a `ordem` da leitura continua (é a chave de `etapas_itens` na prévia); o título novo pega uma que não colide
    livre = max((x.ordem for x in itens), default=0) + 1
    linhas: list[ItemPrevia] = []
    for k in sorted({x.etapa for x in itens if x.tipo == "item"}, key=ordem_da_etapa):
        linhas.append(_titulo_da_etapa(k, livre))
        livre += 1
        linhas.extend(x for x in itens if x.tipo == "item" and x.etapa == k)
    return linhas

async def importar(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str, m: Mapeamento,
                   tipo: TipoVersao, descricao: str, data: str | None, aplicar: bool,
                   chave: str | None, escolhidas: dict[int, str] | None = None, somar: bool = False,
                   ignorar: list[int] | None = None, *, padronizar: bool = False, natureza: str = "custo",
                   bdi: int | None = None, destinos: dict[str, str] | None = None) -> tuple[PreviaVersao, bool]:
    """Devolve (prévia ou versão gravada, criada agora?).

    `somar` (contrato, antes de aprovar): grava um contrato novo com os itens do vigente mais os da planilha, no lugar
    do vigente. É o "completar o orçamento" de quem trouxe as pranchas e agora traz a planilha: aditivo é documento com
    o cliente depois do contrato fechado, e chamar o complemento de aditivo inflava o % de aditivo.

    `padronizar` (contrato novo): encaixa cada linha numa etapa da lista padrão já na prévia (`encaixe`), e a pessoa
    troca o que estiver errado em `destinos`. `natureza="venda"`: a planilha é o preço do cliente; o custo de cada
    linha sai com o `bdi` (o da obra, quando não vem)."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    if natureza not in ("custo", "venda"):
        raise ErroNegocio("A planilha é de custo ou de venda.")
    bdi_ = (o.get("bdi") or 0) if bdi is None else bdi
    if natureza == "venda" and bdi_ <= 0:
        raise ErroNegocio("Diga o BDI da planilha de venda: é com ele que sai o custo de cada linha.")
    destinos = destinos or {}
    if m.nome is None:
        raise ErroNegocio("Diga qual coluna é a descrição.")
    if m.quantidade is None and m.valor_total is None:
        # a planilha de custo feita à mão pode não ter quantidade (só verbas), mas sem as duas não há valor nenhum
        raise ErroNegocio("Diga qual coluna é a quantidade ou o valor total.")
    if aplicar and not chave:
        raise ErroNegocio("Falta a chave de idempotência.")
    escolhidas = escolhidas or {}
    ignorar_ = sorted(set(ignorar or ()))
    # o sufixo só aparece com somar/ignorar: a assinatura de quem não usa nenhum dos dois fica a de antes
    extra = (("|somar" if somar else "") + (f"|ignorar={ignorar_}" if ignorar_ else "")
             + (f"|venda={bdi_}" if natureza == "venda" else "")
             + (f"|padronizar={sorted(destinos.items())}" if padronizar else ""))
    assinatura = hashlib.sha256(conteudo + f"|{tipo.value}|{m.model_dump_json()}|{sorted(escolhidas.items())}{extra}"
                                .encode()).hexdigest()
    id_ = id_versao(u.tenant_id, obra_id, chave) if chave else None
    if aplicar:
        ja = await arm.obter("versoes_orcamento", u.tenant_id, id_)
        if ja:
            if ja["assinatura"] != assinatura:
                raise Conflito("Esta chave de idempotência já foi usada para outra importação.")
            return await _previa_gravada(arm, u, ja), False

    _, _, grade = _grade(conteudo, nome_arquivo, m.planilha)
    itens, ignoradas, erros, avisos = _ler(grade, m, tipo)
    if m.unidade is None:
        avisos.insert(0, Aviso(linha=0, mensagem="Sem a coluna da unidade, as linhas numeradas com valor entram como "
                                                 "verba (vb) e as outras ficam de fora."))
    versoes = await versoes_da_obra(arm, u.tenant_id, obra_id)
    vigente = _contrato_vigente(versoes)
    recusar_se_desfazendo(o)
    aprovada = o["status"] != "em_orcamento"
    if somar:
        if tipo != TipoVersao.CONTRATO:
            raise ErroNegocio("Somar vale para o orçamento, não para o aditivo.")
        if aprovada:
            raise ErroNegocio("O orçamento já foi aprovado: para acrescentar itens, lance um aditivo.")
        if not vigente:
            raise ErroNegocio("Ainda não há orçamento para somar; importe a planilha como o orçamento da obra.")
    if tipo == TipoVersao.CONTRATO and aprovada and (vigente or o.get("aprovada")):
        # o cronograma e os gastos seguem o orçamento aprovado: um contrato novo os deixaria descolados dele
        raise Conflito("O orçamento já foi aprovado: desfaça a aprovação ou lance um aditivo." if vigente
                       else "O orçamento já foi aprovado: desfaça a aprovação para importar o contrato.")
    if ignorar_:
        tirar = set(ignorar_)
        itens = [x for x in itens if not (x.tipo == "item" and x.ordem in tirar)]
    if natureza == "venda":
        _para_custo(itens, bdi_)
    na_lista = bool(vigente and vigente.get("padronizada"))
    if somar and na_lista:
        # as etapas da planilha ("7 PAREDES") não casam com as da lista padrão: importe tudo de novo
        raise ErroNegocio("Este orçamento está nas etapas padrão: importe a planilha inteira de novo (ela substitui "
                          "a atual) ou lance um aditivo.")
    if padronizar and (somar or tipo != TipoVersao.CONTRATO):
        raise ErroNegocio("Encaixar nas etapas padrão vale para o orçamento novo; o aditivo de um orçamento nas "
                          "etapas padrão já cai nelas.")
    encaixe: list[EncaixeEtapa] = []
    if padronizar:
        itens, encaixe = _padronizar_previa(itens, destinos)
    descricoes_contrato = None
    existentes: list[dict] = []
    if (tipo == TipoVersao.ADITIVO or somar) and vigente:
        ativas = sorted((v for v in versoes if v["ativa"]),
                        key=lambda v: (v["tipo"] != TipoVersao.CONTRATO, v["numero"]))
        existentes = await itens_das_versoes(arm, u.tenant_id, obra_id, ativas)
        etapas_vigentes: dict[str, str] = {}
        for v in ativas:
            for e in v.get("lista_etapas", []):
                etapas_vigentes.setdefault(e["etapa"], e["descricao"])
        descricoes_contrato = _encaixar(itens, existentes, etapas_vigentes, escolhidas, somar=somar)
        if na_lista:
            itens = _aditivo_padronizado(itens, existentes, etapas_vigentes)
            descricoes_contrato.update({x.etapa: x.descricao for x in itens if x.tipo == "etapa" and x.nivel == 1})
        _marcar_repetidos(itens, existentes, avisos)
    so_itens = [x for x in itens if x.tipo == "item"]
    previa = PreviaVersao(
        aplicado=False, tipo=tipo, itens=itens, ignoradas=ignoradas, erros=erros, avisos=avisos,
        total=sum(x.total for x in so_itens), n_itens=len(so_itens),
        n_etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in itens),
        etapas=_totais_por_etapa(itens, descricoes_contrato),
        substitui=(Substituida(versao_id=vigente["id"], total=vigente["total"], itens=vigente["itens"])
                   if vigente and tipo == TipoVersao.CONTRATO else None),
        padronizada=padronizar, encaixe=encaixe,
        etapas_disponiveis=[EtapaRef(etapa=e.chave, descricao=e.nome) for e in ETAPAS_FIXAS] if padronizar else [],
        natureza=natureza, bdi=bdi_ if natureza == "venda" else 0,
        total_venda=sum(x.total_venda or 0 for x in so_itens) if natureza == "venda" else None)
    if somar:
        previa.total_apos = vigente["total"] + previa.total
    if tipo == TipoVersao.ADITIVO and not vigente:
        raise ErroNegocio("Importe o orçamento contratado antes do aditivo.")
    linhas = _somadas(existentes, vigente, itens) if somar else itens
    manuais = await topicos_manuais(arm, u.tenant_id, obra_id)
    herdados = None
    if tipo == TipoVersao.ADITIVO:
        herdados = _topicos_por_numero(existentes, *await topicos_das_versoes(arm, u.tenant_id, obra_id, ativas,
                                                                              existentes))
    lista_topicos = _com_topicos(u.tenant_id, obra_id, linhas, manuais, previa.avisos, herdados)
    # no somar, a prévia mostra só o que entra (os tópicos que os itens da planilha tocam)
    previa.topicos = _topicos_da_previa(itens, lista_topicos)
    renumeradas: dict[str, str] = {}
    if somar and (ordem := _pranchas_depois_das_preliminares(vigente, linhas)):
        if _chaves_que_mudam(linhas, ordem) & await etapas_em_uso(arm, u.tenant_id, obra_id):
            ordem = None       # renumerar levaria o gasto lançado numa etapa para outra
        else:
            renumeradas = _renumeradas_no_desfazer(linhas, ordem, {e["etapa"] for e in vigente["lista_etapas"]})
            nomes = _nomes_das_etapas(linhas)
            pranchas = vigente["lista_etapas"][0]["etapa"]
            antes_dela = ordem[ordem.index(pranchas) - 1]
            linhas = _reordenar(linhas, ordem)
            antes, lista_topicos = lista_topicos, _com_topicos(u.tenant_id, obra_id, linhas, manuais)
            previa.topicos = _topicos_da_previa(
                itens, _topicos_renumerados(itens, antes, lista_topicos, _renumeradas(ordem)))
            # a prévia continua com as chaves de antes (são elas que valem na escolha de etapa, `etapas_itens`); o
            # número com que cada etapa é gravada vai à parte, para a tela mostrar o mesmo que o orçamento terá
            previa.numeracao = {k: n for k, n in _renumeradas(ordem).items() if n != k}
            previa.avisos.append(Aviso(linha=0, mensagem=(
                f"Ao somar, {nomes.get(pranchas, 'a etapa')} (das pranchas) vai para depois de "
                f"{nomes.get(antes_dela, 'os serviços preliminares')}, e as etapas são renumeradas nessa ordem.")))
    if len(linhas) > MAX_LINHAS:
        raise ErroNegocio(f"Somado, o orçamento passaria de {MAX_LINHAS} linhas; troque o orçamento inteiro.")
    if not aplicar or erros:
        return previa, False
    if not so_itens:
        raise ErroNegocio("Nenhum item para importar.")
    so_linhas = [x for x in linhas if x.tipo == "item"]
    numero = 0
    if tipo == TipoVersao.ADITIVO:
        numero = max((v["numero"] for v in versoes if v["ativa"] and v["tipo"] == TipoVersao.ADITIVO), default=0) + 1
    versao = VersaoOrcamento(
        id=id_, tenant_id=u.tenant_id, obra_id=obra_id, tipo=tipo, numero=numero, descricao=descricao.strip(),
        data=data, arquivo=(nome_arquivo or "")[:200], itens=len(so_linhas),
        etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in linhas), total=sum(x.total for x in so_linhas),
        substitui=previa.substitui.versao_id if previa.substitui else None,
        contrato_id=vigente["id"] if tipo == TipoVersao.ADITIVO else None, assinatura=assinatura,
        lista_etapas=[{"etapa": e.etapa, "descricao": e.descricao}
                      for e in (_totais_por_etapa(linhas) if somar else previa.etapas) if e.etapa],
        lista_topicos=lista_topicos, natureza=natureza,
        bdi=bdi_ if natureza == "venda" else 0, padronizada=padronizar or na_lista)
    # no somar a `ordem` é a posição no contrato somado (o item novo entra no fim da etapa dele)
    await _gravar_itens(arm, u, obra_id, id_, linhas, por_posicao=somar)

    async def ativar(tx):
        if (await obra_do_tenant(tx, u.tenant_id, obra_id))["status"] != o["status"]:
            raise Conflito("A situação da obra mudou enquanto você importava; envie a planilha de novo.")
        if await tx.obter("versoes_orcamento", u.tenant_id, id_):
            raise Conflito("Esta importação já foi gravada.")
        anterior = await tx.obter("versoes_orcamento", u.tenant_id, versao.substitui) if versao.substitui else None
        contrato = await tx.obter("versoes_orcamento", u.tenant_id, versao.contrato_id) if versao.contrato_id else None
        if (anterior is not None and not anterior["ativa"]) or (contrato is not None and not contrato["ativa"]):
            raise Conflito("O contrato mudou enquanto você importava; envie a planilha de novo.")
        if somar and any(anterior.get(k, 0) != vigente.get(k, 0) for k in ("total", "edicoes", "aditivos")):
            # os itens copiados são os de antes da edição: gravar perderia o que foi mudado no meio
            raise Conflito("O orçamento mudou enquanto você importava; envie a planilha de novo.")

        # `origem` fica só no documento (fora do modelo do domínio): é o que a tela usa para dizer o que o Desfazer faz
        d_versao = {**doc(versao), **({"origem": "somar"} if somar else {})}

        def aplicar_():
            tx.criar("versoes_orcamento", d_versao)
            if anterior is not None:
                tx.atualizar("versoes_orcamento", u.tenant_id, anterior["id"], {"ativa": False})
            if contrato is not None:
                tx.atualizar("versoes_orcamento", u.tenant_id, contrato["id"], {"aditivos": contrato["aditivos"] + 1})
            evento(tx, u.tenant_id, "orcamento.versao", obra_id,
                   {"versao_id": id_, "tipo": tipo.value, "total": versao.total})
            return d_versao

        if somar:
            n = previa.n_itens
            texto = (f"Somou {n} {'item' if n == 1 else 'itens'} ao orçamento de {o['nome']} "
                     f"(+{fmt_reais(previa.total)})")
        elif tipo == TipoVersao.CONTRATO:
            texto = (f"Importou o contrato de {o['nome']} ({versao.itens} itens, {fmt_reais(versao.total)})"
                     + (" — substitui o anterior" if anterior else ""))
        else:
            sinal = "+" if versao.total >= 0 else "−"
            texto = (f"Importou o {_ordinal(numero)} aditivo de {o['nome']}"
                     + (f": {versao.descricao}" if versao.descricao else "")
                     + f" ({sinal}{fmt_reais(abs(versao.total))})")
        op = Operacao(obra_id=obra_id, aplicar=aplicar_, texto=lambda d: texto,
                      desfazer=lambda d: {
                          "op": "desligar_versao", "versao_id": id_, "reativar": anterior["id"] if anterior else None,
                          **({"somou": True} if somar else {}),
                          **({"renumeradas": renumeradas} if renumeradas else {})})
        _, entrada = executar_e_registrar(tx, u, op, registro_id=id_derivado(u.tenant_id, "registro", id_))
        return entrada

    entrada = await arm.em_transacao(ativar)
    return previa.model_copy(update={"aplicado": True, "versao_id": id_, "registro_id": entrada["id"]}), True


# Etapa que na obra vem antes de tudo: locação, tapume, barracão. As pranchas trazem a Estrutura, que vem depois dela
# e depois das fundações ("infraestrutura" é o nome de orçamento para elas).
PRELIMINARES = re.compile(r"\b(preliminar(es)?|canteiro)\b")
FUNDACOES = re.compile(r"\b(fundac(ao|oes)|infra ?estrutura)\b")


def _ordem_das_etapas(linhas) -> list[str]:
    """As chaves de etapa na ordem em que aparecem (sem a linha solta, de etapa vazia)."""
    vistas: dict[str, None] = {}
    for x in linhas:
        if x.etapa:
            vistas.setdefault(x.etapa, None)
    return list(vistas)


def _nomes_das_etapas(linhas) -> dict[str, str]:
    return {x.etapa: x.descricao for x in reversed(linhas) if x.tipo == "etapa" and x.nivel == 1}


def _renumeradas(ordem: list[str]) -> dict[str, str]:
    """{chave de antes: chave nova}. Só renumera quando todas são números: etapa sem numeração (chave pelo nome) só
    muda de lugar."""
    if not all(k.isdigit() for k in ordem):
        return {k: k for k in ordem}
    return {k: str(i) for i, k in enumerate(ordem, start=1)}


def _chaves_que_mudam(linhas, ordem: list[str]) -> set[str]:
    return {k for k, n in _renumeradas(ordem).items() if n != k} & set(_ordem_das_etapas(linhas))


def _renumeradas_no_desfazer(linhas, ordem: list[str], chaves_antes: set[str]) -> dict[str, str]:
    """{chave: etapa que ela passa a ser} das chaves que o contrato de antes também tem, com outro sentido. É o que o
    Desfazer não pode devolver calado: o gasto lançado na "1" depois da mudança iria para a "1" de antes. Chave que
    só o contrato novo tem não entra: desfeito, o lançamento nela aparece "sem etapa do orçamento", à vista."""
    nomes = _nomes_das_etapas(linhas)
    return {n: nomes.get(k, "") for k, n in _renumeradas(ordem).items() if n != k and n in chaves_antes}


async def recusar_se_renumeradas_em_uso(leitor, tenant_id: str, obra_id: str, renumeradas: dict[str, str]) -> None:
    """Antes do Desfazer de uma mudança de ordem (fora da transação, porque consulta)."""
    if presas := sorted(set(renumeradas) & await etapas_em_uso(leitor, tenant_id, obra_id)):
        raise Conflito("Já há lançamento (gasto, tópico ou cronograma) na etapa "
                       f"{', '.join(f'{k} {renumeradas[k]}'.strip() for k in presas)} depois da mudança de ordem: "
                       "desfazer levaria o lançamento para outra etapa. Mude a etapa dele antes de desfazer.")


def _reordenar(linhas: list[ItemPrevia], ordem: list[str]) -> list[ItemPrevia]:
    """As linhas com as etapas na `ordem` dada, renumeradas 1..N (o número do item troca só o prefixo: "1.2" na etapa
    que virou a 2 fica "2.2"). Linha sem etapa fica no fim, como estava. Preço, quantidade e edição vêm junto."""
    nova = _renumeradas(ordem)
    posicao = {k: i for i, k in enumerate(ordem)}
    saida: list[ItemPrevia] = []
    # sorted é estável: dentro da etapa, os itens ficam na ordem que tinham
    for x in sorted(linhas, key=lambda x: posicao.get(x.etapa, len(ordem))):
        y = x.model_copy()
        if y.etapa in nova:
            antes, y.etapa = y.etapa, nova[y.etapa]
            p = y.numero.split(".")
            if y.numero and p[0] == antes:
                y.numero = ".".join([y.etapa, *p[1:]])
        saida.append(y)
    return saida


def _pranchas_depois_das_preliminares(vigente: dict, linhas: list[ItemPrevia]) -> list[str] | None:
    """No somar da planilha sobre o orçamento só das pranchas, a ordem em que a etapa das pranchas fica depois das
    fundações da planilha (da última, quando vêm em sequência: "Fundações - estacas", "Fundações - blocos") ou, sem
    elas, da primeira etapa de serviços preliminares (ou canteiro); None quando não há o que mudar. Sem etapa assim,
    a das pranchas fica onde está: adivinhar a ordem da obra por outros nomes erraria mais do que acertaria."""
    if not (vigente.get("arquivo") or "").endswith(SUFIXO_PROJETO) or len(vigente.get("lista_etapas") or []) != 1:
        return None
    pranchas = vigente["lista_etapas"][0]["etapa"]
    ordem = _ordem_das_etapas(linhas)
    nomes = _nomes_das_etapas(linhas)
    if pranchas not in ordem:
        return None
    nova = [k for k in ordem if k != pranchas]
    eh = [(FUNDACOES.search(n) is not None, PRELIMINARES.search(n) is not None)
          for n in (_normal(nomes.get(k, "")) for k in nova)]
    if (i := next((i for i, (f, _) in enumerate(eh) if f), None)) is not None:
        while i + 1 < len(nova) and eh[i + 1][0]:
            i += 1
    elif (i := next((i for i, (_, p) in enumerate(eh) if p), None)) is None:
        return None
    nova.insert(i + 1, pranchas)
    return nova if nova != ordem else None


async def etapas_em_uso(leitor, tenant_id: str, obra_id: str) -> set[str]:
    """As chaves de etapa que algum lançamento da obra guarda (gasto, tópico, cronograma).
    Renumerar uma delas levaria o lançamento, calado, para outra etapa."""
    from .cronograma import id_cronograma

    def q(colecao: str, *filtros: Filtro):
        return leitor.consultar(Consulta(colecao, tenant_id, [Filtro("obra_id", "==", obra_id), *filtros]))

    usadas = {g.get("etapa") for g in await q("gastos", Filtro("estornado", "==", False))}
    usadas |= {x.get("etapa") for x in await q("topicos")}
    if c := await leitor.obter("cronogramas", tenant_id, id_cronograma(tenant_id, obra_id)):
        usadas |= {e["etapa"] for e in c.get("etapas", [])} | set(c.get("acumulado") or {})
    usadas.discard(None)
    return usadas


async def _gravar_itens(arm, u: UsuarioAtual, obra_id: str, versao_id: str, linhas: list[ItemPrevia], *,
                        por_posicao: bool) -> None:
    """Itens primeiro, ligados a uma versão que ainda não existe: ninguém os enxerga até a última transação criar a
    versão. Se o processo cair no meio, sobram itens órfãos invisíveis, e a mesma chave regrava os mesmos IDs."""
    docs = [doc(ItemOrcamento(id=id_derivado(u.tenant_id, "item-orc", versao_id, str(ordem)), tenant_id=u.tenant_id,
                              obra_id=obra_id, versao_id=versao_id, ordem=ordem,
                              **x.model_dump(exclude={"ordem", "repetido_de"})))
            for ordem, x in ((k if por_posicao else x.ordem, x) for k, x in enumerate(linhas))]
    for i in range(0, len(docs), ITENS_POR_TRANSACAO):
        lote = docs[i:i + ITENS_POR_TRANSACAO]

        async def gravar(tx, lote=lote):
            for d in lote:
                tx.salvar("itens_orcamento", d)
        await arm.em_transacao(gravar)


def _somadas(existentes: list[dict], vigente: dict, itens: list[ItemPrevia]) -> list[ItemPrevia]:
    """O contrato somado: as linhas do vigente (já com as edições) e as da planilha no fim da etapa delas; etapa nova
    vai para o fim."""
    grupos: dict[str, list[ItemPrevia]] = {}
    for x in sorted((x for x in existentes if x["versao_id"] == vigente["id"]), key=lambda x: x["ordem"]):
        grupos.setdefault(x["etapa"], []).append(ItemPrevia.model_validate(x))
    for x in itens:
        grupos.setdefault(x.etapa, []).append(x)
    return [x for g in grupos.values() for x in g]


def _num_br(centesimos: int, divisor: int, casas: int) -> str:
    """Inteiro (milésimos/centavos) -> número brasileiro sem separador de milhar ("220,3", "6,69")."""
    s = f"{centesimos / divisor:.{casas}f}"
    return s.rstrip("0").rstrip(".").replace(".", ",") if "." in s else s


def _csv_estrutura(etapa: str, itens: list) -> bytes:
    """A planilha (CSV ;) de uma etapa com os materiais como itens — o mesmo formato que a importação de versão lê."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Item", "Código", "Fonte", "Descrição", "Unidade", "Quantidade", "Unitário", "Total"])
    w.writerow(["1", "", "", etapa.strip(), "", "", "", ""])
    for n, it in enumerate(itens, start=1):
        total = round(it.quantidade * it.valor_unitario / 1000)
        w.writerow(["1." + str(n), "", "", it.nome.strip(), it.unidade.strip(),
                    _num_br(it.quantidade, 1000, 3), _num_br(it.valor_unitario, 100, 2), _num_br(total, 100, 2)])
    return buf.getvalue().encode("utf-8")


async def importar_estrutura(arm, u: UsuarioAtual, obra_id: str, corpo) -> tuple[PreviaVersao, bool]:
    """Lança os materiais do projeto como uma versão do orçamento (etapa \"Estrutura\"). Contrato se a obra ainda não
    tem um; somado ao contrato se ele ainda não foi aprovado; aditivo depois de aprovado. Reaproveita a importação de
    versão (prévia, gravação, desfazer)."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    versoes = await versoes_da_obra(arm, u.tenant_id, obra_id)
    tem_contrato = any(v["ativa"] and v["tipo"] == TipoVersao.CONTRATO for v in versoes)
    # antes de aprovar, as pranchas completam o orçamento; aditivo só depois que ele foi fechado
    somar = tem_contrato and o["status"] == "em_orcamento"
    tipo = TipoVersao.ADITIVO if tem_contrato and not somar else TipoVersao.CONTRATO
    conteudo = _csv_estrutura(corpo.etapa, corpo.itens)
    m = Mapeamento(planilha="CSV", linha_cabecalho=1, item=0, codigo=1, fonte=2, nome=3, unidade=4, quantidade=5,
                   valor_unitario=6, valor_total=7)
    return await importar(arm, u, obra_id, conteudo, f"{corpo.etapa}{SUFIXO_PROJETO}", m, tipo,
                          descricao=corpo.etapa.strip(), data=None, aplicar=corpo.aplicar,
                          chave=corpo.chave_idempotencia, somar=somar, ignorar=corpo.ignorar_itens)


async def reordenar_etapas(arm, u: UsuarioAtual, obra_id: str, ordem: list[str], chave: str,
                           versao_id: str | None = None) -> tuple[dict, bool]:
    """Grava a ordem das etapas do orçamento (antes de aprovar) como um contrato novo no lugar do vigente, com as
    etapas renumeradas 1..N e os itens junto. É a mesma mecânica do somar: o Desfazer religa o anterior. Devolve
    ({versao_id, registro_id, etapas}, criado agora?)."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    assinatura = hashlib.sha256(f"reordenar|{ordem}".encode()).hexdigest()
    id_ = id_versao(u.tenant_id, obra_id, chave)
    if ja := await arm.obter("versoes_orcamento", u.tenant_id, id_):
        if ja["assinatura"] != assinatura:
            raise Conflito("Esta chave de idempotência já foi usada para outra mudança do orçamento.")
        return _reordenacao(u.tenant_id, ja), False
    recusar_se_desfazendo(o)
    if o["status"] != "em_orcamento":
        raise Conflito("O orçamento já foi aprovado: a ordem das etapas não muda mais.")
    vigente = _contrato_vigente(await versoes_da_obra(arm, u.tenant_id, obra_id))
    if not vigente:
        raise ErroNegocio("Ainda não há orçamento nesta obra para ordenar as etapas.")
    if versao_id and versao_id != vigente["id"]:
        # depois de uma reordenação as chaves são sempre 1..N: a lista montada na tela velha passaria na conferência
        raise Conflito("O orçamento mudou desde que você abriu a tela; recarregue e confira a ordem.")
    if vigente.get("aditivos", 0) > 0:
        raise Conflito("Há aditivo valendo sobre este orçamento; desfaça o aditivo antes de mudar a ordem.")
    if vigente.get("padronizada"):
        # renumerar 1..N tiraria as etapas da lista padrão (a chave "alvenaria" é o que deixa comparar obras)
        raise ErroNegocio("O orçamento está nas etapas padrão: a ordem é a da lista e não muda.")
    existentes = await itens_das_versoes(arm, u.tenant_id, obra_id, [vigente])
    linhas = [ItemPrevia.model_validate(x) for x in existentes]
    atual = _ordem_das_etapas(linhas)
    if len(ordem) != len(set(ordem)) or set(ordem) != set(atual):
        raise ErroNegocio("Mande todas as etapas do orçamento, cada uma uma vez: "
                          f"{', '.join(atual)}.")
    if ordem == atual:
        raise ErroNegocio("Nada mudou: as etapas já estão nesta ordem.")
    if presas := sorted(_chaves_que_mudam(linhas, ordem) & await etapas_em_uso(arm, u.tenant_id, obra_id)):
        nomes = _nomes_das_etapas(linhas)
        raise Conflito("Já há lançamento (gasto, tópico ou cronograma) na etapa "
                       f"{', '.join(f'{k} {nomes.get(k, "")}'.strip() for k in presas)}: renumerar levaria o "
                       "lançamento para outra etapa.")
    renumeradas = _renumeradas_no_desfazer(linhas, ordem, set(atual))
    linhas = _reordenar(linhas, ordem)
    lista_topicos = _com_topicos(u.tenant_id, obra_id, linhas, await topicos_manuais(arm, u.tenant_id, obra_id))
    so_itens = [x for x in linhas if x.tipo == "item"]
    versao = VersaoOrcamento(
        id=id_, tenant_id=u.tenant_id, obra_id=obra_id, tipo=TipoVersao.CONTRATO, descricao=vigente["descricao"],
        data=vigente.get("data"), arquivo=vigente.get("arquivo", ""), itens=len(so_itens),
        etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in linhas), total=sum(x.total for x in so_itens),
        substitui=vigente["id"], assinatura=assinatura,
        lista_etapas=[{"etapa": e.etapa, "descricao": e.descricao} for e in _totais_por_etapa(linhas) if e.etapa],
        lista_topicos=lista_topicos)
    d_versao = {**doc(versao), "origem": "reordenar"}
    await _gravar_itens(arm, u, obra_id, id_, linhas, por_posicao=True)

    async def ativar(tx):
        if (await obra_do_tenant(tx, u.tenant_id, obra_id))["status"] != "em_orcamento":
            raise Conflito("O orçamento foi aprovado enquanto você mudava a ordem; abra de novo.")
        if await tx.obter("versoes_orcamento", u.tenant_id, id_):
            raise Conflito("Esta mudança já foi gravada.")
        anterior = await tx.obter("versoes_orcamento", u.tenant_id, vigente["id"])
        if not anterior["ativa"] or any(anterior.get(k, 0) != vigente.get(k, 0)
                                        for k in ("total", "edicoes", "aditivos")):
            # os itens copiados são os de antes: gravar perderia o que mudou no meio
            raise Conflito("O orçamento mudou enquanto você mudava a ordem; abra de novo.")

        def aplicar_():
            tx.criar("versoes_orcamento", d_versao)
            tx.atualizar("versoes_orcamento", u.tenant_id, anterior["id"], {"ativa": False})
            evento(tx, u.tenant_id, "orcamento.versao", obra_id,
                   {"versao_id": id_, "tipo": TipoVersao.CONTRATO.value, "total": versao.total})
            return d_versao

        op = Operacao(obra_id=obra_id, aplicar=aplicar_,
                      texto=lambda d: f"Reordenou as etapas do orçamento de {o['nome']}",
                      desfazer=lambda d: {"op": "desligar_versao", "versao_id": id_, "reativar": anterior["id"],
                                          "reordenou": True, "renumeradas": renumeradas})
        executar_e_registrar(tx, u, op, registro_id=id_derivado(u.tenant_id, "registro", id_))

    await arm.em_transacao(ativar)
    return _reordenacao(u.tenant_id, d_versao), True


def _reordenacao(tenant_id: str, v: dict) -> dict:
    return {"versao_id": v["id"], "registro_id": id_derivado(tenant_id, "registro", v["id"]),
            "etapas": v["lista_etapas"]}


async def _previa_gravada(arm, u: UsuarioAtual, v: dict) -> PreviaVersao:
    """A repetição da chave devolve o que foi gravado, sem ler o arquivo de novo."""
    itens = await arm.consultar(Consulta("itens_orcamento", u.tenant_id, [Filtro("versao_id", "==", v["id"])]))
    previas = [ItemPrevia.model_validate(x) for x in sorted(itens, key=lambda x: x["ordem"])]
    sub = await arm.obter("versoes_orcamento", u.tenant_id, v["substitui"]) if v.get("substitui") else None
    return PreviaVersao(
        aplicado=True, tipo=v["tipo"], itens=previas, ignoradas=[], erros=[], avisos=[], total=v["total"],
        n_itens=v["itens"], n_etapas=v["etapas"], etapas=_totais_por_etapa(previas),
        substitui=Substituida(versao_id=sub["id"], total=sub["total"], itens=sub["itens"]) if sub else None,
        versao_id=v["id"], registro_id=id_derivado(u.tenant_id, "registro", v["id"]))


async def preparar_desligar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    v = await tx.obter("versoes_orcamento", u.tenant_id, receita["versao_id"])
    if not v:
        raise NaoEncontrado("Versão do orçamento não encontrada.")
    if not v["ativa"]:
        raise Conflito("Esta versão do orçamento já não está valendo; não dá para desfazer.")
    if v.get("aditivos", 0) > 0:
        # o aditivo feito sobre este contrato ficaria somando ao anterior, que ele não aditou
        raise Conflito("Há aditivo valendo sobre este contrato; desfaça o aditivo primeiro.")
    reativar = (await tx.obter("versoes_orcamento", u.tenant_id, receita["reativar"])
                if receita.get("reativar") else None)
    contrato = await tx.obter("versoes_orcamento", u.tenant_id, v["contrato_id"]) if v.get("contrato_id") else None
    o = await obra_do_tenant(tx, u.tenant_id, v["obra_id"])
    if v["tipo"] == TipoVersao.CONTRATO and o.get("aprovada"):
        # sem esta trava, tirar o contrato abriria espaço para importar outro com a obra aprovada
        raise Conflito("O orçamento já foi aprovado: desfaça a aprovação antes de retirar o contrato.")

    def aplicar():
        tx.atualizar("versoes_orcamento", u.tenant_id, v["id"], {"ativa": False})
        if reativar is not None:
            tx.atualizar("versoes_orcamento", u.tenant_id, reativar["id"], {"ativa": True})
        if contrato is not None:
            tx.atualizar("versoes_orcamento", u.tenant_id, contrato["id"],
                         {"aditivos": max(0, contrato.get("aditivos", 0) - 1)})
        return {**v, "ativa": False}

    nome = "contrato" if v["tipo"] == TipoVersao.CONTRATO else f"{_ordinal(v['numero'])} aditivo"
    if receita.get("somou"):
        texto = f"Desfeito: os itens somados ao orçamento de {o['nome']} saíram (o anterior volta a valer)"
    elif receita.get("reordenou"):
        texto = f"Desfeito: as etapas do orçamento de {o['nome']} voltaram à ordem de antes"
    else:
        texto = (f"Desfeito: {nome} de {o['nome']} retirado" + (" (o anterior volta a valer)" if reativar else ""))
    return Operacao(obra_id=v["obra_id"], aplicar=aplicar,
                    texto=lambda d: texto,
                    desfazer=lambda d: None)


async def orcamento_da_obra(arm, tenant_id: str, obra_id: str) -> OrcamentoObra:
    await obra_do_tenant(arm, tenant_id, obra_id)
    versoes = await versoes_da_obra(arm, tenant_id, obra_id)
    ativas = [v for v in versoes if v["ativa"]]
    ativas.sort(key=lambda v: (v["tipo"] != TipoVersao.CONTRATO, v["numero"], v["criado_em"]))
    itens = await itens_das_versoes(arm, tenant_id, obra_id, ativas)
    curvas = _curvas([x for x in itens if x["tipo"] == "item"])
    contrato_ids = {v["id"] for v in ativas if v["tipo"] == TipoVersao.CONTRATO}
    lista_topicos, topico_do_item = await topicos_das_versoes(arm, tenant_id, obra_id, ativas, itens)

    descricoes: dict[str, str] = {}
    somas: dict[str, list[int]] = {}
    por_topico: dict[str, dict[str | None, list[int]]] = {}
    estimado: dict[str, int] = {}
    venda: dict[str, int] = {}
    for x in itens:
        if x["tipo"] == "etapa" and x["nivel"] == 1:
            # a descrição do contrato manda; a do aditivo só para etapa que nasceu nele
            if x["versao_id"] in contrato_ids or x["etapa"] not in descricoes:
                descricoes[x["etapa"]] = x["descricao"]
            somas.setdefault(x["etapa"], [0, 0])
        elif x["tipo"] == "item" and x.get("fonte") == FONTE_ESTIMATIVA:
            # o que a previsão da obra estimou não é orçado: fica à parte (o cronograma soma os dois)
            somas.setdefault(x["etapa"], [0, 0])
            estimado[x["etapa"]] = estimado.get(x["etapa"], 0) + x["total"]
        elif x["tipo"] == "item":
            lado = 0 if x["versao_id"] in contrato_ids else 1
            somas.setdefault(x["etapa"], [0, 0])[lado] += x["total"]
            t = por_topico.setdefault(x["etapa"], {}).setdefault(topico_do_item.get(x["id"]), [0, 0])
            t[lado] += x["total"]
            if x.get("total_venda") is not None:
                venda[x["etapa"]] = venda.get(x["etapa"], 0) + x["total_venda"]
    # na ordem do orçamento (`lista_etapas` das versões, contrato primeiro): os itens vêm pela `ordem` de cada versão,
    # e a etapa que só aparece num item que a previsão criou no fim (a fase estimada) cairia fora do lugar
    posicao: dict[str, int] = {}
    for v in ativas:
        for e in v.get("lista_etapas") or []:
            posicao.setdefault(e["etapa"], len(posicao))
    somas = dict(sorted(somas.items(), key=lambda kv: posicao.get(kv[0], len(posicao))))
    # O gasto (comprovante confirmado) é dinheiro que saiu: soma no gasto da etapa dele, e por
    # isso mexe no saldo e no estouro. A repartição é do domínio (inclusive o que não entra em etapa).
    avulsos = gastos_no_orcamento(await gastos_da_obra(arm, tenant_id, obra_id), {e for e in somas if e})
    nomes_topicos = {**{t["id"]: t["nome"] for t in await arm.consultar(
        Consulta("topicos", tenant_id, [Filtro("obra_id", "==", obra_id)]))},
        **await nomes_dos_topicos_do_orcamento(arm, tenant_id, obra_id)}
    etapas = []
    for e, (c, a) in somas.items():
        etapa = _etapa(e, descricoes.get(e, "Sem etapa" if not e else f"Etapa {e}"), c, a,
                       avulsos.por_etapa.get(e, 0))
        etapa.estimado, etapa.venda = estimado.get(e, 0), venda.get(e)
        etapa.topicos = _topicos_da_etapa(e, [t for t in lista_topicos if t["etapa"] == e], por_topico.get(e, {}),
                                          avulsos.por_topico.get(e, {}), nomes_topicos)
        etapas.append(etapa)
    # gasto que conta na obra sem estar numa etapa do orçamento (extra, a definir, etapa de versão desfeita)
    fora = avulsos.fora_das_etapas
    sem = {"gasto": fora}
    contratado = sum(e.contratado for e in etapas)
    aditivos = sum(e.aditivos for e in etapas)
    andamento = {k: sum(getattr(e, k) for e in etapas) + sem[k] for k in sem}
    return OrcamentoObra(
        versoes=[{**v, "registro_id": id_derivado(tenant_id, "registro", v["id"]), **_textos_da_versao(v, versoes),
                  "situacao": await _situacao(arm, tenant_id, v)}
                 for v in versoes],
        itens=[{**x, "curva": curvas.get(x["id"]), "topico_id": topico_do_item.get(x["id"])} for x in itens],
        etapas=etapas,
        totais=TotaisOrcamento(contratado=contratado, aditivos=aditivos, atual=contratado + aditivos,
                               saldo=contratado + aditivos - andamento["gasto"],
                               aditivos_pct=pct(aditivos, contratado) if contratado else None,
                               n_itens=sum(x["tipo"] == "item" for x in itens),
                               n_etapas=len(descricoes), sem_etapa=sem, estimado=sum(estimado.values()),
                               venda=sum(venda.values()) if venda else None,
                               gastos=GastosNoOrcamentoSaida(total=avulsos.total, nas_etapas=avulsos.nas_etapas,
                                                             fora_do_orcamento=avulsos.fora_do_orcamento,
                                                             na_lista_padrao=avulsos.na_lista_padrao,
                                                             sem_etapa_do_orcamento=avulsos.sem_etapa_do_orcamento,
                                                             a_definir=avulsos.a_definir),
                               **andamento))


async def _situacao(leitor, tenant_id: str, v: dict) -> str:
    """Desligada pelo Desfazer dela mesma é "desfeita"; desligada por outra (reimportação, somar, reordenar) é
    "substituida". `ativa` não basta: nas duas ela fica false. É o registro da versão que diz se ela foi desfeita;
    numa cadeia (pranchas → somar → reordenar) cada elo tem o seu, então não há cadeia a seguir."""
    if v["ativa"]:
        return "vigente"
    e = await leitor.obter("registro", tenant_id, id_derivado(tenant_id, "registro", v["id"]))
    return "desfeita" if e and e.get("desfeito") else "substituida"


def _n_itens(n: int) -> str:
    return f"{n} {'item' if n == 1 else 'itens'}"


def _textos_da_versao(v: dict, versoes: list[dict]) -> dict:
    """O nome da versão e o que o Desfazer dela faz, em palavras de obra: "Estrutura (projeto).csv" é o nome que a API
    grava para reconhecer as pranchas (`SUFIXO_PROJETO`), não um arquivo que a pessoa enviou."""
    das_pranchas = (v.get("arquivo") or "").endswith(SUFIXO_PROJETO)
    anterior = next((x for x in versoes if x["id"] == v.get("substitui")), None)
    origem = v.get("origem")
    desfazer = None
    if origem == "reordenar":
        rotulo = "Etapas reordenadas"
        desfazer = "Volta as etapas do orçamento para a ordem de antes."
    elif origem == "somar":
        o_que = "os materiais das pranchas" if das_pranchas else "a planilha"
        rotulo = f"Orçamento com {o_que} somad{'os' if das_pranchas else 'a'}"
        if anterior:
            desfazer = (f"Volta o orçamento para antes de somar {o_que} "
                        f"({fmt_reais(anterior['total'])}, {_n_itens(anterior['itens'])}).")
    elif das_pranchas:
        rotulo = "Materiais das pranchas"
    else:
        rotulo = v.get("descricao") or v.get("arquivo") or "Orçamento"
    return {"rotulo": rotulo, "desfazer_texto": desfazer}


async def itens_das_versoes(leitor, tenant_id: str, obra_id: str, versoes_: list[dict]) -> list[dict]:
    """As linhas das versões dadas, na ordem delas (de 30 em 30: é o teto do `in`)."""
    itens: list[dict] = []
    for i in range(0, len(versoes_), 30):
        ids = [v["id"] for v in versoes_[i:i + 30]]
        itens += await leitor.consultar(Consulta("itens_orcamento", tenant_id, [Filtro("versao_id", "in", ids)]))
    posicao = {v["id"]: k for k, v in enumerate(versoes_)}
    itens = [x for x in itens if x["obra_id"] == obra_id]
    itens.sort(key=lambda x: (posicao[x["versao_id"]], x["ordem"]))
    return itens


# --- editar a linha do orçamento: preço e quantidade na própria tela ---

_CAMPOS_ITEM = ("quantidade", "valor_unitario", "total")


async def editar_item(arm, u: UsuarioAtual, obra_id: str, item_id: str, mudancas: dict) -> dict:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    if not mudancas:
        raise ErroNegocio("Nada para mudar.")
    item = await arm.obter("itens_orcamento", u.tenant_id, item_id)
    if not item or item["obra_id"] != obra_id:
        raise NaoEncontrado("Item do orçamento não encontrado nesta obra.")

    async def tx_corpo(tx):
        op = await _preparar_editar_item(tx, u, obra_id, item_id, mudancas)
        r, entrada = executar_e_registrar(tx, u, op)
        return {**r, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


async def _preparar_editar_item(tx, u: UsuarioAtual, obra_id: str, item_id: str, mudancas: dict) -> Operacao:
    # a corrida com a aprovação se resolve na versão: esta transação e a da aprovação gravam nela
    o = await obra_do_tenant(tx, u.tenant_id, obra_id)
    recusar_se_desfazendo(o)
    item = await tx.obter("itens_orcamento", u.tenant_id, item_id)
    if not item or item["obra_id"] != obra_id:
        raise NaoEncontrado("Item do orçamento não encontrado nesta obra.")
    if item["tipo"] != "item":
        raise RegraViolada("Esta linha é o título da etapa: não tem preço nem quantidade.")
    v = await tx.obter("versoes_orcamento", u.tenant_id, item["versao_id"])
    if not v or not v["ativa"]:
        raise Conflito("Esta versão do orçamento já não está valendo.")
    novo = {"quantidade": mudancas.get("quantidade", item["quantidade"]),
            "valor_unitario": mudancas.get("valor_unitario", item["valor_unitario"])}
    if novo["quantidade"] == 0:
        raise RegraViolada("A quantidade não pode ser zero.")
    if novo["quantidade"] < 0 and v["tipo"] != TipoVersao.ADITIVO:
        raise RegraViolada("Quantidade negativa (supressão só vale em aditivo).")
    if all(item[k] == x for k, x in novo.items()):
        raise RegraViolada("Nada mudou.")
    aprovado = o["status"] != "em_orcamento"
    if aprovado and (novo["quantidade"] != item["quantidade"] or item["valor_unitario"] != 0):
        # só preço em branco: quem aprovou "mesmo assim" com itens sem preço precisa poder completar; o resto é aditivo
        raise Conflito("O orçamento já foi aprovado: lance um aditivo para mudar este item.")
    depois = {**novo, "total": _arredonda(Decimal(novo["quantidade"]) * novo["valor_unitario"] / 1000)}
    antes = {k: item[k] for k in _CAMPOS_ITEM}
    total_versao = v["total"] + depois["total"] - antes["total"]

    def aplicar() -> dict:
        tx.atualizar("itens_orcamento", u.tenant_id, item_id, depois)
        tx.atualizar("versoes_orcamento", u.tenant_id, v["id"],
                     {"total": total_versao, "edicoes": v.get("edicoes", 0) + 1})
        evento(tx, u.tenant_id, "orcamento.item_editado", obra_id, {"item_id": item_id, "total": depois["total"]})
        return {"item": {**item, **depois}, "total_versao": total_versao}

    partes = []
    if novo["quantidade"] != item["quantidade"]:
        partes.append(f"quantidade {fmt_qtd(item['quantidade'])} → {fmt_qtd(novo['quantidade'])} {item['unidade']}")
    if novo["valor_unitario"] != item["valor_unitario"]:
        partes.append(f"preço {fmt_reais(item['valor_unitario'])} → {fmt_reais(novo['valor_unitario'])}")
    receita = {"op": "editar_item_orcamento", "obra_id": obra_id, "item_id": item_id, "antes": antes, "depois": depois,
               "pos_aprovacao": aprovado}
    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: f"Editou {item['descricao']} no orçamento: {', '.join(partes)}",
                    desfazer=lambda d: receita)


async def preparar_desfazer_item(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfazer (e Refazer) a edição da linha. Se ela mudou de novo depois, não desfaz por cima; e, com o orçamento já
    aprovado, só volta a edição que foi feita depois da aprovação (a de antes já faz parte do que se aprovou)."""
    o = await obra_do_tenant(tx, u.tenant_id, receita["obra_id"])
    item = await tx.obter("itens_orcamento", u.tenant_id, receita["item_id"])
    if not item:
        raise NaoEncontrado("Item do orçamento não encontrado.")
    if any(item[k] != x for k, x in receita["depois"].items()):
        raise Conflito("Este item foi editado de novo depois; não dá para desfazer.")
    v = await tx.obter("versoes_orcamento", u.tenant_id, item["versao_id"])
    if not v or not v["ativa"]:
        raise Conflito("Esta versão do orçamento já não está valendo; não dá para desfazer.")
    if o["status"] != "em_orcamento" and not receita.get("pos_aprovacao"):
        raise Conflito("O orçamento já foi aprovado com esta edição; não dá para desfazer.")
    volta = receita["antes"]
    total_versao = v["total"] + volta["total"] - receita["depois"]["total"]

    def aplicar() -> dict:
        tx.atualizar("itens_orcamento", u.tenant_id, item["id"], volta)
        tx.atualizar("versoes_orcamento", u.tenant_id, v["id"],
                     {"total": total_versao, "edicoes": v.get("edicoes", 0) + 1})
        return {**item, **volta}

    # a receita gravada antes de Compras sair pode trazer o preço do insumo: ele não existe mais, e fica de fora
    inversa = {k: v for k, v in receita_inversa(receita).items() if k != "insumo"}
    return Operacao(obra_id=receita["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"{verbo_desfazer(receita)}: {item['descricao']} no orçamento voltou",
                    desfazer=lambda d: inversa)


async def gastos_da_obra(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    """Os gastos confirmados e não estornados da obra."""
    return await leitor.consultar(Consulta("gastos", tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                  Filtro("estornado", "==", False)]))


def _contas(contratado: int, aditivos: int, gasto: int) -> dict:
    """Saldo e estouro saem daqui prontos: quem lê (tela, agente) só mostra."""
    atual = contratado + aditivos
    return {"contratado": contratado, "aditivos": aditivos, "atual": atual, "saldo": atual - gasto,
            "estourou": gasto > atual, "gasto": gasto}


def _etapa(etapa: str, descricao: str, contratado: int, aditivos: int, gasto: int = 0) -> EtapaOrcamento:
    return EtapaOrcamento(etapa=etapa, descricao=descricao, **_contas(contratado, aditivos, gasto))


SEM_TOPICO = "Sem tópico"


def _topicos_da_etapa(etapa: str, do_orcamento: list[dict], orcado: dict[str | None, list[int]],
                      avulsos: dict[str | None, int], nomes: dict[str, str]) -> list[TopicoOrcamento]:
    """A etapa aberta por tópico, com as mesmas contas da etapa: os tópicos do orçamento dela (mesmo sem nada
    lançado), os manuais que têm lançamento e, no fim, "Sem tópico" (o que entrou só na etapa e os itens sem
    tópico). Somados dão a etapa. Etapa sem tópico nenhum fica com a lista vazia."""
    ids = [t["id"] for t in do_orcamento]
    outros = sorted({k for k in (*orcado, *avulsos) if k is not None and k not in ids},
                    key=lambda k: (nomes.get(k, ""), k))
    if not ids and not outros:
        return []
    saida = []
    for k in [*ids, *outros, None]:
        c, a = orcado.get(k, [0, 0])
        contas = _contas(c, a, avulsos.get(k, 0))
        if k is None and not any(contas[x] for x in ("atual", "gasto")):
            continue
        nome = (next(t["nome"] for t in do_orcamento if t["id"] == k) if k in ids
                else nomes.get(k, "Tópico") if k else SEM_TOPICO)
        origem = None if k is None else "orcamento" if k in ids or k.startswith(PREFIXO_TOPICO_ORCAMENTO) else "manual"
        saida.append(TopicoOrcamento(topico_id=k, nome=nome, origem=origem, **contas))
    return saida


CEM = 10000                            # 100% em centésimos de ponto percentual (290 = 2,9%)


def pct(parte: int, todo: int) -> int:
    """`parte` sobre `todo` em centésimos de %, arredondado para o mais próximo (meio para cima)."""
    return (parte * CEM + todo // 2) // todo if todo else 0


async def etapas_da_obra(leitor, tenant_id: str, obra_id: str) -> list[EtapaRef]:
    """As etapas que podem receber um gasto: as das versões vigentes, contrato primeiro, sem repetir. Lista vazia = obra
    sem orçamento importado (aí a apropriação é opcional)."""
    ativas = [v for v in await versoes_da_obra(leitor, tenant_id, obra_id) if v["ativa"]]
    if any("lista_etapas" not in v for v in ativas):
        # versão gravada antes da lista: monta pelos itens (uma vez por leitura, só em obra antiga)
        o = await orcamento_da_obra(leitor, tenant_id, obra_id)
        return [EtapaRef(etapa=e.etapa, descricao=e.descricao) for e in o.etapas if e.etapa]
    ativas.sort(key=lambda v: (v["tipo"] != TipoVersao.CONTRATO, v["numero"]))
    vistas: dict[str, str] = {}
    for v in ativas:
        for e in v["lista_etapas"]:
            vistas.setdefault(e["etapa"], e["descricao"])
    return [EtapaRef(etapa=k, descricao=d) for k, d in vistas.items()]


# --- tópicos do orçamento (nível 2 da numeração): derivados dos itens, guardados na versão ---

# O tópico do orçamento não é documento: o ID mostra isso, e o `PATCH /topicos/{id}` responde sem procurar.
PREFIXO_TOPICO_ORCAMENTO = "orc-"


def id_topico_orcamento(tenant_id: str, obra_id: str, etapa: str, chave: str) -> str:
    """Estável: reimportar a mesma planilha devolve os mesmos IDs, e o gasto continua no tópico."""
    return PREFIXO_TOPICO_ORCAMENTO + id_derivado(tenant_id, "topico-orc", obra_id, etapa, chave)


async def topicos_manuais(leitor, tenant_id: str, obra_id: str) -> dict[tuple[str, str], str]:
    """{(etapa, nome normalizado): ID} dos tópicos manuais ativos: o do orçamento de mesmo nome na mesma etapa
    reaproveita o ID, e o gasto já lançado nele continua no lugar."""
    ts = await leitor.consultar(Consulta("topicos", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    return {(t["etapa"], normalizar_nome(t["nome"])): t["id"] for t in ts if not t.get("arquivado")}


def _topicos_das_linhas(tenant_id: str, obra_id: str, linhas: list[dict], manuais: dict[tuple[str, str], str],
                        herdados: dict[str, str] | None = None) -> tuple[list[dict], list[str | None], dict[str, int]]:
    """(lista_topicos, o topico_id de cada linha, etapas que passaram do limite). A regra é do domínio."""
    r = topicos_dos_itens(linhas, herdados)
    ids = {t: manuais.get((t.etapa, t.chave)) or id_topico_orcamento(tenant_id, obra_id, t.etapa, t.chave)
           for t in r.topicos}
    lista = [{"etapa": t.etapa, "id": ids[t], "nome": t.nome} for t in r.topicos]
    return lista, [ids[r.do_item[i]] if i in r.do_item else None for i in range(len(linhas))], r.cheias


def _com_topicos(tenant_id: str, obra_id: str, linhas: list[ItemPrevia], manuais: dict[tuple[str, str], str],
                 avisos: list[Aviso] | None = None, herdados: dict[str, str] | None = None) -> list[dict]:
    """Põe o `topico_id` em cada linha e devolve a `lista_topicos` da versão; a etapa cheia de tópicos ganha aviso.
    `herdados` = os tópicos do orçamento que vale, no aditivo (`_topicos_por_numero`)."""
    lista, do_item, cheias = _topicos_das_linhas(tenant_id, obra_id, [x.model_dump() for x in linhas], manuais,
                                                 herdados)
    for x, t in zip(linhas, do_item, strict=True):
        x.topico_id = t
    if avisos is not None:
        nomes = _nomes_das_etapas(linhas)
        for e, n in cheias.items():
            avisos.append(Aviso(linha=0, mensagem=(
                f"A etapa {f'{e} {nomes.get(e, "")}'.strip()} tem {n} tópicos na numeração (o limite é "
                f"{MAX_TOPICOS_POR_ETAPA}): ela fica sem os tópicos do orçamento, e o gasto entra só na etapa.")))
    return lista


def _topicos_renumerados(itens: list[ItemPrevia], antes: list[dict], depois: list[dict],
                         nova: dict[str, str]) -> list[dict]:
    """A prévia continua nas chaves de etapa de antes da renumeração, mas com o ID que cada tópico tem no orçamento
    gravado (a etapa entra no ID): troca o `topico_id` dos itens da prévia e devolve `antes` com os IDs de `depois`.
    O tópico é o mesmo quando é o mesmo nome na etapa renumerada."""
    final = {(t["etapa"], normalizar_nome(t["nome"])): t["id"] for t in depois}
    troca = {t["id"]: final.get((nova.get(t["etapa"], t["etapa"]), normalizar_nome(t["nome"]))) for t in antes}
    for x in itens:
        if x.topico_id:
            x.topico_id = troca[x.topico_id]
    return [{**t, "id": troca[t["id"]]} for t in antes if troca[t["id"]]]


def _topicos_da_previa(linhas: list[ItemPrevia], lista: list[dict]) -> list[TopicoPrevia]:
    """Os tópicos que alguma das `linhas` toca, com a soma delas."""
    somas: dict[str, int] = {}
    for x in linhas:
        if x.tipo == "item" and x.topico_id:
            somas[x.topico_id] = somas.get(x.topico_id, 0) + x.total
    return [TopicoPrevia(**t, total=somas[t["id"]]) for t in lista if t["id"] in somas]


def _topicos_por_numero(linhas: list[dict], lista: list[dict], topico_de: dict[str, str | None]) -> dict[str, str]:
    """{"7.1": nome do tópico} pelo tópico em que os itens numerados de `linhas` estão (`topico_de`, de
    `topicos_das_versoes`). Sai do tópico, não da descrição da linha "7.1": no orçamento organizado em fases antes de
    os tópicos existirem, essa linha virou título de fase."""
    nome = {t["id"]: t["nome"] for t in lista}
    r: dict[str, str] = {}
    for x in linhas:
        tn = numero_do_topico(x.get("numero") or "")
        if tn and (t := topico_de.get(x["id"])) in nome:
            r.setdefault(tn, nome[t])
    return r


async def topicos_das_versoes(leitor, tenant_id: str, obra_id: str, ativas: list[dict],
                              itens: list[dict] | None = None) -> tuple[list[dict], dict[str, str | None]]:
    """Os tópicos do orçamento vigente (`ativas` = as versões vigentes, contrato primeiro), sem repetir ID, e o
    `topico_id` de cada item {item_id: topico_id}. Versão gravada antes de `lista_topicos` monta pelos itens (como
    `lista_etapas`), com os tópicos das versões antes dela (o aditivo antigo cai nos do contrato): só em obra antiga.
    `itens` = os itens das `ativas`, quando quem chama já leu."""
    antigas = {v["id"] for v in ativas if "lista_topicos" not in v}
    if antigas and itens is None:
        itens = await itens_das_versoes(leitor, tenant_id, obra_id, ativas)
    manuais = await topicos_manuais(leitor, tenant_id, obra_id) if antigas else {}
    do_item: dict[str, str | None] = {}
    vistos: dict[str, dict] = {}
    for v in ativas:
        mesmas = [x for x in itens or [] if x["versao_id"] == v["id"]]
        if v["id"] in antigas:
            herdados = _topicos_por_numero([x for x in itens if x["id"] in do_item], list(vistos.values()), do_item)
            lista, tids, _ = _topicos_das_linhas(tenant_id, obra_id, mesmas, manuais, herdados)
        else:
            lista, tids = v.get("lista_topicos", []), [x.get("topico_id") for x in mesmas]
        do_item.update({x["id"]: t for x, t in zip(mesmas, tids, strict=True)})
        for t in lista:
            vistos.setdefault(t["id"], t)
    return list(vistos.values()), do_item


async def topicos_da_obra(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    """Os tópicos do orçamento vigente, `{etapa, id, nome}`, contrato primeiro: os da árvore de lançamento."""
    ativas = [v for v in await versoes_da_obra(leitor, tenant_id, obra_id) if v["ativa"]]
    ativas.sort(key=lambda v: (v["tipo"] != TipoVersao.CONTRATO, v["numero"]))
    return (await topicos_das_versoes(leitor, tenant_id, obra_id, ativas))[0]


async def nomes_dos_topicos_do_orcamento(leitor, tenant_id: str, obra_id: str) -> dict[str, str]:
    """{id: nome} dos tópicos do orçamento de todas as versões, inclusive as desligadas: o gasto lançado num tópico
    de versão desfeita continua com nome."""
    versoes_ = await versoes_da_obra(leitor, tenant_id, obra_id)
    nomes = {t["id"]: t["nome"] for v in reversed(versoes_) for t in v.get("lista_topicos", [])}
    nomes.update({t["id"]: t["nome"] for t in await topicos_da_obra(leitor, tenant_id, obra_id)})
    return nomes


async def validar_etapa(leitor, tenant_id: str, obra_id: str, etapas: list[str | None],
                        exigir: bool = True) -> dict[str, str]:
    """Confere as etapas informadas contra o orçamento da obra; devolve {chave: descrição} para os textos."""
    validas = {e.etapa: e.descricao for e in await etapas_da_obra(leitor, tenant_id, obra_id)}
    for e in etapas:
        if e and validas and e not in validas:
            raise ErroNegocio(f"A etapa {e} não existe no orçamento desta obra.")
        if not e and validas and exigir:
            raise ErroNegocio("Diga de qual etapa do orçamento é este gasto.")
        if e and not validas:
            raise ErroNegocio("Esta obra ainda não tem orçamento importado; deixe a etapa em branco.")
    return validas


def _curvas(itens: list[dict]) -> dict[str, str]:
    """A régua de sempre (80/95% do valor), item a item: na EAP o peso é do serviço, não do material."""
    com_valor = sorted((x for x in itens if x["total"] > 0), key=lambda x: -x["total"])
    total = sum(x["total"] for x in com_valor)
    curvas: dict[str, str] = {}
    acumulado = 0
    for x in com_valor:
        antes = acumulado
        acumulado += x["total"]
        curvas[x["id"]] = "A" if antes * 100 < total * 80 else "B" if antes * 100 < total * 95 else "C"
    for x in itens:
        curvas.setdefault(x["id"], "C")
    return curvas


# ---------------------------------------------------------------------------------------------------------------------
# Organizar o orçamento em fases de obra (EAP): deixa o cronograma nascer completo, por fase, de um orçamento "chapado".
# Reatribui a etapa de cada serviço pela classificação (canteiro_dominio.cronograma.fase). Ação da pessoa, com Desfazer.
# ---------------------------------------------------------------------------------------------------------------------

async def organizar_em_fases(arm, u: UsuarioAtual, obra_id: str) -> dict:
    """Reorganiza os serviços do orçamento contratado em etapas por fase de obra (Fundação, Estrutura, Alvenaria...
    Entrega), pela classificação de cada serviço. É o que faz o cronograma prever a obra toda, por fase."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    recusar_se_desfazendo(o)
    versoes = await arm.consultar(Consulta("versoes_orcamento", u.tenant_id,
                                           [Filtro("obra_id", "==", obra_id), Filtro("ativa", "==", True)]))
    if not versoes:
        raise ErroNegocio("Importe o orçamento antes de organizar em fases.")
    contrato = next((v for v in versoes if v["tipo"] == TipoVersao.CONTRATO), versoes[0])
    if not contrato.get("padronizada") and any(v["tipo"] == TipoVersao.ADITIVO for v in versoes):
        # só o contrato muda de etapa: o aditivo ficaria nas etapas da planilha, com chaves que viram outras fases
        raise ErroNegocio("O orçamento tem aditivo: encaixe nas etapas padrão antes de lançar aditivos (desfaça o "
                          "aditivo, encaixe e lance de novo).")
    linhas = await itens_das_versoes(arm, u.tenant_id, obra_id, [contrato])
    item_ids = [x["id"] for x in linhas if x["tipo"] == "item" and x["obra_id"] == obra_id]
    titulo_ids = [x["id"] for x in sorted((x for x in linhas if x["tipo"] == "etapa" and x["obra_id"] == obra_id),
                                          key=lambda x: x["ordem"])]
    if not item_ids:
        raise ErroNegocio("O orçamento não tem serviços para organizar.")
    manuais = await topicos_manuais(arm, u.tenant_id, obra_id)
    em_uso = await etapas_em_uso(arm, u.tenant_id, obra_id)

    async def tx_corpo(tx):
        op = await _preparar_organizar_fases(tx, u, obra_id, contrato["id"], item_ids, titulo_ids, manuais, em_uso)
        r, entrada = executar_e_registrar(tx, u, op)
        return {**r, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


async def _preparar_organizar_fases(tx, u: UsuarioAtual, obra_id: str, versao_id: str,
                                    item_ids: list[str], titulo_ids: list[str],
                                    manuais: dict[tuple[str, str], str], em_uso: set[str] = frozenset()) -> Operacao:
    o = await obra_do_tenant(tx, u.tenant_id, obra_id)
    recusar_se_desfazendo(o)
    v = await tx.obter("versoes_orcamento", u.tenant_id, versao_id)
    if not v or not v["ativa"]:
        raise Conflito("Esta versão do orçamento já não está valendo.")
    # relê cada linha na transação (o que a transação decide, ela grava): buffer não faz consulta, só leitura pontual
    itens = [x for x in [await tx.obter("itens_orcamento", u.tenant_id, i) for i in item_ids]
             if x and x["obra_id"] == obra_id and x["tipo"] == "item"]
    titulos = [x for x in [await tx.obter("itens_orcamento", u.tenant_id, i) for i in titulo_ids]
               if x and x["obra_id"] == obra_id and x["tipo"] == "etapa"]
    # a etapa da planilha de cada item: a que ele guardou ao ser encaixado (`grupo`), ou o título de nível 1 da etapa
    # em que ele está (orçamento ainda como veio). É ela que decide a etapa da linha genérica ("Mão de obra").
    titulo_de = {t["etapa"]: t["descricao"] for t in titulos if t.get("nivel", 1) == 1}
    da_planilha = {x["id"]: x.get("grupo") or titulo_de.get(x["etapa"], "") for x in itens}
    reais = [x for x in itens if x.get("fonte") != FONTE_ESTIMATIVA]
    # já na lista padrão, o item fica onde está: pode ser a escolha da pessoa na prévia, que a palavra não desfaz
    ja = bool(v.get("padronizada"))
    nova = {x["id"]: x["etapa"] if ja and x["etapa"] in CHAVES_FIXAS
            else etapa_no_contexto(x["descricao"], da_planilha[x["id"]]) for x in reais}
    # o estimado de uma previsão anterior fica onde foi estimado (a de antes das etapas padrão, pela fase do título)
    nova.update({x["id"]: x["etapa"] if x["etapa"] in CHAVES_FIXAS
                 else etapa_da_estimativa(titulo_de.get(x["etapa"], ""), x["descricao"])
                 for x in itens if x.get("fonte") == FONTE_ESTIMATIVA})
    # valor real já orçado por fase de obra, para estimar o porte da obra e as fases que ainda faltam
    valor_por_fase: dict[str, int] = {}
    for x in reais:
        if f := fase_da_etapa(nova[x["id"]]):
            valor_por_fase[f] = valor_por_fase.get(f, 0) + (x.get("total") or 0)
    estimadas = estimar_fases_faltantes(valor_por_fase)  # fases padrão que faltam, com quantidade e valor estimados
    etapa_estimada = [etapa_da_estimativa(e.fase, e.descricao) for e in estimadas]
    todas = sorted(set(nova.values()) | set(etapa_estimada), key=ordem_da_etapa)
    # gasto lançado numa etapa que mudaria de sentido ("7" deixa de ser PAREDES)
    antes = {e["etapa"]: e["descricao"] for e in v.get("lista_etapas") or []}
    mudam = sorted((k for k in em_uso if k in antes and (k not in todas or nome_da_etapa(k) != antes[k])),
                   key=lambda k: (len(k), k))
    if mudam:
        raise Conflito(f"Há gasto lançado nas etapas {', '.join(mudam)}, que mudariam de sentido ao "
                       "encaixar nas etapas padrão. Reclassifique esses lançamentos antes.")
    # o tópico do item na etapa padrão: a etapa da planilha de onde ele veio (`grupo`), ou, quando ela já tem o nome
    # da etapa padrão, a linha de nível 2, como antes (só os títulos de nível 1 viram os das etapas)
    grupo_novo = {x["id"]: _grupo(da_planilha[x["id"]], nova[x["id"]]) for x in itens}
    movidos = [{**x, "etapa": nova[x["id"]], "grupo": grupo_novo[x["id"]]} for x in itens]

    def na_ordem(linhas: list[dict]) -> list[dict]:
        return sorted(titulos + linhas, key=lambda x: x.get("ordem", 0))

    nova_ordem = na_ordem(movidos)
    nova_lista, novos_topicos, _ = _topicos_das_linhas(u.tenant_id, obra_id, nova_ordem, manuais)
    topico_novo = {x["id"]: t for x, t in zip(nova_ordem, novos_topicos, strict=True) if x["tipo"] == "item"}
    if "lista_topicos" in v:
        lista_antes, topico_antes = v["lista_topicos"], {x["id"]: x.get("topico_id") for x in itens}
    else:
        # versão gravada antes de `lista_topicos`: o Desfazer volta para o que a leitura montava pelos itens
        ordem_antes = na_ordem(itens)
        lista_antes, ts, _ = _topicos_das_linhas(u.tenant_id, obra_id, ordem_antes, manuais)
        topico_antes = {x["id"]: t for x, t in zip(ordem_antes, ts, strict=True) if x["tipo"] == "item"}
    itens_mud = [{"id": x["id"], "antes": x["etapa"], "depois": nova[x["id"]],
                  "topico_antes": topico_antes[x["id"]], "topico_depois": topico_novo[x["id"]],
                  "grupo_antes": x.get("grupo", ""), "grupo_depois": grupo_novo[x["id"]]}
                 for x in itens if x["etapa"] != nova[x["id"]]
                 or topico_antes[x["id"]] != topico_novo[x["id"]] or x.get("grupo", "") != grupo_novo[x["id"]]]
    titulos_antes = [{"id": t["id"], "etapa": t["etapa"], "descricao": t["descricao"], "ordem": t.get("ordem", 0),
                      "numero": t.get("numero", "")} for t in titulos]
    # reaproveita os títulos existentes para as etapas; cria os que faltarem (ids determinísticos, idempotente)
    das_fases = {(e["etapa"], e["descricao"]) for e in v.get("lista_etapas") or []}
    principais = [t for t in titulos if t.get("nivel", 1) == 1 or (t["etapa"], t["descricao"]) in das_fases]
    de_fase = {t["id"] for t in principais}
    reusar = [{"id": principais[i]["id"], "etapa": todas[i], "descricao": nome_da_etapa(todas[i]), "ordem": i,
               "numero": str(ordem_da_etapa(todas[i]) + 1)}
              for i in range(min(len(principais), len(todas)))]
    criar_tit = [(id_derivado(u.tenant_id, "etapa-padrao", versao_id, k), k, nome_da_etapa(k), i)
                 for i, k in enumerate(todas) if i >= len(principais)]
    grupos = []
    for k, t in enumerate(nova_ordem):
        if t["tipo"] == "etapa" and t["id"] not in de_fase:
            abaixo = next((x for x in nova_ordem[k + 1:] if x["tipo"] == "item" or x["id"] in de_fase), None)
            if abaixo and abaixo["tipo"] == "item" and abaixo["etapa"] != t["etapa"]:
                grupos.append({"id": t["id"], "etapa": abaixo["etapa"]})
    # itens estimados (os serviços típicos de cada fase que falta), id determinístico — reexecutar atualiza, não duplica
    criar_item = []
    for n, e in enumerate(estimadas):
        iid = id_derivado(u.tenant_id, "item-estimado", versao_id, f"{e.fase}:{n}:{e.descricao}")
        criar_item.append((iid, e, n, await tx.obter("itens_orcamento", u.tenant_id, iid) is not None))
    if (not itens_mud and not criar_tit and not grupos and not any(not existe for (_, _, _, existe) in criar_item)
            and all(t["descricao"] == nome_da_etapa(k) and t["etapa"] == k
                    for t, k in zip(principais, todas, strict=False))):
        raise ErroNegocio("O orçamento já está nas etapas padrão, e a obra já está prevista inteira.")

    def aplicar() -> dict:
        for m in itens_mud:
            tx.atualizar("itens_orcamento", u.tenant_id, m["id"], {"etapa": m["depois"],
                                                                    "topico_id": m["topico_depois"],
                                                                    "grupo": m["grupo_depois"]})
        for r in reusar:
            tx.atualizar("itens_orcamento", u.tenant_id, r["id"],
                         {"etapa": r["etapa"], "descricao": r["descricao"], "ordem": r["ordem"],
                          "numero": r["numero"]})
        for g in grupos:
            tx.atualizar("itens_orcamento", u.tenant_id, g["id"], {"etapa": g["etapa"]})
        for tid, k, nome, i in criar_tit:
            tx.criar("itens_orcamento", doc(ItemOrcamento(
                id=tid, tenant_id=u.tenant_id, obra_id=obra_id, versao_id=versao_id, numero=str(ordem_da_etapa(k) + 1),
                ordem=i, tipo="etapa", nivel=1, etapa=k, descricao=nome)))
        for (iid, e, n, existe), k in zip(criar_item, etapa_estimada, strict=True):
            vu = round(e.valor_centavos * 1000 / e.quantidade_milesimos) if e.quantidade_milesimos else 0
            campos = {"etapa": k, "descricao": e.descricao, "codigo": e.codigo, "fonte": FONTE_ESTIMATIVA,
                      "unidade": e.unidade, "quantidade": e.quantidade_milesimos, "valor_unitario": vu,
                      "total": e.valor_centavos}
            if existe:
                tx.atualizar("itens_orcamento", u.tenant_id, iid, campos)
            else:
                tx.criar("itens_orcamento", doc(ItemOrcamento(
                    id=iid, tenant_id=u.tenant_id, obra_id=obra_id, versao_id=versao_id,
                    ordem=100 + ordem_da_fase(e.fase) * 20 + n, tipo="item", nivel=2, **campos)))
        # cache de etapas da versão (a apropriação lê dele). O total não muda: o estimado fica fora do orçado.
        tx.atualizar("versoes_orcamento", u.tenant_id, versao_id,
                     {"lista_etapas": [{"etapa": k, "descricao": nome_da_etapa(k)} for k in todas],
                      "lista_topicos": nova_lista, "padronizada": True})
        nomes = [nome_da_etapa(k) for k in todas]
        evento(tx, u.tenant_id, "orcamento.organizado_fases", obra_id,
               {"itens": len(itens_mud), "estimadas": len(estimadas), "fases": nomes})
        return {"itens": len(itens_mud), "estimadas": len(estimadas), "fases": nomes}

    # Desfazer: volta a etapa dos itens reais, o nome dos títulos, o cache e o total; zera os itens estimados criados.
    receita = {"op": "restaurar_etapas", "obra_id": obra_id, "versao_id": versao_id,
               "itens": [{"id": m["id"], "etapa": m["antes"], "topico_id": m["topico_antes"],
                          "grupo": m["grupo_antes"]} for m in itens_mud],
               "titulos": titulos_antes, "lista_etapas": v.get("lista_etapas"), "lista_topicos": lista_antes,
               "total": v.get("total"), "padronizada": bool(v.get("padronizada")),
               "zerar": [iid for (iid, e, n, existe) in criar_item if not existe]}
    ne = len(estimadas)
    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: (f"Previu a obra completa: {len(todas)} etapas"
                                     f"{f', {ne} estimadas' if ne else ''}"),
                    desfazer=lambda d: receita)


async def preparar_restaurar_etapas(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz a organização em fases: volta a etapa de cada serviço e o nome dos títulos reaproveitados."""
    o = await obra_do_tenant(tx, u.tenant_id, receita["obra_id"])
    recusar_se_desfazendo(o)
    itens = [m for m in receita.get("itens", []) if await tx.obter("itens_orcamento", u.tenant_id, m["id"])]
    titulos, titulos_agora = [], []
    for t in receita.get("titulos", []):
        if atual := await tx.obter("itens_orcamento", u.tenant_id, t["id"]):
            titulos.append(t)
            titulos_agora.append({"id": t["id"], "etapa": atual["etapa"], "descricao": atual["descricao"],
                                  "ordem": atual.get("ordem", 0), "numero": atual.get("numero", "")})
    versao_id = receita.get("versao_id")
    v = await tx.obter("versoes_orcamento", u.tenant_id, versao_id) if versao_id else None
    lista_atual = v.get("lista_etapas") if v else None  # a de agora, para o Refazer
    topicos_atual = v.get("lista_topicos") if v else None
    total_atual = v.get("total") if v else None
    padronizada_atual = bool(v.get("padronizada")) if v else None
    zerar = [z for z in receita.get("zerar", []) if await tx.obter("itens_orcamento", u.tenant_id, z)]

    def aplicar() -> dict:
        for m in itens:
            # receita de antes dos tópicos do orçamento não tem `topico_id`: só a etapa volta
            tx.atualizar("itens_orcamento", u.tenant_id, m["id"],
                         {"etapa": m["etapa"], **{k: m[k] for k in ("topico_id", "grupo") if k in m}})
        for t in titulos:
            campos_t = {"etapa": t["etapa"], "descricao": t["descricao"]}
            campos_t.update({k: t[k] for k in ("ordem", "numero") if k in t})
            tx.atualizar("itens_orcamento", u.tenant_id, t["id"], campos_t)
        for z in zerar:  # os itens estimados que criamos somem (quantidade/valor zerados)
            tx.atualizar("itens_orcamento", u.tenant_id, z, {"quantidade": 0, "valor_unitario": 0, "total": 0})
        if v is not None:
            campos = {}
            if receita.get("lista_etapas") is not None:
                campos["lista_etapas"] = receita["lista_etapas"]
            if receita.get("lista_topicos") is not None:
                campos["lista_topicos"] = receita["lista_topicos"]
            if receita.get("total") is not None:
                campos["total"] = receita["total"]
            if receita.get("padronizada") is not None:
                campos["padronizada"] = receita["padronizada"]
            if campos:
                tx.atualizar("versoes_orcamento", u.tenant_id, versao_id, campos)
        return {"itens": len(itens)}

    inversa = {"op": "restaurar_etapas", "obra_id": receita["obra_id"], "versao_id": versao_id,
               "itens": [{k: m[k] for k in ("id", "etapa", "topico_id", "grupo") if k in m} for m in itens],
               "titulos": titulos_agora,
               "lista_etapas": lista_atual, "lista_topicos": topicos_atual, "total": total_atual,
               "padronizada": padronizada_atual}
    return Operacao(obra_id=receita["obra_id"], aplicar=aplicar,
                    texto=lambda d: "Desfez a organização do orçamento em fases", desfazer=lambda d: inversa)
