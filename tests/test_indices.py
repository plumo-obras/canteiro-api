"""Índices e TTL do Mongo, conferidos com explain() nas consultas que as rotas fazem de verdade.

Só roda com o Mongo do compose (sem ele, pula como os outros). Em vez de repetir aqui o formato de cada consulta, um
espião grava toda `Consulta` que a API manda para a porta enquanto as rotas são chamadas; depois cada uma passa pelo
explain() do adaptador, que monta exatamente o mesmo filtro e a mesma ordenação de `consultar`.
"""
from datetime import date, timedelta

import httpx
import pytest
from conftest import nova_vila

from canteiro_api.armazenamento.mongo import TTL
from canteiro_api.eventos import PublicadorEmMemoria, publicar_pendentes
from canteiro_api.main import create_app


class Espiao:
    def __init__(self, arm):
        self._arm = arm
        self.consultas = []

    async def consultar(self, consulta):
        self.consultas.append(consulta)
        return await self._arm.consultar(consulta)

    def __getattr__(self, nome):
        return getattr(self._arm, nome)


@pytest.fixture
async def espiao(_mongo_sessao):
    return Espiao(_mongo_sessao)


@pytest.fixture
async def http_espiao(espiao, config):
    app = create_app(config, espiao)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.9.9.9", 5555)),
                                 base_url="http://teste") as c:
        yield c


def _estagios(plano) -> list[dict]:
    """Todos os estágios do plano vencedor (o formato do explain muda entre versões; anda em tudo)."""
    achados = []
    if isinstance(plano, dict):
        if "stage" in plano:
            achados.append(plano)
        for v in plano.values():
            achados += _estagios(v)
    elif isinstance(plano, list):
        for v in plano:
            achados += _estagios(v)
    return achados


# Listagens paginadas (as que crescem com o uso): o plano VENCEDOR tem de vir pronto do índice. Nas outras, o
# planejador do Mongo pode preferir ordenar meia dúzia de documentos em memória a fazer um SORT_MERGE por seis faixas
# de `in` (com pouco dado o teste não o desempata); ali basta existir um plano candidato sem SORT, ou seja, o índice
# certo existir. COLLSCAN e índice que não começa por tenant_id não passam em consulta nenhuma.
QUENTES = {"mensagens", "registro", "sessoes", "movimentos", "propostas", "outbox"}


async def _conferir(arm, consultas) -> None:
    vistos = set()
    problemas = []
    for c in consultas:
        assinatura = (c.colecao, tuple((f.campo, f.op) for f in c.filtros), tuple(c.ordem))
        if assinatura in vistos:
            continue
        vistos.add(assinatura)
        planejador = (await arm.explicar(c))["queryPlanner"]
        vencedor = _estagios(planejador["winningPlan"])
        nomes = {e["stage"] for e in vencedor}
        if "COLLSCAN" in nomes:
            problemas.append((assinatura, "COLLSCAN"))
        if "SORT" in nomes:
            candidatos = [planejador["winningPlan"], *planejador.get("rejectedPlans", [])]
            sem_sort = [p for p in candidatos if not {"SORT", "COLLSCAN"} & {e["stage"] for e in _estagios(p)}]
            if c.colecao in QUENTES or not sem_sort:
                problemas.append((assinatura, "SORT em memória"))
        # índice que não começa pelo tenant percorre a coleção de todos os clientes
        for e in vencedor:
            if e["stage"] == "IXSCAN" and next(iter(e["keyPattern"])) != "tenant_id":
                problemas.append((assinatura, e["indexName"]))
    assert not problemas, problemas


async def test_consultas_das_rotas_usam_indice_sem_ordenar_em_memoria(espiao, http_espiao, config):
    # dois clientes: o índice tem de separar um do outro, não só achar o certo
    await nova_vila(http_espiao, espiao, config)
    vila = await nova_vila(http_espiao, espiao, config)
    h, obra = vila.h, vila.obra
    http = http_espiao

    s = (await http.post("/sessoes", json={"titulo": "conversa", "obra_id": obra}, headers=h)).json()
    await http.post(f"/sessoes/{s['id']}/mensagens", headers=h,
                    json={"seq_inicial": 0, "mensagens": [{"papel": "user", "conteudo": "oi"},
                                                          {"papel": "assistant", "conteudo": "olá"}]})
    espiao.consultas.clear()      # o que importa são as leituras, não o seed

    insumo = next(iter(vila.insumo.values()))
    pedido = next(iter(vila.pedido.values()))
    hoje = date.today()
    for url in [f"/sessoes/{s['id']}/mensagens", f"/sessoes/{s['id']}/mensagens?depois_de=0",
                "/registro", f"/registro?obra_id={obra}", "/registro?antes_de=2099-01-01T00:00:00Z",
                f"/registro?obra_id={obra}&antes_de=2099-01-01T00:00:00Z&limite=1",
                "/sessoes", f"/sessoes?obra_id={obra}", "/sessoes?antes_de=2099-01-01T00:00:00Z",
                "/obras", "/obras/resumo", f"/obras/{obra}/insumos", f"/obras/{obra}/insumos/{insumo}/movimentos",
                f"/obras/{obra}/pedidos", f"/obras/{obra}/pedidos?status=em_cotacao",
                f"/obras/{obra}/pedidos?status=em_cotacao&status=enviado&antes_de=2099-01-01T00:00:00Z",
                f"/pedidos/{pedido}/recebimentos", "/propostas", "/propostas?status=pendente",
                f"/propostas?obra_id={obra}", f"/propostas?status=pendente&obra_id={obra}", "/fornecedores",
                f"/obras/{obra}/orcamento", f"/obras/{obra}/etapas", f"/obras/{obra}/cronograma",
                f"/uso?de={hoje - timedelta(days=30)}&ate={hoje}"]:
        r = await http.get(url, headers=h)
        assert r.status_code < 500, (url, r.text)
    await http.post("/auth/sair", json={"renovacao": vila.sessao["renovacao"]})
    await publicar_pendentes(espiao, PublicadorEmMemoria(), vila.tenant_id)

    colecoes = {c.colecao for c in espiao.consultas}
    assert {"mensagens", "registro", "sessoes", "outbox", "renovacoes"} <= colecoes
    await _conferir(espiao, espiao.consultas)


async def test_colecoes_com_ttl_no_campo_de_vencimento(_mongo_sessao):
    for colecao, campo in TTL.items():
        indices = await _mongo_sessao._db[colecao].index_information()
        ttl = [i for i in indices.values() if i["key"] == [(campo, 1)] and "expireAfterSeconds" in i]
        assert ttl and ttl[0]["expireAfterSeconds"] == 0, (colecao, indices)
