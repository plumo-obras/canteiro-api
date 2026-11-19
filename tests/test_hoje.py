"""Hoje na obra: cada ponto, a ordem, o limite, o resumo e a semana, sem chamar o modelo."""
import uuid

from conftest import nova_vila
from test_aprovar_orcamento import SEM_PRECO, _contrato
from test_cronograma import _medir
from test_gastos import _campo, _leitura
from test_gastos_orcamento import _gasto
from test_obras_orcamento import nova_obra
from test_orcamento_versoes import mapa
from test_propostas import OUTRO_PLANO, proposta

from canteiro_dominio.tipos import qtd

DIA = "2026-11-09"                    # segunda-feira
PLANO = {"inicio": "2026-11", "meses": 3, "etapas": [{"etapa": "1", "percentuais": [10000, 0, 0]},
                                                     {"etapa": "2", "percentuais": [0, 10000, 0]}]}


async def _hoje(vila, obra, dia=DIA, h=None):
    r = await vila.http.get(f"/obras/{obra}/hoje", headers=h or vila.h, params={"hoje": dia})
    assert r.status_code == 200, r.text
    return r.json()


def _ponto(h, tipo):
    return next((p for p in h["pontos"] if p["tipo"] == tipo), None)


async def _obra_aprovada(vila, plano=None):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    if plano:
        r = await vila.http.put(f"/obras/{obra}/cronograma", headers=vila.h, json=plano)
        assert r.status_code == 200, r.text
    a = await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert a.status_code == 200, a.text
    return obra


async def test_obra_sem_nada_aponta_para_o_projeto(vila):
    h = await _hoje(vila, (await nova_obra(vila))["id"])
    assert [p["tipo"] for p in h["pontos"]] == ["sem_orcamento"]
    p = h["pontos"][0]
    assert (p["prioridade"], p["acao"]["tela"]) == ("pede_voce", "projeto")
    assert h["resumo"] == {"situacao": "sem_medicao", "frase": "A obra ainda está em orçamento.", "esperando": 1,
                           "semana_atual": None, "semanas_total": None}
    assert (h["hoje"], h["mais"], h["semana"]) == (DIA, 0, [])


async def test_viewer_le_e_outro_tenant_e_404(vila, http, arm, config):
    obra = (await nova_obra(vila))["id"]
    viewer, _ = await vila.usuario("viewer")
    await _hoje(vila, obra, h=viewer)
    intruso = await nova_vila(http, arm, config)
    assert (await intruso.http.get(f"/obras/{obra}/hoje", headers=intruso.h)).status_code == 404


