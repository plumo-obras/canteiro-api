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
obra que andou a 30% do planejado leva ~3 meses para cada mês de plano. O que se estica é o TRABALHO que falta, não o
calendário: cada mês do plano dura o quanto ainda falta dele (10% de um mês = 10% do tempo), e o atrasado entra em fila
a partir de hoje. Assim um resto de R$ 10 nas Preliminares não empurra o fim da obra. Sem medição só há o primeiro (o
gasto não mede ritmo: material comprado adiantado pareceria obra adiantada).

No teste com uma obra pública real (creche FNDE, pagamentos do TCE-RS, 4 pontos da obra), o primeiro previa sair em 6
meses de 2 a 15 vezes o que saiu; o segundo errou bem menos (o mais perto: 545 mil previstos, 510 mil reais), e o
término real ficou entre os dois em 3 dos 4 pontos (no 4º, 2 meses depois do "no ritmo").

**Pago** = os gastos confirmados (avulsos e pagamentos de pedido) pelo mês do pagamento: é dinheiro que saiu da conta.
O realizado conta o pedido pela entrega; a diferença (entregue e sem pagamento registrado) também é devida agora e cai
no mês corrente. Pago adiantado (pagou antes de receber) desconta dos primeiros meses, sem deixar mês negativo.
"""
import math
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

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


def repartir(valor: int, pesos: dict[int, int | Fraction]) -> dict[int, int]:
    """`valor` pelos meses na proporção dos pesos (inteiros ou frações). Arredonda o acumulado, como
    `cronograma.valores_mes`: a soma fecha no centavo."""
    total = sum(pesos.values())
    saida, acum, antes = {}, 0, 0
    for k in sorted(pesos):
        acum += pesos[k]
        agora = math.floor(Fraction(valor) * acum / total + Fraction(1, 2))
        saida[k] = agora - antes
        antes = agora
    return saida


RITMO_MINIMO = 1000                      # 10% do planejado: abaixo disso a obra está parada, e esticar não informa


def _fila_no_ritmo(resto_mes: dict[int, int], cheio_mes: dict[int, int], hoje: int,
                   ritmo: int) -> dict[int, tuple[Fraction, Fraction]]:
    """O intervalo do calendário (em meses, fracionário) que cada mês do plano ocupa no ritmo atual. O mês k dura
    (o que falta dele ÷ o que ele tinha) × 10000/ritmo e começa quando o anterior acaba, nunca antes de hoje nem do
    próprio mês (já empurrado pelo atraso). Mês sem resto não ocupa tempo; intervalo vazio entre hoje e um mês futuro
    não é esticado (o atraso já mede a lentidão)."""
    saida, cursor = {}, Fraction(hoje)
    for k in sorted(resto_mes):
        if resto_mes[k] <= 0:
            continue
        fracao = Fraction(resto_mes[k], cheio_mes[k]) if cheio_mes.get(k) else Fraction(1)
        ini = max(cursor, Fraction(k))
        cursor = ini + fracao * CEM / ritmo
        saida[k] = (ini, cursor)
    return saida


def _no_calendario(valores: dict[int, int], intervalos: dict[int, tuple[Fraction, Fraction]]) -> dict[int, Fraction]:
    """Espalha o valor de cada mês do plano pelos meses do calendário que o intervalo dele cobre, pelo tempo."""
    pesos: dict[int, Fraction] = {}
    for k, v in valores.items():
        if not v:
            continue
        a, b = intervalos[k]
        m = math.floor(a)
        while m < b:
            parte = min(b, m + 1) - max(a, Fraction(m))
            if parte > 0:
                pesos[m] = pesos.get(m, 0) + v * parte / (b - a)
            m += 1
    return pesos


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
    cheio: dict[int, int] = {}                          # o que o plano previa por mês (centavos × CEM), obra toda
    for e in etapas:
        falta = max(0, e.atual - e.realizado)
        # sem medição, o gasto é o avanço; piso, para "quase tudo gasto" não arredondar para 100% e perder o mês
        executado = e.executado if com_medicao else (min(CEM, e.realizado * CEM // e.atual) if e.atual else CEM)
        executado = executado or 0
        devido = 0
        if com_medicao:
            devido = min(falta, max(0, (e.atual * executado + CEM // 2) // CEM - e.realizado))
        resto = falta - devido
        devido_total += devido
        plano: dict[int, int] = {}
        if e.percentuais and ini is not None:
            for k, p in enumerate(e.percentuais):
                if p:
                    cheio[ini + k + desloca] = cheio.get(ini + k + desloca, 0) + e.atual * p
        if resto and e.percentuais and ini is not None:
            for k, w in enumerate(restante_do_plano(e.percentuais, executado)):
                if w:
                    plano[ini + k + desloca] = plano.get(ini + k + desloca, 0) + w
            if not plano and any(e.percentuais):
                # o plano já foi todo consumido e ainda sobra (medido 100% sem gasto que feche): no último mês dele
                ultimo = max(k for k, p in enumerate(e.percentuais) if p)
                plano = {ini + ultimo + desloca: 1}
        fora = bool(resto) and not plano
        if fora:
            sem_mes += resto
        elif resto:
            restos.append((resto, plano))
        linhas.append(EtapaDesembolso(etapa=e.etapa, descricao=e.descricao, atual=e.atual, realizado=e.realizado,
                                      executado_pct=executado, falta=falta, devido_agora=devido,
                                      sem_cronograma=fora))
    pelo_plano, no_ritmo = cenarios
    por_mes = [(resto, repartir(resto, plano)) for resto, plano in restos]   # o resto de cada etapa por mês do plano
    resto_obra: dict[int, int] = {}
    for _, valores in por_mes:
        for k, v in valores.items():
            resto_obra[k] = resto_obra.get(k, 0) + v
    intervalos = _fila_no_ritmo(resto_obra, {k: v // CEM for k, v in cheio.items()}, hoje, ritmo)
    for resto, valores in por_mes:
        # pelo cronograma, o que já devia ter saído cai agora; no ritmo, a fila
        agora: dict[int, int] = {}
        for k, v in valores.items():
            agora[max(hoje, k)] = agora.get(max(hoje, k), 0) + v
        fila = agora if ritmo >= CEM else repartir(resto, _no_calendario(valores, intervalos))
        for cen, distribuido in ((pelo_plano, agora), (no_ritmo, fila)):
            for m, v in distribuido.items():
                cen[m] = cen.get(m, 0) + v

    pago_total = sum(pago_por_mes.values())
    # Negativo = pagou mais do que recebeu (adiantamento, ou frete e juros pagos além do preço do pedido): vira crédito
    # que desconta dos primeiros meses. Crédito maior que tudo o que falta não aparece em lugar nenhum: a série não
    # fica negativa, e então `pago + falta` passa de `custo_final` (o contrato avisa).
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
                                desvio=custo_final - orcado,
                                entregue_sem_pagamento=versoes.entregue_a_pagar(realizado_total, pago_total),
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
