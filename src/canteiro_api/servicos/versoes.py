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


async def _apropriar_aditivo(arm, tenant_id: str, vigente: dict, itens: list[ItemPrevia],
                             escolhidas: dict[int, str]) -> dict[str, str]:
    """Põe cada item do aditivo na etapa do contrato onde ele conta. A planilha de aditivo costuma vir numa etapa só
    ("A1 ACRÉSCIMOS"), e somar o tapume e o aço numa etapa à parte esconderia o estouro da fundação no previsto ×
    realizado. Sugestão pelo código (o mesmo serviço do contrato está naquela etapa); a pessoa pode apontar outra.
    Devolve {etapa: descrição} do contrato, para os totais."""
    contrato = await arm.consultar(Consulta("itens_orcamento", tenant_id, [Filtro("versao_id", "==", vigente["id"])]))
    descricoes = {e["etapa"]: e["descricao"] for e in vigente.get("lista_etapas", [])}
    for x in contrato:
        if x["tipo"] == "etapa" and x["nivel"] == 1:
            descricoes.setdefault(x["etapa"], x["descricao"])
    etapa_do_codigo: dict[str, str] = {}
    for x in sorted(contrato, key=lambda x: x["ordem"]):
        if x["tipo"] == "item" and x.get("codigo") and any(c.isdigit() for c in x["codigo"]):
            etapa_do_codigo.setdefault(f"{x.get('fonte', '').casefold()}|{x['codigo'].casefold()}", x["etapa"])
    proprias = {x.etapa for x in itens if x.tipo == "etapa"}
    for x in itens:
        if x.tipo != "item":
            continue
        x.etapa_planilha = x.etapa
        if x.ordem in escolhidas:
            alvo = escolhidas[x.ordem]
            if alvo not in descricoes and alvo not in proprias:
                raise ErroNegocio(f"A etapa {alvo} não existe no contrato nem no aditivo.")
            x.etapa, x.etapa_origem = alvo, "escolhida"
        elif x.etapa not in descricoes and (e := etapa_do_codigo.get(f"{x.fonte.casefold()}|{x.codigo.casefold()}")):
            x.etapa, x.etapa_origem = e, "codigo"
    # título de etapa do aditivo que ficou sem item (todos foram para etapas do contrato) não vira etapa vazia
    com_item = {x.etapa for x in itens if x.tipo == "item"}
    # a `ordem` fica a da leitura (com buracos): é a chave da escolha na prévia e do ID do item gravado
    itens[:] = [x for x in itens if x.tipo == "item" or x.etapa in com_item or x.nivel > 1]
    return descricoes


