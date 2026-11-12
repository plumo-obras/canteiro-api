"""Cronograma por visão de obra: a duração de cada serviço sai da quantidade e da produtividade (homem-hora por
unidade), não da proporção do preço. A equipe e as dependências vêm de quem planeja (o agente, como mestre de obras);
a conta — dias de cada serviço, início/fim respeitando o que depende do quê, caminho crítico — é feita aqui.

Produtividade (HH/un) e lags de processo (cura do concreto antes de desformar, p.ex.) são coeficientes de obra
conhecidos (faixas do SINAPI/TCPO). Ficam aqui, versionados e auditáveis; quando o item do orçamento tem código de
composição e a base SINAPI está carregada, o HH vem do analítico real (via serviço de produtos) e entra no lugar do
coeficiente de referência. Quantidade em milésimos (m³, m², kg… × 1000), como no resto do domínio.
"""
from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

JORNADA_H = 8  # horas úteis por dia de trabalho (1 turno)
DIAS_UTEIS_SEMANA = 5
CEM = 10000  # 100% em centésimos de ponto percentual (como no resto do cronograma)


class Servico(StrEnum):
    """Famílias de serviço que a obra conhece, na ordem natural de execução. A classificação casa pela descrição do
    item do orçamento (e, quando houver, pelo código SINAPI). OUTRO = sem produtividade de referência: a duração então
    vem do planejador (ou do HH real do SINAPI), nunca de chute proporcional ao preço."""

    SERVICOS_PRELIMINARES = "servicos_preliminares"
    MOVIMENTO_TERRA = "movimento_terra"
    FORMA = "forma"
    ARMACAO = "armacao"
    FUNDACAO_CONCRETO = "fundacao_concreto"
    ESTRUTURA_CONCRETO = "estrutura_concreto"
    ALVENARIA = "alvenaria"
    COBERTURA = "cobertura"
    INSTALACOES = "instalacoes"
    REVESTIMENTO_INTERNO = "revestimento_interno"
    CONTRAPISO = "contrapiso"
    REVESTIMENTO_CERAMICO = "revestimento_ceramico"
    ESQUADRIAS = "esquadrias"
    PINTURA = "pintura"
    LIMPEZA = "limpeza"
    OUTRO = "outro"


@dataclass(frozen=True)
class Produtividade:
    """Coeficiente de obra de uma família de serviço.

    hh_por_unidade: homem-hora para executar 1 unidade (m³, m², kg…) — mão de obra direta, uma faixa típica do
        SINAPI/TCPO. equipe_padrao: nº de pessoas de uma frente típica, para a duração ter um palpite inicial que o
        planejador ajusta. lag_pos_dias: dias de espera DEPOIS do serviço antes que o dependente comece, por processo
        físico, não por mão de obra (cura do concreto para desformar/carregar). unidade: a unidade que o HH pressupõe.
    """

    servico: Servico
    hh_por_unidade: float
    unidade: str
    equipe_padrao: int
    lag_pos_dias: int = 0


