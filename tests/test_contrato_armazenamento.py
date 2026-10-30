"""Testes de contrato da porta `canteiro_dominio.armazenamento`.

O MESMO arquivo roda contra todos os adaptadores (memória e Mongo hoje; Firestore depois): migrar de banco é fazer o
adaptador novo passar aqui. Só usa a porta: nada de pymongo neste arquivo.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from canteiro_api.armazenamento import DocumentoAusente, EscritaAntesDaLeitura
from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import TipoMovimento
from canteiro_dominio.tipos import novo_id


def doc(tenant, **campos):
    return {"id": novo_id(), "tenant_id": tenant, **campos}


async def semear(arm, tenant, n=5):
    base = datetime(2026, 10, 1, tzinfo=UTC)
    docs = [doc(tenant, nome=f"item {i}", n=i, grupo="par" if i % 2 == 0 else "impar",
                quando=base + timedelta(days=i)) for i in range(n)]
    for d in docs:
        await arm.criar("coisas", d)
    return docs


# --- obter / criar / salvar / atualizar ---

async def test_criar_e_obter_devolve_o_mesmo_documento(arm, tenant):
    d = doc(tenant, nome="bloco", qtd=2_760_000, quando=datetime(2026, 10, 30, 12, 0, 0, 123456, tzinfo=UTC),
            tipo=TipoMovimento.ENTRADA, sub={"a": [1, 2]})
    await arm.criar("coisas", d)
    lido = await arm.obter("coisas", tenant, d["id"])
    # data com precisão de milissegundo e enum como texto: o denominador comum dos bancos
    assert lido == {**d, "quando": datetime(2026, 10, 30, 12, 0, 0, 123000, tzinfo=UTC), "tipo": "entrada"}
    assert lido["quando"].tzinfo is not None


async def test_obter_inexistente_devolve_none(arm, tenant):
    assert await arm.obter("coisas", tenant, novo_id()) is None


async def test_criar_com_id_existente_da_conflito(arm, tenant):
    d = doc(tenant, v=1)
    await arm.criar("coisas", d)
    with pytest.raises(ConflitoDeId):
        await arm.criar("coisas", {**d, "v": 2})
    assert (await arm.obter("coisas", tenant, d["id"]))["v"] == 1


async def test_idempotencia_pelo_id_derivado(arm, tenant):
    # o mesmo evento repetido (mesmo ID) não duplica
    id_ = f"{tenant}-evento-42"
    await arm.criar("eventos", {"id": id_, "tenant_id": tenant, "v": 1})
    with pytest.raises(ConflitoDeId):
        await arm.criar("eventos", {"id": id_, "tenant_id": tenant, "v": 1})
    assert len(await arm.consultar(Consulta("eventos", tenant))) == 1


async def test_salvar_cria_ou_substitui(arm, tenant):
    d = doc(tenant, a=1, b=2)
    await arm.salvar("coisas", d)
    await arm.salvar("coisas", {"id": d["id"], "tenant_id": tenant, "a": 9})
    assert await arm.obter("coisas", tenant, d["id"]) == {"id": d["id"], "tenant_id": tenant, "a": 9}


async def test_atualizar_muda_so_os_campos(arm, tenant):
    d = doc(tenant, a=1, b=2, sub={"x": 1, "y": 2})
    await arm.criar("coisas", d)
    await arm.atualizar("coisas", tenant, d["id"], {"b": 3, "sub.x": 5})
    assert await arm.obter("coisas", tenant, d["id"]) == {**d, "b": 3, "sub": {"x": 5, "y": 2}}


async def test_atualizar_inexistente_falha(arm, tenant):
    with pytest.raises(DocumentoAusente):
        await arm.atualizar("coisas", tenant, novo_id(), {"a": 1})


@pytest.mark.parametrize("campos", [{"id": "x"}, {"tenant_id": "outro"}, {}])
async def test_atualizar_recusa_campos_de_identidade(arm, tenant, campos):
    d = doc(tenant)
    await arm.criar("coisas", d)
    with pytest.raises(ValueError):
        await arm.atualizar("coisas", tenant, d["id"], campos)


@pytest.mark.parametrize("ruim", [{"tenant_id": "t"}, {"id": "x"}, {"id": "a/b", "tenant_id": "t"},
                                  {"id": "x", "tenant_id": "t", "_id": "y"},
                                  {"id": "x", "tenant_id": "t", "quando": datetime(2026, 1, 1)},
                                  {"id": "x", "tenant_id": "t", "v": {1, 2}}])
async def test_criar_recusa_documento_nao_portavel(arm, ruim):
    with pytest.raises((ValueError, TypeError)):
        await arm.criar("coisas", ruim)


# --- isolamento por tenant ---

async def test_tenant_nao_enxerga_o_outro(arm, tenant):
    outro = tenant + "-b"
    d = doc(tenant, nome="meu")
    await arm.criar("coisas", d)
    await arm.criar("coisas", doc(outro, nome="dele"))
    assert await arm.obter("coisas", outro, d["id"]) is None
    assert [x["nome"] for x in await arm.consultar(Consulta("coisas", tenant))] == ["meu"]
    with pytest.raises(DocumentoAusente):
        await arm.atualizar("coisas", outro, d["id"], {"nome": "invadido"})
    with pytest.raises(ConflitoDeId):
        await arm.salvar("coisas", {"id": d["id"], "tenant_id": outro, "nome": "invadido"})
    assert (await arm.obter("coisas", tenant, d["id"]))["nome"] == "meu"


async def test_leitura_sem_tenant_e_recusada(arm):
    with pytest.raises(ValueError):
        await arm.obter("coisas", "", novo_id())
    with pytest.raises(ValueError):
        await arm.consultar(Consulta("coisas", ""))


# --- consultas: cada operador permitido, ordenação e limite ---

async def test_consulta_igualdade(arm, tenant):
    await semear(arm, tenant)
    r = await arm.consultar(Consulta("coisas", tenant, [Filtro("grupo", "==", "par")], [("n", "asc")]))
    assert [x["n"] for x in r] == [0, 2, 4]


async def test_consulta_in(arm, tenant):
    await semear(arm, tenant)
    r = await arm.consultar(Consulta("coisas", tenant, [Filtro("n", "in", [1, 3, 99])], [("n", "asc")]))
    assert [x["n"] for x in r] == [1, 3]


@pytest.mark.parametrize(("op", "valor", "esperado"), [
    ("<", 2, [0, 1]), ("<=", 2, [0, 1, 2]), (">", 2, [3, 4]), (">=", 2, [2, 3, 4])])
async def test_consulta_desigualdade(arm, tenant, op, valor, esperado):
    await semear(arm, tenant)
    r = await arm.consultar(Consulta("coisas", tenant, [Filtro("n", op, valor)], [("n", "asc")]))
    assert [x["n"] for x in r] == esperado


async def test_desigualdade_em_datas_e_intervalo_no_mesmo_campo(arm, tenant):
    docs = await semear(arm, tenant)
    r = await arm.consultar(Consulta("coisas", tenant, [
        Filtro("quando", ">=", docs[1]["quando"]), Filtro("quando", "<", docs[4]["quando"])], [("quando", "desc")]))
    assert [x["n"] for x in r] == [3, 2, 1]


async def test_igualdade_com_desigualdade_e_ordem_e_limite(arm, tenant):
    await semear(arm, tenant, n=8)
    r = await arm.consultar(Consulta("coisas", tenant, [Filtro("grupo", "==", "impar"), Filtro("n", ">", 1)],
                                     [("n", "desc")], limite=2))
    assert [x["n"] for x in r] == [7, 5]


async def test_ordem_desc_e_limite(arm, tenant):
    await semear(arm, tenant)
    r = await arm.consultar(Consulta("coisas", tenant, ordem=[("quando", "desc")], limite=3))
    assert [x["n"] for x in r] == [4, 3, 2]


async def test_ordem_composta_com_desempate_estavel(arm, tenant):
    await semear(arm, tenant, n=6)
    r = await arm.consultar(Consulta("coisas", tenant, ordem=[("grupo", "asc"), ("n", "desc")]))
    assert [x["n"] for x in r] == [5, 3, 1, 4, 2, 0]
    # sem ordem explícita, o resultado ainda é determinístico (pelo ID)
    a = await arm.consultar(Consulta("coisas", tenant))
    b = await arm.consultar(Consulta("coisas", tenant))
    assert [x["id"] for x in a] == [x["id"] for x in b] == sorted(x["id"] for x in a)


async def test_ordenacao_deixa_de_fora_quem_nao_tem_o_campo(arm, tenant):
    await arm.criar("coisas", doc(tenant, n=1))
    await arm.criar("coisas", doc(tenant))
    assert len(await arm.consultar(Consulta("coisas", tenant, ordem=[("n", "asc")]))) == 1


@pytest.mark.parametrize("consulta", [
    Consulta("coisas", "t", [Filtro("a", "<", 1), Filtro("b", ">", 1)]),            # desigualdade em 2 campos
    Consulta("coisas", "t", [Filtro("a", "in", list(range(31)))]),                   # in > 30
    Consulta("coisas", "t", [Filtro("a", "<", 1)], [("b", "asc")]),                  # ordem fora do campo
    Consulta("coisas", "t", limite=0),
])
async def test_consulta_nao_portavel_e_recusada(arm, consulta):
    with pytest.raises(ValueError):
        await arm.consultar(consulta)


# --- transações ---

async def test_transacao_le_e_escreve_atomicamente(arm, tenant):
    conta = doc(tenant, saldo=100)
    await arm.criar("contas", conta)

    async def transferir(tx):
        c = await tx.obter("contas", tenant, conta["id"])
        tx.atualizar("contas", tenant, conta["id"], {"saldo": c["saldo"] - 30})
        tx.criar("lancamentos", doc(tenant, valor=-30))
        return c["saldo"] - 30

    assert await arm.em_transacao(transferir) == 70
    assert (await arm.obter("contas", tenant, conta["id"]))["saldo"] == 70
    assert len(await arm.consultar(Consulta("lancamentos", tenant))) == 1


async def test_transacao_desfaz_tudo_em_erro(arm, tenant):
    conta = doc(tenant, saldo=100)
    await arm.criar("contas", conta)

    class Falhou(Exception):
        pass

    async def quebra(tx):
        await tx.obter("contas", tenant, conta["id"])
        tx.atualizar("contas", tenant, conta["id"], {"saldo": 0})
        tx.criar("lancamentos", doc(tenant, valor=-100))
        raise Falhou

    with pytest.raises(Falhou):
        await arm.em_transacao(quebra)
    assert (await arm.obter("contas", tenant, conta["id"]))["saldo"] == 100
    assert await arm.consultar(Consulta("lancamentos", tenant)) == []


async def test_transacao_com_conflito_de_id_nao_grava_nada(arm, tenant):
    existente = doc(tenant)
    await arm.criar("coisas", existente)

    async def duplica(tx):
        tx.criar("lancamentos", doc(tenant, valor=1))
        tx.criar("coisas", existente)

    with pytest.raises(ConflitoDeId):
        await arm.em_transacao(duplica)
    assert await arm.consultar(Consulta("lancamentos", tenant)) == []


async def test_transacao_exige_ler_antes_de_escrever(arm, tenant):
    async def errada(tx):
        tx.criar("coisas", doc(tenant))
        await tx.obter("coisas", tenant, novo_id())

    with pytest.raises(EscritaAntesDaLeitura):
        await arm.em_transacao(errada)
    assert await arm.consultar(Consulta("coisas", tenant)) == []


async def test_transacao_limita_escritas(arm, tenant):
    async def demais(tx):
        for _ in range(501):
            tx.criar("coisas", doc(tenant))

    with pytest.raises(ValueError):
        await arm.em_transacao(demais)
    assert await arm.consultar(Consulta("coisas", tenant, limite=1)) == []


async def test_transacao_respeita_tenant_na_leitura(arm, tenant):
    d = doc(tenant)
    await arm.criar("coisas", d)

    async def espia(tx):
        return await tx.obter("coisas", tenant + "-b", d["id"])

    assert await arm.em_transacao(espia) is None


async def test_transacoes_concorrentes_nao_perdem_atualizacao(arm, tenant):
    # o contador de pedidos depende disto: duas transações lendo e incrementando o mesmo documento
    contador = {"id": f"{tenant}-contador", "tenant_id": tenant, "n": 0}
    await arm.criar("contadores", contador)

    async def incrementa(tx):
        c = await tx.obter("contadores", tenant, contador["id"])
        await asyncio.sleep(0)          # dá chance à outra transação de intercalar
        tx.atualizar("contadores", tenant, contador["id"], {"n": c["n"] + 1})

    await asyncio.gather(*(arm.em_transacao(incrementa) for _ in range(10)))
    assert (await arm.obter("contadores", tenant, contador["id"]))["n"] == 10
