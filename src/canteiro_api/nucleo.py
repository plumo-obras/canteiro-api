"""Peças que toda operação de domínio usa: erros HTTP de negócio, outbox e registro de auditoria.

Toda operação segue o formato da transação portável: `preparar_*` só LÊ e devolve uma `Operacao`; `aplicar()` só
ESCREVE. Assim dá para compor várias (aprovar proposta = operação + proposta + registro) sem ler depois de escrever.
"""
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import EntradaRegistro

from .armazenamento.base import normalizar
from .auth import UsuarioAtual
from .modelos import EventoOutbox, doc


class ErroNegocio(Exception):
    codigo = 422

    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class NaoEncontrado(ErroNegocio):
    codigo = 404


class Conflito(ErroNegocio):
    codigo = 409


@dataclass
class Operacao:
    """Resultado da fase de leitura. `aplicar()` grava e devolve o objeto criado ou alterado."""
    obra_id: str | None
    aplicar: Callable[[], Any]
    texto: Callable[[Any], str]                      # texto do registro, a partir do resultado
    desfazer: Callable[[Any], dict | None]           # receita da operação inversa, a partir do resultado


def evento(tx, tenant_id: str, tipo: str, obra_id: str | None, dados: dict[str, Any]) -> None:
    """Grava o evento na outbox dentro da transação da mudança: se a mudança vale, o evento existe."""
    e = EventoOutbox(tenant_id=tenant_id, tipo=tipo, obra_id=obra_id, chave_ordenacao=obra_id or tenant_id,
                     dados=dados)
    tx.criar("outbox", doc(e))


def registrar(tx, u: UsuarioAtual, *, obra_id: str | None, texto: str, origem: str = "tela",
              proposta_id: str | None = None, desfazer: dict | None = None, id: str | None = None) -> dict:
    e = EntradaRegistro(tenant_id=u.tenant_id, obra_id=obra_id, quem=u.nome, origem=origem, texto=texto,
                        proposta_id=proposta_id, desfazer=desfazer, **({"id": id} if id else {}))
    # `quem` já é o nome legível; `quem_nome` explicita para a tela, `usuario_id` fica para auditoria
    d = {**doc(e), "quem_nome": u.nome, "usuario_id": u.id}
    tx.criar("registro", d)
    return d


def executar_e_registrar(tx, u: UsuarioAtual, op: Operacao, *, origem: str = "tela",
                               proposta_id: str | None = None, registro_id: str | None = None) -> tuple[Any, dict]:
    """`registro_id` fixo quando a escrita é idempotente: a repetição devolve o mesmo registro (e o mesmo Desfazer)."""
    resultado = op.aplicar()
    entrada = registrar(tx, u, obra_id=op.obra_id, texto=op.texto(resultado), origem=origem,
                        proposta_id=proposta_id, desfazer=op.desfazer(resultado), id=registro_id)
    return resultado, entrada


def receita_inversa(receita: dict) -> dict:
    """Refazer é desfazer o desfazer: a receita das edições (antes/depois) se inverte trocando os dois. `refazer`
    alterna, para o texto e o botão dizerem Refazer ou Desfazer."""
    return {**receita, "antes": receita["depois"], "depois": receita["antes"], "refazer": not receita.get("refazer")}


def verbo_desfazer(receita: dict) -> str:
    return "Refeito" if receita.get("refazer") else "Desfeito"


async def obra_do_tenant(leitor, tenant_id: str, obra_id: str) -> dict:
    o = await leitor.obter("obras", tenant_id, obra_id)
    if not o:
        raise NaoEncontrado("Obra não encontrada.")
    return o


async def insumo_da_obra(leitor, tenant_id: str, obra_id: str, insumo_id: str) -> dict:
    i = await leitor.obter("insumos", tenant_id, insumo_id)
    if not i or i["obra_id"] != obra_id:
        raise NaoEncontrado("Insumo não encontrado nesta obra.")
    return i



LIMITE_GRUPO = 500


async def listar_pagina(leitor, consulta: Consulta, campo: str, antes_de: datetime | None) -> list[dict]:
    """Uma página de listagem por `campo` desc; a seguinte se pede com `antes_de` = o `campo` do último item.

    O cursor é EXCLUSIVO (`campo < antes_de`), e a página nunca termina no meio de um empate: se o item seguinte tem
    o mesmo instante do último, o grupo inteiro daquele instante entra nesta página (por isso ela pode passar um
    pouco do `limite`). Inclusivo devolveria os mesmos itens para sempre quando o empate é do tamanho da página;
    exclusivo sem completar o grupo pularia o resto dele. Só igualdade além da desigualdade: portável ao Firestore.
    Data sem fuso é recusada: adivinhar o fuso pularia registros.
    """
    filtros = list(consulta.filtros)
    if antes_de is not None:
        if antes_de.tzinfo is None:
            raise ErroNegocio("antes_de precisa de fuso (ex.: 2026-11-01T13:00:00Z).")
        filtros.append(Filtro(campo, "<", normalizar(antes_de)))
    limite = consulta.limite
    achados = await leitor.consultar(replace(consulta, filtros=filtros, limite=None if limite is None else limite + 1))
    if limite is None or len(achados) <= limite:
        return achados
    pagina, proximo = achados[:limite], achados[limite]
    fronteira = pagina[-1][campo]
    if proximo[campo] != fronteira:
        return pagina
    grupo = await leitor.consultar(replace(consulta, filtros=[*consulta.filtros, Filtro(campo, "==", fronteira)],
                                           ordem=[(campo, "desc")], limite=LIMITE_GRUPO))
    return [x for x in pagina if x[campo] != fronteira] + grupo
