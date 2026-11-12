"""Orçamento da obra como EAP (etapa → item), em versões: o contrato e os aditivos.

A planilha de orçamento de obra é de serviços, não de materiais: "Pintura látex 3.119 m²" é um item da etapa
Revestimentos, não um insumo para pôr no almoxarifado. Por isso cada linha vira um item com o número, o código e a fonte
dela, nada é juntado (o mesmo serviço em duas etapas são dois itens, e o total fecha no centavo com a planilha) e nada
vira insumo.

Cada importação é uma versão. Reimportar o contrato substitui a versão inteira, e o aditivo soma sem tocar no contrato:
é o que o engenheiro precisa para responder "quanto custou o aditivo". Desfazer é desligar a versão, porque a porta de
armazenamento não apaga.
"""
import hashlib
import re
from decimal import Decimal

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.cronograma import fase, ordem_da_fase
from canteiro_dominio.gastos import gastos_no_orcamento
from canteiro_dominio.modelos import Insumo, ItemOrcamento, Saldo, StatusPedido, TipoVersao, VersaoOrcamento
from canteiro_dominio.regras import (
    EM_COTACAO,
    ItemParaCompras,
    RegraViolada,
    chave_do_nome,
    insumo_com_aditivo,
    itens_para_compras,
)
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

from ..auth import UsuarioAtual
from ..esquemas import (
    Aviso,
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
    obra_do_tenant,
    receita_inversa,
    recusar_se_desfazendo,
    verbo_desfazer,
)
from .estoque import (
    criado,
    gravar_insumo_novo,
    id_derivado,
    id_insumo,
    linhas_de,
    preparar_restaurar_insumos,
    receita_insumos,
)
from .obras import insumos_da_obra
from .orcamento import MAX_LINHAS, _arredonda, _grade, _normal, _numero, _texto, _vazio

# Itens por transação: uma escrita cada, folgado nas 500 da transação portável.
ITENS_POR_TRANSACAO = 400
# Linha de subtotal ou total no meio da planilha: soma o que já está nos itens, contar de novo dobraria a etapa.
SUBTOTAL = re.compile(r"^(sub ?total|total|soma)\b")
NUMERO_ITEM = re.compile(r"^\d+(\.\d+)*\.?$")
# O nome que `importar_estrutura` grava: é por ele que se sabe que a versão veio das pranchas (material, não serviço).
SUFIXO_PROJETO = " (projeto).csv"
# Aditivo com a obra aprovada mexe nos insumos na transação que liga a versão: até 2 escritas cada, folgado nas 500.
MAX_INSUMOS_ADITIVO = 200


def _ordinal(n: int) -> str:
    return f"{n}º"


def _numero_item(t: str) -> str:
    """'3.2.1.' perde o ponto final; o resto fica como veio ("A1.2" de aditivo também dá etapa e nível pelos pontos)."""
    return t[:-1] if t.endswith(".") and NUMERO_ITEM.match(t) else t


def _ler(grade: list[list], m: Mapeamento, tipo: TipoVersao):
    itens: list[ItemPrevia] = []
    ignoradas: list[LinhaIgnorada] = []
    erros: list[ErroLinha] = []
    avisos: list[Aviso] = []
    unidade_do_codigo: dict[str, tuple[str, int]] = {}
    etapa_atual = ""

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
        if SUBTOTAL.match(_normal(descricao)):
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="subtotal ou total"))
            continue
        numero = _numero_item(_texto(cel(row, m.item)))
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
        if not unidade and _vazio(q_crua):
            # título de etapa: sem unidade e sem quantidade
            if erros_linha:
                erros.extend(erros_linha)
                continue
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
                                valor_unitario=unit, total=total, linha=n))
    return itens, ignoradas, erros, avisos


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
    planilha com o das pranchas conta duas vezes, e daí Compras e o Mestre propõem comprar em dobro. Só avisa; quem
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


