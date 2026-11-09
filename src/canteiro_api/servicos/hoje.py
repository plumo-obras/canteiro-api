"""Hoje na obra: o que a casa da obra mostra sem gastar token. Junta o que as telas já calculam e escreve em frases.

Um ponto por tipo: dez entregas atrasadas são uma coisa só para resolver na tela de Compras, e cada tipo em linha
própria estouraria o limite de 6 antes de chegar nos outros assuntos.
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.gastos import soma_nos_gastos_avulsos
from canteiro_dominio.modelos import DESFAZENDO_APROVACAO, StatusComprovante, StatusProposta
from canteiro_dominio.regras import A_CAMINHO
from canteiro_dominio.tipos import fmt_reais, reais

from ..esquemas import AcaoHoje, EntregaResumo, EtapaOrcamento, EventoHoje, HojeNaObra, PontoHoje, ResumoHoje
from ..nucleo import obra_do_tenant
from . import cronograma, estoque, versoes
from .resumo import comecou, primeiro_mes, resumo_cronograma, resumo_entregas

MAX_PONTOS = 6
SEMANAS_ATE_A_ETAPA = 4
_DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
_ORDEM = {"pede_voce": 0, "atencao": 1, "a_caminho": 2, "ok": 3}


def _dia(d: date) -> str:
    return f"{d.day:02d}/{d.month:02d}"


def _dia_semana(d: date) -> str:
    return f"{_DIAS_SEMANA[d.weekday()]}, {_dia(d)}"


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


def _lista(nomes: list[str], maximo: int = 3) -> str:
    if len(nomes) > maximo:
        return f"{', '.join(nomes[:maximo])} e mais {len(nomes) - maximo}"
    return nomes[0] if len(nomes) == 1 else f"{', '.join(nomes[:-1])} e {nomes[-1]}"


def _dia_iso(texto: str) -> date:
    return date.fromisoformat(texto)


def _mes_em_data(inicio: str, deslocamento: int) -> date:
    a, m = (int(x) for x in inicio.split("-")[:2])
    idx = a * 12 + m - 1 + deslocamento
    return date(idx // 12, idx % 12 + 1, 1)


def _semanas(c, hoje: date) -> tuple[int | None, int | None]:
    if not c.inicio or not c.meses:
        return None, None
    inicio = _mes_em_data(c.inicio, 0)
    total = ((_mes_em_data(c.inicio, c.meses) - inicio).days + 6) // 7
    return ((hoje - inicio).days // 7 + 1 if hoje >= inicio else None), total


def _resumo(obra: dict, atraso_decimos: int | None) -> tuple[str, str]:
    if obra["status"] == "em_orcamento":
        return "sem_medicao", "A obra ainda está em orçamento."
    if atraso_decimos is None:
        return "sem_medicao", "A obra ainda não tem medição."
    if atraso_decimos <= 0:
        return "no_prazo", "A obra está no prazo."
    numero = f"{atraso_decimos / 10:.1f}".removesuffix(".0").replace(".", ",")
    return "atrasada", f"A obra está atrasada {numero} {'mês' if atraso_decimos < 20 else 'meses'}."


@dataclass
class Dados:
    obra: dict
    hoje: date
    crono: object
    resumo_crono: object
    orc: object
    insumos: list[dict]
    pedidos: dict[str, dict]
    entregas: object
    comprovantes: list[dict]
    gastos: list[dict]


def _nome_entrega(e: EntregaResumo) -> str:
    nomes = [x.nome for x in e.itens if x.nome]
    if not nomes:
        return e.numero
    return nomes[0] if len(nomes) == 1 else f"{nomes[0]} e mais {_plural(len(nomes) - 1, 'item', 'itens')}"


_COMPRAS = AcaoHoje(tela="suprimentos", rotulo="Ver em Compras")
_ORCAMENTO = AcaoHoje(tela="orcamento", rotulo="Ver o orçamento")


def _detalhe_entrega(d: Dados, e: EntregaResumo) -> str:
    valor = d.pedidos[e.pedido_id].get("valor", 0)
    return f"{e.numero} de {fmt_reais(valor)} com {e.fornecedor}."


def _desfazendo(d: Dados) -> PontoHoje | None:
    if d.obra["status"] != DESFAZENDO_APROVACAO:
        return None
    return PontoHoje(tipo="desfazendo_aprovacao", prioridade="pede_voce",
                     titulo="O Desfazer da aprovação parou no meio.",
                     detalhe="Compras está pela metade até ele terminar. Resolva o que travou e desfaça de novo.",
                     acao=_ORCAMENTO)


def _sem_orcamento(d: Dados) -> PontoHoje | None:
    if any(v.ativa for v in d.orc.versoes) or any(x["insumo"].get("precisa_ate_o_fim", 0) > 0 for x in d.insumos):
        return None
    return PontoHoje(tipo="sem_orcamento", prioridade="pede_voce", titulo="Esta obra ainda não tem orçamento.",
                     detalhe="Envie as pranchas no Projeto, ou importe a planilha no Orçamento, para começar.",
                     acao=AcaoHoje(tela="projeto", rotulo="Ir para o Projeto"))


def _valor_lido(c: dict) -> int | None:
    """O valor que o leitor tirou do comprovante, se tirou: a soma é aviso ("pelo que foi lido"), não conta de obra."""
    opcoes = ((c.get("leitura") or {}).get("valor") or {}).get("opcoes") or []
    if not opcoes:
        return None
    try:
        return reais(str(opcoes[0]["valor"]).replace("R$", "").strip())
    except (ValueError, ArithmeticError):
        return None


def _gastos_a_conferir(d: Dados) -> PontoHoje | None:
    if not d.comprovantes:
        return None
    n = len(d.comprovantes)
    lidos = [v for v in (_valor_lido(c) for c in d.comprovantes) if v is not None]
    if lidos:
        faltam = n - len(lidos)
        detalhe = (f"{fmt_reais(sum(lidos))} pelo que já foi lido" +
                   (f"; {faltam} ainda sem leitura." if faltam else ".") +
                   " Falta você confirmar.")
    else:
        detalhe = "Ainda sem leitura. Falta você conferir o valor e confirmar."
    return PontoHoje(tipo="gastos_a_conferir", prioridade="pede_voce",
                     titulo=f"{_plural(n, 'comprovante', 'comprovantes')} para conferir.", detalhe=detalhe,
                     acao=AcaoHoje(tela="gastos", rotulo="Conferir", filtro={"status": "a_revisar"}))


def _entregas_atrasadas(d: Dados) -> PontoHoje | None:
    atrasadas = d.entregas.atrasadas
    if not atrasadas:
        return None
    if len(atrasadas) == 1:
        e = atrasadas[0]
        titulo = f"{_nome_entrega(e)} estava previsto para {_dia_semana(_dia_iso(e.previsao))}."
        detalhe = _detalhe_entrega(d, e)
    else:
        mais_antiga = min(_dia_iso(e.previsao) for e in atrasadas)
        titulo = f"{len(atrasadas)} entregas atrasadas."
        pedidos = _lista([f"{e.numero} ({e.fornecedor})" for e in atrasadas])
        detalhe = f"{pedidos}. A mais antiga era para {_dia(mais_antiga)}."
    return PontoHoje(tipo="entregas_atrasadas", prioridade="atencao", titulo=titulo, detalhe=detalhe, acao=_COMPRAS)


def _faltas_por_etapa(d: Dados) -> dict[str, list[dict]]:
    faltas: dict[str, list[dict]] = {}
    for x in d.insumos:
        # de qualquer tipo, como a tela de Compras: item de planilha é serviço, e só material deixaria a obra de
        # planilha sem o aviso. O serviço já medido sai pelo `Conta.medido`.
        if x["conta"]["comprar"] > 0 and x["insumo"].get("etapa"):
            faltas.setdefault(x["insumo"]["etapa"], []).append(x)
    return faltas


def _valor_da_falta(xs: list[dict]) -> int:
    return sum((x["conta"]["comprar"] * x["insumo"].get("valor_unitario", 0) + 500) // 1000 for x in xs)


def _etapas_com_falta(d: Dados, faltas: dict[str, list[dict]]) -> list[tuple[str, str, str]]:
    """(etapa, descrição, quando). Com cronograma: as em andamento primeiro (o material já está fazendo falta), depois
    as que começam em até 4 semanas. Sem cronograma não há data: a 1ª etapa do orçamento com falta."""
    if not d.crono.inicio or not d.crono.etapas:
        for e in d.orc.etapas:
            if e.etapa in faltas:
                return [(e.etapa, e.descricao, "")]
        return []
    limite = d.hoje + timedelta(weeks=SEMANAS_ATE_A_ETAPA)
    andamento, proximas = [], []
    for e in d.crono.etapas:
        if e.etapa not in faltas or e.situacao == "concluida":
            continue
        j = primeiro_mes(e)
        if comecou(e):
            andamento.append((e.etapa, e.descricao, "Já começou."))
        elif j is not None:
            inicio = _mes_em_data(d.crono.inicio, j)
            if inicio <= limite:
                # mês já iniciado sem medição: pelo cronograma ela deveria estar andando
                quando = ("Pelo cronograma, já deveria ter começado." if inicio < d.hoje
                          else f"Começa em {_dia(inicio)}.")
                proximas.append((inicio, e.etapa, e.descricao, quando))
    return andamento + [(a, b, c) for _, a, b, c in sorted(proximas, key=lambda x: x[0])]


def _falta_comprar(d: Dados) -> PontoHoje | None:
    faltas = _faltas_por_etapa(d)
    etapas = _etapas_com_falta(d, faltas)
    if not etapas:
        return None
    filtro = {"etapa": etapas[0][0]}
    acao = AcaoHoje(tela="suprimentos", rotulo="Ver o que falta", filtro=filtro)
    if len(etapas) == 1:
        etapa, descricao, quando = etapas[0]
        xs = faltas[etapa]
        nomes = _lista([x["insumo"]["nome"] for x in xs])
        detalhe = f"{_plural(len(xs), 'item', 'itens')} · {fmt_reais(_valor_da_falta(xs))} pelo orçamento: {nomes}."
        return PontoHoje(tipo="falta_comprar", prioridade="atencao", titulo=f"Falta comprar para {descricao}.",
                         detalhe=f"{detalhe} {quando}".strip(), acao=acao)
    todos = [x for etapa, _, _ in etapas for x in faltas[etapa]]
    partes = _lista([f"{descricao} ({_plural(len(faltas[etapa]), 'item', 'itens')})" for etapa, descricao, _ in etapas])
    return PontoHoje(tipo="falta_comprar", prioridade="atencao", titulo=f"Falta comprar para {len(etapas)} etapas.",
                     detalhe=f"{partes} · {fmt_reais(_valor_da_falta(todos))} pelo orçamento.", acao=acao)


def _etapas_estouradas(d: Dados) -> PontoHoje | None:
    estouradas: list[EtapaOrcamento] = [e for e in d.orc.etapas if e.estourou]
    if not estouradas:
        return None
    excesso = sum(-e.saldo for e in estouradas)
    if len(estouradas) == 1:
        e = estouradas[0]
        titulo = f"{e.descricao} passou do orçado em {fmt_reais(excesso)}."
        detalhe = f"Comprometido {fmt_reais(e.comprometido)} de {fmt_reais(e.atual)} orçados."
    else:
        titulo = f"{len(estouradas)} etapas passaram do orçado."
        detalhe = f"{_lista([e.descricao for e in estouradas])}: {fmt_reais(excesso)} acima do orçado."
    return PontoHoje(tipo="etapas_estouradas", prioridade="atencao", titulo=titulo, detalhe=detalhe, acao=_ORCAMENTO)


def _pct(centesimos: int) -> str:
    return f"{centesimos / 100:.1f}".removesuffix(".0").replace(".", ",") + "%"


def _etapas_atrasadas(d: Dados) -> PontoHoje | None:
    atrasadas = d.resumo_crono.etapas_atrasadas
    if not atrasadas:
        return None
    if len(atrasadas) == 1:
        e = atrasadas[0]
        titulo = f"{e.descricao} está atrasada."
        detalhe = f"Medido {_pct(e.medido_acumulado)} de {_pct(e.previsto_ate_referencia)} previstos na última medição."
    else:
        titulo = f"{len(atrasadas)} etapas atrasadas."
        detalhe = f"{_lista([e.descricao for e in atrasadas])}, pela última medição."
    return PontoHoje(tipo="etapas_atrasadas", prioridade="atencao", titulo=titulo, detalhe=detalhe,
                     acao=AcaoHoje(tela="cronograma", rotulo="Ver o cronograma"))


def _itens_sem_preco(d: Dados) -> PontoHoje | None:
    sem = [x for x in d.orc.itens if x.tipo == "item" and x.quantidade > 0 and x.valor_unitario == 0]
    if not sem:
        return None
    titulo = "1 item do orçamento sem preço." if len(sem) == 1 else f"{len(sem)} itens do orçamento sem preço."
    detalhe = f"{_lista([x.descricao for x in sem])}. Sem preço, o total fica abaixo do real."
    return PontoHoje(tipo="itens_sem_preco", prioridade="atencao", titulo=titulo, detalhe=detalhe, acao=_ORCAMENTO)


def _so_pranchas(d: Dados) -> PontoHoje | None:
    ativas = [v for v in d.orc.versoes if v.ativa]
    if d.obra["status"] != "em_orcamento" or not ativas:
        return None
    if not all((v.arquivo or "").endswith(versoes.SUFIXO_PROJETO) for v in ativas):
        return None
    return PontoHoje(tipo="orcamento_so_pranchas", prioridade="atencao",
                     titulo="O orçamento só tem o que veio das pranchas.",
                     detalhe="Faltam os serviços e etapas que as pranchas não trazem, como cobertura e piso. Importe a "
                             "planilha para completar.", acao=_ORCAMENTO)


def _gastos_sem_etapa(d: Dados) -> PontoHoje | None:
    # pagamento de pedido não entra: o custo dele já conta pela entrega, e o a_definir de Gastos também o deixa de fora
    sem = [g for g in d.gastos if g.get("etapa") is None and soma_nos_gastos_avulsos(g)]
    if not sem:
        return None
    n = len(sem)
    total = fmt_reais(sum(g["valor"] for g in sem))
    return PontoHoje(tipo="gastos_sem_etapa", prioridade="atencao",
                     titulo=f"{_plural(n, 'gasto', 'gastos')} sem etapa.",
                     detalhe=f"{total} contam na obra e em nenhuma etapa até você definir.",
                     acao=AcaoHoje(tela="gastos", rotulo="Definir a etapa", filtro={"status": "a_definir"}))


def _entregas_semana(d: Dados) -> PontoHoje | None:
    proximas = [*d.entregas.hoje, *d.entregas.proximos_7_dias]
    if not proximas:
        return None
    if len(proximas) == 1:
        e = proximas[0]
        dia = _dia_iso(e.previsao)
        quando = "hoje" if dia == d.hoje else _dia_semana(dia)
        titulo = f"{_nome_entrega(e)} chega {quando}."
        detalhe = _detalhe_entrega(d, e) + ' Quando chegar, é só marcar "Chegou".'
    else:
        limite = d.hoje + timedelta(days=7)
        titulo = f"{len(proximas)} entregas previstas até {_dia(limite)}."
        detalhe = f"{_lista([f'{e.numero} ({e.fornecedor})' for e in proximas])}."
    return PontoHoje(tipo="entregas_semana", prioridade="a_caminho", titulo=titulo, detalhe=detalhe, acao=_COMPRAS)


# a ordem aqui é a ordem dentro da mesma prioridade
_PONTOS: tuple[Callable[[Dados], PontoHoje | None], ...] = (
    _desfazendo, _sem_orcamento, _gastos_a_conferir, _entregas_atrasadas, _falta_comprar,
    _etapas_estouradas, _etapas_atrasadas, _itens_sem_preco, _so_pranchas, _gastos_sem_etapa, _entregas_semana,
)


def _semana(d: Dados) -> list[EventoHoje]:
    eventos: list[EventoHoje] = []
    for e in [*d.entregas.hoje, *d.entregas.proximos_7_dias]:
        eventos.append(EventoHoje(data=e.previsao, tipo="entrega", titulo=f"Entrega de {_nome_entrega(e)}",
                                  detalhe=_detalhe_entrega(d, e), acao=_COMPRAS))
    if d.crono.inicio:
        fim = d.hoje + timedelta(days=7)
        for e in d.crono.etapas:
            j = primeiro_mes(e)
            if j is None or comecou(e):
                continue
            inicio = _mes_em_data(d.crono.inicio, j)
            if d.hoje <= inicio <= fim:
                eventos.append(EventoHoje(data=inicio.isoformat(), tipo="inicio_etapa", titulo=f"Começa {e.descricao}",
                                          detalhe="Pelo cronograma da obra.",
                                          acao=AcaoHoje(tela="cronograma", rotulo="Ver o cronograma")))
    return sorted(eventos, key=lambda x: (x.data, x.tipo))


class _LeituraUnica:
    """Cronograma, orçamento e Conta leem os mesmos pedidos e gastos, cada um por conta própria. Vale só durante uma
    chamada, em que todos precisam ver o mesmo retrato da obra."""

    def __init__(self, arm):
        self._arm = arm
        self._consultas: dict[str, list[dict]] = {}

    async def consultar(self, consulta: Consulta) -> list[dict]:
        chave = repr(consulta)
        if chave not in self._consultas:
            self._consultas[chave] = await self._arm.consultar(consulta)
        return list(self._consultas[chave])         # lista nova: quem recebe pode ordenar sem afetar os outros

    def __getattr__(self, nome: str):
        return getattr(self._arm, nome)


async def hoje_da_obra(arm, tenant_id: str, obra_id: str, hoje: date) -> HojeNaObra:
    arm = _LeituraUnica(arm)
    obra = await obra_do_tenant(arm, tenant_id, obra_id)
    crono = await cronograma.cronograma_da_obra(arm, tenant_id, obra_id)
    orc = await versoes.orcamento_da_obra(arm, tenant_id, obra_id)
    insumos = await estoque.insumos_com_conta(arm, tenant_id, obra_id)
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    propostas = await arm.consultar(Consulta("propostas", tenant_id, [
        Filtro("obra_id", "==", obra_id), Filtro("status", "==", StatusProposta.PENDENTE.value)]))
    comprovantes = await arm.consultar(Consulta("comprovantes", tenant_id, [
        Filtro("obra_id", "==", obra_id), Filtro("status", "==", StatusComprovante.A_REVISAR.value)]))
    gastos = await versoes.gastos_da_obra(arm, tenant_id, obra_id)
    por_id = {x["insumo"]["id"]: x["insumo"] for x in insumos}
    a_caminho = [p for p in pedidos if p["status"] in {s.value for s in A_CAMINHO}]
    resumo_c = resumo_cronograma(crono, hoje)
    d = Dados(obra=obra, hoje=hoje, crono=crono, resumo_crono=resumo_c, orc=orc, insumos=insumos,
              pedidos={p["id"]: p for p in a_caminho}, entregas=resumo_entregas(a_caminho, hoje, por_id),
              comprovantes=comprovantes, gastos=gastos)
    pontos = sorted((p for p in (f(d) for f in _PONTOS) if p is not None), key=lambda p: _ORDEM[p.prioridade])
    situacao, frase = _resumo(obra, resumo_c.atraso_decimos)
    semana_atual, semanas_total = _semanas(crono, hoje)
    return HojeNaObra(hoje=hoje.isoformat(),
                      resumo=ResumoHoje(situacao=situacao, frase=frase,
                                        # as propostas não viram ponto (a tela já mostra um cartão para cada), mas
                                        # continuam sendo coisas esperando a pessoa
                                        esperando=sum(p.prioridade == "pede_voce" for p in pontos) + len(propostas),
                                        semana_atual=semana_atual, semanas_total=semanas_total),
                      pontos=pontos[:MAX_PONTOS], mais=max(0, len(pontos) - MAX_PONTOS), semana=_semana(d))