# Faixas de referência (HH/un) de mão de obra direta. Fontes: composições SINAPI e TCPO (ordens de grandeza usuais de
# obra residencial). São ponto de partida auditável; o HH real do SINAPI, quando disponível pelo código, prevalece.
REFERENCIA: dict[Servico, Produtividade] = {
    Servico.SERVICOS_PRELIMINARES: Produtividade(Servico.SERVICOS_PRELIMINARES, 0.40, "m2", 4),
    Servico.MOVIMENTO_TERRA: Produtividade(Servico.MOVIMENTO_TERRA, 1.50, "m3", 4),
    # Fôrma (carpinteiro) e armação (armador): mesma produtividade na fundação e na estrutura; o que muda de verdade é
    # a cura do concreto antes de seguir, que fica na família do concreto.
    Servico.FORMA: Produtividade(Servico.FORMA, 0.95, "m2", 3),
    Servico.ARMACAO: Produtividade(Servico.ARMACAO, 0.12, "kg", 3),
    # Concreto de fundação: cura curta antes de carregar (sapata/baldrame). Estrutura (laje/viga/pilar): cura maior
    # antes de desformar a laje e subir o pavimento.
    Servico.FUNDACAO_CONCRETO: Produtividade(Servico.FUNDACAO_CONCRETO, 2.50, "m3", 5, lag_pos_dias=3),
    Servico.ESTRUTURA_CONCRETO: Produtividade(Servico.ESTRUTURA_CONCRETO, 2.80, "m3", 6, lag_pos_dias=7),
    Servico.ALVENARIA: Produtividade(Servico.ALVENARIA, 1.10, "m2", 4),
    Servico.COBERTURA: Produtividade(Servico.COBERTURA, 0.80, "m2", 4),
    Servico.INSTALACOES: Produtividade(Servico.INSTALACOES, 0.70, "m2", 4),
    Servico.REVESTIMENTO_INTERNO: Produtividade(Servico.REVESTIMENTO_INTERNO, 0.75, "m2", 4),
    Servico.CONTRAPISO: Produtividade(Servico.CONTRAPISO, 0.45, "m2", 4, lag_pos_dias=2),
    Servico.REVESTIMENTO_CERAMICO: Produtividade(Servico.REVESTIMENTO_CERAMICO, 0.80, "m2", 4),
    Servico.ESQUADRIAS: Produtividade(Servico.ESQUADRIAS, 1.20, "un", 2),
    Servico.PINTURA: Produtividade(Servico.PINTURA, 0.30, "m2", 3),
    Servico.LIMPEZA: Produtividade(Servico.LIMPEZA, 0.15, "m2", 3),
}

# Palavras que classificam a descrição do item do orçamento numa família. A ordem importa: a primeira que casar vence,
# então o mais específico ("forma"/"armadura") vem antes do genérico ("concreto").
_PALAVRAS: list[tuple[Servico, tuple[str, ...]]] = [
    (Servico.MOVIMENTO_TERRA, ("escavacao", "aterro", "reaterro", "movimento de terra", "corte", "terraplen")),
    # Fôrma e armação vêm ANTES do concreto: "fôrma de sapata" é fôrma, não concreto de sapata.
    (Servico.FORMA, ("forma", "formas", "cimbramento", "escoramento")),
    (Servico.ARMACAO, ("armacao", "armadura", "aco ca", "aco ca-50", "aco ca-60", "ferragem", "aco estrutural")),
    (Servico.FUNDACAO_CONCRETO,
     ("sapata", "baldrame", "radier", "bloco de fundacao", "estaca", "concreto magro", "lastro", "fundacao")),
    (Servico.ESTRUTURA_CONCRETO,
     ("laje", "viga", "pilar", "concreto estrutural", "concreto fck", "superestrutura", "concreto")),
    (Servico.ALVENARIA, ("alvenaria", "bloco ceramico", "bloco de concreto", "tijolo", "vedacao")),
    (Servico.COBERTURA, ("cobertura", "telha", "telhado", "madeiramento", "trama", "cumeeira")),
    (Servico.INSTALACOES,
     ("instalacao", "eletrica", "hidraulica", "sanitaria", "tubulacao", "eletrodut", "conduite", "esgoto")),
    (Servico.CONTRAPISO, ("contrapiso", "regularizacao de piso", "lastro de piso")),
    (Servico.REVESTIMENTO_CERAMICO, ("ceramica", "porcelanato", "revestimento ceramico", "piso ceramico", "azulejo")),
    (Servico.REVESTIMENTO_INTERNO, ("reboco", "emboco", "chapisco", "massa unica", "gesso", "revestimento argamass")),
    (Servico.ESQUADRIAS, ("esquadria", "porta", "janela", "caixilho", "batente")),
    (Servico.PINTURA, ("pintura", "tinta", "latex", "massa corrida", "selador", "verniz")),
    (Servico.LIMPEZA, ("limpeza", "entrega da obra", "limpeza final")),
    (Servico.SERVICOS_PRELIMINARES,
     ("servicos preliminares", "canteiro", "tapume", "barracao", "locacao de obra", "placa de obra")),
]

