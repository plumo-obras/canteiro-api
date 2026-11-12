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
from enum import StrEnum

JORNADA_H = 8  # horas úteis por dia de trabalho (1 turno)
DIAS_UTEIS_SEMANA = 5


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
        dur = duracao_dias(s.quantidade_milesimos, hh, equipe)
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
