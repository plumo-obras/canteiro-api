"""Gasto confirmado sem etapa ("ver depois"): conta nos totais da obra e em nenhuma etapa."""
from datetime import date

import pytest

from canteiro_dominio.etapas import montar_arvore, nome_da_etapa, origem_da_etapa
from canteiro_dominio.gastos import gastos_no_orcamento, validar_gasto
from canteiro_dominio.regras import RegraViolada


def _validar(**kw):
    return validar_gasto(**{"valor": 100, "data_pagamento": "2026-11-01", "favorecido": "João", "descricao": "areia",
                            "etapa": None, "topico_id": None, "arvore": montar_arvore([], []),
                            "hoje": date(2026, 11, 9), **kw})


def test_sem_etapa_vale_sem_conferir_a_arvore():
    assert _validar().favorecido == "João"


def test_topico_sem_etapa_e_recusado():
    with pytest.raises(RegraViolada, match="tópico"):
        _validar(topico_id="t1")


def test_origem_e_nome():
    assert origem_da_etapa(None, {"1"}) == "a_definir"
    assert nome_da_etapa(None, orcamento={}, extras={}) == "Sem etapa"


def test_conta_nos_totais_e_em_nenhuma_etapa():
    r = gastos_no_orcamento([{"etapa": None, "valor": 700}, {"etapa": "1", "valor": 300}], {"1"})
    assert (r.a_definir, r.por_etapa, r.total, r.fora_das_etapas) == (700, {"1": 300}, 1000, 700)
