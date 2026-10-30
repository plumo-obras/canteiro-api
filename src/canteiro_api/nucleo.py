"""Peças que toda operação de domínio usa: erros HTTP de negócio, outbox e registro de auditoria.

Toda operação segue o formato da transação portável: `preparar_*` só LÊ e devolve uma `Operacao`; `aplicar()` só
ESCREVE. Assim dá para compor várias (aprovar proposta = operação + proposta + registro) sem ler depois de escrever.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from canteiro_dominio.modelos import EntradaRegistro

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
              proposta_id: str | None = None, desfazer: dict | None = None) -> dict:
    e = EntradaRegistro(tenant_id=u.tenant_id, obra_id=obra_id, quem=u.nome, origem=origem, texto=texto,
                        proposta_id=proposta_id, desfazer=desfazer)
    # `quem` já é o nome legível; `quem_nome` explicita para a tela, `usuario_id` fica para auditoria
    d = {**doc(e), "quem_nome": u.nome, "usuario_id": u.id}
    tx.criar("registro", d)
    return d


def executar_e_registrar(tx, u: UsuarioAtual, op: Operacao, *, origem: str = "tela",
                               proposta_id: str | None = None) -> tuple[Any, dict]:
    resultado = op.aplicar()
    entrada = registrar(tx, u, obra_id=op.obra_id, texto=op.texto(resultado), origem=origem,
                        proposta_id=proposta_id, desfazer=op.desfazer(resultado))
    return resultado, entrada


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
