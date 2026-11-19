import pytest

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Proposta, TipoProposta
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais

# --- tipos portáveis ---

@pytest.mark.parametrize("entrada,esperado", [("1,5", 1500), ("2.760,0", 2760000), ("2760", 2760000), ("1.5", 1500),
                                              (3.2, 3200), ("1.299,9", 1299900), (600, 600000)])
def test_qtd_aceita_formato_brasileiro(entrada, esperado):
    assert qtd(entrada) == esperado


def test_ponto_com_tres_casas_e_ambiguo():
    with pytest.raises(ValueError):
        qtd("2.760")


def test_formatacao():
    assert fmt_qtd(2760000) == "2.760"
    assert fmt_qtd(1500) == "1,5"
    assert fmt_qtd(-500) == "-0,5"
    assert fmt_qtd(-2000) == "-2"
    assert fmt_reais(reais("1.299,90")) == "R$ 1.299,90"
    assert fmt_reais(-47200) == "-R$ 472,00"


# --- a porta de armazenamento só aceita o que o Firestore aceita ---

def test_consulta_com_desigualdade_em_dois_campos_e_recusada():
    c = Consulta("pedidos", "t1", [Filtro("valor", ">", 0), Filtro("criado_em", "<", 1)])
    with pytest.raises(ValueError):
        c.validar()


def test_consulta_in_grande_e_recusada():
    with pytest.raises(ValueError):
        Consulta("pedidos", "t1", [Filtro("status", "in", list(range(31)))]).validar()


def test_ordem_tem_de_comecar_pelo_campo_da_desigualdade():
    with pytest.raises(ValueError):
        Consulta("pedidos", "t1", [Filtro("valor", ">", 0)], ordem=[("criado_em", "desc")]).validar()
    Consulta("pedidos", "t1", [Filtro("valor", ">", 0)], ordem=[("valor", "desc")]).validar()


def test_proposta_antiga_de_compra_ainda_se_le():
    """O histórico guarda propostas de compra de antes de Compras sair: o documento continua válido."""
    pr = Proposta(tenant_id="t1", obra_id="o1", tipo="criar_pedido", titulo="x", dados={})
    assert (pr.tipo, pr.ajustada, pr.dados_originais) == (TipoProposta.CRIAR_PEDIDO, False, None)


def test_cnpj_confere_os_dois_digitos():
    from canteiro_dominio.regras import cnpj_valido
    assert cnpj_valido("11222333000181")
    assert not cnpj_valido("11222333000182")
    assert not cnpj_valido("11111111111111")       # passa na conta, mas é sequência
    assert not cnpj_valido("1122233300018")


@pytest.mark.parametrize("ps,medido,ref,esperado", [
    ([10000, 0], 8886, 0, ("atrasada", 10000, 1114)),
    ([0, 5000, 5000], 0, 0, ("nao_comecou", 0, 0)),
    ([1500, 8500], 2483, 0, ("adiantada", 1500, 0)),
    ([5000, 5000], 5000, 0, ("em_dia", 5000, 0)),
    ([5000, 5000], 10000, 0, ("concluida", 5000, 0)),
    ([5000, 5000], 0, None, ("nao_comecou", 0, 0)),
])
def test_situacao_etapa(ps, medido, ref, esperado):
    from canteiro_dominio.regras import situacao_etapa

    s = situacao_etapa(ps, medido, ref)
    assert (s.situacao, s.previsto, s.falta) == esperado
