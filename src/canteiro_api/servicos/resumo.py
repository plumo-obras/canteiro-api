"""Resumo de cada obra para a página inicial. Lê o tenant inteiro coleção por coleção e soma aqui: agregação no
banco não é portável para o Firestore, e uma consulta por coleção custa menos que uma por obra."""
import re
from datetime import date, timedelta

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import StatusPedido, StatusProposta
from canteiro_dominio.regras import A_CAMINHO, falta_na_linha

from ..esquemas import (
    EntregaResumo,
    EtapaAtrasada,
    EtapaEstourada,
    EtapaNoMes,
    FaltaResumo,
    ItemEntrega,
    ResumoAlmoxarifado,
    ResumoCronograma,
    ResumoDaObra,
    ResumoEntregas,
    ResumoFinanceiro,
    ResumoObra,
    ResumoPendencias,
)
from ..nucleo import obra_do_tenant
from . import cronograma, estoque, versoes


async def resumo_obras(arm, tenant_id: str) -> list[ResumoObra]:
    obras = await arm.consultar(Consulta("obras", tenant_id, ordem=[("nome", "asc")]))
    if not obras:
        return []
    insumos = estoque.ativos(await arm.consultar(Consulta("insumos", tenant_id)))
    saldos = {s["id"]: s["saldo"] for s in await arm.consultar(Consulta("saldos", tenant_id))}
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("status", "in", estoque.PEDIDOS_ABERTOS)]))
    pendentes = await arm.consultar(Consulta("propostas", tenant_id,
                                             [Filtro("status", "==", StatusProposta.PENDENTE.value)]))
    # o prazo projetado depende do orçamento e das medições: só se calcula para a obra que tem cronograma montado
    com_cronograma = {c["obra_id"] for c in await arm.consultar(Consulta("cronogramas", tenant_id)) if c.get("etapas")}

    def por_obra(docs: list[dict]) -> dict[str, list[dict]]:
        g: dict[str, list[dict]] = {}
        for d in docs:
            g.setdefault(d["obra_id"], []).append(d)
        return g

    insumos_de, pedidos_de, pendentes_de = por_obra(insumos), por_obra(pedidos), por_obra(pendentes)
    saida = []
    for o in obras:
        ped = pedidos_de.get(o["id"], [])
        contas = estoque.com_conta(insumos_de.get(o["id"], []), saldos, ped)
        # a última atividade é a entrada mais nova do registro: uma leitura de 1 documento por obra
        ultima = await arm.consultar(Consulta("registro", tenant_id, [Filtro("obra_id", "==", o["id"])],
                                              [("criado_em", "desc")], limite=1))
        saida.append(ResumoObra(
            obra=o,
            pendencias=len(pendentes_de.get(o["id"], [])),
            falta_comprar=sum(1 for c in contas if c["conta"]["comprar"] > 0),
            pedidos_abertos=len(ped),
            valor_pedidos_abertos=sum(estoque.valor_em_aberto(p) for p in ped),
            nf_para_conferir=sum(1 for p in ped if p["status"] == StatusPedido.RECEBIDO.value),
            ultima_atividade=ultima[0]["criado_em"] if ultima else None,
            prazo=(await cronograma.cronograma_da_obra(arm, tenant_id, o["id"])).prazo
            if o["id"] in com_cronograma else None))
    return saida


_DIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _mes_idx(mes: str) -> int:
    a, m = mes.split("-")[:2]
    return int(a) * 12 + int(m) - 1


