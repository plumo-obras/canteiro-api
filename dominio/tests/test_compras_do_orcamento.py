"""Do orçamento para Compras: linhas de mesmo nome viram um insumo só, sem somar kg com m³."""
import pytest

from canteiro_dominio.regras import (
    ItemParaCompras,
    RegraViolada,
    insumo_ao_aprovar,
    insumo_com_aditivo,
    itens_para_compras,
    preco_medio,
)


def item(descricao, quantidade, total, *, unidade="kg", etapa="1", tipo="item"):
    return {"tipo": tipo, "descricao": descricao, "unidade": unidade, "quantidade": quantidade, "total": total,
            "etapa": etapa}


def compra(quantidade=1000, valor_unitario=500, etapa="2"):
    return ItemParaCompras(nome="Aço", unidade="kg", quantidade=quantidade, valor_unitario=valor_unitario,
                           total=500, etapa=etapa)


def test_mesmo_nome_em_duas_etapas_vira_um_item_na_etapa_de_maior_valor():
    [x] = itens_para_compras([item("Aço  CA-50", 1000, 500, etapa="1"), item("aço ca-50", 3000, 2700, etapa="2")])
    assert (x.nome, x.quantidade, x.total, x.etapa) == ("Aço CA-50", 4000, 3200, "2")
    assert x.valor_unitario == 800


def test_titulo_de_etapa_nao_vira_item():
    assert itens_para_compras([item("FUNDAÇÕES", 0, 0, tipo="etapa", unidade="")]) == []


def test_sem_precos_a_etapa_e_a_de_maior_quantidade():
    [x] = itens_para_compras([item("Aço", 1000, 0, etapa="1"), item("Aço", 3000, 0, etapa="2")])
    assert (x.etapa, x.valor_unitario) == ("2", 0)


def test_etapa_vazia_vira_none():
    assert itens_para_compras([item("Aço", 1000, 500, etapa="")])[0].etapa is None


def test_supressao_desconta_da_soma():
    [x] = itens_para_compras([item("Aço", 3000, 900), item("Aço", -1000, -300)])
    assert (x.quantidade, x.total) == (2000, 600)


def test_unidades_diferentes_no_mesmo_nome_e_recusado_listando():
    with pytest.raises(RegraViolada, match=r"Aço \(kg e m²\)"):
        itens_para_compras([item("Aço", 1000, 500), item("Aço", 1000, 500, unidade="m²")])


def test_preco_medio_arredonda_e_nao_divide_por_zero():
    assert preco_medio(1000, 3000) == 333
    assert preco_medio(500, 0) == 0
    assert preco_medio(500, -1000) == 0


def test_ao_aprovar_fica_o_maior_e_nao_troca_preco_ja_lancado():
    d = insumo_ao_aprovar({"precisa_ate_o_fim": 5000, "valor_unitario": 300, "etapa": None}, compra())
    assert d == {"precisa_ate_o_fim": 5000, "valor_unitario": 300, "etapa": "2"}
    d = insumo_ao_aprovar({"precisa_ate_o_fim": 500, "valor_unitario": 0, "etapa": "9"}, compra())
    assert d == {"precisa_ate_o_fim": 1000, "valor_unitario": 500, "etapa": "9"}


def test_aditivo_soma_e_supressao_nao_passa_de_zero():
    assert insumo_com_aditivo({"precisa_ate_o_fim": 5000, "valor_unitario": 300}, compra())["precisa_ate_o_fim"] == 6000
    assert insumo_com_aditivo({"precisa_ate_o_fim": 500}, compra(quantidade=-900))["precisa_ate_o_fim"] == 0
