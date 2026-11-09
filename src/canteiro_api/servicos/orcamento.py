"""Orçamento pela planilha que a construtora já tem (Excel ou CSV), não por um modelo nosso.

A planilha de verdade tem título e dados da obra antes do cabeçalho, linhas de etapa e subtotal no meio, colunas com
nomes livres ("Discriminação", "Qtde.", "P. Unit.") e o mesmo material repetido em etapas diferentes. Então a leitura
é em dois passos, sem estado no servidor: `ler` acha o cabeçalho e sugere qual coluna é o quê; a tela confirma e manda
o arquivo de novo com o mapeamento para `importar`, que devolve a prévia (ou grava, com `aplicar`).
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from canteiro_dominio.modelos import Insumo, Movimento, Saldo, TipoMovimento
from canteiro_dominio.tipos import fmt_reais, qtd, reais

from ..auth import UsuarioAtual
from ..esquemas import (
    ColunaPlanilha,
    ErroLinha,
    LeituraPlanilha,
    LinhaIgnorada,
    LinhaOrcamento,
    Mapeamento,
    MateriaisDoProjeto,
    PreviaOrcamento,
)
from ..modelos import doc
from ..nucleo import Conflito, ErroNegocio, Operacao, evento, executar_e_registrar, obra_do_tenant
from .estoque import _nome_chave, criado, gravar_insumo_novo, id_insumo, receita_insumos
from .importacao import decodificar
from .obras import insumos_da_obra

MAX_LINHAS = 2000
# Planilha com mais linhas que isso nem é orçamento de obra; o teto protege a memória de quem lê arquivo enorme.
MAX_LINHAS_LIDAS = MAX_LINHAS + 500
LINHAS_CABECALHO = 30
# até 3 escritas por insumo novo (insumo, saldo, movimento) + registro e evento: folgado nas 500 da transação, e o
# desfazer do lote (insumo, movimento, saldo por linha) também
LINHAS_POR_TRANSACAO = 100

# Sinônimos por "contém", sobre o título sem acento e sem pontuação. A ordem importa: unidade e quantidade primeiro,
# e o preço unitário antes do total ("preço unitário total" não existe, mas "valor total" contém "valor").
SINONIMOS: list[tuple[str, tuple[str, ...]]] = [
    ("codigo", ("codigo", "cod")),
    ("fonte", ("fonte", "banco", "referencia")),
    ("unidade", ("unid", "und", "unidade")),
    ("quantidade", ("quant", "qtd", "qtde")),
    ("valor_unitario", ("unit",)),
    ("valor_total", ("total",)),
    ("saldo", ("saldo", "estoque")),
    ("curva", ("curva", "abc")),
    ("nome", ("descricao", "discriminacao", "insumo", "material", "servico", "nome", "especificacao")),
    ("item", ("item",)),
]
# Títulos curtos que só valem sozinhos: "un" dentro de "unitário" não é unidade, "n" dentro de outra palavra não é item.
EXATOS = {"un": "unidade", "n": "item", "no": "item", "n o": "item", "nr": "item", "ref": "item"}


def _normal(t: str) -> str:
    t = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def _categoria(titulo: str) -> list[str]:
    n = _normal(titulo)
    if not n:
        return []
    achadas = [c for c, sins in SINONIMOS if any(s in n for s in sins)]
    if n in EXATOS:
        achadas.insert(0, EXATOS[n])
    if achadas[:1] == ["codigo"] and "item" in achadas:   # "código do item" é código
        achadas.remove("item")
    if "valor_unitario" in achadas and "valor_total" in achadas:
        achadas.remove("valor_total")
    return achadas


def _texto(v) -> str:
    """Célula como a pessoa vê: 37.9 → "37,9", data → dd/mm/aaaa."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "sim" if v else "não"
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
        if d == d.to_integral_value():
            return str(int(d))
        return format(d.normalize(), "f").replace(".", ",")
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return " ".join(str(v).split())


# --- leitura do arquivo para uma grade de células ---

