from canteiro_dominio.tributos import (
    NATUREZAS,
    conta_ibs_cbs,
    conta_inss,
    correcao_selic,
    decadente,
    equivalencia,
    fator_social,
    natureza_padrao,
    transicao_ibs_cbs,
    uso_concreto,
)

SELIC = {"2026-09": 108, "2026-10": 107}
GASTOS = [
    {"valor": 1_000_000, "natureza_fiscal": "folha", "data_pagamento": "2026-08-10"},          # + Selic 1,07% + 1%
    {"valor": 2_000_000, "natureza_fiscal": "mao_de_obra_nf", "nf_cno": True, "data_pagamento": "2026-10-05"},
    {"valor": 500_000, "natureza_fiscal": "mao_de_obra_nf", "data_pagamento": "2026-10-05"},   # sem o CNO: não abate
    {"valor": 1_000_000, "natureza_fiscal": "concreto_usinado", "data_pagamento": "2026-09-01"},
    {"valor": 1_000_000, "natureza_fiscal": "mao_de_obra_sem_nf", "data_pagamento": "2026-09-01"},
    {"valor": 3_000_000, "pedido_id": "p1", "data_pagamento": "2026-09-01"},                    # material do pedido
    {"valor": 9_000_000, "natureza_fiscal": "folha", "estornado": True, "data_pagamento": "2026-09-01"},
    {"valor": 700_000, "data_pagamento": "2026-09-01"},                                        # a classificar
]


def _casa(**kw):
    base = dict(area_principal=150_000, area_complementar=20_000, vau=200_000, material="alvenaria",
                proprietario="pf", destinacao="residencial_unifamiliar", padrao="normal", gastos=GASTOS, uf="SP",
                afericao="2026-11", selic=SELIC)
    return conta_inss(**{**base, **kw})


def test_inss_casa_pf_como_o_sero():
    c = _casa()
    assert c.situacao == "calculado"
    assert c.area_equivalente == 143_500                 # 150 × 89% (equivalência) + 20 × 50%
    assert c.custo_obra == 28_700_000                    # R$ 2.000/m² × 143,5 m²
    assert c.fator_social == 4000                        # 170 m² no total: faixa de 101 a 200
    assert c.rmt == 2_296_000                            # 287.000 × 20% (alvenaria) × 40%
    assert c.credito_concreto == 70_315                  # 287.000 × 4,90% (SP, unifamiliar) × 5%
    assert c.comprovado_por_natureza == {"folha": 1_020_700, "mao_de_obra_nf": 800_000, "concreto_usinado": 70_315}
    assert c.comprovado == 1_891_015
    assert c.base == 404_985
    assert c.inss_sem_fator_ajuste == c.inss == 149_034  # 4.049,85 × 36,8%
    assert (c.mao_de_obra_sem_nf, c.notas_sem_cno) == (1_000_000, 500_000)
    assert c.economia_com_notas == 149_034               # 40% de 15.000 passa da base que resta
    # declarou mais da metade da RMT (até 350 m²): falta só confirmar a DCTFWeb para zerar
    assert (c.fator_ajuste, c.fator_ajuste_minimo, c.creditos_fator_ajuste) == ("atingido", 5000, 1_820_700)
    assert any("CNO" in a for a in c.avisos) and any("DCTFWeb" in a for a in c.avisos)
    assert c.linhas[2] == ("mão de obra presumida (RMT) = R$ 287.000,00 × 20% (alvenaria) × fator social 40% = "
                           "R$ 22.960,00")


def test_fator_de_ajuste_zera_o_inss_com_a_dctfweb_em_dia():
    c = _casa(dctfweb_em_dia=True)
    assert (c.fator_ajuste, c.inss, c.inss_sem_fator_ajuste, c.economia_com_notas) == ("aplicado", 0, 149_034, 0)
    assert c.linhas[-1].endswith("INSS a pagar = R$ 0,00")


def test_fator_de_ajuste_diz_quanto_falta():
    c = _casa(gastos=[{"valor": 500_000, "natureza_fiscal": "folha", "data_pagamento": "2026-11-02"}],
              dctfweb_em_dia=True)
    assert (c.fator_ajuste, c.falta_fator_ajuste) == ("nao_atingido", 648_000)    # 50% de 22.960 − 5.000
    assert c.inss == 660_928                                                         # (22.960 − 5.000) × 36,8%
    # acima de 350 m² o mínimo é 70%
    assert _casa(area_principal=400_000, gastos=[]).fator_ajuste_minimo == 7000


def test_inss_pj_sem_fator_social_nem_fator_de_ajuste():
    c = _casa(proprietario="pj", gastos=[])
    assert (c.fator_social, c.rmt, c.inss) == (10000, 5_740_000, 2_112_320)
    assert c.fator_ajuste == "nao_se_aplica" and c.economia_com_notas == 0
    assert "fator social" not in c.linhas[2]


