import pytest

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Movimento, Pedido, Proposta, StatusPedido, TipoMovimento, TipoProposta
from canteiro_dominio.regras import (
    RegraViolada,
    conta_de_falta,
    quantidade_conferida,
    saldo_de,
    validar_conferencia,
    validar_saida,
    validar_transicao,
)
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, qtd, reais


def mov(tipo, q, **kw):
    return Movimento(tenant_id="t1", obra_id="o1", insumo_id="i1", tipo=tipo, quantidade=q, quem="teste", **kw)


# --- a conta da falta (números da Casa Vila Nova no protótipo) ---

def test_aco_desconta_o_que_esta_a_caminho():
    c = conta_de_falta(precisa=qtd(3940), saldo=qtd(140), a_caminho=qtd(3200))
    assert c.falta == qtd(600)


def test_compra_em_dobro_nao_acontece_com_item_em_cotacao():
    # aço: os 600 kg do plano B já estão em cotação (PED-0167) → não comprar de novo
    c = conta_de_falta(precisa=qtd(3940), saldo=qtd(140), a_caminho=qtd(3200), cotando=qtd(600))
    assert c.comprar == 0


def test_cimento_com_transferencia_de_outra_obra():
    c = conta_de_falta(precisa=qtd(200), saldo=qtd(46), a_caminho=qtd(120), transferencia=qtd(20))
    assert c.falta == qtd(14)


def test_saldo_negativo_nao_infla_a_falta():
    c = conta_de_falta(precisa=qtd(10), saldo=qtd(-5))
    assert c.falta == qtd(10)


def test_falta_nunca_negativa():
    assert conta_de_falta(precisa=qtd(10), saldo=qtd(50)).falta == 0


# --- livro-razão ---

def test_saldo_do_bloco_a_partir_dos_movimentos():
    movs = [
        mov(TipoMovimento.ENTRADA, qtd(4000), origem_doc="PED-0149"),
        mov(TipoMovimento.SAIDA, qtd(1050), destino="alvenaria térreo, eixos A–C"),
        mov(TipoMovimento.SAIDA, qtd(190), destino="quebra na descarga"),
    ]
    assert saldo_de(movs) == qtd(2760)


def test_ajuste_de_contagem_carrega_o_sinal():
    assert saldo_de([mov(TipoMovimento.ENTRADA, qtd(10)), mov(TipoMovimento.AJUSTE, qtd(-2))]) == qtd(8)


def test_movimento_sem_quantidade_e_recusado():
    with pytest.raises(RegraViolada):
        saldo_de([mov(TipoMovimento.SAIDA, 0)])


def test_saida_de_curva_a_exige_destino():
    with pytest.raises(RegraViolada):
        validar_saida(curva="A", destino="  ")
    validar_saida(curva="C", destino=None)  # parafuso: contagem semanal, sem destino


# --- pedido ---

def test_transicoes_validas_e_invalidas():
    validar_transicao(StatusPedido.EM_COTACAO, StatusPedido.APROVADO)
    validar_transicao(StatusPedido.RECEBIDO, StatusPedido.CONFERIDO)
    with pytest.raises(RegraViolada):
        validar_transicao(StatusPedido.EM_COTACAO, StatusPedido.CONFERIDO)
    with pytest.raises(RegraViolada):
        validar_transicao(StatusPedido.CONFERIDO, StatusPedido.CANCELADO)


# --- tipos portáveis ---

@pytest.mark.parametrize("entrada,esperado", [("1,5", 1500), ("2.760,0", 2760000), ("2760", 2760000), ("1.5", 1500), (3.2, 3200), ("1.299,9", 1299900), (600, 600000)])
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


# --- conferência com o que chegou de fato ---

def test_conferida_padrao_e_o_pedido_inteiro():
    assert quantidade_conferida(pedida=qtd(40), recebida=None) == qtd(40)


def test_conferida_aceita_a_menos_e_a_mais():
    assert quantidade_conferida(pedida=qtd(40), recebida=qtd(36)) == qtd(36)
    assert quantidade_conferida(pedida=qtd(40), recebida=qtd(45)) == qtd(45)


def test_entrega_parcial_devolve_a_diferenca_para_a_falta():
    # areia: precisa 60, saldo 12, PED-0157 de 40 a caminho → falta 8; chegaram 36 e o pedido fechou → falta 12
    antes = conta_de_falta(precisa=qtd(60), saldo=qtd(12), a_caminho=qtd(40))
    depois = conta_de_falta(precisa=qtd(60), saldo=qtd(12) + quantidade_conferida(pedida=qtd(40), recebida=qtd(36)))
    assert (antes.falta, depois.falta) == (qtd(8), qtd(12))


def test_quantidade_recebida_e_nf_so_ao_conferir():
    validar_conferencia(StatusPedido.CONFERIDO, quantidade_recebida=qtd(36), nf="4471")
    validar_conferencia(StatusPedido.RECEBIDO)
    for extra in ({"quantidade_recebida": qtd(1)}, {"nf": "4471"}):
        with pytest.raises(RegraViolada, match="só valem ao conferir"):
            validar_conferencia(StatusPedido.RECEBIDO, **extra)


def test_quantidade_recebida_tem_de_ser_positiva():
    for q in (0, -qtd(1)):
        with pytest.raises(RegraViolada):
            validar_conferencia(StatusPedido.CONFERIDO, quantidade_recebida=q)


def test_campos_novos_tem_padrao_compativel_com_documento_antigo():
    p = Pedido(tenant_id="t1", numero="PED-0001", obra_id="o1", insumo_id="i1", quantidade=1, fornecedor="f", valor=0)
    # pedido de antes das linhas vira um pedido de uma linha, sem nada recebido
    assert ([(x.insumo_id, x.quantidade, x.recebida) for x in p.linhas], p.recebimentos) == ([("i1", 1, 0)], 0)
    pr = Proposta(tenant_id="t1", obra_id="o1", tipo=TipoProposta.CRIAR_PEDIDO, titulo="x", dados={})
    assert (pr.ajustada, pr.dados_originais) == (False, None)
    assert mov(TipoMovimento.ENTRADA, 1).usuario_id is None


def test_cnpj_confere_os_dois_digitos():
    from canteiro_dominio.regras import cnpj_valido
    assert cnpj_valido("11222333000181")
    assert not cnpj_valido("11222333000182")
    assert not cnpj_valido("11111111111111")       # passa na conta, mas é sequência
    assert not cnpj_valido("1122233300018")


def test_falta_na_linha_nao_fica_negativa():
    from canteiro_dominio.regras import falta_na_linha
    assert falta_na_linha({"quantidade": 25, "recebida": 18}) == 7
    assert falta_na_linha({"quantidade": 25, "recebida": 30}) == 0
    assert falta_na_linha({"quantidade": 25}) == 25