async def test_proposta_conta_em_esperando_sem_ocupar_vaga_de_ponto(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    antes = (await _hoje(vila, obra))["resumo"]["esperando"]
    # planos diferentes: duas propostas iguais seriam a mesma, travada
    for plano in (PLANO, OUTRO_PLANO):
        corpo = proposta(obra, plano, chave_idempotencia=uuid.uuid4().hex)
        r = await vila.http.post("/propostas", headers=vila.h, json=corpo)
        assert r.status_code == 201, r.text
    h = await _hoje(vila, obra)
    assert _ponto(h, "aprovacoes_pendentes") is None
    assert h["resumo"]["esperando"] - antes == 2


async def test_comprovantes_para_conferir_somam_o_que_foi_lido(vila, arm):
    obra = (await nova_obra(vila))["id"]
    ids = []
    for _ in range(2):
        c = await vila.http.post(f"/obras/{obra}/comprovantes", headers=vila.h,
                                 files={"arquivo": ("p.jpg", b"\xff\xd8\xff\xe0" + uuid.uuid4().bytes, "image/jpeg")})
        assert c.status_code == 201, c.text
        ids.append(c.json()["id"])
    p = _ponto(await _hoje(vila, obra), "gastos_a_conferir")
    assert p["titulo"] == "2 comprovantes para conferir." and "sem leitura" in p["detalhe"]
    await arm.atualizar("comprovantes", vila.tenant_id, ids[0], {"leitura": _leitura(valor=_campo("alta", "1.022,00"))})
    p = _ponto(await _hoje(vila, obra), "gastos_a_conferir")
    assert "R$ 1.022,00" in p["detalhe"] and "1 ainda sem leitura" in p["detalhe"]
    assert (p["acao"]["tela"], p["acao"]["filtro"]) == ("gastos", {"status": "a_revisar"})


async def test_itens_sem_preco(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    p = _ponto(await _hoje(vila, obra), "itens_sem_preco")
    assert p["titulo"] == "1 item do orçamento sem preço."
    assert (p["prioridade"], p["acao"]["tela"]) == ("atencao", "orcamento")
    assert p["detalhe"].startswith("Areia média.")


async def test_orcamento_so_das_pranchas_e_so_em_orcamento(vila):
    obra = (await nova_obra(vila))["id"]
    r = await vila.http.post(f"/obras/{obra}/orcamento/estrutura", headers=vila.h, json={
        "itens": [{"nome": "Aço", "unidade": "kg", "quantidade": qtd(100), "valor_unitario": 669}],
        "aplicar": True, "chave_idempotencia": uuid.uuid4().hex})
    assert r.status_code == 201, r.text
    assert _ponto(await _hoje(vila, obra), "orcamento_so_pranchas")["acao"]["tela"] == "orcamento"
    await vila.http.post(f"/obras/{obra}/orcamento/aprovar", headers=vila.h)
    assert _ponto(await _hoje(vila, obra), "orcamento_so_pranchas") is None


async def test_planilha_nao_e_so_pranchas(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    assert _ponto(await _hoje(vila, obra), "orcamento_so_pranchas") is None


async def test_gastos_sem_etapa(vila):
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra)
    await _gasto(vila, obra, valor=70000, etapa=None)
    p = _ponto(await _hoje(vila, obra), "gastos_sem_etapa")
    assert p["titulo"] == "1 gasto sem etapa." and "R$ 700,00" in p["detalhe"]
    assert p["acao"]["filtro"] == {"status": "a_definir"}


async def test_gasto_sem_etapa_fecha_com_o_a_definir_de_gastos(vila):
    obra = await _obra_aprovada(vila)
    await _gasto(vila, obra, valor=30000, etapa=None)
    await _gasto(vila, obra, valor=40000, etapa=None)
    ponto = _ponto(await _hoje(vila, obra), "gastos_sem_etapa")
    assert ponto["titulo"] == "2 gastos sem etapa." and "R$ 700,00" in ponto["detalhe"]
    resumo = (await vila.http.get(f"/obras/{obra}/gastos/resumo", headers=vila.h)).json()
    assert resumo["a_definir"] == 70000


async def test_le_os_gastos_uma_vez_por_chamada(vila, arm, monkeypatch):
    obra = await _obra_aprovada(vila, PLANO)
    original, lidas = arm.consultar, {}

    async def espia(consulta):
        lidas[consulta.colecao] = lidas.get(consulta.colecao, 0) + 1
        return await original(consulta)

    monkeypatch.setattr(arm, "consultar", espia)
    await _hoje(vila, obra)
    assert lidas["gastos"] == 1 and "pedidos" not in lidas and "insumos" not in lidas


async def test_etapa_estourada(vila):
    obra = await _obra_aprovada(vila)
    await _gasto(vila, obra, valor=1_000_000, etapa="1")
    p = _ponto(await _hoje(vila, obra), "etapas_estouradas")
    assert p["titulo"] == "SERVIÇOS PRELIMINARES passou do orçado em R$ 6.194,11."
    assert "Gasto R$ 10.000,00 de R$ 3.805,89 orçados." == p["detalhe"]
    assert p["acao"]["tela"] == "orcamento"


async def test_etapa_atrasada_e_situacao_atrasada(vila):
    obra = await _obra_aprovada(vila, PLANO)
    assert (await _hoje(vila, obra))["resumo"]["situacao"] == "sem_medicao"
    r = await _medir(vila, obra, "2026-11", {"1": 1000})
    assert r.status_code == 201, r.text
    h = await _hoje(vila, obra)
    p = _ponto(h, "etapas_atrasadas")
    assert p["titulo"] == "SERVIÇOS PRELIMINARES está atrasada." and p["acao"]["tela"] == "cronograma"
    assert "Medido 10% de 100% previstos" in p["detalhe"]
    assert h["resumo"]["situacao"] == "atrasada" and h["resumo"]["frase"].startswith("A obra está atrasada ")


async def test_no_prazo_e_semanas(vila):
    obra = await _obra_aprovada(vila, PLANO)
    r = await _medir(vila, obra, "2026-11", {"1": 10000})
    assert r.status_code == 201, r.text
    h = await _hoje(vila, obra)
    assert (h["resumo"]["situacao"], h["resumo"]["frase"]) == ("no_prazo", "A obra está no prazo.")
    # 01/11 a 01/02: 92 dias = 14 semanas; 09/11 cai na 2ª
    assert (h["resumo"]["semana_atual"], h["resumo"]["semanas_total"]) == (2, 14)
    assert (await _hoje(vila, obra, "2026-10-30"))["resumo"]["semana_atual"] is None


async def test_inicio_de_etapa_na_semana(vila):
    obra = await _obra_aprovada(vila, PLANO)
    h = await _hoje(vila, obra, "2026-11-28")
    assert [(e["data"], e["tipo"]) for e in h["semana"]] == [("2026-12-01", "inicio_etapa")]
    assert h["semana"][0]["titulo"] == "Começa FUNDAÇÕES"
    assert (await _hoje(vila, obra, "2026-11-20"))["semana"] == []


async def test_desfazendo_aprovacao(vila, arm):
    obra = await _obra_aprovada(vila)
    await arm.atualizar("obras", vila.tenant_id, obra, {"status": "desfazendo_aprovacao"})
    h = await _hoje(vila, obra)
    assert h["pontos"][0]["tipo"] == "desfazendo_aprovacao" and h["pontos"][0]["prioridade"] == "pede_voce"


async def test_pontos_saem_na_ordem_de_prioridade(vila, arm):
    obra = await _obra_aprovada(vila, PLANO)
    obra = (await nova_obra(vila))["id"]
    await _contrato(vila, obra, SEM_PRECO, mapa(linha_cabecalho=1))
    r = await vila.http.put(f"/obras/{obra}/cronograma", headers=vila.h, json={
        "inicio": "2026-11", "meses": 1, "etapas": [{"etapa": "1", "percentuais": [10000]}]})
    assert r.status_code == 200, r.text
    await _medir(vila, obra, "2026-11", {"1": 1000})
    await _gasto(vila, obra, valor=1_000_000, etapa="1")
    await _gasto(vila, obra, etapa=None)
    await vila.http.post(f"/obras/{obra}/comprovantes", headers=vila.h,
                         files={"arquivo": ("p.jpg", b"\xff\xd8\xff\xe0" + uuid.uuid4().bytes, "image/jpeg")})
    await arm.atualizar("obras", vila.tenant_id, obra, {"status": "desfazendo_aprovacao"})
    h = await _hoje(vila, obra)
    tipos = [p["tipo"] for p in h["pontos"]]
    assert tipos == ["desfazendo_aprovacao", "gastos_a_conferir", "etapas_estouradas", "etapas_atrasadas",
                     "itens_sem_preco", "gastos_sem_etapa"]
    assert h["mais"] == 0
    ordem = {"pede_voce": 0, "atencao": 1, "a_caminho": 2, "ok": 3}
    ranks = [ordem[p["prioridade"]] for p in h["pontos"]]
    assert ranks == sorted(ranks)
    assert h["resumo"]["esperando"] == 2
