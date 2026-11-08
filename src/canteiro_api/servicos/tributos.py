"""Tributos da obra (estimativa): INSS da aferição e IBS/CBS, com a conta pronta de `canteiro_dominio.tributos`.

Só leitura. O que muda o resultado são os dados fiscais da obra (PATCH /obras/{id}) e a natureza fiscal de cada gasto
(PATCH /gastos/{id}/fiscal), ambos com registro e Desfazer.
"""
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from canteiro_dominio.gastos import soma_nos_gastos_avulsos
from canteiro_dominio.tributos import (
    PARAMETROS_IBS_CBS,
    PARAMETROS_INSS,
    RESIDENCIAIS,
    conta_ibs_cbs,
    conta_inss,
    natureza_padrao,
)

from .. import selic as tabela_selic
from .. import vau as tabela_vau
from ..auth import UsuarioAtual
from ..nucleo import obra_do_tenant
from .versoes import gastos_da_obra

MAX_LISTA = 20
AVISO = ("Estimativa feita com os dados da obra e os percentuais em `parametros`. O valor oficial sai da aferição na "
         "Receita (SERO/DCTFWeb) e da apuração do IBS/CBS; confira com o contador antes de pagar ou declarar.")
_DESTINACAO_DA_TIPOLOGIA = {"casa": "residencial_unifamiliar", "sobrado": "residencial_unifamiliar",
                            "predio": "residencial_multifamiliar", "comercial": "comercial", "galpao": "galpao"}


def _resumo(g: dict) -> dict:
    return {k: g.get(k) for k in ("id", "valor", "data_pagamento", "favorecido", "descricao", "nf_cno")} | {
        "natureza_fiscal": natureza_padrao(g)}


def _sem_comprovacao(g: dict) -> bool:
    """Mão de obra que a Receita não liga à obra: paga sem nota, ou com nota sem o CNO da obra."""
    nat = natureza_padrao(g)
    return nat == "mao_de_obra_sem_nf" or (nat == "mao_de_obra_nf" and g.get("nf_cno") is not True)


async def tributos_da_obra(arm, u: UsuarioAtual, obra_id: str) -> dict:
    o = await obra_do_tenant(arm, u.tenant_id, obra_id)
    gastos = await gastos_da_obra(arm, u.tenant_id, obra_id)
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo"))
    afericao = f"{hoje:%Y-%m}"
    categoria = o.get("categoria") or ("reforma" if o.get("tipologia") == "reforma" else "obra_nova")
    # Selic só dos anos em que houve pagamento (a correção vai da competência até hoje)
    competencias = [g["data_pagamento"][:7] for g in gastos if g.get("data_pagamento")]
    selic = await tabela_selic.tabela(arm, min(competencias, default=afericao), afericao)
    # sem destinação informada, a tipologia do onboarding já diz quase sempre (casa e sobrado = unifamiliar)
    destinacao = o.get("destinacao") or _DESTINACAO_DA_TIPOLOGIA.get(o.get("tipologia") or "")
    # VAU à mão vence; sem ele, o vigente da tabela do SERO para a UF e a destinação (aferida hoje, usa o de hoje)
    if o.get("vau"):
        vau, referencia, fonte = o["vau"], o.get("vau_referencia"), "informado"
    elif (linha := await tabela_vau.vigente(arm, o.get("uf", ""), destinacao or "", f"{hoje:%Y-%m}")):
        vau, referencia, fonte = linha["vau"], linha["mes"], "tabela_sero"
    else:
        vau, referencia, fonte = 0, None, None
    inss = conta_inss(area_principal=o.get("area_construida", 0), area_complementar=o.get("area_complementar", 0),
                      area_descoberta=o.get("area_descoberta", 0), vau=vau, material=o.get("material"),
                      proprietario=o.get("proprietario"), destinacao=destinacao, padrao=o.get("padrao"),
                      gastos=gastos, afericao=afericao, uf=o.get("uf"), categoria=categoria,
                      finalidade=o.get("finalidade"), inicio=o.get("inicio"),
                      dctfweb_em_dia=o.get("dctfweb_em_dia"), selic=selic)
    ibs = conta_ibs_cbs(finalidade=o.get("finalidade"), valor_operacao=o.get("verba", 0),
                        valor_terreno=o.get("valor_terreno", 0), unidades=o.get("unidades", 0),
                        residencial=destinacao in RESIDENCIAIS,
                        creditos=sum(g.get("ibs_cbs", 0) for g in gastos),
                        ano=hoje.year)
    avulsos = [g for g in gastos if soma_nos_gastos_avulsos(g)]
    pendentes = sorted((g for g in avulsos if natureza_padrao(g) is None), key=lambda g: -g["valor"])
    sem_nota = sorted((g for g in gastos if not g.get("estornado") and _sem_comprovacao(g)),
                      key=lambda g: -g["valor"])
    return {"obra_id": obra_id, "cno": o.get("cno", ""), "vau_referencia": referencia, "vau_usado": vau,
            "vau_fonte": fonte, "destinacao": destinacao, "categoria": categoria, "afericao": afericao,
            "selic_ate": max(selic, default=None),
            "inss": asdict(inss), "ibs_cbs": asdict(ibs),
            "a_classificar": len(pendentes), "valor_a_classificar": sum(g["valor"] for g in pendentes),
            "sem_nota": [_resumo(g) for g in sem_nota[:MAX_LISTA]],
            "pendentes": [_resumo(g) for g in pendentes[:MAX_LISTA]],
            "parametros": {"inss": PARAMETROS_INSS, "ibs_cbs": PARAMETROS_IBS_CBS}, "aviso": AVISO}
