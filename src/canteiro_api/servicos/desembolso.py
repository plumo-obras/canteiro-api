"""Projeção de desembolso: quanto já saiu, mês a mês, e quanto ainda vai sair até o fim da obra.

É a planilha que o engenheiro manda ao dono na reta final ("previsões até o fim da obra"). Por etapa:

- **O que falta** = orçado atual − realizado, nunca negativo. A projeção não inventa economia nem estouro por
  tendência: etapa que já gastou mais que o orçado mostra o estouro que já houve, e o resto custa o orçamento. Material
  comprado antes de executar (o aço do mês que vem) é comum em obra, e a conta de "custo real + restante pelo
  orçamento" o leria como estouro.
- **Quando sai:** com medição, o executado e ainda não pago (orçado × % medido − realizado) é dívida de agora e cai no
  mês corrente; o resto segue a parte do cronograma que a medição ainda não consumiu, empurrada pelo atraso medido (o
  mesmo do prazo projetado), e nada antes do mês corrente. Sem medição, o realizado faz o papel do avanço (o que se
  gastou conta como feito), e o que o cronograma previa para meses que já passaram cai no mês corrente.
- Etapa fora do cronograma: o que falta dela fica em `sem_mes` (sabe-se quanto, não quando).

Dois cenários, porque nenhum acerta sozinho. **Pelo cronograma** supõe que, empurrado o atraso, o resto anda no ritmo
planejado. **No ritmo atual** estica o resto pelo ritmo medido até aqui (% medido ÷ % previsto na última medição): uma
obra que andou a 30% do planejado leva ~3 meses para cada mês de plano. No teste com uma obra pública real (creche
FNDE, pagamentos do TCE-RS), o primeiro previa sair em 12 meses o dobro do que saiu; o segundo ficou perto em 3 de 4
pontos. Sem medição só há o primeiro (o gasto não mede ritmo: material comprado adiantado pareceria obra adiantada).

**Pago** = os gastos confirmados (avulsos e pagamentos de pedido) pelo mês do pagamento: é dinheiro que saiu da conta.
O realizado conta o pedido pela entrega; a diferença (entregue e sem pagamento registrado) também é devida agora e cai
no mês corrente. Pago adiantado (pagou antes de receber) desconta dos primeiros meses, sem deixar mês negativo.
"""
from dataclasses import dataclass, field
from datetime import date

from ..esquemas import DesembolsoObra, EtapaDesembolso, MesDesembolso, TotaisDesembolso
from . import cronograma as cron
from . import versoes
from .versoes import CEM, pct


@dataclass
class EtapaBase:
    etapa: str
    descricao: str
    atual: int                              # orçado atual (contrato + aditivos), centavos
    realizado: int
    percentuais: list[int] = field(default_factory=list)   # o plano vigente; vazio = fora do cronograma
    executado: int | None = None            # % medido acumulado (centésimos); None = obra sem medição


def restante_do_plano(percentuais: list[int], executado: int) -> list[int]:
    """O plano da etapa menos o que já foi executado, consumido do começo: 30/40/30 com 50% feito sobra 0/20/30."""
    saida, acum = [], 0
    for p in percentuais:
        feito = min(p, max(0, executado - acum))
        acum += p
        saida.append(p - feito)
    return saida


