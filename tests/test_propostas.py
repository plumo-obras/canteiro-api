"""Propostas: só a aprovação executa, o registro guarda o Desfazer e a mesma proposta não nasce duas vezes."""
import asyncio

from conftest import nova_vila
from test_cronograma import PLANO, _cron, _obra

OUTRO_PLANO = {**PLANO, "inicio": "2026-02"}


def proposta(obra: str, dados: dict | None = None, **kw) -> dict:
    return {"obra_id": obra, "tipo": "cronograma", "titulo": "Cronograma da obra", "fonte": "agente",
            "justificativa": "A obra começa em janeiro.", "dados": dados or PLANO, **kw}


async def _pendentes(vila, obra):
    return (await vila.http.get(f"/propostas?status=pendente&obra_id={obra}", headers=vila.h)).json()


async def test_proposta_so_guarda_nao_executa(vila):
    obra = await _obra(vila, com_cronograma=False)
    r = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))
    assert r.status_code == 201, r.text
    p = r.json()
    assert (p["status"], p["sugerido_por"], p["justificativa"], p["efeito"]) == (
        "pendente", "agente", "A obra começa em janeiro.", "cronograma")
    assert (await _cron(vila, obra))["meses"] == 0
    assert [x["id"] for x in await _pendentes(vila, obra)] == [p["id"]]


async def test_viewer_pode_propor_mas_nao_aprovar(vila):
    obra = await _obra(vila, com_cronograma=False)
    h, _ = await vila.usuario("viewer")
    p = (await vila.http.post("/propostas", headers=h, json=proposta(obra))).json()
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=h)).status_code == 403
    assert (await vila.http.post(f"/propostas/{p['id']}/recusar", headers=h, json={"motivo": "x"})).status_code == 403


async def test_dados_validados_pelo_tipo(vila):
    obra = await _obra(vila, com_cronograma=False)
    casos = [{"inicio": "2026-01"}, {**PLANO, "extra": 1}, {**PLANO, "meses": 0}]
    for dados in casos:
        r = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra, dados))
        assert r.status_code == 422, (dados, r.text)


async def test_aprovar_executa_e_registra_com_desfazer(vila):
    obra = await _obra(vila, com_cronograma=False)
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)
    assert r.status_code == 200, r.text
    prop, reg = r.json()["proposta"], r.json()["registro"]
    assert prop["status"] == "aprovada" and prop["decidido_por"] == vila.sessao["usuario"]["id"]
    assert prop["decidido_por_nome"] == reg["quem"] == reg["quem_nome"] == "Admin da Vila Nova"
    assert prop["registro_id"] == reg["id"] and p["registro_id"] is None
    assert (reg["proposta_id"], reg["origem"]) == (p["id"], "agente")
    assert (await _cron(vila, obra))["meses"] == 3
    # aprovar de novo não executa de novo
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 409
    # desfazer volta o cronograma e marca a proposta
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 200
    assert (await _cron(vila, obra))["meses"] == 0
    desfeitas = (await vila.http.get("/propostas?status=desfeita", headers=vila.h)).json()
    assert [x["id"] for x in desfeitas] == [p["id"]]
    assert desfeitas[0]["registro_id"] == reg["id"]          # continua apontando para a execução desfeita


async def test_proposta_de_cronograma_nao_se_ajusta(vila):
    obra = await _obra(vila, com_cronograma=False)
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    r = await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h, json={"ajustes": {"meses": 4}})
    assert r.status_code == 422 and "aprove ou recuse" in r.json()["detail"]
    assert [x["id"] for x in await _pendentes(vila, obra)] == [p["id"]]


async def test_recusar_exige_motivo(vila):
    obra = await _obra(vila, com_cronograma=False)
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    assert (await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={})).status_code == 422
    r = await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={"motivo": "começa em março"})
    assert (r.json()["status"], r.json()["motivo_recusa"]) == ("recusada", "começa em março")
    assert r.json()["decidido_por_nome"] == "Admin da Vila Nova"
    assert (await vila.http.post(f"/propostas/{p['id']}/aprovar", headers=vila.h)).status_code == 409


