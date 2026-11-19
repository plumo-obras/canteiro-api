"""Orçamento pela planilha que a construtora já tem (Excel ou CSV), não por um modelo nosso.

A planilha de verdade tem título e dados da obra antes do cabeçalho, linhas de etapa e subtotal no meio e colunas com
nomes livres ("Discriminação", "Qtde.", "P. Unit."). Então a leitura é em dois passos, sem estado no servidor: `ler`
acha o cabeçalho e sugere qual coluna é o quê; a tela confirma e manda o arquivo de novo com o mapeamento para a
importação da versão (`versoes.importar`), que devolve a prévia (ou grava, com `aplicar`).
"""
import csv
import io
import re
import unicodedata
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from ..auth import UsuarioAtual
from ..esquemas import (
    ColunaPlanilha,
    LeituraPlanilha,
    Mapeamento,
)
from ..nucleo import (
    ErroNegocio,
    obra_do_tenant,
)

MAX_LINHAS = 2000
# Planilha com mais linhas que isso nem é orçamento de obra; o teto protege a memória de quem lê arquivo enorme.
MAX_LINHAS_LIDAS = MAX_LINHAS + 500
LINHAS_CABECALHO = 30

# Sinônimos por "contém", sobre o título sem acento e sem pontuação. A ordem importa: unidade e quantidade primeiro,
# e o preço unitário antes do total ("preço unitário total" não existe, mas "valor total" contém "valor").
SINONIMOS: list[tuple[str, tuple[str, ...]]] = [
    ("complemento", ("complemento",)),
    ("codigo", ("codigo", "cod")),
    ("fonte", ("fonte", "banco", "referencia")),
    ("unidade", ("unid", "und", "unidade")),
    ("quantidade", ("quant", "qtd", "qtde")),
    ("valor_unitario", ("unit",)),
    # "ORÇADO SET/23": na planilha de custo feita à mão, o título do valor é o mês dos preços
    ("valor_total", ("total", "orcado")),
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
        # Só abas de células: a de gráfico (a curva S, por exemplo) não tem linhas para ler, nem entra na lista da tela.
        nomes = [ws.title for ws in wb.worksheets]
        if not nomes:
            raise ErroNegocio("A planilha não tem nenhuma aba com células, só gráficos.")
        if planilha is not None and planilha not in nomes:
            raise ErroNegocio(f"A planilha não tem a aba '{planilha}'.")

        def linhas_de(nome: str, limite: int) -> list[list]:
            return [list(row) for row in wb[nome].iter_rows(max_row=limite, values_only=True)]

        if planilha is not None:
            return nomes, planilha, linhas_de(planilha, MAX_LINHAS_LIDAS)
        # Planilha de licitação traz várias abas com cabeçalho (capa, encargos, orçamento, composições): vale a de
        # cabeçalho mais completo, não a 1ª que tem um. Os encargos têm só CÓDIGO e DESCRIÇÃO e vinham antes do
        # orçamento. Empate fica com a 1ª. Sem cabeçalho em nenhuma, a 1ª com conteúdo, e a pessoa troca a aba na tela.
        melhor, forca_melhor, reserva = None, 0, None
        for nome in nomes:
            topo = linhas_de(nome, LINHAS_CABECALHO)
            forca = _forca_do_cabecalho(topo)
            if forca > forca_melhor:
                melhor, forca_melhor = nome, forca
            if reserva is None and any(any(_texto(c) for c in r) for r in topo):
                reserva = nome
        escolhida = melhor or reserva
        return (nomes, escolhida, linhas_de(escolhida, MAX_LINHAS_LIDAS)) if escolhida else (nomes, nomes[0], [])
    finally:
        wb.close()


def decodificar(conteudo: bytes) -> str:
    """UTF-8 (com ou sem BOM) primeiro; senão Windows-1252, que é o que o Excel brasileiro salva."""
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252")


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


def _forca_do_cabecalho(grade: list[list]) -> int:
    """Quantos tipos de coluna o cabeçalho conhecido reconhece (código, descrição, quantidade…); 0 sem cabeçalho."""
    i = _cabecalho_conhecido(grade)
    if i is None:
        return 0
    return len({c for t in (_texto(x) for x in grade[i]) for c in _categoria(t)[:1]})


def _achar_cabecalho(grade: list[list]) -> int:
    """O cabeçalho conhecido; senão a 1ª linha não vazia (a pessoa diz as colunas na mão)."""
    i = _cabecalho_conhecido(grade)
    if i is not None:
        return i
    return next((j for j, row in enumerate(grade[:LINHAS_CABECALHO]) if any(_texto(c) for c in row)), 0)


# Preço sem dizer se é unitário ou total ("PREÇO (R$)", "VALOR", "P.U."), depois de tirar a moeda. As planilhas
# SINAPI de licitação trazem "PREÇO(R$)" ao lado de "PREÇO TOTAL (R$)": com o total em outra coluna, o preço solto só
# pode ser o unitário. Sem total ao lado ele fica sem palpite, porque pode ser qualquer um dos dois.
PRECO_SOLTO = {"preco", "valor", "custo", "pu", "p u", "vl", "vlr"}
MOEDA = {"r", "rs", "brl", "reais"}


def _preco_solto(titulo: str) -> bool:
    return " ".join(p for p in _normal(titulo).split() if p not in MOEDA) in PRECO_SOLTO


def _sugerir(cabecalho: list) -> dict[str, int | None]:
    sugestao: dict[str, int | None] = {c: None for c, _ in SINONIMOS}
    usadas: set[int] = set()
    for categoria, _ in SINONIMOS:
        for i, titulo in enumerate(cabecalho):
            if i not in usadas and categoria in _categoria(_texto(titulo)):
                sugestao[categoria] = i
                usadas.add(i)
                break
    if sugestao["valor_unitario"] is None and sugestao["valor_total"] is not None:
        sugestao["valor_unitario"] = next(
            (i for i, t in enumerate(cabecalho) if i not in usadas and _preco_solto(_texto(t))), None)
    return sugestao


_MESES = {m: i for i, m in enumerate(("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov",
                                      "dez"), start=1)}
_MES_ANO = re.compile(r"\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*\s*(\d{4}|\d{2})\b")
_MES_NUM = re.compile(r"\b(0?[1-9]|1[0-2])\s*/\s*(\d{4})\b")
# na amostra a célula numérica 7.1 aparece como o brasileiro lê, "7,1"
_NUMERO_ITEM = re.compile(r"^\d+([.,]\d+)*\.?$")
# quantas linhas abaixo do cabeçalho se olham para achar coluna sem título com dado (a medida das portas pode
# aparecer só lá embaixo)
LINHAS_SEM_TITULO = 300


def _data_base(titulos: list[str]) -> str | None:
    """"ORÇADO SET/23" → "2023-09": na planilha de custo, o mês dos preços costuma estar no título da coluna do
    valor. Só sugere; quem grava a data-base da obra é a pessoa."""
    for t in titulos:
        n = unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().lower()
        if m := _MES_ANO.search(re.sub(r"[/.\-]", " ", n)):
            ano = int(m.group(2)) + (2000 if len(m.group(2)) == 2 else 0)
            return f"{ano:04d}-{_MESES[m.group(1)]:02d}"
        if m := _MES_NUM.search(n):
            return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    return None


# Bancos de preço escritos no topo das planilhas ("Sinapi não desonerado - Data referência: 18/05/2023").
_BANCOS = ("sinapi", "sicro", "seinfra", "orse", "setop", "emop", "sudecap", "cpos", "fde", "agetop", "sbc")
# Linha do topo só vale se fala de preço de referência: "Avenida Monjolo, Chácara 22" não pode virar data-base, e
# "outros 12" casaria com out/12.
_FALA_DE_REFERENCIA = re.compile(r"\b(data|refer|base|tabela|preco|precos|mes)\w*|\b(" + "|".join(_BANCOS) + r")\b")


def _topo(grade: list[list], i: int) -> list[str]:
    """As linhas acima do cabeçalho, cada uma como um texto só (título, obra, local, "SINAPI 05/2023")."""
    return [" ".join(t for t in (_texto(c) for c in row) if t) for row in grade[:i]]


def _referencia_do_topo(linhas: list[str]) -> tuple[str | None, str | None]:
    """(data-base "AAAA-MM", banco "SINAPI não desonerado") lidos do topo da planilha, onde as tabelas oficiais
    escrevem de quando são os preços. Só linhas que falam de referência; a primeira data e o primeiro banco valem."""
    data_base = banco = None
    for linha in linhas:
        n = unicodedata.normalize("NFD", linha).encode("ascii", "ignore").decode().lower()
        if not _FALA_DE_REFERENCIA.search(n):
            continue
        data_base = data_base or _data_base([linha])
        if banco is None and (m := re.search(r"\b(" + "|".join(_BANCOS) + r")\b", n)):
            banco = m.group(1).upper()
            if "nao desonerad" in n or "sem desonera" in n:
                banco += " não desonerado"
            elif "desonerad" in n:
                banco += " desonerado"
    return data_base, banco


def _letra(j: int) -> str:
    """Índice 0-based → letra da coluna como o Excel mostra (0 → A, 26 → AA)."""
    t = ""
    j += 1
    while j:
        j, r = divmod(j - 1, 26)
        t = chr(65 + r) + t
    return t


def _sugerir_sem_titulo(sugestao: dict[str, int | None], sem_titulo: list[int], amostra: list[list[str]]) -> None:
    """Planilha feita à mão costuma ter o número e a descrição sem título nenhum (só os valores têm). Coluna só com
    "7", "7.1" é o item; a de mais texto é a descrição. A pessoa confere na tela de qualquer jeito."""
    def valores(j):
        return [r[j] for r in amostra if j < len(r) and r[j]]
    if sugestao.get("item") is None:
        for j in sem_titulo:
            v = valores(j)
            if v and all(_NUMERO_ITEM.match(x) for x in v) and any("." in x or "," in x for x in v):
                sugestao["item"] = j
                break
    if sugestao.get("nome") is None:
        texto = {j: sum(len(x) for x in valores(j) if any(c.isalpha() for c in x))
                 for j in sem_titulo if j != sugestao.get("item")}
        melhor = max(texto, key=texto.get, default=None)
        if melhor is not None and texto[melhor] >= 12:
            sugestao["nome"] = melhor


async def ler(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, nome_arquivo: str,
              planilha: str | None) -> LeituraPlanilha:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    nomes, aba, grade = _grade(conteudo, nome_arquivo, planilha)
    if not any(any(_texto(c) for c in r) for r in grade):
        raise ErroNegocio("A planilha está vazia.")
    i = _achar_cabecalho(grade)
    cabecalho = grade[i]
    abaixo = grade[i + 1:i + 1 + LINHAS_SEM_TITULO]
    # a linha de dados pode ser mais larga que o cabeçalho (coluna sem título à direita): só conta a coluna que tem
    # dado, senão a célula vazia que o Excel guarda no fim da linha viraria coluna
    com_dado = {j for row in abaixo for j, c in enumerate(row) if _texto(c)}
    titulos_ = [_texto(c) for c in cabecalho]
    largura = max([j + 1 for j, t in enumerate(titulos_) if t] + [j + 1 for j in com_dado] + [len(cabecalho)])
    titulos = [titulos_[j] if j < len(titulos_) else "" for j in range(largura)]
    amostra = []
    for row in grade[i + 1:]:
        textos = [_texto(c) for c in row[:largura]] + [""] * max(0, largura - len(row))
        if any(textos):
            amostra.append(textos)
        if len(amostra) == 8:
            break
    # coluna sem título mas com dado também aparece ("Coluna C"): senão a pessoa não consegue apontar o número e a
    # descrição numa planilha em que só os valores têm cabeçalho
    sem_titulo = [j for j in range(largura) if not titulos[j] and j in com_dado]
    colunas = [ColunaPlanilha(indice=j, titulo=titulos[j] or f"Coluna {_letra(j)}") for j in range(largura)
               if titulos[j] or j in com_dado]
    sugestao = _sugerir(cabecalho)
    _sugerir_sem_titulo(sugestao, sem_titulo, amostra)
    data_topo, banco = _referencia_do_topo(_topo(grade, i))
    return LeituraPlanilha(
        planilhas=nomes, planilha=aba, linha_cabecalho=i + 1, colunas=colunas, amostra=amostra,
        sugestao=Mapeamento(planilha=aba, linha_cabecalho=i + 1, **sugestao),
        # o mês no título da coluna ("ORÇADO SET/23") é o mais perto do valor; senão, o que o topo da planilha diz
        data_base=_data_base(titulos) or data_topo, banco=banco)


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


def _arredonda(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