def repartir(valor: int, pesos: dict[int, int]) -> dict[int, int]:
    """`valor` pelos meses na proporção dos pesos. Arredonda o acumulado, como `cronograma.valores_mes`: a soma fecha
    no centavo."""
    total = sum(pesos.values())
    saida, acum, antes = {}, 0, 0
    for k in sorted(pesos):
        acum += pesos[k]
        agora = (valor * acum + total // 2) // total
        saida[k] = agora - antes
        antes = agora
    return saida


RITMO_MINIMO = 1000                      # 10% do planejado: abaixo disso a obra está parada, e esticar não informa


def _espalhar(pesos_plano: dict[int, int], hoje: int, ritmo: int, origem: int) -> dict[int, int]:
    """Leva o peso de cada mês do plano (já empurrado pelo atraso) para o calendário. No ritmo do plano (10000), o
    mês k cai em k, nunca antes de hoje: o que já devia ter saído cai agora. No ritmo r, o resto do plano se estica por
    10000/r a partir de hoje, na ordem do plano: o mês k ocupa [hoje + (k − origem)·10000/r, + 10000/r) e o peso se
    divide pelos meses do calendário que esse intervalo cobre. `origem` = o primeiro mês com resto na obra toda (ou
    hoje, se for depois); com ela, o atrasado entra em fila em vez de se empilhar no mês corrente, e as etapas mantêm
    a sequência entre si."""
    saida: dict[int, int] = {}
    for k, w in pesos_plano.items():
        if ritmo >= CEM:
            m = max(hoje, k)
            saida[m] = saida.get(m, 0) + w * CEM
            continue
        # em décimos de milésimo de mês, para ficar em inteiros: a = início, b = fim do intervalo
        a = hoje * CEM + (k - origem) * CEM * CEM // ritmo
        b = a + CEM * CEM // ritmo
        m = a // CEM
        while m * CEM < b:
            parte = min(b, (m + 1) * CEM) - max(a, m * CEM)
            saida[m] = saida.get(m, 0) + w * parte * ritmo // CEM    # w·CEM no total do intervalo
            m += 1
    return saida


def projetar(etapas: list[EtapaBase], *, inicio: str | None, atraso: int, mes_atual: str,
             pago_por_mes: dict[str, int], previsto_por_mes: dict[str, int], realizado_total: int,
             mes_referencia: str | None = None, ritmo: int = CEM) -> DesembolsoObra:
    """A conta inteira, sem banco: entra o que a API já sabe (orçamento, cronograma, medição, gastos), sai a série.
    `ritmo` = % medido ÷ % previsto na última medição (centésimos; 10000 = no ritmo do plano)."""
    hoje = cron._mes_idx(mes_atual)
    ini = cron._mes_idx(inicio) if inicio else None
    com_medicao = mes_referencia is not None
    desloca = max(0, atraso) if com_medicao else 0     # obra adiantada não puxa desembolso para antes
    ritmo = max(RITMO_MINIMO, min(CEM, ritmo)) if com_medicao else CEM
    cenarios: list[dict[int, int]] = [{}, {}]          # pelo cronograma, no ritmo atual
    sem_mes = devido_total = 0
    linhas = []
    restos = []
    for e in etapas:
        falta = max(0, e.atual - e.realizado)
        executado = e.executado if com_medicao else (min(CEM, pct(e.realizado, e.atual)) if e.atual else CEM)
        executado = executado or 0
        devido = 0
        if com_medicao:
            devido = min(falta, max(0, (e.atual * executado + CEM // 2) // CEM - e.realizado))
        resto = falta - devido
        devido_total += devido
        plano: dict[int, int] = {}
        if resto and e.percentuais and ini is not None:
            for k, w in enumerate(restante_do_plano(e.percentuais, executado)):
                if w:
                    plano[ini + k + desloca] = plano.get(ini + k + desloca, 0) + w
        fora = bool(resto) and not plano
        if fora:
            sem_mes += resto
        elif resto:
            restos.append((resto, plano))
        linhas.append(EtapaDesembolso(etapa=e.etapa, descricao=e.descricao, atual=e.atual, realizado=e.realizado,
                                      executado_pct=executado, falta=falta, devido_agora=devido,
                                      sem_cronograma=fora))
    origem = min([hoje] + [k for _, plano in restos for k in plano])
    for resto, plano in restos:
        for cen, r in zip(cenarios, (CEM, ritmo), strict=True):
            for m, v in repartir(resto, _espalhar(plano, hoje, r, origem)).items():
                cen[m] = cen.get(m, 0) + v

    pago_total = sum(pago_por_mes.values())
    entregue_sem_pagamento = realizado_total - pago_total
    for cen in cenarios:
        agora = devido_total + max(0, entregue_sem_pagamento)
        if agora:
            cen[hoje] = cen.get(hoje, 0) + agora
        credito = max(0, -entregue_sem_pagamento)
        for m in sorted(cen):
            usado = min(credito, cen[m])
            cen[m] -= usado
            credito -= usado

    def ultimo_com_valor(cen):
        meses = [m for m, v in cen.items() if v]
        return max(meses) if meses else None

    fins = [ultimo_com_valor(c) for c in cenarios]
    idx_pagos = [cron._mes_idx(m) for m, v in pago_por_mes.items() if v]
    idx_prev = [cron._mes_idx(m) for m in previsto_por_mes]
    primeiro = min([hoje] + idx_pagos + idx_prev)
    ultimo = max([hoje] + idx_pagos + idx_prev + [f for f in fins if f is not None])
    pelo_plano, no_ritmo = cenarios
    meses = [MesDesembolso(mes=cron._mes(k), pago=pago_por_mes.get(cron._mes(k), 0),
                           previsto=previsto_por_mes.get(cron._mes(k), 0), projetado=pelo_plano.get(k, 0),
                           projetado_no_ritmo=no_ritmo.get(k, 0))
             for k in range(primeiro, ultimo + 1)]
    orcado = sum(e.atual for e in etapas)
    custo_final = realizado_total + sum(x.falta for x in linhas)
    base = "medicao" if com_medicao else ("gastos" if ini is not None else "sem_cronograma")
    return DesembolsoObra(
        mes_atual=mes_atual, base=base, mes_referencia=mes_referencia, atraso_meses=desloca, ritmo_pct=ritmo,
        termino_projetado=cron._mes(fins[0]) if fins[0] is not None else None,
        termino_no_ritmo=cron._mes(fins[1]) if fins[1] is not None else None, meses=meses, etapas=linhas,
        totais=TotaisDesembolso(orcado=orcado, realizado=realizado_total, pago=pago_total,
                                falta=sum(pelo_plano.values()) + sem_mes, custo_final=custo_final,
                                desvio=custo_final - orcado, entregue_sem_pagamento=max(0, entregue_sem_pagamento),
                                sem_mes=sem_mes))


async def desembolso_da_obra(arm, tenant_id: str, obra_id: str, hoje: date) -> DesembolsoObra:
    o = await versoes.orcamento_da_obra(arm, tenant_id, obra_id)
    c = await cron.cronograma_da_obra(arm, tenant_id, obra_id, orcamento=o)
    realizado = {e.etapa: e.realizado for e in o.etapas if e.etapa}
    ref = c.prazo.mes_referencia
    etapas = [EtapaBase(etapa=e.etapa, descricao=e.descricao, atual=e.atual, realizado=realizado.get(e.etapa, 0),
                        percentuais=e.percentuais if any(e.percentuais) else [],
                        executado=e.medido_acumulado if ref else None)
              for e in c.etapas]
    pago: dict[str, int] = {}
    for g in await versoes.gastos_da_obra(arm, tenant_id, obra_id):
        pago[g["data_pagamento"][:7]] = pago.get(g["data_pagamento"][:7], 0) + g["valor"]
    p = c.prazo
    ritmo = pct(p.pct_medido, p.pct_previsto_na_medicao) if ref and p.pct_previsto_na_medicao else CEM
    return projetar(etapas, inicio=c.inicio, atraso=p.atraso_meses or 0, mes_atual=hoje.isoformat()[:7],
                    pago_por_mes=pago, previsto_por_mes={m.mes: m.previsto for m in c.mensal},
                    realizado_total=o.totais.realizado, mes_referencia=ref, ritmo=ritmo)
