"""Tabela VAU do SERO: o texto da tela vira linhas, a importação é idempotente e a obra sem VAU à mão usa o vigente
da tabela para a UF e a destinação dela."""
import pytest
from test_obras_orcamento import nova_obra

from canteiro_api import vau
from canteiro_api.modelos import TENANT_SISTEMA

# como a tela "Consultar Tabela VAU" do SERO aparece copiada (innerText da tabela)
TEXTO_SERO = (
    "Mês/Ano\tUF\tCasa popular\tComercial salas e lojas\tConjunto habitacional popular\tEdifício de Garagens\t"
    "Galpão industrial\tResidencial multifamiliar\tResidencial unifamiliar\n"
    "10/2026\tSP\t1.523,96\t2.698,05\t1.523,96\t2.698,05\t1.271,10\t2.370,10\t2.717,44\n"
    "11/2026\tSP\t1.528,38\t2.705,87\t1.528,38\t2.705,87\t1.274,79\t2.376,97\t2.725,32\n"
)


def test_le_o_texto_da_tela_do_sero():
    linhas = vau.ler_tabela_sero(TEXTO_SERO)
    assert len(linhas) == 14
    assert {"uf": "SP", "mes": "2026-11", "destinacao": "residencial_unifamiliar", "vau": 272532} in linhas
    assert {"uf": "SP", "mes": "2026-10", "destinacao": "galpao", "vau": 127110} in linhas


@pytest.mark.parametrize("texto,erro", [
    ("", "Nenhuma linha"),
    ("11/2026\tSP\t1,00", "antes do cabeçalho"),
    (TEXTO_SERO.replace("Galpão industrial", "Hangar"), "coluna"),
    (TEXTO_SERO.replace("\tSP\t1.528,38", "\tXX\t1.528,38"), "UF"),
    (TEXTO_SERO.replace("2.725,32", "abc"), "não é um valor"),
])
def test_tabela_estranha_e_recusada_inteira(texto, erro):
    with pytest.raises(vau.TabelaInvalida, match=erro):
        vau.ler_tabela_sero(texto)


def test_os_dados_do_repositorio_cobrem_as_27_ufs():
    linhas, fonte = vau.carregar(vau.DADOS)
    assert "SERO" in fonte
    assert len({x["uf"] for x in linhas}) == 27
    assert {x["destinacao"] for x in linhas} == set(vau.COLUNAS_SERO.values())


async def test_importa_de_novo_sem_duplicar_e_conta_o_que_mudou(arm):
    linhas = vau.ler_tabela_sero(TEXTO_SERO)
    assert await vau.importar(arm, linhas, "teste") == {"linhas": 14, "novas": 14, "mudaram": 0}
    assert await vau.importar(arm, linhas, "teste") == {"linhas": 14, "novas": 0, "mudaram": 0}
    novo = [{**x, "vau": x["vau"] + 1} if x["mes"] == "2026-11" else x for x in linhas]
    assert await vau.importar(arm, novo, "teste") == {"linhas": 14, "novas": 0, "mudaram": 7}
    assert (await vau.vigente(arm, "SP", "residencial_unifamiliar", "2026-11"))["vau"] == 272533
    assert (await vau.vigente(arm, "SP", "residencial_unifamiliar", "2026-10"))["vau"] == 271744
    assert await vau.vigente(arm, "SP", "residencial_unifamiliar", "2026-09") is None
    assert await vau.vigente(arm, "XX", "residencial_unifamiliar", "2026-11") is None
    doc = await arm.obter("vau", TENANT_SISTEMA, "SP:2026-11:galpao")
    assert doc["fonte"] == "teste"


async def test_linha_fora_do_contrato_nao_entra(arm):
    with pytest.raises(vau.TabelaInvalida):
        await vau.importar(arm, [{"uf": "SP", "mes": "2026-11", "destinacao": "castelo", "vau": 1}], "teste")


async def test_obra_sem_vau_usa_a_tabela_e_o_vau_a_mao_vence(vila):
    await vau.importar(vila.arm, vau.ler_tabela_sero(TEXTO_SERO), "teste")
    obra = (await nova_obra(vila, uf="SP", tipologia="casa", area_construida=100_000, proprietario="pj"))["id"]
    t = (await vila.http.get(f"/obras/{obra}/tributos", headers=vila.h)).json()
    assert (t["vau_fonte"], t["destinacao"], t["inss"]["situacao"]) == ("tabela_sero", "residencial_unifamiliar",
                                                                         "calculado")
    assert t["vau_usado"] in (272532, 271744)              # o vigente do mês corrente (o teste roda em qualquer mês)
    assert t["vau_referencia"] in ("2026-11", "2026-10")
    # à mão: vence a tabela
    r = await vila.http.patch(f"/obras/{obra}", headers=vila.h, json={"vau": 300_000, "vau_referencia": "2026-11"})
    assert r.status_code == 200, r.text
    t = (await vila.http.get(f"/obras/{obra}/tributos", headers=vila.h)).json()
    assert (t["vau_fonte"], t["vau_usado"]) == ("informado", 300_000)
    # sem UF não há linha na tabela: a conta diz o que falta
    sem_uf = (await nova_obra(vila, tipologia="casa", area_construida=100_000))["id"]
    t = (await vila.http.get(f"/obras/{sem_uf}/tributos", headers=vila.h)).json()
    assert t["inss"]["situacao"] == "sem_dados" and t["vau_fonte"] is None
    assert any("UF" in m for m in t["inss"]["motivos"])


async def test_rota_da_tabela_vau(vila):
    await vau.importar(vila.arm, vau.ler_tabela_sero(TEXTO_SERO), "teste")
    r = await vila.http.get("/vau?uf=sp&destinacao=galpao", headers=vila.h)
    assert r.status_code == 200, r.text
    assert [(x["mes"], x["vau"]) for x in r.json()] == [("2026-11", 127479), ("2026-10", 127110)]
    assert (await vila.http.get("/vau?uf=SP&destinacao=galpao")).status_code == 401