# A ordem de obra (sequência natural das famílias): guia a dependência padrão e a ordenação na tela.
ORDEM: dict[Servico, int] = {s: i for i, s in enumerate(Servico)}


def _normalizar(texto: str) -> str:
    """minúsculas, sem acento: 'Fôrma de Laje' → 'forma de laje', para casar as palavras-chave."""
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def classificar(descricao: str, codigo: str = "", unidade: str = "") -> Servico:
    """Família de serviço de um item do orçamento, pela descrição (e código, quando a base mapear). OUTRO quando nada
    casa: aí a duração não é inventada por produtividade, vem do planejador ou do HH real do SINAPI."""
    t = _normalizar(descricao)
    for servico, palavras in _PALAVRAS:
        if any(p in t for p in palavras):
            return servico
    return Servico.OUTRO


def _quant_unidades(quantidade_milesimos: int) -> float:
    return quantidade_milesimos / 1000


def duracao_dias(quantidade_milesimos: int, hh_por_unidade: float, equipe: int, jornada_h: int = JORNADA_H) -> int:
    """Dias ÚTEIS para o serviço: quantidade × HH/un ÷ (equipe × jornada), arredondado para cima (um dia começado é um
    dia na obra). Mínimo de 1 dia quando há quantidade. 0 só quando não há o que fazer."""
    if quantidade_milesimos <= 0 or hh_por_unidade <= 0 or equipe <= 0:
        return 0
    horas = _quant_unidades(quantidade_milesimos) * hh_por_unidade
    return max(1, math.ceil(horas / (equipe * jornada_h)))


@dataclass
class ServicoDoPlano:
    """Um serviço a planejar. hh_por_unidade e equipe podem vir do planejador; vazios, caem na referência da família.
    depende_de: ids de serviços que precisam terminar (mais o lag de processo) antes deste começar."""

    id: str
    descricao: str
    quantidade_milesimos: int
    unidade: str = ""
    codigo: str = ""
    servico: Servico | None = None
    hh_por_unidade: float | None = None
    equipe: int | None = None
    depende_de: list[str] = field(default_factory=list)
    # Quando o nó já tem a duração pronta (ex.: uma ETAPA, que soma vários serviços): usa esta em vez de calcular por
    # quantidade. lag_fixo idem para a cura do nó composto.
    duracao_dias_fixa: int | None = None
    lag_fixo: int | None = None


@dataclass(frozen=True)
class ServicoAgendado:
    """O serviço posicionado no tempo, em dias úteis a partir do dia 0 (o início da obra). duracao em dias úteis;
    lag_pos é a espera de processo depois do fim (cura) que empurra os dependentes."""

    id: str
    descricao: str
    servico: Servico
    quantidade_milesimos: int
    unidade: str
    equipe: int
    hh_por_unidade: float
    inicio_dia: int
    duracao_dias: int
    lag_pos_dias: int
    depende_de: list[str]

    @property
    def fim_dia(self) -> int:
        """Dia útil (exclusivo) em que o serviço termina: início + duração."""
        return self.inicio_dia + self.duracao_dias

    @property
    def liberado_dia(self) -> int:
        """Quando os dependentes podem começar: fim + lag de processo (cura)."""
        return self.fim_dia + self.lag_pos_dias


@dataclass(frozen=True)
class Plano:
    servicos: list[ServicoAgendado]
    dias_uteis: int  # duração total do caminho crítico, em dias úteis
    critico: list[str]  # ids no caminho crítico


