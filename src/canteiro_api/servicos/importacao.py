"""Importação de planilha de insumos (CSV do Excel brasileiro): prévia com erro por linha, depois aplica."""
import csv
import io

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Insumo, Movimento, Saldo, TipoMovimento
from canteiro_dominio.tipos import qtd

from ..auth import UsuarioAtual
from ..esquemas import ErroLinha, LinhaImportada, Previa
from ..modelos import doc
from ..nucleo import ErroNegocio, Operacao, evento, executar_e_registrar, obra_do_tenant
from .estoque import _nome_chave, ativos, criado, gravar_insumo_novo, id_insumo, nome_limpo, receita_insumos

COLUNAS = ["nome", "unidade", "saldo", "precisa_ate_o_fim", "curva"]
MAX_LINHAS = 2000
# 3 escritas por linha (insumo, movimento, saldo) + registro e outbox: cabe folgado nas 500 da transação portável, e
# o desfazer do lote (insumo, movimento, saldo por linha) também
LINHAS_POR_TRANSACAO = 100


def decodificar(conteudo: bytes) -> str:
    """UTF-8 (com ou sem BOM) primeiro; senão Windows-1252, que é o que o Excel brasileiro salva."""
    try:
        return conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        return conteudo.decode("cp1252")


def _separador(cabecalho: str) -> str:
    return ";" if cabecalho.count(";") >= cabecalho.count(",") else ","


def ler_csv(conteudo: bytes, nomes_existentes: set[str]) -> tuple[list[LinhaImportada], list[ErroLinha]]:
    texto = decodificar(conteudo)
    primeira = texto.splitlines()[0] if texto.strip() else ""
    if not primeira:
        return [], [ErroLinha(linha=1, mensagem="Arquivo vazio.")]
    leitor = csv.reader(io.StringIO(texto), delimiter=_separador(primeira))
    cabecalho = [c.strip().lower() for c in next(leitor)]
    faltando = [c for c in COLUNAS if c not in cabecalho]
    if faltando:
        return [], [ErroLinha(linha=1, mensagem=f"Faltam colunas: {', '.join(faltando)}. "
                                                f"Esperado: {';'.join(COLUNAS)}.")]
    pos = {c: cabecalho.index(c) for c in COLUNAS}
    linhas: list[LinhaImportada] = []
    erros: list[ErroLinha] = []
    # a mesma chave do cadastro pela tela: "Areia  média" e "areia média" são o mesmo insumo
    vistos = {_nome_chave(n) for n in nomes_existentes}
    for n, crua in enumerate(leitor, start=2):
        if not any(c.strip() for c in crua):
            continue
        if n - 1 > MAX_LINHAS:
            erros.append(ErroLinha(linha=n, mensagem=f"Mais de {MAX_LINHAS} linhas; divida o arquivo."))
            break
        valores = {c: (crua[i].strip() if i < len(crua) else "") for c, i in pos.items()}
        valores["nome"] = nome_limpo(valores["nome"])
        erros_linha: list[ErroLinha] = []
        if not valores["nome"]:
            erros_linha.append(ErroLinha(linha=n, campo="nome", mensagem="Nome vazio."))
        elif _nome_chave(valores["nome"]) in vistos:
            erros_linha.append(ErroLinha(linha=n, campo="nome", mensagem=f"'{valores['nome']}' já existe na obra "
                                                                         "ou se repete no arquivo."))
        if not valores["unidade"]:
            erros_linha.append(ErroLinha(linha=n, campo="unidade", mensagem="Unidade vazia."))
        numeros = {}
        for campo in ("saldo", "precisa_ate_o_fim"):
            try:
                numeros[campo] = qtd(valores[campo] or "0")
            except (ValueError, ArithmeticError) as e:
                msg = str(e) if "ambíguo" in str(e) else f"'{valores[campo]}' não é um número."
                erros_linha.append(ErroLinha(linha=n, campo=campo, mensagem=msg))
        curva = (valores["curva"] or "B").upper()
        if curva not in ("A", "B", "C"):
            erros_linha.append(ErroLinha(linha=n, campo="curva", mensagem="Curva tem de ser A, B ou C."))
        if numeros.get("precisa_ate_o_fim", 0) < 0:
            erros_linha.append(ErroLinha(linha=n, campo="precisa_ate_o_fim", mensagem="Não pode ser negativo."))
        if erros_linha:
            erros.extend(erros_linha)
            continue
        vistos.add(_nome_chave(valores["nome"]))
        linhas.append(LinhaImportada(linha=n, nome=valores["nome"], unidade=valores["unidade"],
                                     saldo=numeros["saldo"], precisa_ate_o_fim=numeros["precisa_ate_o_fim"],
                                     curva=curva))
    return linhas, erros