def _grade_xlsx(conteudo: bytes, planilha: str | None) -> tuple[list[str], str, list[list]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True)
    except Exception:
        raise ErroNegocio("Não consegui abrir a planilha. Confira se o arquivo é um .xlsx válido.") from None
    try:
        nomes = list(wb.sheetnames)
        if planilha is not None and planilha not in nomes:
            raise ErroNegocio(f"A planilha não tem a aba '{planilha}'.")
        candidatas = [planilha] if planilha is not None else nomes
        reserva = None
        for nome in candidatas:
            linhas = []
            for i, row in enumerate(wb[nome].iter_rows(values_only=True)):
                if i >= MAX_LINHAS_LIDAS:
                    break
                linhas.append(list(row))
            if planilha is not None:
                return nomes, nome, linhas
            # A 1ª aba costuma ser capa ou resumo: vale a 1ª em que se acha um cabeçalho de orçamento; sem nenhuma,
            # a 1ª com conteúdo, e a pessoa troca a aba na tela.
            if _cabecalho_conhecido(linhas) is not None:
                return nomes, nome, linhas
            if reserva is None and any(any(_texto(c) for c in r) for r in linhas):
                reserva = (nome, linhas)
        return (nomes, *reserva) if reserva else (nomes, nomes[0], [])
    finally:
        wb.close()


def _grade_csv(conteudo: bytes) -> list[list]:
    texto = decodificar(conteudo)
    # o separador é decidido no começo do arquivo inteiro: a 1ª linha costuma ser o título, sem separador nenhum
    inicio = "\n".join(texto.splitlines()[:LINHAS_CABECALHO])
    sep = ";" if inicio.count(";") >= inicio.count(",") else ","
    linhas = []
    for i, row in enumerate(csv.reader(io.StringIO(texto), delimiter=sep)):
        if i >= MAX_LINHAS_LIDAS:
            break
        linhas.append(row)
    return linhas


def _grade(conteudo: bytes, nome_arquivo: str, planilha: str | None) -> tuple[list[str], str, list[list]]:
    nome = (nome_arquivo or "").lower()
    if conteudo[:4] == b"\xd0\xcf\x11\xe0" or nome.endswith(".xls"):
        raise ErroNegocio("Este é o formato antigo do Excel (.xls). Salve como .xlsx ou .csv e envie de novo.")
    if conteudo[:2] == b"PK" or nome.endswith(".xlsx"):
        return _grade_xlsx(conteudo, planilha)
    if planilha not in (None, "CSV"):
        raise ErroNegocio(f"A planilha não tem a aba '{planilha}'.")
    return ["CSV"], "CSV", _grade_csv(conteudo)


def _cabecalho_conhecido(grade: list[list]) -> int | None:
    """Índice 0-based da 1ª linha (das 30 primeiras) com pelo menos dois títulos conhecidos."""
    for i, row in enumerate(grade[:LINHAS_CABECALHO]):
        textos = [_texto(c) for c in row]
        if len({c for t in textos for c in _categoria(t)[:1]}) >= 2:
            return i
    return None


def _achar_cabecalho(grade: list[list]) -> int:
    """O cabeçalho conhecido; senão a 1ª linha não vazia (a pessoa diz as colunas na mão)."""
    i = _cabecalho_conhecido(grade)
    if i is not None:
        return i
    return next((j for j, row in enumerate(grade[:LINHAS_CABECALHO]) if any(_texto(c) for c in row)), 0)


def _sugerir(cabecalho: list) -> dict[str, int | None]:
    sugestao: dict[str, int | None] = {c: None for c, _ in SINONIMOS}
    usadas: set[int] = set()
    for categoria, _ in SINONIMOS:
        for i, titulo in enumerate(cabecalho):
            if i not in usadas and categoria in _categoria(_texto(titulo)):
                sugestao[categoria] = i
                usadas.add(i)
                break
    return sugestao