class CronogramaInvalido(ValueError):
    """Dependência desconhecida ou ciclo entre serviços."""


def _resolver(servico: ServicoDoPlano) -> tuple[Servico, float, int, int]:
    """Família, HH/un, equipe e lag efetivos de um serviço: o que o planejador passou, senão a referência da família."""
    fam = servico.servico or classificar(servico.descricao, servico.codigo, servico.unidade)
    ref = REFERENCIA.get(fam)
    hh = servico.hh_por_unidade if servico.hh_por_unidade is not None else (ref.hh_por_unidade if ref else 0.0)
    equipe = servico.equipe if servico.equipe is not None else (ref.equipe_padrao if ref else 1)
    lag = ref.lag_pos_dias if ref else 0
    return fam, hh, max(1, equipe), lag


def montar_plano(servicos: list[ServicoDoPlano]) -> Plano:
    """Posiciona cada serviço no tempo (dias úteis) respeitando as dependências e os lags de processo, e devolve a
    duração total (caminho crítico). Lança CronogramaInvalido em dependência inexistente ou ciclo.

    Um serviço começa assim que TODOS os que ele depende estão liberados (fim + cura). Sem dependência, começa no dia 0
    — o planejador controla o paralelismo pelas dependências que declara (serviços sem relação correm juntos)."""
    por_id = {s.id: s for s in servicos}
    for s in servicos:
        for dep in s.depende_de:
            if dep not in por_id:
                raise CronogramaInvalido(f"Serviço {s.id!r} depende de {dep!r}, que não existe no plano.")

    agendados: dict[str, ServicoAgendado] = {}
    # resolve em ordem topológica por memória (DFS com detecção de ciclo)
    visitando: set[str] = set()

    def agendar(sid: str) -> ServicoAgendado:
        if sid in agendados:
            return agendados[sid]
        if sid in visitando:
            raise CronogramaInvalido(f"Ciclo de dependências envolvendo {sid!r}.")
        visitando.add(sid)
        s = por_id[sid]
        fam, hh, equipe, lag = _resolver(s)
        dur = (s.duracao_dias_fixa if s.duracao_dias_fixa is not None
               else duracao_dias(s.quantidade_milesimos, hh, equipe))
        if s.lag_fixo is not None:
            lag = s.lag_fixo
        inicio = 0
        for dep in s.depende_de:
            inicio = max(inicio, agendar(dep).liberado_dia)
        visitando.discard(sid)
        ag = ServicoAgendado(
            id=s.id, descricao=s.descricao, servico=fam, quantidade_milesimos=s.quantidade_milesimos,
            unidade=s.unidade, equipe=equipe, hh_por_unidade=hh, inicio_dia=inicio, duracao_dias=dur,
            lag_pos_dias=lag, depende_de=list(s.depende_de),
        )
        agendados[sid] = ag
        return ag

    for s in servicos:
        agendar(s.id)

    ordenados = sorted(agendados.values(), key=lambda a: (a.inicio_dia, ORDEM.get(a.servico, 99), a.id))
    total = max((a.liberado_dia for a in agendados.values()), default=0)
    fim_total = max((a.fim_dia for a in agendados.values()), default=0)
    # caminho crítico: serviços cujo fim toca o fim total, subindo pelas dependências que determinaram o início
    critico: list[str] = []
    fim = {sid: a for sid, a in agendados.items()}
    alvo = next((a for a in agendados.values() if a.fim_dia == fim_total), None)
    while alvo is not None:
        critico.append(alvo.id)
        anterior = None
        for dep in alvo.depende_de:
            if fim[dep].liberado_dia == alvo.inicio_dia:
                anterior = fim[dep]
                break
        alvo = anterior
    critico.reverse()
    return Plano(servicos=ordenados, dias_uteis=total, critico=critico)