def test_pj_sem_retencao_na_nota_avisa():
    c = _casa(proprietario="pj", gastos=[{"valor": 100, "natureza_fiscal": "mao_de_obra_nf", "nf_cno": True}])
    assert any("11%" in a for a in c.avisos)
    c = _casa(proprietario="pj", gastos=[{"valor": 100, "natureza_fiscal": "mao_de_obra_nf", "nf_cno": True,
                                          "retencao": 11}])
    assert not any("11%" in a for a in c.avisos)


def test_inss_base_nunca_negativa():
    c = _casa(gastos=[{"valor": 100_000_000, "natureza_fiscal": "folha", "data_pagamento": "2026-11-01"},
                      {"valor": 1_000_000, "natureza_fiscal": "mao_de_obra_sem_nf"}])
    assert c.base == 0 and c.inss == 0 and c.economia_com_notas == 0


def test_tipo_categoria_e_destinacao():
    assert _casa(material="mista", gastos=[], proprietario="pj").rmt == 4_305_000      # 15%
    assert _casa(material="madeira", gastos=[], proprietario="pj").rmt == 4_305_000
    assert _casa(material=None, gastos=[], proprietario="pj").rmt == 5_740_000         # sem tipo, alvenaria
    assert _casa(categoria="reforma", gastos=[], proprietario="pj").rmt == 2_009_000   # × 35%
    assert _casa(categoria="demolicao", gastos=[], proprietario="pj").rmt == 574_000   # × 10%
    # casa popular: equivalência 98% e 12% da alvenaria
    c = _casa(destinacao="casa_popular", area_complementar=0, gastos=[], proprietario="pj")
    assert (c.area_equivalente, c.percentual_rmt, c.rmt) == (147_000, 1200, 3_528_000)
    # edifício garagem: equivalência do comercial (86%) e 80% da RMT
    c = _casa(destinacao="edificio_garagens", area_complementar=0, gastos=[], proprietario="pj")
    assert (c.area_equivalente, c.rmt) == (129_000, 4_128_000)


def test_equivalencia_e_areas_descobertas():
    assert equivalencia("residencial_unifamiliar", 1_000_000) == 8900
    assert equivalencia("residencial_unifamiliar", 1_000_001) == 8500
    assert equivalencia("comercial", 3_000_001) == 8300
    assert equivalencia(None, 1) == 10000
    assert _casa(area_complementar=0, area_descoberta=40_000, gastos=[]).area_equivalente == 143_500  # 133,5 + 10


def test_pre_moldado_reduz_70_por_cento_ou_vira_mista():
    cod = 28_700_000
    c = _casa(proprietario="pj", gastos=[{"valor": cod * 4 // 10, "natureza_fiscal": "pre_moldado",
                                          "data_pagamento": "2026-11-01"}])
    assert (c.pre_moldado, c.rmt) == ("reduziu", 1_722_000)          # 57.400 × 30%
    c = _casa(proprietario="pj", gastos=[{"valor": 1_000_000, "natureza_fiscal": "pre_moldado",
                                          "data_pagamento": "2026-11-01"}])
    assert (c.pre_moldado, c.percentual_rmt, c.rmt) == ("virou_mista", 1500, 4_305_000)


def test_decadencia_tira_os_meses_e_a_remuneracao_decadentes():
    # aferida em 11/2026: decaíram 2019 e 2020 (24 dos 95 meses desde 01/2019)
    c = _casa(proprietario="pj", inicio="2019-01-15",
              gastos=[{"valor": 1_000_000, "natureza_fiscal": "folha", "data_pagamento": "2020-06-01"}])
    assert c.nao_decadente == 7473
    assert c.rmt == 4_289_502                                         # 57.400 × 74,73%
    assert c.comprovado == 0
    assert decadente("2020-12", "2026-11") and not decadente("2021-01", "2026-11")


def test_correcao_selic():
    assert correcao_selic("2026-08", "2026-11", SELIC) == (207, [])
    assert correcao_selic("2026-07", "2026-11", {"2026-10": 107}) == (207, ["2026-09"])
    assert correcao_selic("2026-10", "2026-11", SELIC) == (0, [])     # vence no mês seguinte: não corrige
    assert correcao_selic("2026-09", "2026-11", SELIC) == (100, [])   # só o 1% do mês da aferição


