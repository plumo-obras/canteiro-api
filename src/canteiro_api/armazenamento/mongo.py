"""Adaptador MongoDB com a API assíncrona nativa do PyMongo (o Motor foi descontinuado).

Este é o ÚNICO módulo que importa `pymongo`. Ele mapeia `id` ↔ `_id`, exige tenant em toda leitura e usa transações
reais (replica set), repetidas pelo próprio driver em conflito transitório.
"""
import logging
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta

from .base import DocumentoAusente, TransacaoComBuffer, validar_campos, validar_consulta, validar_doc

# Em DEBUG o driver loga o comando inteiro, com o documento dentro: e-mail da lista de espera, hash de senha, chave
# cifrada. Fica no máximo em INFO, mesmo com o resto da aplicação em DEBUG.
logging.getLogger("pymongo.command").setLevel(logging.INFO)

_OPS = {"==": "$eq", "in": "$in", "<": "$lt", "<=": "$lte", ">": "$gt", ">=": "$gte"}

# Índices das consultas que a API faz (filtros de igualdade, depois o campo da ordenação ou da desigualdade). No
# Firestore viram índices compostos declarados. `consultar` sempre acrescenta `_id` ao fim da ordenação (desempate
# igual ao do Firestore), então todo índice termina em `_id`: sem ele o Mongo ordena em memória mesmo com o resto
# casando. Consulta sem filtro também precisa de `tenant_id` na frente, senão o plano anda pelo `_id_` de todos os
# clientes. O teste `test_indices.py` confere com explain() que nenhuma consulta da API cai em COLLSCAN ou SORT.
INDICES: dict[str, list[list[str]]] = {
    "obras": [["tenant_id", "nome", "_id"]],
    "insumos": [["tenant_id", "obra_id", "nome", "_id"], ["tenant_id", "obra_id", "_id"], ["tenant_id", "_id"]],
    "saldos": [["tenant_id", "obra_id", "_id"], ["tenant_id", "_id"]],
    "versoes_orcamento": [["tenant_id", "obra_id", "_id"], ["tenant_id", "_id"]],
    "itens_orcamento": [["tenant_id", "versao_id", "_id"]],
    "recebimentos": [["tenant_id", "pedido_id", "_id"]],
    "medicoes": [["tenant_id", "obra_id", "_id"]],
    "movimentos": [["tenant_id", "obra_id", "insumo_id", "criado_em", "_id"],
                   ["tenant_id", "obra_id", "criado_em", "_id"]],
    "pedidos": [["tenant_id", "obra_id", "status", "criado_em", "_id"], ["tenant_id", "obra_id", "criado_em", "_id"],
                ["tenant_id", "obra_id", "status", "_id"], ["tenant_id", "obra_id", "_id"],
                ["tenant_id", "status", "_id"]],
    "propostas": [["tenant_id", "status", "obra_id", "criado_em", "_id"], ["tenant_id", "status", "criado_em", "_id"],
                  ["tenant_id", "obra_id", "criado_em", "_id"], ["tenant_id", "criado_em", "_id"],
                  ["tenant_id", "status", "_id"]],
    "registro": [["tenant_id", "obra_id", "criado_em", "_id"], ["tenant_id", "criado_em", "_id"]],
    "uso_ia": [["tenant_id", "dia", "_id"]],
    "outbox": [["tenant_id", "publicado", "criado_em", "_id"]],
    "renovacoes": [["tenant_id", "familia", "_id"], ["tenant_id", "usuario_id", "revogado", "usado", "_id"]],
    "sessoes": [["tenant_id", "dono", "atualizado_em", "_id"],
                ["tenant_id", "dono", "obra_id", "atualizado_em", "_id"]],
    "mensagens": [["tenant_id", "sessao_id", "seq", "_id"]],
    "cronogramas": [["tenant_id", "_id"]],
    "fornecedores": [["tenant_id", "_id"]],
    "etapas_extras": [["tenant_id", "obra_id", "_id"]],
    "topicos": [["tenant_id", "obra_id", "_id"]],
    "comprovantes": [["tenant_id", "obra_id", "status", "criado_em", "_id"],
                     ["tenant_id", "obra_id", "criado_em", "_id"], ["tenant_id", "obra_id", "status", "_id"]],
    "gastos": [["tenant_id", "obra_id", "estornado", "etapa", "criado_em", "_id"],
               ["tenant_id", "obra_id", "estornado", "criado_em", "_id"], ["tenant_id", "obra_id", "estornado", "_id"]],
}

# Coleções que se apagam sozinhas: o campo guarda o INSTANTE em que o documento vence (data BSON, nunca texto: o TTL
# do Mongo ignora string). É o mesmo formato da política de TTL do Firestore, que apaga quando a data do campo passa;
# por isso o índice usa expireAfterSeconds=0 em vez de "N segundos depois de criado_em". O apagamento é preguiçoso
# nos dois bancos (minutos no Mongo, até um dia no Firestore): quem lê continua conferindo a validade no código.
TTL: dict[str, str] = {"renovacoes": "expira_em", "tentativas": "expira_em", "outbox": "expira_em"}


def _para_mongo(doc: dict[str, Any]) -> dict[str, Any]:
    d = dict(doc)
    d["_id"] = d.pop("id")
    return d