# ---------------------------------------------------------------------------------------------------------------------
# Nível de ETAPA: o corpo armazenado do cronograma é etapa × mês (%). Então agregamos os serviços de cada etapa numa
# duração só (em dias úteis), agendamos as etapas com as dependências do planejador, e convertemos para meses.
# ---------------------------------------------------------------------------------------------------------------------

def dias_uteis_de_itens(itens: list[ServicoDoPlano], equipe: int) -> tuple[int, int, list[tuple[Servico, int]]]:
    """Dias úteis de uma etapa a partir dos seus itens, com uma equipe.

    Soma o homem-hora por família de serviço (quantidade × HH/un), vira dias por família, e soma as famílias na ORDEM
    de obra — dentro de uma etapa fôrma→armação→concreto são sequenciais, então somar é o realista (e conservador).
    Devolve (dias_uteis, lag_de_cura, [(família, dias)]). lag = maior cura entre as famílias de concreto da etapa."""
    hh_por_familia: dict[Servico, float] = {}
    for it in itens:
        fam, hh, _equipe, _lag = _resolver(it)
        if hh <= 0 or it.quantidade_milesimos <= 0:
            continue
        hh_por_familia[fam] = hh_por_familia.get(fam, 0.0) + _quant_unidades(it.quantidade_milesimos) * hh
    por_familia: list[tuple[Servico, int]] = []
    for fam in sorted(hh_por_familia, key=lambda f: ORDEM.get(f, 99)):
        dias = max(1, math.ceil(hh_por_familia[fam] / (max(1, equipe) * JORNADA_H)))
        por_familia.append((fam, dias))
    total = sum(d for _, d in por_familia)
    lag = max((REFERENCIA[f].lag_pos_dias for f, _ in por_familia if f in REFERENCIA), default=0)
    return total, lag, por_familia


def _primeiro_dia_util(d: date) -> date:
    while d.weekday() >= DIAS_UTEIS_SEMANA:
        d += timedelta(days=1)
    return d


def _data_do_dia_util(base: date, dia_util: int) -> date:
    """Data de calendário do n-ésimo dia útil a partir de `base` (dia útil 0 = o primeiro dia útil em/após base)."""
    d = _primeiro_dia_util(base)
    passos = 0
    while passos < dia_util:
        d += timedelta(days=1)
        if d.weekday() < DIAS_UTEIS_SEMANA:
            passos += 1
    return d


def meses_das_etapas(agendados: list[ServicoAgendado], inicio: date) -> tuple[int, dict[str, tuple[int, int]]]:
    """Converte o agendamento (dias úteis) em índices de mês, contando do mês de `inicio`. Devolve (total_de_meses,
    {id_da_etapa: (mês_de_início, mês_de_fim)}), ambos inclusivos e começando em 0."""
    base = _primeiro_dia_util(inicio)

    def mes_idx(d: date) -> int:
        return (d.year - base.year) * 12 + (d.month - base.month)

    faixas: dict[str, tuple[int, int]] = {}
    for a in agendados:
        de = mes_idx(_data_do_dia_util(base, a.inicio_dia))
        # o último dia trabalhado é fim_dia - 1 (fim_dia é exclusivo)
        ate = mes_idx(_data_do_dia_util(base, max(a.inicio_dia, a.fim_dia - 1)))
        faixas[a.id] = (de, ate)
    total = max((ate for _, ate in faixas.values()), default=0) + 1
    return total, faixas


def grade_meses(de: int, ate: int, total_meses: int) -> list[int]:
    """% por mês (centésimos, soma CEM) de uma etapa que vai do mês `de` ao `ate` (inclusive), espalhado por igual —
    o mesmo critério do resto do sistema. Fora da faixa fica 0. Maior-resto para fechar exatamente CEM."""
    de = max(0, de)
    ate = max(de, min(ate, total_meses - 1))
    n = ate - de + 1
    base, resto = divmod(CEM, n)
    grade = [0] * total_meses
    for i in range(n):
        grade[de + i] = base + (1 if i >= n - resto else 0)
    return grade


