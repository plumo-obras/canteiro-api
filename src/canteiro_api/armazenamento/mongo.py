"""Adaptador MongoDB com a API assíncrona nativa do PyMongo (o Motor foi descontinuado).

Este é o ÚNICO módulo que importa `pymongo` (e o `gridfs` que vem com ele, para os arquivos). Ele mapeia `id` ↔ `_id`,
exige tenant em toda leitura e usa transações reais (replica set), repetidas pelo driver em conflito transitório.
"""
import logging
from typing import Any

from gridfs import AsyncGridFSBucket
from gridfs.errors import NoFile
from pymongo import ASCENDING, DESCENDING, AsyncMongoClient
from pymongo.errors import DuplicateKeyError
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta

from ..arquivos import conferir_caminho, do_tenant
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
    "versoes_orcamento": [["tenant_id", "obra_id", "_id"]],
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
    "renovacoes": [["tenant_id", "familia", "_id"]],
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

    def arquivos(self) -> ArquivosMongo:
        return ArquivosMongo(self._db)

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


class ArquivosMongo:
    """Arquivos no GridFS do mesmo banco (bucket `arquivos`): persiste entre deploys e quando o Render dorme, sem
    serviço novo nem chave nova. Custo: ocupa o mesmo espaço do Atlas M0 (512 MB) que os dados — veja docs/deploy.md.

    Cada envio é um arquivo GridFS com `_id` próprio, `filename` = caminho (`tenant/obra/nome`) e `metadata` com
    tenant e obra. O documento em `arquivos.files` só é gravado depois de todos os pedaços: se ele existe, o arquivo
    está inteiro. Envio interrompido deixa só pedaços órfãos (nunca lidos). Leitura sempre filtra pelo tenant.
    """

    def __init__(self, db):
        self._bucket = AsyncGridFSBucket(db, bucket_name="arquivos")
        self._files = db["arquivos.files"]

    async def _achar(self, tenant_id: str, caminho: str) -> dict | None:
        return await self._files.find_one({"filename": caminho, "metadata.tenant_id": tenant_id},
                                          projection={"_id": 1})

    async def salvar(self, tenant_id: str, obra_id: str, caminho: str, conteudo: bytes, tipo: str) -> None:
        conferir_caminho(caminho, tenant_id, obra_id)
        # o nome sai do sha256 do conteúdo: já existe = mesmo arquivo, não regrava. Dois envios simultâneos do mesmo
        # arquivo podem gravar duas cópias iguais; a leitura pega qualquer uma (e o espaço extra é raro e pequeno).
        if await self._achar(tenant_id, caminho):
            return
        await self._bucket.upload_from_stream(caminho, conteudo, metadata={
            "tenant_id": tenant_id, "obra_id": obra_id, "tipo_conteudo": tipo, "tamanho": len(conteudo)})

    async def ler(self, tenant_id: str, caminho: str) -> bytes | None:
        if not do_tenant(caminho, tenant_id):
            return None
        conferir_caminho(caminho, tenant_id)
        achado = await self._achar(tenant_id, caminho)
        if not achado:
            return None
        try:
            fluxo = await self._bucket.open_download_stream(achado["_id"])
        except NoFile:
            return None
        return await fluxo.read()