async def importar(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str, m: Mapeamento,
                   tipo: TipoVersao, descricao: str, data: str | None, aplicar: bool,
                   chave: str | None, escolhidas: dict[int, str] | None = None) -> tuple[PreviaVersao, bool]:
    """Devolve (prévia ou versão gravada, criada agora?)."""
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    faltando = [r for c, r in (("nome", "descrição"), ("unidade", "unidade"), ("quantidade", "quantidade"))
                if getattr(m, c) is None]
    if faltando:
        raise ErroNegocio(f"Diga qual coluna é a {', '.join(faltando)}.")
    if aplicar and not chave:
        raise ErroNegocio("Falta a chave de idempotência.")
    escolhidas = escolhidas or {}
    assinatura = hashlib.sha256(conteudo + f"|{tipo.value}|{m.model_dump_json()}|{sorted(escolhidas.items())}"
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
    if aprovada and vigente and tipo == TipoVersao.CONTRATO:
        # Compras já saiu deste contrato: trocá-lo deixaria os insumos e os pedidos descolados do orçamento
        raise Conflito("O orçamento já foi aprovado: desfaça a aprovação ou lance um aditivo.")
    descricoes_contrato = None
    if tipo == TipoVersao.ADITIVO and vigente:
        descricoes_contrato = await _apropriar_aditivo(arm, u.tenant_id, vigente, itens, escolhidas)
    so_itens = [x for x in itens if x.tipo == "item"]
    previa = PreviaVersao(
        aplicado=False, tipo=tipo, itens=itens, ignoradas=ignoradas, erros=erros, avisos=avisos,
        total=sum(x.total for x in so_itens), n_itens=len(so_itens),
        n_etapas=sum(x.tipo == "etapa" and x.nivel == 1 for x in itens),
        etapas=_totais_por_etapa(itens, descricoes_contrato),
        substitui=(Substituida(versao_id=vigente["id"], total=vigente["total"], itens=vigente["itens"])
                   if vigente and tipo == TipoVersao.CONTRATO else None))
    if tipo == TipoVersao.ADITIVO and not vigente:
        raise ErroNegocio("Importe o orçamento contratado antes do aditivo.")
    if not aplicar or erros:
        return previa, False
    if not so_itens:
        raise ErroNegocio("Nenhum item para importar.")
    compras = (await _compras_do_aditivo(arm, u.tenant_id, obra_id, so_itens, nome_arquivo)
               if aprovada and tipo == TipoVersao.ADITIVO else [])

    numero = 0
    if tipo == TipoVersao.ADITIVO:
        numero = max((v["numero"] for v in versoes if v["ativa"] and v["tipo"] == TipoVersao.ADITIVO), default=0) + 1
    versao = VersaoOrcamento(
        id=id_, tenant_id=u.tenant_id, obra_id=obra_id, tipo=tipo, numero=numero, descricao=descricao.strip(),
        data=data, arquivo=(nome_arquivo or "")[:200], itens=previa.n_itens, etapas=previa.n_etapas,
        total=previa.total, substitui=previa.substitui.versao_id if previa.substitui else None,
        contrato_id=vigente["id"] if tipo == TipoVersao.ADITIVO else None, assinatura=assinatura,
        lista_etapas=[{"etapa": e.etapa, "descricao": e.descricao} for e in previa.etapas if e.etapa],
        gerou_compras=bool(compras))
    # Itens primeiro, ligados a uma versão que ainda não existe: ninguém os enxerga até a última transação criar a
    # versão. Se o processo cair no meio, sobram itens órfãos invisíveis, e a mesma chave regrava os mesmos IDs.
    docs = [doc(ItemOrcamento(id=id_derivado(u.tenant_id, "item-orc", id_, str(x.ordem)), tenant_id=u.tenant_id,
                              obra_id=obra_id, versao_id=id_, **x.model_dump())) for x in itens]
    for i in range(0, len(docs), ITENS_POR_TRANSACAO):
        lote = docs[i:i + ITENS_POR_TRANSACAO]

        async def gravar(tx, lote=lote):
            for d in lote:
                tx.salvar("itens_orcamento", d)
        await arm.em_transacao(gravar)

    async def ativar(tx):
        if (await obra_do_tenant(tx, u.tenant_id, obra_id))["status"] != o["status"]:
            raise Conflito("A situação da obra mudou enquanto você importava; envie a planilha de novo.")
        if await tx.obter("versoes_orcamento", u.tenant_id, id_):
            raise Conflito("Esta importação já foi gravada.")
        anterior = await tx.obter("versoes_orcamento", u.tenant_id, versao.substitui) if versao.substitui else None
        contrato = await tx.obter("versoes_orcamento", u.tenant_id, versao.contrato_id) if versao.contrato_id else None
        if (anterior is not None and not anterior["ativa"]) or (contrato is not None and not contrato["ativa"]):
            raise Conflito("O contrato mudou enquanto você importava; envie a planilha de novo.")
        lidos = [await tx.obter("insumos", u.tenant_id, x["id"]) for x in compras]
        novos: list[dict] = []
        restaurar: list[dict] = []

        def aplicar_():
            _aplicar_compras(tx, u, obra_id, compras, lidos, novos, restaurar)
            tx.criar("versoes_orcamento", doc(versao))
            if anterior is not None:
                tx.atualizar("versoes_orcamento", u.tenant_id, anterior["id"], {"ativa": False})
            if contrato is not None:
                tx.atualizar("versoes_orcamento", u.tenant_id, contrato["id"], {"aditivos": contrato["aditivos"] + 1})
            evento(tx, u.tenant_id, "orcamento.versao", obra_id,
                   {"versao_id": id_, "tipo": tipo.value, "total": versao.total})
            return doc(versao)

        if tipo == TipoVersao.CONTRATO:
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
                          **({"compras": receita_insumos(obra_id, f"compras do {_ordinal(numero)} aditivo",
                                                         arquivar=novos, restaurar=restaurar)}
                             if novos or restaurar else {})})
        _, entrada = executar_e_registrar(tx, u, op, registro_id=id_derivado(u.tenant_id, "registro", id_))
        return entrada

    entrada = await arm.em_transacao(ativar)
    return previa.model_copy(update={"aplicado": True, "versao_id": id_, "registro_id": entrada["id"]}), True


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
    tem um; aditivo se já tem. Reaproveita a importação de versão (prévia, gravação, desfazer)."""
    versoes = await versoes_da_obra(arm, u.tenant_id, obra_id)
    tem_contrato = any(v["ativa"] and v["tipo"] == TipoVersao.CONTRATO for v in versoes)
    tipo = TipoVersao.ADITIVO if tem_contrato else TipoVersao.CONTRATO
    conteudo = _csv_estrutura(corpo.etapa, corpo.itens)
    m = Mapeamento(planilha="CSV", linha_cabecalho=1, item=0, codigo=1, fonte=2, nome=3, unidade=4, quantidade=5,
                   valor_unitario=6, valor_total=7)
    return await importar(arm, u, obra_id, conteudo, f"{corpo.etapa}{SUFIXO_PROJETO}", m, tipo,
                          descricao=corpo.etapa.strip(), data=None, aplicar=corpo.aplicar,
                          chave=corpo.chave_idempotencia)


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
    return Operacao(obra_id=v["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {nome} de {o['nome']} retirado"
                                    + (" (o anterior volta a valer)" if reativar else ""),
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
        versoes=[{**v, "registro_id": id_derivado(tenant_id, "registro", v["id"])} for v in versoes],
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
    """Cotando, comprometido e realizado por etapa, somados das linhas dos pedidos da obra (centavos).

    Realizado sai do que cada linha já recebeu, pelo preço do pedido: é a mesma soma das entregas, sem ler cada
    uma. Comprometido é o maior entre o pedido e o entregue (entrega a mais também custa), exceto no pedido
    encerrado com sobra, que só compromete o que chegou.
    """
    pedidos = await leitor.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id)]))
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
            q, recebida = x["quantidade"], x.get("recebida", 0)
            recebido = (recebida * total + q // 2) // q if q else 0
            g["realizado"] += recebido
            encerrado_com_sobra = st == StatusPedido.CONFERIDO and recebida < q
            g["comprometido"] += recebido if encerrado_com_sobra else max(total, recebido)
    return soma


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