def _de_mongo(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    d = dict(doc)
    d["id"] = d.pop("_id")
    return d


class ArmazenamentoMongo:
    def __init__(self, url: str, banco: str, *, cliente: AsyncMongoClient | None = None):
        # tz_aware: as datas voltam com fuso UTC, iguais às do adaptador em memória
        self._cliente = cliente or AsyncMongoClient(url, tz_aware=True, uuidRepresentation="standard")
        self._db = self._cliente[banco]

    async def fechar(self) -> None:
        await self._cliente.close()

    async def garantir_indices(self) -> None:
        for colecao, lista in INDICES.items():
            for campos in lista:
                await self._db[colecao].create_index([(c, ASCENDING) for c in campos])
        for colecao, campo in TTL.items():
            await self._db[colecao].create_index([(campo, ASCENDING)], expireAfterSeconds=0)

    # --- manutenção (scripts como migrar_arquivos): FORA da porta `Armazenamento` de propósito. Atravessam clientes
    # e apagam, duas coisas que nenhuma rota pode fazer; quem usa é só código de manutenção, rodado à mão.

    async def tenants_da_colecao(self, colecao: str) -> list[str]:
        return sorted(await self._db[colecao].distinct("tenant_id"))

    async def apagar_ids(self, colecao: str, tenant_id: str, ids: list[str]) -> None:
        if not tenant_id:
            raise ValueError("Apagar sem tenant_id.")
        await self._db[colecao].delete_many({"_id": {"$in": ids}, "tenant_id": tenant_id})

    async def apagar_banco(self) -> None:
        """Só para testes."""
        await self._cliente.drop_database(self._db.name)

    async def _ler(self, colecao: str, tenant_id: str, id: str, session=None) -> dict[str, Any] | None:
        if not tenant_id:
            raise ValueError("Leitura sem tenant_id.")
        return _de_mongo(await self._db[colecao].find_one({"_id": id, "tenant_id": tenant_id}, session=session))

    async def obter(self, colecao: str, tenant_id: str, id: str) -> dict[str, Any] | None:
        return await self._ler(colecao, tenant_id, id)

    def _montar(self, consulta: Consulta) -> tuple[dict[str, Any], list[tuple[str, int]]]:
        validar_consulta(consulta)
        filtro: dict[str, Any] = {"tenant_id": consulta.tenant_id}
        condicoes: list[dict[str, Any]] = []
        for f in consulta.filtros:
            condicoes.append({f.campo: {_OPS[f.op]: list(f.valor) if f.op == "in" else f.valor}})
        # O Firestore deixa de fora quem não tem o campo da ordenação; aqui também, para o contrato ser o mesmo.
        for campo, _ in consulta.ordem:
            condicoes.append({campo: {"$exists": True}})
        if condicoes:
            filtro["$and"] = condicoes
        ultima = consulta.ordem[-1][1] if consulta.ordem else "asc"
        ordem = [(c, ASCENDING if s == "asc" else DESCENDING) for c, s in consulta.ordem]
        ordem.append(("_id", ASCENDING if ultima == "asc" else DESCENDING))
        return filtro, ordem

    async def consultar(self, consulta: Consulta) -> list[dict[str, Any]]:
        filtro, ordem = self._montar(consulta)
        cursor = self._db[consulta.colecao].find(filtro, sort=ordem, limit=consulta.limite or 0)
        return [_de_mongo(d) async for d in cursor]

    async def explicar(self, consulta: Consulta) -> dict[str, Any]:
        """Plano da MESMA consulta que `consultar` faria. Só para testes (conferir que os índices servem)."""
        filtro, ordem = self._montar(consulta)
        return await self._db[consulta.colecao].find(filtro, sort=ordem, limit=consulta.limite or 0).explain()

    async def _criar(self, colecao: str, doc: dict[str, Any], session=None) -> None:
        try:
            await self._db[colecao].insert_one(_para_mongo(doc), session=session)
        except DuplicateKeyError as e:
            raise ConflitoDeId(f"{colecao}/{doc['id']} já existe.") from e

    async def _salvar(self, colecao: str, doc: dict[str, Any], session=None) -> None:
        d = _para_mongo(doc)
        try:
            # o filtro por tenant faz o upsert colidir (em vez de sobrescrever) se o ID for de outro cliente
            await self._db[colecao].replace_one({"_id": d["_id"], "tenant_id": d["tenant_id"]}, d, upsert=True,
                                                session=session)
        except DuplicateKeyError as e:
            raise ConflitoDeId(f"{colecao}/{doc['id']} pertence a outro tenant.") from e

    async def _atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any], session=None) -> None:
        r = await self._db[colecao].update_one({"_id": id, "tenant_id": tenant_id}, {"$set": campos}, session=session)
        if r.matched_count == 0:
            raise DocumentoAusente(f"{colecao}/{id} não existe.")

    async def criar(self, colecao: str, doc: dict[str, Any]) -> None:
        await self._criar(colecao, validar_doc(doc))

    async def salvar(self, colecao: str, doc: dict[str, Any]) -> None:
        await self._salvar(colecao, validar_doc(doc))

    async def atualizar(self, colecao: str, tenant_id: str, id: str, campos: dict[str, Any]) -> None:
        await self._atualizar(colecao, tenant_id, id, validar_campos(campos))

    async def em_transacao(self, funcao) -> Any:
        async with self._cliente.start_session() as sessao:
            async def corpo(s):
                # Cada tentativa começa do zero: `with_transaction` repete o corpo inteiro em erro transitório
                # (conflito de escrita), que é o mesmo modelo do Firestore.
                tx = TransacaoComBuffer(lambda c, t, i: self._ler(c, t, i, session=s))
                resultado = await funcao(tx)
                for op, colecao, dados, tenant in tx.escritas:
                    match op:
                        case "criar":
                            await self._criar(colecao, dados, session=s)
                        case "salvar":
                            await self._salvar(colecao, dados, session=s)
                        case "atualizar":
                            await self._atualizar(colecao, tenant, dados["id"], dados["campos"], session=s)
                return resultado

            return await sessao.with_transaction(
                corpo, read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))