async def test_proposta_de_outro_tenant_e_invisivel(vila, http, arm, config):
    obra = await _obra(vila, com_cronograma=False)
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    intruso = await nova_vila(http, arm, config)
    assert (await http.post(f"/propostas/{p['id']}/aprovar", headers=intruso.h)).status_code == 404
    assert (await http.get("/propostas", headers=intruso.h)).json() == []
    assert (await http.post("/propostas", headers=intruso.h, json=proposta(obra))).status_code == 404
    reg = (await vila.http.get("/registro", headers=vila.h)).json()
    assert (await http.post(f"/registro/{reg[0]['id']}/desfazer", headers=intruso.h)).status_code == 404


async def test_registro_sem_desfazer(vila):
    obra = await _obra(vila, com_cronograma=False)
    p = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    await vila.http.post(f"/propostas/{p['id']}/recusar", headers=vila.h, json={"motivo": "não"})
    reg = (await vila.http.get("/registro", headers=vila.h)).json()[0]
    assert (await vila.http.post(f"/registro/{reg['id']}/desfazer", headers=vila.h)).status_code == 422


# --- sem proposta duplicada ---

async def test_mesma_chave_devolve_a_mesma_proposta(vila):
    """O agente manda (sessão, tool_use) como chave: a repetição da mesma chamada de ferramenta não duplica."""
    obra = await _obra(vila, com_cronograma=False)
    corpo = proposta(obra, chave_idempotencia="sessao-1:toolu_1")
    a = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    b = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    assert (a.status_code, b.status_code) == (201, 200)
    assert a.json()["id"] == b.json()["id"] and (a.json()["ja_existia"], b.json()["ja_existia"]) == (False, True)
    assert [x["id"] for x in await _pendentes(vila, obra)] == [a.json()["id"]]
    # a mesma chave com outro conteúdo é erro de quem chama, não uma proposta nova
    outra = proposta(obra, OUTRO_PLANO, chave_idempotencia="sessao-1:toolu_1")
    assert (await vila.http.post("/propostas", headers=vila.h, json=outra)).status_code == 409
    # decidida, a mesma chave continua devolvendo ela (com o status de agora), sem criar outra
    assert (await vila.http.post(f"/propostas/{a.json()['id']}/aprovar", headers=vila.h)).status_code == 200
    de_novo = await vila.http.post("/propostas", headers=vila.h, json=corpo)
    assert (de_novo.status_code, de_novo.json()["status"], de_novo.json()["ja_existia"]) == (200, "aprovada", True)


async def test_proposta_igual_pendente_volta_a_existente(vila):
    """Sem chave (outra volta da conversa, outra aba): a mesma proposta pendente na mesma obra não nasce de novo."""
    obra = await _obra(vila, com_cronograma=False)
    a = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))
    # título e justificativa são texto do agente; o que decide é tipo + dados
    b = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra, titulo="Outro título",
                                                                         justificativa="de novo"))
    assert (a.status_code, b.status_code, b.json()["ja_existia"]) == (201, 200, True)
    assert b.json()["id"] == a.json()["id"] and b.json()["titulo"] == "Cronograma da obra"
    c = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra, OUTRO_PLANO))
    assert (c.status_code, c.json()["ja_existia"]) == (201, False)
    assert len(await _pendentes(vila, obra)) == 2


async def test_decidir_solta_a_trava_e_a_mesma_proposta_pode_voltar(vila):
    obra = await _obra(vila, com_cronograma=False)
    a = (await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))).json()
    await vila.http.post(f"/propostas/{a['id']}/recusar", headers=vila.h, json={"motivo": "agora não"})
    b = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))
    assert (b.status_code, b.json()["ja_existia"]) == (201, False) and b.json()["id"] != a["id"]
    assert (await vila.http.post(f"/propostas/{b.json()['id']}/aprovar", headers=vila.h)).status_code == 200
    c = await vila.http.post("/propostas", headers=vila.h, json=proposta(obra))
    assert (c.status_code, c.json()["ja_existia"]) == (201, False)
    assert [x["id"] for x in await _pendentes(vila, obra)] == [c.json()["id"]]


async def test_propostas_iguais_ao_mesmo_tempo_viram_uma(vila):
    obra = await _obra(vila, com_cronograma=False)
    rs = await asyncio.gather(*[vila.http.post("/propostas", headers=vila.h, json=proposta(obra)) for _ in range(4)])
    assert sorted(r.status_code for r in rs) == [200, 200, 200, 201], [r.text for r in rs]
    assert len({r.json()["id"] for r in rs}) == 1
    assert len(await _pendentes(vila, obra)) == 1