async def ler(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str,
              planilha: str | None) -> LeituraPlanilha:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    nomes, aba, grade = _grade(conteudo, nome_arquivo, planilha)
    if not any(any(_texto(c) for c in r) for r in grade):
        raise ErroNegocio("A planilha está vazia.")
    i = _achar_cabecalho(grade)
    cabecalho = grade[i]
    largura = len(cabecalho)
    amostra = []
    for row in grade[i + 1:]:
        textos = [_texto(c) for c in row[:largura]] + [""] * max(0, largura - len(row))
        if any(textos):
            amostra.append(textos)
        if len(amostra) == 8:
            break
    return LeituraPlanilha(
        planilhas=nomes, planilha=aba, linha_cabecalho=i + 1,
        colunas=[ColunaPlanilha(indice=j, titulo=_texto(t)) for j, t in enumerate(cabecalho) if _texto(t)],
        amostra=amostra, sugestao=Mapeamento(planilha=aba, linha_cabecalho=i + 1, **_sugerir(cabecalho)))


# --- importação ---

def _numero(v, ler):
    """Célula numérica do Excel vem float (37.9): passa por str para não carregar o erro binário. Texto vai para o
    leitor brasileiro de tipos (vírgula decimal, "2.760" ambíguo recusado), sem o "R$" que muita planilha formata."""
    if isinstance(v, bool):
        raise ValueError("não é número")
    if isinstance(v, (int, float, Decimal)):
        return ler(Decimal(str(v)))
    t = str(v).replace("R$", "").strip()
    return ler(t)