@dataclass
class EtapaParaPlano:
    """Uma etapa a planejar: seus itens (serviços do orçamento), a equipe que o planejador alocou e de que etapas ela
    depende (chaves). equipe/depende_de vêm do planejador (mestre de obras)."""

    id: str
    itens: list[ServicoDoPlano]
    equipe: int
    depende_de: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EtapaNoCronograma:
    id: str
    dias_uteis: int
    lag_dias: int
    mes_inicio: int  # 0 = primeiro mês da obra
    mes_fim: int
    por_familia: list[tuple[Servico, int]]
    percentuais: list[int]


@dataclass(frozen=True)
class CronogramaDeObra:
    meses: int
    etapas: list[EtapaNoCronograma]
    dias_uteis: int
    critico: list[str]


def montar_cronograma_de_etapas(etapas: list[EtapaParaPlano], inicio: date) -> CronogramaDeObra:
    """Pipeline completo: duração de cada etapa pelos seus serviços (produtividade), agenda com as dependências e a
    cura, e converte para o corpo etapa × mês (%). A conta é toda daqui; o planejador só deu equipe e dependências."""
    nos: list[ServicoDoPlano] = []
    dur_lag: dict[str, tuple[int, int, list[tuple[Servico, int]]]] = {}
    for e in etapas:
        dias, lag, por_fam = dias_uteis_de_itens(e.itens, e.equipe)
        dur_lag[e.id] = (dias, lag, por_fam)
        nos.append(ServicoDoPlano(id=e.id, descricao=e.id, quantidade_milesimos=0, depende_de=list(e.depende_de),
                                  duracao_dias_fixa=dias, lag_fixo=lag))
    plano = montar_plano(nos)
    total_meses, faixas = meses_das_etapas(plano.servicos, inicio)
    saida: list[EtapaNoCronograma] = []
    for a in plano.servicos:
        de, ate = faixas[a.id]
        dias, lag, por_fam = dur_lag[a.id]
        saida.append(EtapaNoCronograma(id=a.id, dias_uteis=dias, lag_dias=lag, mes_inicio=de, mes_fim=ate,
                                       por_familia=por_fam, percentuais=grade_meses(de, ate, total_meses)))
    return CronogramaDeObra(meses=total_meses, etapas=saida, dias_uteis=plano.dias_uteis, critico=plano.critico)


# ---------------------------------------------------------------------------------------------------------------------
# Fase de obra (a EAP padrão): para organizar um orçamento "chapado" em etapas por fase, de onde o cronograma nasce
# completo. A fase é mais grossa que a família de serviço: fôrma/armação/concreto de sapata são todos "Fundação".
# ---------------------------------------------------------------------------------------------------------------------

# As fases na ordem de obra. O nome é o que vira a etapa do orçamento.
FASES: list[str] = [
    "Serviços preliminares", "Fundação", "Estrutura", "Alvenaria", "Cobertura",
    "Instalações", "Revestimento", "Esquadrias", "Pintura", "Entrega",
]

# Palavras que indicam fundação num serviço genérico de fôrma/armação ("fôrma de sapata" é Fundação, não Estrutura).
_FUNDACAO = ("sapata", "baldrame", "radier", "fundacao", "estaca", "bloco de fundacao", "lastro", "concreto magro")

_FAMILIA_FASE: dict[Servico, str] = {
    Servico.SERVICOS_PRELIMINARES: "Serviços preliminares",
    Servico.MOVIMENTO_TERRA: "Fundação",
    Servico.FUNDACAO_CONCRETO: "Fundação",
    Servico.ESTRUTURA_CONCRETO: "Estrutura",
    Servico.ALVENARIA: "Alvenaria",
    Servico.COBERTURA: "Cobertura",
    Servico.INSTALACOES: "Instalações",
    Servico.REVESTIMENTO_INTERNO: "Revestimento",
    Servico.CONTRAPISO: "Revestimento",
    Servico.REVESTIMENTO_CERAMICO: "Revestimento",
    Servico.ESQUADRIAS: "Esquadrias",
    Servico.PINTURA: "Pintura",
    Servico.LIMPEZA: "Entrega",
}