def _mes(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _resumo_cronograma(c, hoje: date) -> ResumoCronograma:
    mes = hoje.isoformat()[:7]
    if not c.inicio:
        return ResumoCronograma(cadastrado=False, mes_corrente=mes, termino_contratual=c.prazo.termino_contratual)
    k = _mes_idx(mes) - _mes_idx(c.inicio)
    p = c.prazo
    r = ResumoCronograma(
        cadastrado=True, mes_corrente=mes, termino_contratual=p.termino_contratual,
        termino_projetado=p.termino_projetado, termino_original=p.termino_base, atraso_decimos=p.atraso_decimos,
        atraso_base_decimos=p.atraso_base_decimos, revisoes=c.revisoes, mes_ultima_medicao=p.mes_referencia,
        medicoes=len(c.medicoes), pct_previsto_na_medicao=p.pct_previsto_na_medicao, pct_medido=p.pct_medido)
    if 0 <= k < len(c.mensal):
        r.previsto_no_mes, r.pct_previsto_acumulado_no_mes = c.mensal[k].previsto, c.mensal[k].pct_previsto_acumulado

    def primeiro_mes(e) -> int | None:
        return next((j for j, x in enumerate(e.percentuais) if x > 0), None)

    def comecou(e) -> bool:
        # Pela medição, não só pelo cronograma: na EMEI as Fundações tinham 4,51% medidos (adiantadas) com o primeiro
        # % previsto só em dezembro, e o resumo anunciava "em dezembro começam as Fundações".
        return e.medido_acumulado > 0 or e.situacao not in (None, "nao_comecou")

    def no_mes(e, j: int) -> EtapaNoMes:
        return EtapaNoMes(etapa=e.etapa, descricao=e.descricao, pct_no_mes=e.percentuais[j],
                          valor_no_mes=e.valores_mes[j], previsto_ate_referencia=e.previsto_ate_referencia,
                          medido_acumulado=e.medido_acumulado, situacao=e.situacao,
                          comeca=not comecou(e) and primeiro_mes(e) == j)

    def do_mes(j: int) -> list[EtapaNoMes]:
        # maior valor primeiro: o panorama cita as primeiras, e a frente que mais pesa não pode ficar de fora
        return sorted((no_mes(e, j) for e in c.etapas if j < len(e.percentuais) and e.percentuais[j] > 0),
                      key=lambda x: -x.valor_no_mes)

    if 0 <= k < c.meses:
        r.etapas_do_mes = do_mes(k)
    # O mês seguinte inteiro (as que continuam E as que começam): só as que começavam deixava de fora o movimento de
    # terra de dezembro na EMEI (85%, a 2ª maior frente do mês).
    j = k + 1
    if 0 <= j < c.meses:
        r.proximo_mes = _mes(_mes_idx(c.inicio) + j)
        if j < len(c.mensal):
            r.previsto_no_proximo_mes = c.mensal[j].previsto
            r.pct_previsto_acumulado_no_proximo_mes = c.mensal[j].pct_previsto_acumulado
        r.etapas_do_proximo_mes = do_mes(j)
    # a próxima: o primeiro mês depois do corrente em que alguma etapa COMEÇA (a que já está andando, pelo
    # cronograma ou pela medição, não é próxima)
    inicios = {}
    for e in c.etapas:
        primeiro = primeiro_mes(e)
        if primeiro is not None and primeiro > k and not comecou(e):
            inicios.setdefault(primeiro, []).append(e)
    if inicios:
        j = min(inicios)
        r.proximo_mes_com_inicio = _mes(_mes_idx(c.inicio) + j)
        r.proximas_etapas = sorted((no_mes(e, j) for e in inicios[j]), key=lambda x: -x.valor_no_mes)
    atrasadas = [e for e in c.etapas if e.situacao == "atrasada"]
    r.etapas_atrasadas = [EtapaAtrasada(etapa=e.etapa, descricao=e.descricao, medido_acumulado=e.medido_acumulado,
                                        previsto_ate_referencia=e.previsto_ate_referencia, atraso_pct=e.atraso_pct)
                          for e in sorted(atrasadas, key=lambda e: -e.atraso_pct)]
    return r


def _resumo_financeiro(o) -> ResumoFinanceiro:
    t = o.totais
    tem = any(v.ativa and v.tipo == "contrato" for v in o.versoes)
    if not tem and not t.comprometido and not t.cotando:
        return ResumoFinanceiro(tem_orcamento=False)
    return ResumoFinanceiro(
        tem_orcamento=tem, contratado=t.contratado, aditivos=t.aditivos, aditivos_pct=t.aditivos_pct,
        orcado_atual=t.atual, cotando=t.cotando, comprometido=t.comprometido, realizado=t.realizado, saldo=t.saldo,
        falta_gastar=max(t.atual - t.realizado, 0),
        realizado_pct=t.realizado * 10000 // t.atual if t.atual > 0 else None,
        etapas_estouradas=[EtapaEstourada(etapa=e.etapa, descricao=e.descricao, saldo=e.saldo)
                           for e in o.etapas if e.estourou],
        gastos=t.gastos)


def _resumo_entregas(pedidos: list[dict], hoje: date, insumos: dict[str, dict]) -> ResumoEntregas:
    r = ResumoEntregas()
    semana = hoje + timedelta(days=7)
    for p in sorted(pedidos, key=lambda p: p.get("previsao") or ""):
        st = StatusPedido(p["status"])
        if st == StatusPedido.RECEBIDO:
            r.nf_para_conferir += 1
        if st not in A_CAMINHO:
            continue
        # pedido de antes das linhas não tem a cópia do nome: vem do insumo
        ins = lambda x: insumos.get(x["insumo_id"], {})  # noqa: E731
        itens = [ItemEntrega(nome=x.get("nome") or ins(x).get("nome", ""),
                             unidade=x.get("unidade") or ins(x).get("unidade", ""), falta_entregar=falta_na_linha(x))
                 for x in estoque.linhas_de(p) if falta_na_linha(x)]
        e = EntregaResumo(pedido_id=p["id"], numero=p["numero"], fornecedor=p.get("fornecedor", ""), status=st,
                          previsao=p.get("previsao"), itens=itens)
        prev = (p.get("previsao") or "").strip()
        if not _DIA.match(prev):
            r.sem_previsao += 1
            continue
        d = date.fromisoformat(prev)
        if d == hoje:
            r.hoje.append(e)
        elif d < hoje:
            r.atrasadas.append(e)
        elif d <= semana:
            r.proximos_7_dias.append(e)
    return r


async def resumo_da_obra(arm, tenant_id: str, obra_id: str, hoje: date) -> ResumoDaObra:
    """O "como está a obra?" numa leitura: prazo e etapas do mês, financeiro, entregas, falta e pendências.

    Junta o que as telas já calculam (cronograma, orçamento, Conta, pedidos) em vez de refazer as contas: o agente cita
    este resumo, e cada bloco diz quando o dado não existe (sem cronograma, sem orçamento, sem previsão)."""
    obra = await obra_do_tenant(arm, tenant_id, obra_id)
    crono = await cronograma.cronograma_da_obra(arm, tenant_id, obra_id)
    orc = await versoes.orcamento_da_obra(arm, tenant_id, obra_id)
    insumos = await estoque.insumos_com_conta(arm, tenant_id, obra_id)
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                  Filtro("status", "in", estoque.PEDIDOS_ABERTOS)]))
    pendentes = await arm.consultar(Consulta("propostas", tenant_id, [
        Filtro("obra_id", "==", obra_id), Filtro("status", "==", StatusProposta.PENDENTE.value)]))
    pendentes.sort(key=lambda x: x["criado_em"], reverse=True)
    por_id = {x["insumo"]["id"]: x["insumo"] for x in insumos}
    materiais = [x for x in insumos if x["insumo"].get("tipo", "material") == "material"]
    return ResumoDaObra(
        obra=obra, hoje=hoje.isoformat(), cronograma=_resumo_cronograma(crono, hoje),
        financeiro=_resumo_financeiro(orc), entregas=_resumo_entregas(pedidos, hoje, por_id),
        almoxarifado=ResumoAlmoxarifado(
            insumos=len(materiais),
            falta_comprar=[FaltaResumo(insumo_id=x["insumo"]["id"], nome=x["insumo"]["nome"],
                                       unidade=x["insumo"].get("unidade", ""), falta=x["conta"]["falta"],
                                       texto=x["conta"]["texto"])
                           for x in materiais if x["conta"]["comprar"] > 0],
            sem_previsao=sum(1 for x in materiais if x["conta"]["sem_previsao"])),
        pendencias=ResumoPendencias(propostas=len(pendentes), titulos=[x["titulo"] for x in pendentes[:5]]))
