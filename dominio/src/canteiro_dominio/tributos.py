"""Tributos da obra: o INSS da aferição (CNO/SERO) e o IBS/CBS da reforma tributária, como ESTIMATIVA.

O cálculo oficial é da Receita (SERO, DCTFWeb). Aqui a plataforma refaz a conta com o que a obra já registra, para a
pessoa ver durante a obra quanto vai pagar e o que reduz o valor (documentar a mão de obra, a nota do concreto), e não
descobrir no fim. Toda conta devolve as linhas prontas para a tela e o agente citarem.

Os percentuais ficam TODOS em `PARAMETROS_INSS` e `PARAMETROS_IBS_CBS`, com a fonte ao lado, para o contador revisar num
lugar só (a IN RFB 2.021/2021 e a LC 214/2025 mudam). A resposta da API devolve os parâmetros usados.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from .regras import CEM
from .tipos import fmt_qtd, fmt_reais

# --- parâmetros (centésimos de ponto percentual: 40% = 4000; área em milésimos de m²) ---

# Faixas: (teto em milésimos de m², percentual); None = acima da última faixa.
_EQUIVALENCIA_COMERCIAL = [(3_000_000, 8600), (None, 8300)]
# IN RFB 2.021/2021, Anexo I: quanto do custo da obra é concreto/argamassa usinada, por UF e destinação (a ordem de
# `DESTINACOES_ANEXO_I`). É sobre ele que a obra que usou usinado abate 5% do custo.
DESTINACOES_ANEXO_I = ("casa_popular", "comercial", "conjunto_habitacional", "edificio_garagens", "galpao",
                       "residencial_multifamiliar", "residencial_unifamiliar")
_ANEXO_I = {
    "AC": (469, 1333, 469, 1333, 452, 961, 743), "AL": (398, 1135, 398, 1135, 382, 812, 611),
    "AM": (469, 1333, 469, 1333, 452, 961, 743), "AP": (488, 1293, 488, 1293, 438, 941, 748),
    "BA": (373, 1031, 373, 1031, 362, 746, 553), "CE": (370, 1069, 370, 1069, 344, 769, 572),
    "DF": (353, 962, 353, 962, 343, 706, 524), "ES": (333, 945, 333, 945, 326, 685, 515),
    "GO": (388, 1027, 388, 1027, 360, 762, 579), "MA": (418, 1206, 418, 1206, 407, 873, 694),
    "MG": (315, 866, 315, 866, 305, 622, 468), "MS": (434, 1220, 434, 1220, 428, 874, 674),
    "MT": (402, 1096, 402, 1096, 389, 801, 622), "PA": (491, 1348, 491, 1348, 445, 977, 758),
    "PB": (412, 1181, 412, 1181, 381, 858, 632), "PE": (351, 974, 351, 947, 342, 689, 512),
    "PI": (353, 1000, 353, 1000, 330, 716, 533), "PR": (318, 878, 318, 878, 308, 650, 491),
    "RJ": (320, 902, 320, 902, 308, 652, 494), "RN": (401, 1041, 401, 1041, 363, 762, 596),
    "RO": (402, 1096, 402, 1096, 389, 801, 622), "RR": (469, 1333, 469, 1333, 452, 961, 743),
    "RS": (325, 877, 325, 877, 323, 654, 501), "SC": (293, 836, 293, 836, 287, 619, 479),
    "SE": (434, 1250, 434, 1250, 418, 905, 697), "SP": (315, 869, 315, 869, 296, 635, 490),
    "TO": (353, 1000, 353, 1000, 330, 716, 533),
}

PARAMETROS_INSS = {
    "fonte": "IN RFB 2.021/2021 (arts. 22 a 34 e Anexo I) e Manual do SERO. Conferir com o contador.",
    # Art. 25 §6: só uma parte da área principal entra no custo, conforme a destinação e o tamanho.
    "equivalencia": {
        "residencial_unifamiliar": [(1_000_000, 8900), (None, 8500)],
        "residencial_multifamiliar": [(1_000_000, 9000), (None, 8600)],
        "comercial": _EQUIVALENCIA_COMERCIAL,
        "edificio_garagens": _EQUIVALENCIA_COMERCIAL,      # art. 26 §6: enquadrado como comercial
        "galpao": [(None, 9500)],
        "casa_popular": [(None, 9800)],
        "conjunto_habitacional": [(None, 9800)],
    },
    # Art. 25 §9: área complementar (piscina, quadra, garagem fora do corpo da casa) entra reduzida.
    "area_complementar_coberta": 5000,
    "area_complementar_descoberta": 2500,
    # Art. 25 §16: mão de obra presumida (RMT) = custo × percentual do tipo de obra.
    "percentual_rmt": {"alvenaria": 2000, "mista": 1500, "madeira": 1500},
    "percentual_rmt_interesse_social": {"alvenaria": 1200, "mista": 700, "madeira": 700},
    # Art. 26: reforma e demolição pagam uma parte; edifício garagem tem 20% de redução.
    "categoria": {"obra_nova": 10000, "acrescimo": 10000, "reforma": 3500, "demolicao": 1000},
    "destinacao": {"edificio_garagens": 8000},
    # Art. 26 §1: fator social, obra de pessoa física (qualquer destinação), pela área total.
    "fator_social": [(100_000, 2000), (200_000, 4000), (300_000, 5500), (400_000, 7000), (None, 9000)],
    # Art. 26 §2 e §3: notas de pré-moldado/pré-fabricado ≥ 40% do custo → RMT cai 70% (alvenaria); abaixo disso a
    # área é calculada como mista.
    "pre_moldado_minimo": 4000,
    "pre_moldado_fator": 3000,
    # Art. 32 §3: usou concreto/argamassa usinada ou massa asfáltica → abate 5% do custo × Anexo I × categoria.
    "concreto_usinado": 500,
    "concreto_usinado_categoria": {"obra_nova": 10000, "acrescimo": 10000, "reforma": 3500, "demolicao": 0},
    # Quanto de cada pagamento vira remuneração comprovada (abate da RMT), corrigido pela Selic (art. 31):
    # folha e autônomo/MEI declarados no eSocial; GPS paga com o CNO (o SERO divide por 36,8%); nota de empreiteira
    # com o CNO da obra (estimativa: a Receita usa a folha que a empreiteira declarou, o art. 22 presume 40% da nota).
    "abatimento": {
        "folha": 10000,
        "mao_de_obra_nf": 4000,
        "gps": 27174,
        "autonomo_mei": 10000,      # conta no fator de ajuste, mas no débito só abate a parte patronal (20%)
        "pre_moldado": 0,           # entra pela regra dos 40% do custo, não por gasto
        "concreto_usinado": 0,      # entra pelo percentual do custo, não pelo valor da nota
        "mao_de_obra_sem_nf": 0,    # Pix ou recibo sem nota nem eSocial: a Receita não aceita
        "material": 0,
        "equipamento": 0,
        "outros": 0,
    },
    # Art. 19: 20% patronal + 8% segurados + 3% GILRAT + 5,8% terceiros (FPAS 507), como no Manual do SERO.
    "aliquota": 3680,
    "aliquota_composicao": {"patronal": 2000, "segurados": 800, "gilrat": 300, "salario_educacao": 250,
                            "incra": 20, "sebrae": 60, "sesi": 150, "senai": 100},
    "aliquota_patronal": 2000,
    # Art. 31: remuneração corrigida pela Selic do 2º mês depois da competência até o mês anterior + 1%.
    "selic_mes_da_afericao": 100,
    # Art. 33: obra de PF com remuneração declarada ≥ 50% da RMT (até 350 m²) ou ≥ 70% (acima) e DCTFWeb entregue
    # todo mês → nada a pagar na aferição.
    "fator_ajuste_area": 350_000,
    "fator_ajuste_ate_area": 5000,
    "fator_ajuste_acima_area": 7000,
    # Art. 29 e CTN art. 173: o mês decai 5 anos depois do 1º de janeiro do ano seguinte.
    "decadencia_anos": 5,
    # Art. 34 I: isenta a casa da pessoa física até 70 m², uso próprio, popular, sem mão de obra remunerada.
    "isencao_area_max": 70_000,
}

PARAMETROS_IBS_CBS = {
    "fonte": "LC 214/2025, regime específico de bens imóveis. Alíquota de referência estimada; conferir com o "
             "contador.",
    "aliquota_referencia": 2650,      # IBS + CBS somados
    "reducao_imoveis": 5000,          # operações com bens imóveis e construção pagam 50% da alíquota
    "redutor_social_residencial": 10_000_000,   # R$ 100.000 por imóvel residencial novo vendido
    "redutor_social_lote": 3_000_000,           # R$ 30.000 por lote residencial
}

# O que a obra pode comprovar com cada tipo de gasto. "pendente" = ainda não classificado.
NATUREZAS = tuple(PARAMETROS_INSS["abatimento"])

# As destinações da tabela VAU do SERO: é por elas (com UF e mês) que a Receita dá o valor do m², não pelo padrão.
DESTINACOES = ("residencial_unifamiliar", "residencial_multifamiliar", "casa_popular", "conjunto_habitacional",
               "comercial", "edificio_garagens", "galpao")
# Casa de pessoa física: é onde a isenção da casa pequena vale (o fator social vale para toda obra de PF).
UNIFAMILIARES = frozenset({"residencial_unifamiliar", "casa_popular"})
RESIDENCIAIS = frozenset({"residencial_unifamiliar", "residencial_multifamiliar", "casa_popular",
                          "conjunto_habitacional"})


def vau_vigente(linhas: list[dict], ate: str) -> dict | None:
    """A linha da tabela VAU do mês mais recente até `ate` ("AAAA-MM"): a Receita publica o mês corrente durante o
    mês, e uma obra aferida hoje usa o VAU de hoje. `linhas`: as da UF e destinação da obra."""
    validas = [x for x in linhas if x["mes"] <= ate]
    return max(validas, key=lambda x: x["mes"]) if validas else None

TRANSICAO_IBS_CBS = {
    2026: "2026 é ano de teste: CBS 0,9% e IBS 0,1% destacados na nota e compensáveis, sem custo efetivo.",
    2027: "De 2027 a 2028 a CBS vale inteira e o IBS fica em 0,1%.",
    2029: "De 2029 a 2032 o IBS sobe por etapas (10%, 20%, 30%, 40% da alíquota) e ICMS/ISS caem na mesma proporção.",
    2033: "A partir de 2033 vale o regime pleno, que é o que esta estimativa calcula.",
}


def _pct(valor: int, pct: int) -> int:
    """valor × pct (centésimos de ponto), arredondado meio para cima: a mesma regra de `tipos.reais`."""
    return int((Decimal(valor) * pct / CEM).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt_pct(pct: int) -> str:
    return f"{fmt_qtd(pct * 10)}%"


def _m2(m: int) -> str:
    return f"{fmt_qtd(m)} m²"


def natureza_padrao(gasto: dict) -> str | None:
    """Gasto de pedido é material comprado: o pedido não paga mão de obra. Os outros a pessoa classifica."""
    if gasto.get("natureza_fiscal"):
        return gasto["natureza_fiscal"]
    if gasto.get("pedido_id"):
        return "material"
    return None


# --- INSS da obra ---

# Pagamentos que são mão de obra remunerada: tiram a isenção da casa pequena.
_MAO_DE_OBRA = frozenset({"folha", "mao_de_obra_nf", "mao_de_obra_sem_nf", "gps", "autonomo_mei"})
CATEGORIAS = tuple(PARAMETROS_INSS["categoria"])
INTERESSE_SOCIAL = frozenset({"casa_popular", "conjunto_habitacional"})


@dataclass
class ContaINSS:
    situacao: str                     # calculado | isenta | sem_dados
    motivos: list[str] = field(default_factory=list)     # por que não calculou (sem_dados) ou por que é isenta
    area_equivalente: int = 0         # área para cálculo (equivalência + redução das complementares), milésimos de m²
    custo_obra: int = 0               # COD: VAU × área para cálculo, centavos
    rmt: int = 0                      # mão de obra presumida não decadente, com todas as reduções
    fator_social: int = CEM
    percentual_rmt: int = 0           # do tipo de obra (já mista se o pré-moldado não chegou a 40%)
    percentual_categoria: int = CEM
    pre_moldado: str | None = None    # reduziu (−70%) | virou_mista | None (sem nota de pré-moldado)
    nao_decadente: int = CEM          # parte dos meses da obra ainda não decadente
    comprovado: int = 0               # o que abate da RMT: remunerações corrigidas + crédito do concreto
    comprovado_por_natureza: dict[str, int] = field(default_factory=dict)
    credito_concreto: int = 0
    credito_autonomo: int = 0         # abatido do débito (só a parte patronal de 20%)
    base: int = 0
    inss_sem_fator_ajuste: int = 0
    inss: int = 0
    # Fator de ajuste (art. 33): nao_se_aplica (PJ) | aplicado | atingido (falta confirmar a DCTFWeb) | nao_atingido
    fator_ajuste: str = "nao_se_aplica"
    fator_ajuste_minimo: int = 0      # percentual da RMT exigido (50% ou 70%)
    creditos_fator_ajuste: int = 0    # remunerações que contam para ele
    falta_fator_ajuste: int = 0       # quanto de remuneração declarada ainda falta para atingir
    # o que a obra deixaria de pagar se a mão de obra paga sem nota (ou com nota sem o CNO) fosse comprovada
    economia_com_notas: int = 0
    mao_de_obra_sem_nf: int = 0
    notas_sem_cno: int = 0
    selic_faltando: list[str] = field(default_factory=list)   # meses sem taxa na tabela (contou 0)
    avisos: list[str] = field(default_factory=list)
    linhas: list[str] = field(default_factory=list)


def _faixa(faixas: list[tuple[int | None, int]], area: int) -> int:
    for teto, pct in faixas:
        if teto is None or area <= teto:
            return pct
    raise AssertionError("a última faixa não tem teto")


def fator_social(area_total: int) -> int:
    return _faixa(PARAMETROS_INSS["fator_social"], area_total)


def equivalencia(destinacao: str | None, area_principal: int) -> int:
    faixas = PARAMETROS_INSS["equivalencia"].get(destinacao or "")
    return _faixa(faixas, area_principal) if faixas else CEM


def uso_concreto(uf: str | None, destinacao: str | None) -> int:
    """Anexo I: percentual do custo da obra que é concreto/argamassa usinada, na UF e destinação. 0 = sem tabela."""
    linha = _ANEXO_I.get((uf or "").upper())
    if not linha or destinacao not in DESTINACOES_ANEXO_I:
        return 0
    return linha[DESTINACOES_ANEXO_I.index(destinacao)]


def _mes_mais(mes: str, n: int) -> str:
    a, m = int(mes[:4]), int(mes[5:7]) - 1 + n
    return f"{a + m // 12:04d}-{m % 12 + 1:02d}"


def _meses(de: str, ate: str) -> list[str]:
    """Os meses "AAAA-MM" de `de` até `ate`, inclusive."""
    saida, m = [], de
    while m <= ate:
        saida.append(m)
        m = _mes_mais(m, 1)
    return saida


def correcao_selic(competencia: str, afericao: str, selic: dict[str, int]) -> tuple[int, list[str]]:
    """Art. 31: Selic acumulada do 2º mês depois da competência até o mês anterior ao da aferição, + 1% no mês dela.
    Paga no próprio mês ou no seguinte não corrige. Devolve (percentual em centésimos de ponto, meses sem taxa)."""
    if afericao <= _mes_mais(competencia, 1):
        return 0, []
    meses = _meses(_mes_mais(competencia, 2), _mes_mais(afericao, -1))
    return (sum(selic.get(m, 0) for m in meses) + PARAMETROS_INSS["selic_mes_da_afericao"],
            [m for m in meses if m not in selic])


def decadente(mes: str, afericao: str) -> bool:
    """CTN art. 173 I: o mês decai 5 anos contados de 1º de janeiro do ano seguinte (aferida em 2026, até 2020)."""
    return int(mes[:4]) <= int(afericao[:4]) - PARAMETROS_INSS["decadencia_anos"] - 1


def conta_inss(*, area_principal: int, area_complementar: int = 0, area_descoberta: int = 0, vau: int,
               material: str | None, proprietario: str | None, destinacao: str | None, padrao: str | None,
               gastos: list[dict], afericao: str, uf: str | None = None, categoria: str | None = None,
               finalidade: str | None = None, inicio: str | None = None, dctfweb_em_dia: bool | None = None,
               selic: dict[str, int] | None = None) -> ContaINSS:
    """INSS estimado se a obra fosse aferida no SERO no mês `afericao` ("AAAA-MM"), como o Manual do SERO faz:

    área para cálculo = principal × equivalência + complementar coberta × 50% + descoberta × 25%
    custo (COD) = VAU × área para cálculo
    RMT = COD × tipo de obra × categoria × destinação × fator social (PF) × pré-moldado × parte não decadente
    base = RMT − remunerações comprovadas (corrigidas pela Selic) − crédito do concreto usinado
    INSS = base × 36,8% − 20% do autônomo/MEI declarado; zero se a obra de PF atinge o fator de ajuste

    `vau` em centavos por m². `gastos`: os da obra (estornado não entra), com `valor`, `data_pagamento`, a
    `natureza_fiscal` e, na nota de mão de obra, `nf_cno` (a nota traz o CNO da obra). `selic`: {"AAAA-MM": taxa do
    mês em centésimos de ponto}.
    """
    p = PARAMETROS_INSS
    selic = selic or {}
    faltando = []
    if area_principal <= 0:
        faltando.append("a área construída da obra")
    if vau <= 0:
        faltando.append("a UF e a destinação da obra, para achar o VAU na tabela da Receita (ou o VAU à mão)")
    if faltando:
        return ContaINSS(situacao="sem_dados", motivos=[f"Falta informar {x}." for x in faltando])

    vivos = [g for g in gastos if not g.get("estornado")]
    area_total = area_principal + area_complementar + area_descoberta
    if (proprietario == "pf" and destinacao in UNIFAMILIARES and padrao == "baixo" and finalidade == "uso_proprio"
            and area_total <= p["isencao_area_max"]
            and not any(natureza_padrao(g) in _MAO_DE_OBRA for g in vivos)):
        return ContaINSS(situacao="isenta", motivos=[
            f"Casa de pessoa física para uso próprio, popular, com {_m2(area_total)} (até "
            f"{_m2(p['isencao_area_max'])}) e sem mão de obra paga: não há INSS da obra (art. 34 da IN 2.021), desde "
            f"que o dono não tenha outro imóvel."])

    categoria = categoria or "obra_nova"
    material = material or "alvenaria"
    avisos: list[str] = []

    # custo da obra por destinação
    eq_pct = equivalencia(destinacao, area_principal)
    eq = (_pct(area_principal, eq_pct) + _pct(area_complementar, p["area_complementar_coberta"])
          + _pct(area_descoberta, p["area_complementar_descoberta"]))
    cod = vau * eq // 1000

    # meses da obra e a parte não decadente
    meses_obra = _meses(inicio[:7], afericao) if inicio and inicio[:7] <= afericao else []
    nd = CEM
    if meses_obra:
        vivos_meses = [m for m in meses_obra if not decadente(m, afericao)]
        nd = len(vivos_meses) * CEM // len(meses_obra)

    # remunerações comprovadas, corrigidas pela Selic; decadente não abate (art. 31 §4)
    faltam_selic: set[str] = set()
    por_natureza: dict[str, int] = {}
    autonomo = sem_nf = nf_sem_cno = pre_moldado = 0
    usou_concreto = False
    for g in vivos:
        nat = natureza_padrao(g)
        comp = (g.get("data_pagamento") or f"{afericao}-01")[:7]
        corr, falt = correcao_selic(comp, afericao, selic)
        valor = _pct(g["valor"], CEM + corr)
        if nat == "concreto_usinado":
            usou_concreto = True
            continue
        if nat == "pre_moldado":
            pre_moldado += valor
            faltam_selic.update(falt)
            continue
        if nat == "mao_de_obra_sem_nf":
            sem_nf += g["valor"]
            continue
        if nat == "mao_de_obra_nf" and g.get("nf_cno") is not True:
            nf_sem_cno += g["valor"]
            continue
        if nat not in ("folha", "mao_de_obra_nf", "gps", "autonomo_mei") or decadente(comp, afericao):
            continue
        faltam_selic.update(falt)
        if nat == "gps":
            rem = int((Decimal(valor) * CEM / p["aliquota"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        else:
            rem = _pct(valor, p["abatimento"][nat])
        if nat == "autonomo_mei":
            autonomo += rem
        else:
            por_natureza[nat] = por_natureza.get(nat, 0) + rem

    # RMT
    social = destinacao in INTERESSE_SOCIAL
    tipo = material
    pm = None
    if pre_moldado and material == "alvenaria":
        if pre_moldado * CEM >= cod * p["pre_moldado_minimo"]:
            pm = "reduziu"
        else:
            pm, tipo = "virou_mista", "mista"
            avisos.append(f"As notas de pré-moldado ({fmt_reais(pre_moldado)}) não chegam a "
                          f"{_fmt_pct(p['pre_moldado_minimo'])} do custo da obra: a área é calculada como mista.")
    pct_rmt = p["percentual_rmt_interesse_social" if social else "percentual_rmt"][tipo]
    pct_cat = p["categoria"][categoria]
    pct_dest = p["destinacao"].get(destinacao or "", CEM)
    fs = fator_social(area_total) if proprietario == "pf" else CEM
    rmt = _pct(cod, pct_rmt)
    for fator in (pct_cat, pct_dest, fs, p["pre_moldado_fator"] if pm == "reduziu" else CEM, nd):
        rmt = _pct(rmt, fator)

    # crédito do concreto usinado (art. 32 §3)
    concreto = 0
    if usou_concreto:
        uso = uso_concreto(uf, destinacao)
        if uso:
            concreto = cod
            for fator in (uso, p["concreto_usinado_categoria"][categoria], p["concreto_usinado"], nd):
                concreto = _pct(concreto, fator)
        else:
            avisos.append("Sem a UF da obra não dá para calcular o crédito do concreto usinado (Anexo I da IN).")
    if concreto:
        por_natureza["concreto_usinado"] = concreto

    remuneracoes = sum(v for k, v in por_natureza.items() if k != "concreto_usinado")
    comprovado = remuneracoes + concreto
    base = max(0, rmt - comprovado)
    credito_autonomo = min(_pct(autonomo, p["aliquota_patronal"]), _pct(base, p["aliquota_patronal"]))
    inss_cheio = _pct(base, p["aliquota"]) - credito_autonomo

    # fator de ajuste (art. 33): só obra de PF
    fa, minimo, falta = "nao_se_aplica", 0, 0
    creditos_fa = remuneracoes + autonomo
    if proprietario == "pf":
        minimo = p["fator_ajuste_ate_area" if area_total <= p["fator_ajuste_area"] else "fator_ajuste_acima_area"]
        exigido = _pct(rmt, minimo)
        falta = max(0, exigido - creditos_fa)
        fa = "nao_atingido" if falta else ("aplicado" if dctfweb_em_dia else "atingido")
        if fa == "atingido":
            avisos.append("A remuneração declarada já atinge o fator de ajuste: com a DCTFWeb entregue em todos os "
                          "meses da obra, o INSS da aferição é zero. Confirme a DCTFWeb nos dados fiscais.")
    inss = 0 if fa == "aplicado" else inss_cheio

    # com nota (e o CNO da obra nela), a mesma mão de obra abateria 40%; o ganho para na base que ainda resta
    pct_nf = p["abatimento"]["mao_de_obra_nf"]
    economia = _pct(min(base, _pct(sem_nf + nf_sem_cno, pct_nf)), p["aliquota"]) if inss else 0
    if nf_sem_cno:
        avisos.append(f"{fmt_reais(nf_sem_cno)} em notas de mão de obra sem o CNO da obra confirmado: a Receita só "
                      f"liga a nota à obra com o CNO e o endereço dela na nota (art. 14 §2).")
    if proprietario == "pj" and any(natureza_padrao(g) == "mao_de_obra_nf" and not g.get("retencao") for g in vivos):
        avisos.append("Empresa que contrata empreitada retém 11% na nota (IN 2.110/2022, art. 110): há nota de mão "
                      "de obra sem a retenção informada.")
    selic_faltando = sorted(faltam_selic)
    if selic_faltando:
        avisos.append(f"Sem a Selic de {', '.join(selic_faltando)}: esses meses não corrigiram a remuneração.")

    linhas = [f"área para cálculo = {_m2(area_principal)} × {_fmt_pct(eq_pct)} (equivalência)"
              + (f" + {_m2(area_complementar)} × {_fmt_pct(p['area_complementar_coberta'])}"
                 if area_complementar else "")
              + (f" + {_m2(area_descoberta)} × {_fmt_pct(p['area_complementar_descoberta'])}"
                 if area_descoberta else "")
              + f" = {_m2(eq)}",
              f"custo da obra = VAU {fmt_reais(vau)}/m² × {_m2(eq)} = {fmt_reais(cod)}"]
    redutores = [f"{_fmt_pct(pct_rmt)} ({tipo})"]
    if pct_cat != CEM:
        redutores.append(f"{_fmt_pct(pct_cat)} ({categoria})")
    if pct_dest != CEM:
        redutores.append(f"{_fmt_pct(pct_dest)} (edifício garagem)")
    if fs != CEM:
        redutores.append(f"fator social {_fmt_pct(fs)}")
    if pm == "reduziu":
        redutores.append(f"{_fmt_pct(p['pre_moldado_fator'])} (pré-moldado)")
    if nd != CEM:
        redutores.append(f"{_fmt_pct(nd)} não decadente")
    linhas.append(f"mão de obra presumida (RMT) = {fmt_reais(cod)} × {' × '.join(redutores)} = {fmt_reais(rmt)}")
    if concreto:
        linhas.append(f"crédito do concreto usinado = {fmt_reais(cod)} × {_fmt_pct(uso_concreto(uf, destinacao))} "
                      f"(Anexo I, {uf}) × {_fmt_pct(p['concreto_usinado'])} = {fmt_reais(concreto)}")
    linhas += [f"remuneração comprovada (corrigida pela Selic) = {fmt_reais(remuneracoes)}",
               f"base = {fmt_reais(rmt)} − {fmt_reais(comprovado)} = {fmt_reais(base)}",
               f"INSS = {fmt_reais(base)} × {_fmt_pct(p['aliquota'])}"
               + (f" − autônomo/MEI {fmt_reais(credito_autonomo)}" if credito_autonomo else "")
               + f" = {fmt_reais(inss_cheio)}"]
    if fa == "aplicado":
        linhas.append(f"fator de ajuste: remuneração declarada {fmt_reais(creditos_fa)} ≥ {_fmt_pct(minimo)} da RMT e "
                      f"DCTFWeb em dia → INSS a pagar = R$ 0,00")
    elif fa == "nao_atingido":
        linhas.append(f"fator de ajuste: faltam {fmt_reais(falta)} de remuneração declarada para chegar a "
                      f"{_fmt_pct(minimo)} da RMT e zerar o INSS")
    if economia:
        linhas.append(f"{fmt_reais(sem_nf + nf_sem_cno)} de mão de obra sem nota (ou sem o CNO na nota): comprovada, o "
                      f"INSS cairia {fmt_reais(economia)}")
    return ContaINSS(situacao="calculado", area_equivalente=eq, custo_obra=cod, rmt=rmt, fator_social=fs,
                     percentual_rmt=pct_rmt, percentual_categoria=pct_cat, pre_moldado=pm, nao_decadente=nd,
                     comprovado=comprovado, comprovado_por_natureza=por_natureza, credito_concreto=concreto,
                     credito_autonomo=credito_autonomo, base=base, inss_sem_fator_ajuste=inss_cheio, inss=inss,
                     fator_ajuste=fa, fator_ajuste_minimo=minimo, creditos_fator_ajuste=creditos_fa,
                     falta_fator_ajuste=falta, economia_com_notas=economia, mao_de_obra_sem_nf=sem_nf,
                     notas_sem_cno=nf_sem_cno, selic_faltando=selic_faltando, avisos=avisos, linhas=linhas)


# --- IBS/CBS ---

@dataclass
class ContaIBSCBS:
    situacao: str                     # calculado | nao_incide | sem_dados
    motivos: list[str] = field(default_factory=list)
    aliquota_efetiva: int = 0
    valor_operacao: int = 0
    redutor_ajuste: int = 0
    redutor_social: int = 0
    base: int = 0
    debito: int = 0
    creditos: int = 0
    a_pagar: int = 0
    transicao: str = ""
    linhas: list[str] = field(default_factory=list)


def transicao_ibs_cbs(ano: int) -> str:
    chave = max((a for a in TRANSICAO_IBS_CBS if a <= ano), default=None)
    return TRANSICAO_IBS_CBS[chave] if chave else "Antes de 2026 ainda valem PIS/Cofins, ICMS e ISS."


def conta_ibs_cbs(*, finalidade: str | None, valor_operacao: int, valor_terreno: int = 0, unidades: int = 0,
                  residencial: bool = True, creditos: int = 0, ano: int) -> ContaIBSCBS:
    """IBS + CBS estimados no regime pleno (2033), com a redução de imóveis.

    venda de imóvel novo: base = valor de venda − redutor de ajuste (terreno) − redutor social (por unidade
    residencial); prestação de serviço de construção (empreitada): base = valor do contrato. Débito = base × alíquota
    reduzida; a pagar = débito − créditos (IBS/CBS das notas de compra da obra).
    Obra para uso próprio não é operação: não incide.
    """
    p = PARAMETROS_IBS_CBS
    transicao = transicao_ibs_cbs(ano)
    if finalidade == "uso_proprio":
        return ContaIBSCBS(situacao="nao_incide", transicao=transicao, creditos=creditos, motivos=[
            "Obra para uso próprio não é venda nem prestação de serviço: não há IBS/CBS sobre ela. O imposto já vem "
            "embutido no preço do material e do serviço comprados."])
    faltando = []
    if finalidade not in ("venda", "prestacao_servico"):
        faltando.append("a finalidade da obra (venda, prestação de serviço ou uso próprio)")
    if valor_operacao <= 0:
        faltando.append("o valor do contrato ou da venda")
    if faltando:
        return ContaIBSCBS(situacao="sem_dados", transicao=transicao, creditos=creditos,
                           motivos=[f"Falta informar {x}." for x in faltando])

    efetiva = _pct(p["aliquota_referencia"], CEM - p["reducao_imoveis"])
    ajuste = social = 0
    if finalidade == "venda":
        ajuste = min(valor_terreno, valor_operacao)
        if residencial:
            social = min(unidades * p["redutor_social_residencial"], valor_operacao - ajuste)
    base = valor_operacao - ajuste - social
    debito = _pct(base, efetiva)
    a_pagar = max(0, debito - creditos)

    if finalidade == "venda":
        linhas = [f"base = venda {fmt_reais(valor_operacao)} − redutor de ajuste (terreno) {fmt_reais(ajuste)}"
                  f" − redutor social {fmt_reais(social)} = {fmt_reais(base)}"]
    else:
        linhas = [f"base = contrato {fmt_reais(valor_operacao)}"]
    linhas += [f"alíquota = {_fmt_pct(p['aliquota_referencia'])} × (100% − redução de imóveis "
               f"{_fmt_pct(p['reducao_imoveis'])}) = {_fmt_pct(efetiva)}",
               f"débito = {fmt_reais(base)} × {_fmt_pct(efetiva)} = {fmt_reais(debito)}",
               f"a pagar = {fmt_reais(debito)} − créditos das compras {fmt_reais(creditos)} = {fmt_reais(a_pagar)}"]
    return ContaIBSCBS(situacao="calculado", aliquota_efetiva=efetiva, valor_operacao=valor_operacao,
                       redutor_ajuste=ajuste, redutor_social=social, base=base, debito=debito, creditos=creditos,
                       a_pagar=a_pagar, transicao=transicao, linhas=linhas)
