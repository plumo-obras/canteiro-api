"""Porta de armazenamento. A API fala só com esta interface; Mongo e Firestore são adaptadores.

Ela é deliberadamente MENOR que o Mongo: só aceita o que o Firestore também aceita, para a migração ser trocar o
adaptador e rodar a mesma suíte de testes de contrato.
- Filtros: igualdade, `in` (até 30 valores) e desigualdade (<, <=, >, >=) em UM campo só por consulta.
- Ordenação + limite. Sem joins, sem regex, sem agregação: relatório é projeção pré-calculada.
- Transação: todas as leituras antes de qualquer escrita; até 500 escritas.
- Unicidade: pelo ID do documento (o Firestore não tem índice único).
"""
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Operador = Literal["==", "in", "<", "<=", ">", ">="]
LIMITE_IN = 30
LIMITE_ESCRITAS_TRANSACAO = 500


@dataclass(frozen=True)
class Filtro:
    campo: str
    op: Operador
    valor: Any


@dataclass
class Consulta:
    colecao: str
    tenant_id: str                         # obrigatório: nenhuma consulta atravessa clientes
    filtros: list[Filtro] = field(default_factory=list)
    ordem: list[tuple[str, Literal["asc", "desc"]]] = field(default_factory=list)
    limite: int | None = None

    def validar(self) -> None:
        desig = {f.campo for f in self.filtros if f.op in ("<", "<=", ">", ">=")}
        if len(desig) > 1:
            raise ValueError(f"Desigualdade em mais de um campo ({sorted(desig)}) não é portável para o Firestore.")
        for f in self.filtros:
            if f.op == "in" and len(f.valor) > LIMITE_IN:
                raise ValueError(f"'in' com mais de {LIMITE_IN} valores não é portável para o Firestore.")
        if desig and self.ordem and self.ordem[0][0] not in desig:
            raise ValueError("Com desigualdade, a 1ª ordenação tem de ser no mesmo campo (regra do Firestore).")


class ConflitoDeId(Exception):
    """Documento com esse ID já existe (é assim que garantimos unicidade e idempotência)."""


class Transacao(Protocol):
    async def obter(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None: ...
    def criar(self, colecao: str, doc: dict[str, Any]) -> None: ...          # falha com ConflitoDeId se já existir
    def salvar(self, colecao: str, doc: dict[str, Any]) -> None: ...         # cria ou substitui
    def atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None: ...


class Armazenamento(Protocol):
    async def obter(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None: ...
    async def consultar(self, consulta: Consulta) -> list[dict[str, Any]]: ...
    async def criar(self, colecao: str, doc: dict[str, Any]) -> None: ...
    async def salvar(self, colecao: str, doc: dict[str, Any]) -> None: ...
    async def atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None: ...
    async def em_transacao(self, funcao) -> Any:
        """Roda `funcao(tx)`; leia tudo primeiro, depois escreva. Repete em conflito, como o Firestore faz."""
        ...
