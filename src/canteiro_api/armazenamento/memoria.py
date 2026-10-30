"""Adaptador em memória: para testes rápidos e para provar que a porta não depende de nenhum banco."""
import asyncio
import copy
import functools
from typing import Any

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro

from .base import DocumentoAusente, TransacaoComBuffer, validar_campos, validar_consulta, validar_doc

_AUSENTE = object()


def _campo(doc: dict[str, Any], caminho: str) -> Any:
    atual: Any = doc
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return _AUSENTE
        atual = atual[parte]
    return atual


def _definir(doc: dict[str, Any], caminho: str, valor: Any) -> None:
    *pais, ultimo = caminho.split(".")
    for parte in pais:
        doc = doc.setdefault(parte, {})
    doc[ultimo] = valor


def _casa(doc: dict[str, Any], f: Filtro) -> bool:
    v = _campo(doc, f.campo)
    if v is _AUSENTE:
        return False
    try:
        match f.op:
            case "==":
                return v == f.valor
            case "in":
                return v in f.valor
            case "<":
                return v < f.valor
            case "<=":
                return v <= f.valor
            case ">":
                return v > f.valor
            case ">=":
                return v >= f.valor
    except TypeError:
        return False      # tipos diferentes não se comparam (o Firestore também os separa)
    return False


class ArmazenamentoMemoria:
    def __init__(self) -> None:
        # chave = (coleção, id): o ID é único na coleção inteira, como no Mongo e no Firestore
        self._docs: dict[tuple[str, str], dict[str, Any]] = {}
        self._trava = asyncio.Lock()

    async def fechar(self) -> None:
        pass

    def _ler(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None:
        doc = self._docs.get((colecao, id))
        if doc is None or doc["tenant_id"] != tenant_id:
            return None
        return copy.deepcopy(doc)

    async def obter(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None:
        if not tenant_id:
            raise ValueError("Leitura sem tenant_id.")
        return self._ler(colecao, tenant_id, id)

    async def consultar(self, consulta: Consulta) -> list[dict[str, Any]]:
        validar_consulta(consulta)
        achados = [
            d for (col, _), d in self._docs.items()
            if col == consulta.colecao and d["tenant_id"] == consulta.tenant_id
            and all(_casa(d, f) for f in consulta.filtros)
            and all(_campo(d, campo) is not _AUSENTE for campo, _ in consulta.ordem)
        ]

        # Desempate pelo ID no sentido da última ordenação, como o Firestore faz implicitamente.
        ultima = consulta.ordem[-1][1] if consulta.ordem else "asc"
        ordem = [*consulta.ordem, ("id", ultima)]

        def comparar(a, b):
            for campo, sentido in ordem:
                va, vb = _campo(a, campo), _campo(b, campo)
                if va == vb:
                    continue
                menor = va < vb
                return (-1 if menor else 1) * (1 if sentido == "asc" else -1)
            return 0

        achados.sort(key=functools.cmp_to_key(comparar))
        if consulta.limite is not None:
            achados = achados[: consulta.limite]
        return copy.deepcopy(achados)

    def _criar(self, colecao: str, doc: dict[str, Any]) -> None:
        if (colecao, doc["id"]) in self._docs:
            raise ConflitoDeId(f"{colecao}/{doc['id']} já existe.")
        self._docs[(colecao, doc["id"])] = copy.deepcopy(doc)

    def _salvar(self, colecao: str, doc: dict[str, Any]) -> None:
        atual = self._docs.get((colecao, doc["id"]))
        if atual is not None and atual["tenant_id"] != doc["tenant_id"]:
            raise ConflitoDeId(f"{colecao}/{doc['id']} pertence a outro tenant.")
        self._docs[(colecao, doc["id"])] = copy.deepcopy(doc)

    def _atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None:
        atual = self._docs.get((colecao, id))
        if atual is None or atual["tenant_id"] != tenant_id:
            raise DocumentoAusente(f"{colecao}/{id} não existe.")
        for caminho, valor in campos.items():
            _definir(atual, caminho, copy.deepcopy(valor))

    async def criar(self, colecao: str, doc: dict[str, Any]) -> None:
        self._criar(colecao, validar_doc(doc))

    async def salvar(self, colecao: str, doc: dict[str, Any]) -> None:
        self._salvar(colecao, validar_doc(doc))

    async def atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None:
        self._atualizar(colecao, tenant_id, id, validar_campos(campos))

    async def em_transacao(self, funcao) -> Any:
        # Uma transação por vez: em memória não há conflito, então não há o que repetir.
        async with self._trava:
            async def ler(colecao, tenant_id, id):
                return await self.obter(colecao, tenant_id, id)

            tx = TransacaoComBuffer(ler)
            resultado = await funcao(tx)
            # Confere tudo antes de aplicar para que um ConflitoDeId no meio não deixe metade gravada.
            sombra = dict(self._docs)
            try:
                for op, colecao, dados, tenant in tx.escritas:
                    match op:
                        case "criar":
                            self._criar(colecao, dados)
                        case "salvar":
                            self._salvar(colecao, dados)
                        case "atualizar":
                            # cópia para não sujar o original caso algo falhe depois
                            chave = (colecao, dados["id"])
                            if chave in self._docs:
                                self._docs[chave] = copy.deepcopy(self._docs[chave])
                            self._atualizar(colecao, tenant, dados["id"], dados["campos"])
            except Exception:
                self._docs = sombra
                raise
            return resultado