def fase(descricao: str, codigo: str = "", unidade: str = "") -> str:
    """Fase de obra de um serviço do orçamento (a EAP padrão). Fôrma e armação vão para Fundação quando a descrição
    fala de sapata/baldrame/etc., senão para Estrutura. OUTRO (sem família conhecida) cai em 'Outros serviços'."""
    fam = classificar(descricao, codigo, unidade)
    if fam in (Servico.FORMA, Servico.ARMACAO):
        t = _normalizar(descricao)
        return "Fundação" if any(p in t for p in _FUNDACAO) else "Estrutura"
    return _FAMILIA_FASE.get(fam, "Outros serviços")


def ordem_da_fase(nome: str) -> int:
    """Posição da fase na ordem de obra, para ordenar as etapas; a desconhecida ('Outros serviços') vai para o fim."""
    return FASES.index(nome) if nome in FASES else len(FASES)


# ---------------------------------------------------------------------------------------------------------------------
# Obra completa estimada: a 1ª previsão é especulação. Das fases que já têm orçamento, estima o porte da obra e as
# fases que faltam (distribuição típica de custo por fase, à la CUB), para o cronograma nascer da fundação à entrega.
# Conforme a pessoa detalha o orçamento de cada fase, a estimativa vira real.
# ---------------------------------------------------------------------------------------------------------------------

# Fração típica do custo total por fase (obra residencial). Soma 1,0. É ponto de partida; a pessoa ajusta ao detalhar.
DISTRIBUICAO_FASE: dict[str, float] = {
    "Serviços preliminares": 0.04, "Fundação": 0.07, "Estrutura": 0.18, "Alvenaria": 0.09, "Cobertura": 0.06,
    "Instalações": 0.16, "Revestimento": 0.16, "Esquadrias": 0.10, "Pintura": 0.08, "Entrega": 0.06,
}

# Custo por m² de área construída (ordem de grandeza do CUB), em centavos: serve para estimar a área da obra a partir
# do valor já orçado, e daí as quantidades de cada serviço.
CUB_POR_M2 = 220_000

