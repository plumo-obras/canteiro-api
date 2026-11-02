"""Fornecedores do tenant: valem para todas as obras. O pedido guarda o nome além do ID (a folha impressa não
busca o cadastro)."""
import re

from canteiro_dominio.armazenamento import Consulta
from canteiro_dominio.modelos import Fornecedor
from canteiro_dominio.regras import cnpj_valido

from ..auth import UsuarioAtual
from ..esquemas import FornecedorCorpo, FornecedorEdicao
from ..modelos import doc
from ..nucleo import Conflito, ErroNegocio, NaoEncontrado, Operacao, executar_e_registrar
from .estoque import id_derivado

_ROTULOS = {"nome": "nome", "cnpj": "CNPJ", "contato": "contato", "telefone": "telefone", "email": "e-mail"}


def _cnpj(texto: str) -> str:
    digitos = re.sub(r"\D", "", texto or "")
    if digitos and not cnpj_valido(digitos):
        raise ErroNegocio("CNPJ inválido: confira os 14 números.")
    return digitos


def _chave_nome(nome: str) -> str:
    return " ".join(nome.split()).casefold()


async def listar(arm, tenant_id: str) -> list[dict]:
    fs = await arm.consultar(Consulta("fornecedores", tenant_id, []))
    return sorted(fs, key=lambda f: _chave_nome(f["nome"]))


async def _repetido(arm, tenant_id: str, *, cnpj: str, nome: str, menos: str | None = None) -> dict | None:
    """CNPJ repetido é o mesmo fornecedor; sem CNPJ, o nome é a chave. Consulta antes da transação (a transação só lê
    por ID); o ID derivado do CNPJ ou do nome fecha a corrida de dois cadastros iguais ao mesmo tempo."""
    for f in await listar(arm, tenant_id):
        if f["id"] == menos:
            continue
        if (cnpj and f.get("cnpj") == cnpj) or (not cnpj and not f.get("cnpj")
                                                 and _chave_nome(f["nome"]) == _chave_nome(nome)):
            return f
    return None


async def criar(arm, u: UsuarioAtual, corpo: FornecedorCorpo) -> dict:
    cnpj = _cnpj(corpo.cnpj)
    nome = " ".join(corpo.nome.split())
    if ja := await _repetido(arm, u.tenant_id, cnpj=cnpj, nome=nome):
        raise Conflito(f"Já cadastrado: {ja['nome']}.")
    f = Fornecedor(id=id_derivado(u.tenant_id, "fornecedor", cnpj or _chave_nome(nome)), tenant_id=u.tenant_id,
                   nome=nome, cnpj=cnpj, contato=corpo.contato or "", telefone=corpo.telefone or "",
                   email=corpo.email or "")
    d = doc(f)

    async def corpo_tx(tx):
        op = Operacao(obra_id=None, aplicar=lambda: (tx.criar("fornecedores", d), d)[1],
                      texto=lambda r: f"Cadastrou o fornecedor {nome}" + (f" (CNPJ {_fmt_cnpj(cnpj)})" if cnpj else ""),
                      desfazer=lambda r: None)
        _, reg = executar_e_registrar(tx, u, op)
        return {**d, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


def _fmt_cnpj(c: str) -> str:
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 else c


async def editar(arm, u: UsuarioAtual, fornecedor_id: str, corpo: FornecedorEdicao) -> dict:
    mudancas = corpo.model_dump(exclude_none=True)
    if "cnpj" in mudancas:
        mudancas["cnpj"] = _cnpj(mudancas["cnpj"])
    if "nome" in mudancas:
        mudancas["nome"] = " ".join(mudancas["nome"].split())
    atual = await arm.obter("fornecedores", u.tenant_id, fornecedor_id)
    if not atual:
        raise NaoEncontrado("Fornecedor não encontrado.")
    cnpj = mudancas.get("cnpj", atual.get("cnpj", ""))
    if ("cnpj" in mudancas or "nome" in mudancas) and (ja := await _repetido(
            arm, u.tenant_id, cnpj=cnpj, nome=mudancas.get("nome", atual["nome"]), menos=fornecedor_id)):
        raise Conflito(f"Já cadastrado: {ja['nome']}.")

    async def corpo_tx(tx):
        f = await tx.obter("fornecedores", u.tenant_id, fornecedor_id)
        mudou = {k: v for k, v in mudancas.items() if f.get(k) != v}
        if not mudou:
            raise ErroNegocio("Nada mudou.")
        antes = {k: f.get(k, "") for k in mudou}

        def aplicar():
            tx.atualizar("fornecedores", u.tenant_id, fornecedor_id, mudou)
            return {**f, **mudou}

        op = Operacao(obra_id=None, aplicar=aplicar,
                      texto=lambda r: f"Editou o fornecedor {r['nome']}: {', '.join(_ROTULOS[k] for k in mudou)}",
                      desfazer=lambda r: {"op": "editar_fornecedor", "fornecedor_id": fornecedor_id,
                                          "antes": antes, "depois": mudou})
        novo, reg = executar_e_registrar(tx, u, op)
        return {**novo, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo_tx)


async def preparar_desfazer_edicao(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    f = await tx.obter("fornecedores", u.tenant_id, receita["fornecedor_id"])
    if not f:
        raise NaoEncontrado("Fornecedor não encontrado.")
    if any(f.get(k) != v for k, v in receita["depois"].items()):
        raise Conflito(f"{f['nome']} foi editado de novo depois; não dá para desfazer esta edição.")

    def aplicar():
        tx.atualizar("fornecedores", u.tenant_id, f["id"], receita["antes"])
        return {**f, **receita["antes"]}

    return Operacao(obra_id=None, aplicar=aplicar, texto=lambda r: f"Desfeito: edição do fornecedor {r['nome']}",
                    desfazer=lambda r: None)