async def importar(arm, u: UsuarioAtual, obra_id: str, conteudo: bytes, aplicar: bool) -> Previa:
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    existentes = {i["nome"] for i in
                  ativos(await arm.consultar(Consulta("insumos", u.tenant_id, [Filtro("obra_id", "==", obra_id)])))}
    linhas, erros = ler_csv(conteudo, existentes)
    if not aplicar or erros:
        # com qualquer erro nada é gravado: a pessoa corrige a planilha e manda de novo
        return Previa(aplicado=False, linhas=linhas, erros=erros)
    if not linhas:
        raise ErroNegocio("Nenhuma linha para importar.")
    lotes = [linhas[i:i + LINHAS_POR_TRANSACAO] for i in range(0, len(linhas), LINHAS_POR_TRANSACAO)]
    registros = []
    for k, lote in enumerate(lotes, start=1):
        parte = f" — parte {k} de {len(lotes)}" if len(lotes) > 1 else ""
        registros.append(await arm.em_transacao(_gravar_lote(u, obra_id, lote, parte)))
    return Previa(aplicado=True, linhas=linhas, erros=[], registro_ids=registros)


def _gravar_lote(u: UsuarioAtual, obra_id: str, lote: list[LinhaImportada], parte: str):
    """Um lote = uma transação = uma entrada no registro, com o seu Desfazer (arquivar os insumos do lote)."""
    async def corpo(tx):
        # ID derivado do nome: duas importações iguais ao mesmo tempo colidem no mesmo documento, não duplicam
        ids = [id_insumo(u.tenant_id, obra_id, linha.nome) for linha in lote]
        lidos = [await tx.obter("insumos", u.tenant_id, id_) for id_ in ids]
        criados = []

        def aplicar():
            for linha, id_, existente in zip(lote, ids, lidos, strict=True):
                ins = Insumo(id=id_, tenant_id=u.tenant_id, obra_id=obra_id, nome=linha.nome, unidade=linha.unidade,
                             precisa_ate_o_fim=linha.precisa_ate_o_fim, curva=linha.curva)
                d = doc(ins)
                gravar_insumo_novo(tx, d, existente)
                if linha.saldo:
                    tx.criar("movimentos", doc(Movimento(tenant_id=u.tenant_id, obra_id=obra_id, insumo_id=ins.id,
                                                         tipo=TipoMovimento.AJUSTE, quantidade=linha.saldo,
                                                         quem=u.nome, usuario_id=u.id, origem="sistema",
                                                         origem_doc="importação")))
                # o saldo nasce com o instante do cadastro: é por ele que o desfazer sabe que nada se mexeu depois
                tx.salvar("saldos", doc(Saldo(id=ins.id, tenant_id=u.tenant_id, obra_id=obra_id, saldo=linha.saldo,
                                              atualizado_em=ins.criado_em)))
                criados.append(criado(d, linha.saldo))
            evento(tx, u.tenant_id, "estoque.importado", obra_id, {"insumos": len(lote)})
            return criados

        texto = f"Importou {len(lote)} insumo(s) da planilha{parte}"
        op = Operacao(obra_id=obra_id, aplicar=aplicar, texto=lambda _: texto,
                      desfazer=lambda c: receita_insumos(obra_id, f"importação de {len(c)} insumo(s) da planilha"
                                                                  f"{parte}", arquivar=c))
        _, reg = executar_e_registrar(tx, u, op)
        return reg["id"]
    return corpo