# Os serviços TÍPICOS de cada fase, com código SINAPI, unidade, custo unitário (centavos) e o quanto rende por m² de
# área construída (fator). Da área estimada da obra sai a quantidade de cada serviço — descrevendo o que vai ser feito,
# cruzado com o SINAPI. É ponto de partida; quando a pessoa detalha a fase, troca pelo real. Códigos de referência.
ServicoFase = tuple[str, str, str, int, float]  # (descrição, código SINAPI, unidade, custo_unit_centavos, fator/m²)
SERVICOS_POR_FASE: dict[str, list[ServicoFase]] = {
    "Serviços preliminares": [
        ("Limpeza e preparo do terreno", "97622", "m2", 480, 1.0),
        ("Tapume de obra", "74209/001", "m2", 5200, 0.5),
        ("Locação da obra", "99059", "m2", 350, 1.0),
    ],
    "Fundação": [
        ("Escavação manual de valas", "93358", "m3", 5500, 0.35),
        ("Fôrma de madeira para fundação", "92775", "m2", 7500, 0.6),
        ("Armação em aço CA-50 (fundação)", "92916", "kg", 1200, 9.0),
        ("Concreto fck 25 MPa (fundação)", "94966", "m3", 55000, 0.12),
        ("Impermeabilização de baldrame", "98557", "m2", 2800, 0.5),
    ],
    "Alvenaria": [
        ("Alvenaria de blocos cerâmicos", "87905", "m2", 8000, 1.9),
        ("Vergas e contravergas", "92761", "m", 4500, 0.5),
        ("Chapisco das alvenarias", "87878", "m2", 800, 3.6),
    ],
    "Cobertura": [
        ("Estrutura de madeira para telhado", "92543", "m2", 9000, 1.1),
        ("Telhamento com telha cerâmica", "94213", "m2", 5500, 1.1),
        ("Calhas e rufos", "94227", "m", 6000, 0.3),
    ],
    "Instalações": [
        ("Instalação elétrica (pontos e fiação)", "91926", "m2", 5500, 1.0),
        ("Instalação hidrossanitária", "89712", "m2", 4800, 1.0),
        ("Louças e metais", "86888", "un", 25000, 0.06),
    ],
    "Revestimento": [
        ("Emboço/reboco de paredes", "87879", "m2", 3800, 3.0),
        ("Contrapiso", "87703", "m2", 4200, 1.0),
        ("Revestimento cerâmico de piso", "87251", "m2", 6500, 1.2),
        ("Revestimento cerâmico de parede", "87265", "m2", 7000, 0.8),
    ],
    "Esquadrias": [
        ("Portas de madeira (folha + batente)", "90821", "un", 45000, 0.09),
        ("Janelas de alumínio", "94573", "m2", 38000, 0.18),
        ("Ferragens e fechaduras", "91303", "un", 12000, 0.09),
    ],
    "Pintura": [
        ("Massa corrida e selador", "88485", "m2", 1400, 2.6),
        ("Pintura látex (2 demãos)", "88489", "m2", 2200, 2.6),
    ],
    "Entrega": [
        ("Limpeza final da obra", "9537", "m2", 1500, 1.0),
        ("Vistoria e retoques", "97644", "m2", 800, 0.8),
    ],
}


@dataclass(frozen=True)
class FaseEstimada:
    """Um serviço estimado de uma fase que ainda não tem orçamento (descrito e cruzado com o SINAPI)."""

    fase: str
    descricao: str
    codigo: str
    unidade: str
    quantidade_milesimos: int
    valor_centavos: int


def estimar_fases_faltantes(valor_por_fase_real: dict[str, int], apenas: list[str] | None = None) -> list[FaseEstimada]:
    """Para cada fase padrão que ainda não tem orçamento, DESCREVE os serviços típicos (com código SINAPI, unidade,
    quantidade e valor estimados), para o cronograma prever a obra toda — e o motor cruzar as dependências de verdade.
    A quantidade sai da área construída estimada (valor já orçado → porte ÷ CUB → área → quantidade de cada serviço).

    valor_por_fase_real: {fase: valor em centavos} do que já está orçado. Vazio → não há como estimar (devolve [])."""
    reais = {f: v for f, v in valor_por_fase_real.items() if f in DISTRIBUICAO_FASE and v > 0}
    fracao_coberta = sum(DISTRIBUICAO_FASE[f] for f in reais)
    if not reais or fracao_coberta <= 0:
        return []
    porte = sum(reais.values()) / fracao_coberta   # custo total estimado da obra (centavos)
    area_m2 = max(1.0, porte / CUB_POR_M2)          # área construída estimada (m²)
    alvo = [f for f in FASES if f in SERVICOS_POR_FASE and f not in reais and (apenas is None or f in apenas)]
    out: list[FaseEstimada] = []
    for f in sorted(alvo, key=ordem_da_fase):
        for descricao, codigo, unidade, custo_unit, fator in SERVICOS_POR_FASE[f]:
            quantidade = max(1000, round(area_m2 * fator * 1000))   # milésimos da unidade
            valor = round(quantidade / 1000 * custo_unit)
            if valor <= 0:
                continue
            out.append(FaseEstimada(fase=f, descricao=descricao, codigo=codigo, unidade=unidade,
                                    quantidade_milesimos=quantidade, valor_centavos=valor))
    return out