def test_gps_e_autonomo():
    c = _casa(proprietario="pj", gastos=[{"valor": 368_000, "natureza_fiscal": "gps", "data_pagamento": "2026-11-01"}])
    assert c.comprovado_por_natureza == {"gps": 1_000_000}            # o SERO divide a GPS por 36,8%
    c = _casa(proprietario="pj", gastos=[{"valor": 1_000_000, "natureza_fiscal": "autonomo_mei",
                                          "data_pagamento": "2026-11-01"}])
    assert c.comprovado == 0 and c.credito_autonomo == 200_000        # só os 20% patronais
    assert c.inss == 2_112_320 - 200_000


def test_concreto_sem_uf_avisa():
    c = _casa(uf=None, gastos=[{"valor": 1, "natureza_fiscal": "concreto_usinado"}])
    assert c.credito_concreto == 0 and any("Anexo I" in a for a in c.avisos)
    assert uso_concreto("TO", "residencial_multifamiliar") == 716    # o exemplo do Manual do SERO


def test_inss_isenta_casa_pequena_popular_de_uso_proprio():
    pequena = dict(area_principal=60_000, area_complementar=0, padrao="baixo", finalidade="uso_proprio", gastos=[])
    assert _casa(**pequena).situacao == "isenta"
    # qualquer mão de obra paga, ou obra para vender, tira a isenção
    assert _casa(**{**pequena, "gastos": [{"valor": 100, "natureza_fiscal": "mao_de_obra_sem_nf"}]}
                 ).situacao == "calculado"
    assert _casa(**{**pequena, "finalidade": "venda"}).situacao == "calculado"


def test_inss_sem_dados_diz_o_que_falta():
    c = conta_inss(area_principal=0, vau=0, material=None, proprietario=None, destinacao=None, padrao=None,
                   gastos=[], afericao="2026-11")
    assert c.situacao == "sem_dados"
    assert len(c.motivos) == 2


def test_faixas_do_fator_social():
    assert fator_social(100_000) == 2000
    assert fator_social(100_001) == 4000
    assert fator_social(400_000) == 7000
    assert fator_social(400_001) == 9000


def test_fator_social_vale_para_toda_obra_de_pf():
    assert _casa(destinacao="comercial", gastos=[]).fator_social == 4000


def test_natureza_padrao():
    assert natureza_padrao({"pedido_id": "x"}) == "material"
    assert natureza_padrao({"natureza_fiscal": "folha", "pedido_id": "x"}) == "folha"
    assert natureza_padrao({}) is None
    assert {"mao_de_obra_nf", "gps", "autonomo_mei"} <= set(NATUREZAS)


def test_ibs_cbs_venda_com_redutores_e_creditos():
    c = conta_ibs_cbs(finalidade="venda", valor_operacao=80_000_000, valor_terreno=20_000_000, unidades=1,
                      creditos=3_000_000, ano=2026)
    assert c.situacao == "calculado"
    assert c.aliquota_efetiva == 1325
    assert (c.redutor_ajuste, c.redutor_social, c.base) == (20_000_000, 10_000_000, 50_000_000)
    assert c.debito == 6_625_000
    assert c.a_pagar == 3_625_000
    assert c.linhas[-1] == "a pagar = R$ 66.250,00 − créditos das compras R$ 30.000,00 = R$ 36.250,00"
    assert "teste" in c.transicao


def test_ibs_cbs_redutores_nao_passam_do_valor():
    c = conta_ibs_cbs(finalidade="venda", valor_operacao=10_000_000, valor_terreno=8_000_000, unidades=3, ano=2033)
    assert c.base == 0 and c.debito == 0


def test_ibs_cbs_prestacao_de_servico_e_creditos_maiores():
    c = conta_ibs_cbs(finalidade="prestacao_servico", valor_operacao=48_000_000, creditos=99_000_000, ano=2027)
    assert c.debito == 6_360_000
    assert c.a_pagar == 0


def test_ibs_cbs_uso_proprio_e_sem_dados():
    assert conta_ibs_cbs(finalidade="uso_proprio", valor_operacao=1, ano=2026).situacao == "nao_incide"
    c = conta_ibs_cbs(finalidade=None, valor_operacao=0, ano=2026)
    assert c.situacao == "sem_dados" and len(c.motivos) == 2


def test_transicao():
    assert "2033" in transicao_ibs_cbs(2040)
    assert "Antes de 2026" in transicao_ibs_cbs(2025)
    assert "2029" in transicao_ibs_cbs(2030)


def test_vau_vigente_pega_o_mes_mais_recente_ate_hoje():
    from canteiro_dominio.tributos import vau_vigente
    linhas = [{"mes": "2026-10", "vau": 1}, {"mes": "2026-12", "vau": 3}, {"mes": "2026-11", "vau": 2}]
    assert vau_vigente(linhas, "2026-11")["vau"] == 2
    assert vau_vigente(linhas, "2027-01")["vau"] == 3
    assert vau_vigente(linhas, "2026-09") is None
    assert vau_vigente([], "2026-11") is None
