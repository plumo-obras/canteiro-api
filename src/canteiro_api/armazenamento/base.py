"""Peças comuns aos adaptadores da porta `canteiro_dominio.armazenamento`.

Os dois adaptadores (memória e Mongo) normalizam os valores do mesmo jeito para que a suíte de contrato veja
exatamente os mesmos dados nos dois — e, depois, no Firestore.
"""
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from canteiro_dominio.armazenamento import LIMITE_ESCRITAS_TRANSACAO, ConflitoDeId, Consulta

__all__ = ["ConflitoDeId", "DocumentoAusente", "EscritaAntesDaLeitura", "normalizar", "validar_doc"]


class DocumentoAusente(LookupError):
    """`atualizar` em documento que não existe (ou é de outro tenant). O Firestore também recusa."""


class EscritaAntesDaLeitura(RuntimeError):
    """Leitura depois de escrita na mesma transação: o Firestore não deixa, então ninguém deixa."""


def normalizar(valor: Any) -> Any:
    """Deixa o valor no formato que os três bancos guardam igual.

    Datas: sempre UTC e com precisão de milissegundo (o Mongo trunca no milissegundo; sem isso o adaptador em memória
    devolveria um valor que o Mongo não devolve). Enum vira o valor; tupla vira lista.
    """
    if isinstance(valor, Enum):
        return normalizar(valor.value)
    if isinstance(valor, bool | int | float | str | bytes) or valor is None:
        return valor
    if isinstance(valor, datetime):
        if valor.tzinfo is None:
            raise ValueError("Data sem fuso: grave sempre em UTC com tzinfo.")
        valor = valor.astimezone(UTC)
        return valor.replace(microsecond=valor.microsecond // 1000 * 1000)
    if isinstance(valor, dict):
        return {str(k): normalizar(v) for k, v in valor.items()}
    if isinstance(valor, list | tuple):
        return [normalizar(v) for v in valor]
    raise TypeError(f"Tipo {type(valor).__name__} não é portável para o banco.")


def validar_doc(doc: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(doc.get("id"), str) or not doc["id"]:
        raise ValueError("Documento sem 'id' (string).")
    if not isinstance(doc.get("tenant_id"), str) or not doc["tenant_id"]:
        raise ValueError("Documento sem 'tenant_id'.")
    if "_id" in doc:
        raise ValueError("Use 'id', não '_id': o mapeamento é do adaptador.")
    if "/" in doc["id"]:
        raise ValueError("ID com '/' não é aceito pelo Firestore.")
    return normalizar(doc)


def validar_campos(campos: dict[str, Any]) -> dict[str, Any]:
    if not campos:
        raise ValueError("Nada para atualizar.")
    proibidos = {"id", "_id", "tenant_id"} & set(campos)
    if proibidos:
        raise ValueError(f"Não se altera {sorted(proibidos)} de um documento.")
    return normalizar(campos)


def validar_consulta(c: Consulta) -> None:
    if not c.tenant_id:
        raise ValueError("Consulta sem tenant_id.")
    c.validar()
    if c.limite is not None and c.limite <= 0:
        raise ValueError("Limite tem de ser positivo.")


class TransacaoComBuffer:
    """Junta as escritas e só aplica no fim, como o Firestore: leituras primeiro, depois até 500 escritas."""

    def __init__(self, ler: Callable[[str, str, str], Awaitable[dict[str, Any] | None]]):
        self._ler = ler
        self.escritas: list[tuple[str, str, dict[str, Any], str | None]] = []   # (op, colecao, doc/campos, tenant)

    async def obter(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None:
        if self.escritas:
            raise EscritaAntesDaLeitura("Na transação, leia tudo antes da primeira escrita.")
        return await self._ler(colecao, tenant_id, id)

    def _anotar(self, item) -> None:
        if len(self.escritas) >= LIMITE_ESCRITAS_TRANSACAO:
            raise ValueError(f"Transação com mais de {LIMITE_ESCRITAS_TRANSACAO} escritas não é portável.")
        self.escritas.append(item)

    def criar(self, colecao: str, doc: dict[str, Any]) -> None:
        self._anotar(("criar", colecao, validar_doc(doc), None))

    def salvar(self, colecao: str, doc: dict[str, Any]) -> None:
        self._anotar(("salvar", colecao, validar_doc(doc), None))

    def atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None:
        self._anotar(("atualizar", colecao, {"id": id, "campos": validar_campos(campos)}, tenant_id))