def _vazio(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _campo_numerico(v, campo: str, leitor, n: int, erros: list[ErroLinha]) -> int | None:
    """Coluna opcional: vazia é None; inválida ou negativa vira erro da linha (e None)."""
    if _vazio(v):
        return None
    try:
        x = _numero(v, leitor)
    except (ValueError, ArithmeticError) as e:
        msg = str(e) if "ambíguo" in str(e) else f"'{_texto(v)}' não é um número."
        erros.append(ErroLinha(linha=n, campo=campo, mensagem=msg))
        return None
    if x < 0:
        erros.append(ErroLinha(linha=n, campo=campo, mensagem="Não pode ser negativo."))
        return None
    return x


def _curva_abc(itens: list[dict]) -> None:
    """Curva pelo peso no valor: A são os itens que somam os primeiros 80% do total (o que cruza os 80% também é A),
    B até 95%, o resto C. É a régua de qualquer engenheiro de custos, e poupa a pessoa de classificar 300 linhas."""
    sem_coluna = [x for x in itens if x["curva"] is None]
    total = sum(x["total"] for x in itens)
    if total <= 0:
        for x in sem_coluna:
            x["curva"] = "B"          # sem preço não há como pesar: fica o padrão de sempre
        return
    acumulado = 0
    for x in sorted(itens, key=lambda x: -x["total"]):
        antes = acumulado
        acumulado += x["total"]
        if x["curva"] is not None:
            continue
        if x["total"] <= 0:
            x["curva"] = "C"
        elif antes * 100 < total * 80:
            x["curva"] = "A"
        elif antes * 100 < total * 95:
            x["curva"] = "B"
        else:
            x["curva"] = "C"


def _ler_linhas(grade: list[list], m: Mapeamento, existentes: dict[str, dict]):
    ignoradas: list[LinhaIgnorada] = []
    erros: list[ErroLinha] = []
    itens: dict[str, dict] = {}

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
        nome = _texto(cel(row, m.nome))
        texto = nome or next(t for t in textos if t)
        unidade = _texto(cel(row, m.unidade))
        q_crua = cel(row, m.quantidade)
        if not nome:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem descrição"))
            continue
        if not unidade:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem unidade (título ou subtotal?)"))
            continue
        if _vazio(q_crua):
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem quantidade"))
            continue
        erros_linha: list[ErroLinha] = []
        quantidade = 0
        try:
            quantidade = _numero(q_crua, qtd)
        except (ValueError, ArithmeticError) as e:
            msg = str(e) if "ambíguo" in str(e) else f"'{_texto(q_crua)}' não é um número."
            erros_linha.append(ErroLinha(linha=n, campo="quantidade", mensagem=msg))
        if quantidade == 0 and not erros_linha:
            ignoradas.append(LinhaIgnorada(linha=n, texto=texto, motivo="sem quantidade"))
            continue
        if quantidade < 0:
            erros_linha.append(ErroLinha(linha=n, campo="quantidade", mensagem="Quantidade negativa."))
        if len(nome) > 120:
            erros_linha.append(ErroLinha(linha=n, campo="nome", mensagem="Descrição passa de 120 caracteres."))
        if len(unidade) > 12:
            erros_linha.append(ErroLinha(linha=n, campo="unidade", mensagem="Unidade com mais de 12 caracteres."))

        unit = _campo_numerico(cel(row, m.valor_unitario), "valor_unitario", reais, n, erros_linha)
        total_cel = (_campo_numerico(cel(row, m.valor_total), "valor_total", reais, n, erros_linha)
                     if unit is None else None)
        saldo = _campo_numerico(cel(row, m.saldo), "saldo", qtd, n, erros_linha) or 0
        curva = _texto(cel(row, m.curva)).upper() or None
        if curva is not None and curva not in ("A", "B", "C"):
            erros_linha.append(ErroLinha(linha=n, campo="curva", mensagem="Curva tem de ser A, B ou C."))
        if erros_linha:
            erros.extend(erros_linha)
            continue
        if unit is not None:
            total = _arredonda(Decimal(quantidade) * unit / 1000)
        elif total_cel is not None:
            total = total_cel
        else:
            total = 0

        chave = _nome_chave(nome)
        ja = itens.get(chave)
        if ja:
            if ja["unidade"].casefold() != unidade.casefold():
                msg = f"'{nome}' aparece em {ja['unidade']} na linha {ja['linhas'][0]} e em {unidade} aqui."
                erros.append(ErroLinha(linha=n, campo="unidade", mensagem=msg))
                continue
            ja["linhas"].append(n)
            ja["quantidade"] += quantidade
            ja["total"] += total
            ja["saldo"] += saldo
            ja["curva"] = ja["curva"] or curva
            continue
        itens[chave] = {"linhas": [n], "nome": nome, "unidade": unidade, "quantidade": quantidade, "total": total,
                        "saldo": saldo, "curva": curva}

    lista = list(itens.values())
    _curva_abc(lista)
    linhas: list[LinhaOrcamento] = []
    for x in lista:
        unit = _arredonda(Decimal(x["total"]) * 1000 / x["quantidade"]) if x["quantidade"] else 0
        atual = existentes.get(_nome_chave(x["nome"]))
        if atual and atual["unidade"].strip().casefold() != x["unidade"].casefold():
            erros.append(ErroLinha(linha=x["linhas"][0], campo="unidade",
                                   mensagem=f"'{atual['nome']}' já está cadastrado em {atual['unidade']} nesta obra."))
            continue
        linhas.append(LinhaOrcamento(
            linhas=x["linhas"], nome=x["nome"], unidade=x["unidade"], quantidade=x["quantidade"], valor_unitario=unit,
            # o total mostrado é o que fica gravado (quantidade × unitário em centavos): com linhas juntadas o
            # unitário médio é arredondado, e a prévia não pode prometer um número que a aba Orçamento não mostra
            total=_arredonda(Decimal(x["quantidade"]) * unit / 1000), curva=x["curva"], saldo=x["saldo"],
            situacao="atualiza" if atual else "novo",
            precisa_antes=atual.get("precisa_ate_o_fim", 0) if atual else None,
            valor_antes=atual.get("valor_unitario", 0) if atual else None))
    erros.sort(key=lambda e: e.linha)
    return linhas, ignoradas, erros


def _arredonda(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def importar(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str, m: Mapeamento,
                   aplicar: bool) -> PreviaOrcamento:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    faltando = [r for c, r in (("nome", "descrição"), ("unidade", "unidade"), ("quantidade", "quantidade"))
                if getattr(m, c) is None]
    if faltando:
        raise ErroNegocio(f"Diga qual coluna é a {', '.join(faltando)}.")
    _, _, grade = _grade(conteudo, nome_arquivo, m.planilha)
    existentes = {_nome_chave(i["nome"]): i for i in await insumos_da_obra(arm, u.tenant_id, obra_id)}
    linhas, ignoradas, erros = _ler_linhas(grade, m, existentes)
    previa = PreviaOrcamento(
        aplicado=False, linhas=linhas, ignoradas=ignoradas, erros=erros, total=sum(x.total for x in linhas),
        novos=sum(x.situacao == "novo" for x in linhas), atualizados=sum(x.situacao == "atualiza" for x in linhas),
        juntadas=sum(len(x.linhas) - 1 for x in linhas))
    if not aplicar or erros:
        # com qualquer erro nada é gravado: a pessoa corrige a planilha (ou o mapeamento) e manda de novo
        return previa
    if not linhas:
        raise ErroNegocio("Nenhuma linha para importar.")
    lotes = [linhas[i:i + LINHAS_POR_TRANSACAO] for i in range(0, len(linhas), LINHAS_POR_TRANSACAO)]
    registros = []
    for k, lote in enumerate(lotes, start=1):
        parte = f" — parte {k} de {len(lotes)}" if len(lotes) > 1 else ""
        registros.append(await arm.em_transacao(_gravar_lote(u, obra_id, lote, existentes, parte)))
    return previa.model_copy(update={"aplicado": True, "registro_ids": registros})


def _gravar_lote(u: UsuarioAtual, obra_id: str, lote: list[LinhaOrcamento], existentes: dict[str, dict], parte: str,
                 titulo: str = "o orçamento", assunto: str = "do orçamento"):
    """Um lote = uma transação = uma entrada no registro. O Desfazer arquiva os insumos novos e devolve aos atualizados
    os valores de antes; para isso o insumo atualizado é relido aqui dentro (o da prévia pode ter mudado)."""
    campos = ("precisa_ate_o_fim", "valor_unitario", "curva")

    async def corpo(tx):
        alvos = []
        for x in lote:
            atual = existentes.get(_nome_chave(x.nome))
            id_ = atual["id"] if atual else id_insumo(u.tenant_id, obra_id, x.nome)
            alvos.append((x, atual is not None, await tx.obter("insumos", u.tenant_id, id_), id_))
        novos: list[dict] = []
        restaurar: list[dict] = []

        def aplicar():
            total = 0
            for x, atualiza, lido, id_ in alvos:
                total += x.total
                if atualiza:
                    if not lido or lido.get("arquivado"):
                        raise Conflito(f"'{x.nome}' mudou agora há pouco; envie a planilha de novo.")
                    depois = {"precisa_ate_o_fim": x.quantidade, "valor_unitario": x.valor_unitario, "curva": x.curva}
                    tx.atualizar("insumos", u.tenant_id, id_, depois)
                    restaurar.append({"id": id_, "antes": {k: lido.get(k) for k in campos}, "depois": depois})
                    continue
                # o mesmo ID do cadastro pela tela: um "Cadastrar «Cal»" depois da importação colide, não duplica
                ins = Insumo(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, nome=x.nome, unidade=x.unidade,
                             precisa_ate_o_fim=x.quantidade, valor_unitario=x.valor_unitario, curva=x.curva)
                d = doc(ins)
                gravar_insumo_novo(tx, d, lido)
                if x.saldo:
                    tx.criar("movimentos", doc(Movimento(
                        tenant_id=u.tenant_id, obra_id=obra_id, insumo_id=ins.id, tipo=TipoMovimento.AJUSTE,
                        quantidade=x.saldo, quem=u.nome, usuario_id=u.id, origem="sistema", origem_doc="orçamento")))
                # o saldo nasce com o instante do cadastro: é por ele que o desfazer sabe que nada se mexeu depois
                tx.salvar("saldos", doc(Saldo(id=ins.id, tenant_id=u.tenant_id, obra_id=obra_id, saldo=x.saldo,
                                              atualizado_em=ins.criado_em)))
                novos.append(criado(d, x.saldo))
            evento(tx, u.tenant_id, "orcamento.importado", obra_id,
                   {"novos": len(novos), "atualizados": len(restaurar)})
            return total

        op = Operacao(obra_id=obra_id, aplicar=aplicar,
                      texto=lambda total: f"Importou {titulo}: {len(novos)} novos, {len(restaurar)} atualizados "
                                          f"({fmt_reais(total)}){parte}",
                      desfazer=lambda _: receita_insumos(obra_id, f"importação {assunto}{parte}", arquivar=novos,
                                                         restaurar=restaurar))
        _, reg = executar_e_registrar(tx, u, op)
        return reg["id"]
    return corpo


# o tradutor escreve a unidade em ASCII ("m3"); o almoxarifado usa a do orçamento ("m³")
_UNIDADE = {"m3": "m³", "m2": "m²"}


async def materiais_do_projeto(arm, u: UsuarioAtual, obra_id: str, corpo: MateriaisDoProjeto) -> PreviaOrcamento:
    """A lista de materiais das pranchas entra como a lista de materiais da planilha: a mesma prévia (novo ou atualiza,
    com o antes), os mesmos lotes e o mesmo Desfazer. O "precisa" de cada insumo passa a ser o do projeto; a conta do
    que falta comprar (precisa − saldo − a caminho) é a de sempre."""
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    existentes = {_nome_chave(i["nome"]): i for i in await insumos_da_obra(arm, u.tenant_id, obra_id)}
    itens: dict[str, dict] = {}
    erros: list[ErroLinha] = []
    for n, m in enumerate(corpo.itens, start=1):
        nome, unidade = m.nome.strip(), _UNIDADE.get(m.unidade.strip().casefold(), m.unidade.strip())
        chave = _nome_chave(nome)
        total = _arredonda(Decimal(m.quantidade) * m.valor_unitario / 1000)
        if chave in itens:
            x = itens[chave]
            if x["unidade"].casefold() != unidade.casefold():
                erros.append(ErroLinha(linha=n, campo="unidade", mensagem=f"'{nome}' aparece com duas unidades."))
                continue
            x["linhas"].append(n)
            x["quantidade"] += m.quantidade
            x["total"] += total
            continue
        itens[chave] = {"linhas": [n], "nome": nome, "unidade": unidade, "quantidade": m.quantidade, "total": total,
                        "saldo": 0, "curva": None}
    lista = list(itens.values())
    _curva_abc(lista)
    linhas: list[LinhaOrcamento] = []
    for x in lista:
        atual = existentes.get(_nome_chave(x["nome"]))
        if atual and atual["unidade"].strip().casefold() != x["unidade"].casefold():
            erros.append(ErroLinha(linha=x["linhas"][0], campo="unidade",
                                   mensagem=f"'{atual['nome']}' já está cadastrado em {atual['unidade']} nesta obra."))
            continue
        unit = _arredonda(Decimal(x["total"]) * 1000 / x["quantidade"])
        linhas.append(LinhaOrcamento(
            linhas=x["linhas"], nome=x["nome"], unidade=x["unidade"], quantidade=x["quantidade"], valor_unitario=unit,
            total=_arredonda(Decimal(x["quantidade"]) * unit / 1000), curva=x["curva"], saldo=0,
            situacao="atualiza" if atual else "novo",
            precisa_antes=atual.get("precisa_ate_o_fim", 0) if atual else None,
            valor_antes=atual.get("valor_unitario", 0) if atual else None))
    erros.sort(key=lambda e: e.linha)
    previa = PreviaOrcamento(
        aplicado=False, linhas=linhas, ignoradas=[], erros=erros, total=sum(x.total for x in linhas),
        novos=sum(x.situacao == "novo" for x in linhas), atualizados=sum(x.situacao == "atualiza" for x in linhas),
        juntadas=sum(len(x.linhas) - 1 for x in linhas))
    if not corpo.aplicar or erros:
        return previa
    origem = corpo.origem.strip()
    lotes = [linhas[i:i + LINHAS_POR_TRANSACAO] for i in range(0, len(linhas), LINHAS_POR_TRANSACAO)]
    registros = []
    for k, lote in enumerate(lotes, start=1):
        parte = f" — parte {k} de {len(lotes)}" if len(lotes) > 1 else ""
        gravar = _gravar_lote(u, obra_id, lote, existentes, parte, titulo=f"os materiais do {origem}",
                              assunto=f"dos materiais do {origem}")
        registros.append(await arm.em_transacao(gravar))
    return previa.model_copy(update={"aplicado": True, "registro_ids": registros})