async def importar(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str, m: Mapeamento,
                   tipo: TipoVersao, descricao: str, data: str | None, aplicar: bool,
                   chave: str | None, escolhidas: dict[int, str] | None = None, somar: bool = False,
                   ignorar: list[int] | None = None) -> tuple[PreviaVersao, bool]:
    """Devolve (prévia ou versão gravada, criada agora?).

    `somar` (contrato, antes de aprovar): grava um contrato novo com os itens do vigente mais os da planilha, no lugar
    do vigente. É o "completar o orçamento" de quem trouxe as pranchas e agora traz a planilha: aditivo é documento com
    o cliente depois do contrato fechado, e chamar o complemento de aditivo inflava o % de aditivo."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    faltando = [r for c, r in (("nome", "descrição"), ("unidade", "unidade"), ("quantidade", "quantidade"))
                if getattr(m, c) is None]
    if faltando:
        raise ErroNegocio(f"Diga qual coluna é a {', '.join(faltando)}.")
    if aplicar and not chave:
        raise ErroNegocio("Falta a chave de idempotência.")
    escolhidas = escolhidas or {}
    ignorar_ = sorted(set(ignorar or ()))
    # o sufixo só aparece com somar/ignorar: a assinatura de quem não usa nenhum dos dois fica a de antes
    extra = ("|somar" if somar else "") + (f"|ignorar={ignorar_}" if ignorar_ else "")
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
        # Compras já saiu do orçamento aprovado: um contrato novo deixaria os insumos e os pedidos descolados dele
        raise Conflito("O orçamento já foi aprovado: desfaça a aprovação ou lance um aditivo." if vigente
                       else "O orçamento já foi aprovado: desfaça a aprovação para importar o contrato.")
    if ignorar_:
        tirar = set(ignorar_)
        itens = [x for x in itens if not (x.tipo == "item" and x.ordem in tirar)]
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
        _marcar_repetidos(itens, existentes, avisos)
    so_itens = [x for x in itens if x.tipo == "item"]
    previa = PreviaVersao(
        aplicado=False, tipo=tipo, itens=itens, ignoradas=ignoradas, erros=erros, avisos=avisos,
        total=sum(x.total for x in so_itens), n_itens=len(so_itens),
        n_etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in itens),
        etapas=_totais_por_etapa(itens, descricoes_contrato),
        substitui=(Substituida(versao_id=vigente["id"], total=vigente["total"], itens=vigente["itens"])
                   if vigente and tipo == TipoVersao.CONTRATO else None))
    if somar:
        previa.total_apos = vigente["total"] + previa.total
    if tipo == TipoVersao.ADITIVO and not vigente:
        raise ErroNegocio("Importe o orçamento contratado antes do aditivo.")
    linhas = _somadas(existentes, vigente, itens) if somar else itens
    renumeradas: dict[str, str] = {}
    if somar and (ordem := _pranchas_depois_das_preliminares(vigente, linhas)):
        if _chaves_que_mudam(linhas, ordem) & await etapas_em_uso(arm, u.tenant_id, obra_id):
            ordem = None       # renumerar levaria o gasto (ou pedido) lançado numa etapa para outra
        else:
            renumeradas = _renumeradas_no_desfazer(linhas, ordem, {e["etapa"] for e in vigente["lista_etapas"]})
            nomes = _nomes_das_etapas(linhas)
            pranchas = vigente["lista_etapas"][0]["etapa"]
            antes_dela = ordem[ordem.index(pranchas) - 1]
            linhas = _reordenar(linhas, ordem)
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
    compras = (await _compras_do_aditivo(arm, u.tenant_id, obra_id, so_itens, nome_arquivo)
               if aprovada and tipo == TipoVersao.ADITIVO else [])

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
        gerou_compras=bool(compras))
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
        lidos = [await tx.obter("insumos", u.tenant_id, x["id"]) for x in compras]
        novos: list[dict] = []
        restaurar: list[dict] = []

        # `origem` fica só no documento (fora do modelo do domínio): é o que a tela usa para dizer o que o Desfazer faz
        d_versao = {**doc(versao), **({"origem": "somar"} if somar else {})}

        def aplicar_():
            _aplicar_compras(tx, u, obra_id, compras, lidos, novos, restaurar)
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
                          **({"renumeradas": renumeradas} if renumeradas else {}),
                          **({"compras": receita_insumos(obra_id, f"compras do {_ordinal(numero)} aditivo",
                                                         arquivar=novos, restaurar=restaurar)}
                             if novos or restaurar else {})})
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
        raise Conflito("Já há lançamento (gasto, pedido, tópico ou cronograma) na etapa "
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
    """As chaves de etapa que algum lançamento da obra guarda (gasto, pedido, tópico, movimento, insumo, cronograma).
    Renumerar uma delas levaria o lançamento, calado, para outra etapa."""
    from .cronograma import id_cronograma

    def q(colecao: str, *filtros: Filtro):
        return leitor.consultar(Consulta(colecao, tenant_id, [Filtro("obra_id", "==", obra_id), *filtros]))

    usadas = {g.get("etapa") for g in await q("gastos", Filtro("estornado", "==", False))}
    for p in await q("pedidos"):
        if p["status"] != StatusPedido.CANCELADO:
            usadas |= {x.get("etapa") for x in p.get("linhas", [])}
    for colecao in ("topicos", "movimentos", "insumos"):
        usadas |= {x.get("etapa") for x in await q(colecao)}
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


async def _compras_do_aditivo(leitor, tenant_id: str, obra_id: str, so_itens: list[ItemPrevia],
                              nome_arquivo: str) -> list[dict]:
    """O que o aditivo muda em Compras quando a obra já está aprovada. Fora da transação porque consulta os insumos."""
    existentes = {chave_do_nome(i["nome"]): i for i in await insumos_da_obra(leitor, tenant_id, obra_id)}
    tipo = "material" if (nome_arquivo or "").endswith(SUFIXO_PROJETO) else "servico"
    plano: list[dict] = []
    divergentes: list[str] = []
    for c in itens_para_compras(x.model_dump() for x in so_itens):
        atual = existentes.get(chave_do_nome(c.nome))
        if atual and atual["unidade"].strip().casefold() != c.unidade.strip().casefold():
            divergentes.append(f"{c.nome} ({atual['unidade']} e {c.unidade})")
        elif c.quantidade > 0 or (atual and c.quantidade < 0):
            # supressão de algo que nunca entrou em Compras não tem o que reduzir
            plano.append({"compra": c, "existe": atual is not None, "tipo": tipo,
                          "id": atual["id"] if atual else id_insumo(tenant_id, obra_id, c.nome)})
    if divergentes:
        raise ErroNegocio(f"Item do aditivo com unidade diferente da que está em Compras: {'; '.join(divergentes)}.")
    if len(plano) > MAX_INSUMOS_ADITIVO:
        raise ErroNegocio(f"Aditivo com mais de {MAX_INSUMOS_ADITIVO} itens numa obra aprovada; divida em partes.")
    return plano


def _aplicar_compras(tx, u: UsuarioAtual, obra_id: str, plano: list[dict], lidos: list[dict | None], novos: list[dict],
                     restaurar: list[dict]) -> None:
    """Grava o plano do aditivo e anota em `novos` e `restaurar` o que o Desfazer precisa."""
    for x, lido in zip(plano, lidos, strict=True):
        c: ItemParaCompras = x["compra"]
        if x["existe"]:
            if not lido or lido.get("arquivado"):
                raise Conflito(f"'{c.nome}' mudou agora há pouco; envie a planilha de novo.")
            depois = insumo_com_aditivo(lido, c)
            tx.atualizar("insumos", u.tenant_id, x["id"], depois)
            restaurar.append({"id": x["id"], "antes": {k: lido.get(k) for k in depois}, "depois": depois})
            continue
        # "B": a curva pesa o item contra a obra inteira, e aqui só se conhecem as linhas do aditivo
        d = doc(Insumo(id=x["id"], tenant_id=u.tenant_id, obra_id=obra_id, nome=c.nome, unidade=c.unidade,
                       tipo=x["tipo"], precisa_ate_o_fim=c.quantidade, valor_unitario=c.valor_unitario, etapa=c.etapa))
        gravar_insumo_novo(tx, d, lido)
        tx.salvar("saldos", doc(Saldo(id=x["id"], tenant_id=u.tenant_id, obra_id=obra_id, saldo=0,
                                      atualizado_em=d["criado_em"])))
        novos.append(criado(d, 0))


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
        raise Conflito("Já há lançamento (gasto, pedido, tópico ou cronograma) na etapa "
                       f"{', '.join(f'{k} {nomes.get(k, "")}'.strip() for k in presas)}: renumerar levaria o "
                       "lançamento para outra etapa.")
    renumeradas = _renumeradas_no_desfazer(linhas, ordem, set(atual))
    linhas = _reordenar(linhas, ordem)
    so_itens = [x for x in linhas if x.tipo == "item"]
    versao = VersaoOrcamento(
        id=id_, tenant_id=u.tenant_id, obra_id=obra_id, tipo=TipoVersao.CONTRATO, descricao=vigente["descricao"],
        data=vigente.get("data"), arquivo=vigente.get("arquivo", ""), itens=len(so_itens),
        etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in linhas), total=sum(x.total for x in so_itens),
        substitui=vigente["id"], assinatura=assinatura,
        lista_etapas=[{"etapa": e.etapa, "descricao": e.descricao} for e in _totais_por_etapa(linhas) if e.etapa])
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
    compras = await preparar_restaurar_insumos(tx, u, receita["compras"]) if receita.get("compras") else None

    def aplicar():
        if compras is not None:
            compras.aplicar()
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

    descricoes: dict[str, str] = {}
    somas: dict[str, list[int]] = {}
    for x in itens:
        if x["tipo"] == "etapa" and x["nivel"] == 1:
            # a descrição do contrato manda; a do aditivo só para etapa que nasceu nele
            if x["versao_id"] in contrato_ids or x["etapa"] not in descricoes:
                descricoes[x["etapa"]] = x["descricao"]
            somas.setdefault(x["etapa"], [0, 0])
        elif x["tipo"] == "item":
            s = somas.setdefault(x["etapa"], [0, 0])
            s[0 if x["versao_id"] in contrato_ids else 1] += x["total"]
    gasto = await andamento_por_etapa(arm, tenant_id, obra_id)
    # Gasto avulso (comprovante confirmado, sem pedido) é dinheiro que saiu: soma no comprometido e no realizado da
    # etapa dele, e por isso mexe no saldo e no estouro. A repartição é do domínio (inclusive o que não entra em etapa).
    avulsos = gastos_no_orcamento(await gastos_da_obra(arm, tenant_id, obra_id), {e for e in somas if e})
    etapas = [_etapa(e, descricoes.get(e, "Sem etapa" if not e else f"Etapa {e}"), c, a, gasto.pop(e, {}),
                     avulsos.por_etapa.get(e, 0))
              for e, (c, a) in somas.items()]
    # gasto numa etapa que não está no orçamento (pedido de antes da importação, ou etapa de versão desfeita)
    sem = {k: sum(g[k] for g in gasto.values()) for k in ("cotando", "comprometido", "realizado")}
    fora = avulsos.fora_das_etapas
    contratado = sum(e.contratado for e in etapas)
    aditivos = sum(e.aditivos for e in etapas)
    andamento = {k: sum(getattr(e, k) for e in etapas) + sem[k] for k in ("cotando", "comprometido", "realizado")}
    andamento["comprometido"] += fora
    andamento["realizado"] += fora
    return OrcamentoObra(
        versoes=[{**v, "registro_id": id_derivado(tenant_id, "registro", v["id"]), **_textos_da_versao(v, versoes),
                  "situacao": await _situacao(arm, tenant_id, v)}
                 for v in versoes],
        itens=[{**x, "curva": curvas.get(x["id"])} for x in itens], etapas=etapas,
        totais=TotaisOrcamento(contratado=contratado, aditivos=aditivos, atual=contratado + aditivos,
                               saldo=contratado + aditivos - andamento["comprometido"],
                               aditivos_pct=pct(aditivos, contratado) if contratado else None,
                               n_itens=sum(x["tipo"] == "item" for x in itens),
                               n_etapas=len(descricoes), sem_etapa=sem,
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


def _campos_preco_insumo(insumo: dict, preco: int) -> dict:
    """O preço do insumo e, se ele veio da aprovação, o rastro dela: senão o Desfazer da aprovação o acharia editado."""
    campos: dict = {"valor_unitario": preco}
    if a := insumo.get("aprovacao"):
        campos["aprovacao"] = {**a, "depois": {**a["depois"], "valor_unitario": preco}}
    return campos


async def editar_item(arm, u: UsuarioAtual, obra_id: str, item_id: str, mudancas: dict) -> dict:
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    if not mudancas:
        raise ErroNegocio("Nada para mudar.")
    item = await arm.obter("itens_orcamento", u.tenant_id, item_id)
    if not item or item["obra_id"] != obra_id:
        raise NaoEncontrado("Item do orçamento não encontrado nesta obra.")
    insumo_id, preco = None, None
    if o["status"] != "em_orcamento" and "valor_unitario" in mudancas:
        insumo_id, preco = await _preco_no_insumo(arm, u.tenant_id, obra_id, item, mudancas["valor_unitario"])

    async def tx_corpo(tx):
        op = await _preparar_editar_item(tx, u, obra_id, item_id, mudancas, insumo_id, preco)
        r, entrada = executar_e_registrar(tx, u, op)
        return {**r, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


async def _preco_no_insumo(leitor, tenant_id: str, obra_id: str, item: dict, unitario: int) -> tuple[str | None, int]:
    """O insumo deste item e o preço médio dele com a linha já preenchida. Fora da transação porque consulta."""
    chave = chave_do_nome(item["descricao"])
    insumo = next((i for i in await insumos_da_obra(leitor, tenant_id, obra_id)
                   if chave_do_nome(i["nome"]) == chave), None)
    if insumo is None:
        return None, 0
    ativas = [v for v in await versoes_da_obra(leitor, tenant_id, obra_id) if v["ativa"]]
    mesmas = [x for x in await itens_das_versoes(leitor, tenant_id, obra_id, ativas)
              if x["tipo"] == "item" and chave_do_nome(x["descricao"]) == chave]
    total = _arredonda(Decimal(item["quantidade"]) * unitario / 1000)
    linhas = [{**x, "total": total} if x["id"] == item["id"] else x for x in mesmas]
    return insumo["id"], itens_para_compras(linhas)[0].valor_unitario


async def _preparar_editar_item(tx, u: UsuarioAtual, obra_id: str, item_id: str, mudancas: dict,
                                insumo_id: str | None, preco: int | None) -> Operacao:
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
    insumo = await tx.obter("insumos", u.tenant_id, insumo_id) if insumo_id else None
    # insumo cadastrado com preço próprio fica com ele; só o gerado pela aprovação acompanha o orçamento
    gerado = insumo is not None and insumo.get("aprovacao") and insumo["aprovacao"]["antes"] is None
    if insumo is not None and insumo.get("valor_unitario", 0) and not gerado:
        insumo = None
    total_versao = v["total"] + depois["total"] - antes["total"]

    def aplicar() -> dict:
        tx.atualizar("itens_orcamento", u.tenant_id, item_id, depois)
        tx.atualizar("versoes_orcamento", u.tenant_id, v["id"],
                     {"total": total_versao, "edicoes": v.get("edicoes", 0) + 1})
        if insumo is not None:
            tx.atualizar("insumos", u.tenant_id, insumo["id"], _campos_preco_insumo(insumo, preco))
        evento(tx, u.tenant_id, "orcamento.item_editado", obra_id, {"item_id": item_id, "total": depois["total"]})
        return {"item": {**item, **depois}, "total_versao": total_versao}

    partes = []
    if novo["quantidade"] != item["quantidade"]:
        partes.append(f"quantidade {fmt_qtd(item['quantidade'])} → {fmt_qtd(novo['quantidade'])} {item['unidade']}")
    if novo["valor_unitario"] != item["valor_unitario"]:
        partes.append(f"preço {fmt_reais(item['valor_unitario'])} → {fmt_reais(novo['valor_unitario'])}")
    receita = {"op": "editar_item_orcamento", "obra_id": obra_id, "item_id": item_id, "antes": antes, "depois": depois,
               "pos_aprovacao": aprovado,
               "insumo": {"id": insumo["id"], "antes": insumo.get("valor_unitario", 0), "depois": preco}
               if insumo is not None else None}
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
    alvo = receita.get("insumo")
    insumo = await tx.obter("insumos", u.tenant_id, alvo["id"]) if alvo else None
    if insumo is not None and insumo.get("arquivado"):
        insumo = None                  # aprovação desfeita: não há mais preço de Compras para voltar
    if insumo is not None and insumo.get("valor_unitario", 0) != alvo["depois"]:
        raise Conflito(f"O preço de {insumo['nome']} mudou depois; não dá para desfazer.")
    volta = receita["antes"]
    total_versao = v["total"] + volta["total"] - receita["depois"]["total"]

    def aplicar() -> dict:
        tx.atualizar("itens_orcamento", u.tenant_id, item["id"], volta)
        tx.atualizar("versoes_orcamento", u.tenant_id, v["id"],
                     {"total": total_versao, "edicoes": v.get("edicoes", 0) + 1})
        if insumo is not None:
            tx.atualizar("insumos", u.tenant_id, insumo["id"], _campos_preco_insumo(insumo, alvo["antes"]))
        return {**item, **volta}

    inversa = {**receita_inversa(receita),
               "insumo": {**alvo, "antes": alvo["depois"], "depois": alvo["antes"]} if alvo else None}
    return Operacao(obra_id=receita["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"{verbo_desfazer(receita)}: {item['descricao']} no orçamento voltou",
                    desfazer=lambda d: inversa)


async def gastos_da_obra(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    """Os gastos confirmados e não estornados da obra (o domínio separa os de pedido)."""
    return await leitor.consultar(Consulta("gastos", tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                  Filtro("estornado", "==", False)]))


def _etapa(etapa: str, descricao: str, contratado: int, aditivos: int, gasto: dict[str, int],
           avulsos: int = 0) -> EtapaOrcamento:
    """Saldo e estouro saem daqui prontos: quem lê (tela, agente) só mostra."""
    gasto = {"cotando": gasto.get("cotando", 0), "comprometido": gasto.get("comprometido", 0) + avulsos,
             "realizado": gasto.get("realizado", 0) + avulsos}
    atual, comprometido = contratado + aditivos, gasto["comprometido"]
    return EtapaOrcamento(etapa=etapa, descricao=descricao, contratado=contratado, aditivos=aditivos, atual=atual,
                          saldo=atual - comprometido, estourou=comprometido > atual, gastos_avulsos=avulsos, **gasto)


CEM = 10000                            # 100% em centésimos de ponto percentual (290 = 2,9%)


def pct(parte: int, todo: int) -> int:
    """`parte` sobre `todo` em centésimos de %, arredondado para o mais próximo (meio para cima)."""
    return (parte * CEM + todo // 2) // todo if todo else 0


async def andamento_por_etapa(leitor, tenant_id: str, obra_id: str) -> dict[str, dict[str, int]]:
    return andamento_dos_pedidos(await pedidos_da_obra(leitor, tenant_id, obra_id))


async def pedidos_da_obra(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    return await leitor.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id)]))


def _recebido(x: dict) -> int:
    """O que a linha do pedido já recebeu, pelo preço do pedido: a mesma soma das entregas, sem ler cada uma."""
    q, recebida = x["quantidade"], x.get("recebida", 0)
    return (recebida * x.get("total", 0) + q // 2) // q if q else 0


def andamento_dos_pedidos(pedidos: list[dict]) -> dict[str, dict[str, int]]:
    """Cotando, comprometido e realizado por etapa, somados das linhas dos pedidos da obra (centavos).

    Realizado sai do que cada linha já recebeu. Comprometido é o maior entre o pedido e o entregue (entrega a mais
    também custa), exceto no pedido encerrado com sobra, que só compromete o que chegou.
    """
    soma: dict[str, dict[str, int]] = {}
    for p in pedidos:
        st = StatusPedido(p["status"])
        if st == StatusPedido.CANCELADO:
            continue
        for x in linhas_de(p):
            g = soma.setdefault(x.get("etapa") or "", {"cotando": 0, "comprometido": 0, "realizado": 0})
            total = x.get("total", 0)
            if st in EM_COTACAO:
                g["cotando"] += total
                continue
            recebido = _recebido(x)
            g["realizado"] += recebido
            encerrado_com_sobra = st == StatusPedido.CONFERIDO and x.get("recebida", 0) < x["quantidade"]
            g["comprometido"] += recebido if encerrado_com_sobra else max(total, recebido)
    return soma


def entregue_a_pagar(realizado: int, pago: int) -> int:
    """Entregue (ou medido) e ainda sem pagamento: realizado − pago, a conta do desembolso. Por isso "gasto até hoje"
    (realizado) = pago + entregue a pagar. Quem pagou além do que chegou (adiantamento) fica com 0 aqui, e a diferença
    é crédito que o desembolso desconta dos próximos meses."""
    return max(0, realizado - pago)


def a_pagar_por_etapa(pedidos: list[dict], gastos: list[dict], total: int) -> dict[str, int]:
    """O entregue a pagar da obra (`total`, a conta do desembolso) repartido pelas etapas. Por pedido, o recebido menos
    o que já se pagou dele vai para as etapas das linhas na proporção do recebido; depois o `total` é dividido na
    proporção desse bruto. É o que faz as etapas fecharem com o total quando um pedido foi pago adiantado: o total já
    abate esse adiantamento, e a etapa do pedido entregue não pode mostrar a pagar o que a obra não deve."""
    pago: dict[str, int] = {}
    for g in gastos:
        if g.get("pedido_id"):
            pago[g["pedido_id"]] = pago.get(g["pedido_id"], 0) + g["valor"]
    soma: dict[str, int] = {}
    for p in pedidos:
        st = StatusPedido(p["status"])
        if st == StatusPedido.CANCELADO or st in EM_COTACAO:
            continue
        por_etapa: dict[str, int] = {}
        for x in linhas_de(p):
            if r := _recebido(x):
                por_etapa[x.get("etapa") or ""] = por_etapa.get(x.get("etapa") or "", 0) + r
        falta = sum(por_etapa.values()) - pago.get(p["id"], 0)
        if falta <= 0:
            continue
        etapas = list(por_etapa)
        for k, v in _repartir(falta, [por_etapa[e] for e in etapas]).items():
            soma[etapas[k]] = soma.get(etapas[k], 0) + v
    return _maior_resto(total, soma) if total < sum(soma.values()) else soma


def _maior_resto(valor: int, pesos: dict[str, int]) -> dict[str, int]:
    """`valor` pelas chaves na proporção dos pesos; o centavo que sobra do arredondamento vai para o maior resto."""
    todo = sum(pesos.values())
    if not todo or not valor:
        return {k: 0 for k in pesos}
    base = {k: valor * w // todo for k, w in pesos.items()}
    sobra = valor - sum(base.values())
    for k in sorted(pesos, key=lambda k: -(valor * pesos[k] % todo))[:sobra]:
        base[k] += 1
    return base


def _repartir(valor: int, pesos: list[int]) -> dict[int, int]:
    """`valor` pelos índices na proporção dos pesos, arredondando o acumulado: a soma fecha no centavo."""
    total, saida, acum, antes = sum(pesos), {}, 0, 0
    for k, w in enumerate(pesos):
        acum += w
        agora = (2 * valor * acum + total) // (2 * total)
        saida[k] = agora - antes
        antes = agora
    return saida


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
    linhas = await itens_das_versoes(arm, u.tenant_id, obra_id, [contrato])
    item_ids = [x["id"] for x in linhas if x["tipo"] == "item" and x["obra_id"] == obra_id]
    titulo_ids = [x["id"] for x in sorted((x for x in linhas if x["tipo"] == "etapa" and x["obra_id"] == obra_id),
                                          key=lambda x: x["ordem"])]
    if not item_ids:
        raise ErroNegocio("O orçamento não tem serviços para organizar.")

    async def tx_corpo(tx):
        op = await _preparar_organizar_fases(tx, u, obra_id, contrato["id"], item_ids, titulo_ids)
        r, entrada = executar_e_registrar(tx, u, op)
        return {**r, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


async def _preparar_organizar_fases(tx, u: UsuarioAtual, obra_id: str, versao_id: str,
                                    item_ids: list[str], titulo_ids: list[str]) -> Operacao:
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
    nova_fase = {x["id"]: fase(x["descricao"], x.get("codigo", ""), x.get("unidade", "")) for x in itens}
    fases = sorted({f for f in nova_fase.values()}, key=ordem_da_fase)
    chave = {f: str(i + 1) for i, f in enumerate(fases)}  # chave numérica da etapa, na ordem de obra ("1 Fundação"...)
    itens_mud = [{"id": x["id"], "antes": x["etapa"], "depois": chave[nova_fase[x["id"]]]}
                 for x in itens if x["etapa"] != chave[nova_fase[x["id"]]]]
    titulos_antes = [{"id": t["id"], "etapa": t["etapa"], "descricao": t["descricao"]} for t in titulos]
    # reaproveita os títulos existentes como as fases; cria os que faltarem (ids determinísticos, idempotente)
    reusar = [{"id": titulos[i]["id"], "etapa": chave[f], "descricao": f} for i, f in enumerate(fases)
              if i < len(titulos)]
    criar = [(id_derivado(u.tenant_id, "etapa-fase", versao_id, f), chave[f], f, i)
             for i, f in enumerate(fases) if i >= len(titulos)]
    if not itens_mud and all(t["descricao"] == f and t["etapa"] == chave[f]
                             for t, f in zip(titulos, fases, strict=False)) and not criar:
        raise ErroNegocio("O orçamento já está organizado em fases.")

    def aplicar() -> dict:
        for m in itens_mud:
            tx.atualizar("itens_orcamento", u.tenant_id, m["id"], {"etapa": m["depois"]})
        for r in reusar:
            tx.atualizar("itens_orcamento", u.tenant_id, r["id"], {"etapa": r["etapa"], "descricao": r["descricao"]})
        for tid, k, f, i in criar:
            tx.criar("itens_orcamento", doc(ItemOrcamento(
                id=tid, tenant_id=u.tenant_id, obra_id=obra_id, versao_id=versao_id,
                ordem=i, tipo="etapa", nivel=1, etapa=k, descricao=f)))
        # o cache de etapas da versão (Compras e apropriação leem dele, não dos 500 itens) acompanha as novas fases
        tx.atualizar("versoes_orcamento", u.tenant_id, versao_id,
                     {"lista_etapas": [{"etapa": chave[f], "descricao": f} for f in fases]})
        evento(tx, u.tenant_id, "orcamento.organizado_fases", obra_id, {"itens": len(itens_mud), "fases": fases})
        return {"itens": len(itens_mud), "fases": fases}

    # Desfazer volta a etapa dos itens, o nome/chave dos títulos reaproveitados e o cache de etapas da versão.
    receita = {"op": "restaurar_etapas", "obra_id": obra_id, "versao_id": versao_id,
               "itens": [{"id": m["id"], "etapa": m["antes"]} for m in itens_mud], "titulos": titulos_antes,
               "lista_etapas": v.get("lista_etapas")}
    n = len(itens_mud)
    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: f"Organizou o orçamento em {len(fases)} fases ({n} serviço{'' if n == 1 else 's'})",
                    desfazer=lambda d: receita)


async def preparar_restaurar_etapas(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz a organização em fases: volta a etapa de cada serviço e o nome dos títulos reaproveitados."""
    o = await obra_do_tenant(tx, u.tenant_id, receita["obra_id"])
    recusar_se_desfazendo(o)
    itens = [m for m in receita.get("itens", []) if await tx.obter("itens_orcamento", u.tenant_id, m["id"])]
    titulos = [t for t in receita.get("titulos", []) if await tx.obter("itens_orcamento", u.tenant_id, t["id"])]
    versao_id = receita.get("versao_id")
    v = await tx.obter("versoes_orcamento", u.tenant_id, versao_id) if versao_id else None
    lista_atual = v.get("lista_etapas") if v else None  # a de agora, para o Refazer

    def aplicar() -> dict:
        for m in itens:
            tx.atualizar("itens_orcamento", u.tenant_id, m["id"], {"etapa": m["etapa"]})
        for t in titulos:
            tx.atualizar("itens_orcamento", u.tenant_id, t["id"], {"etapa": t["etapa"], "descricao": t["descricao"]})
        if v is not None and receita.get("lista_etapas") is not None:
            tx.atualizar("versoes_orcamento", u.tenant_id, versao_id, {"lista_etapas": receita["lista_etapas"]})
        return {"itens": len(itens)}

    inversa = {"op": "restaurar_etapas", "obra_id": receita["obra_id"], "versao_id": versao_id,
               "itens": [{"id": m["id"], "etapa": m["etapa"]} for m in itens],
               "titulos": [{"id": t["id"], "etapa": t["etapa"], "descricao": t["descricao"]} for t in titulos],
               "lista_etapas": lista_atual}
    return Operacao(obra_id=receita["obra_id"], aplicar=aplicar,
                    texto=lambda d: "Desfez a organização do orçamento em fases", desfazer=lambda d: inversa)
