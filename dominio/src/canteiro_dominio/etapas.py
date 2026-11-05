"""Árvore de gasto da obra: as etapas onde o gasto entra, as etapas extras e os tópicos que cada obra cria.

Todo gasto cai numa etapa; o tópico é opcional. A base da árvore depende da obra:

- **Obra com orçamento**: as etapas do orçamento vigente (contrato + aditivos, na ordem dele, chave = o código da etapa
  da EAP, como `"18"`). É o que faz o gasto somar no "realizado" da etapa certa do orçamento: a pessoa pensa na obra
  pela planilha dela ("18 INSTALAÇÃO ELÉTRICA - 220V"), não por uma lista genérica.
- **Obra sem orçamento**: a lista padrão (`ETAPAS_FIXAS`), que mora no código e é igual para todo cliente.

Nas duas, as etapas extras da obra (chave `x-<id>`) entram depois; com orçamento, o gasto nelas fica "fora do
orçamento" (não engorda etapa nenhuma).

A lista padrão nasceu da planilha real de um prédio (incorporação), por isso cobre também terreno, encargos e vendas.
Regra para mexer nela: **chave nunca sai nem muda**, porque gastos gravados apontam para ela (inclusive os de obras que
depois importaram orçamento: viram "sem etapa do orçamento" e continuam com o nome). Nome e descrição podem ser
melhorados; etapa nova entra antes de "Outros".
"""
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .regras import RegraViolada


@dataclass(frozen=True)
class EtapaFixa:
    chave: str
    nome: str
    inclui: str          # o que entra nela: guia a pessoa na tela e o leitor de comprovantes na sugestão


ETAPAS_FIXAS: tuple[EtapaFixa, ...] = (
    EtapaFixa("terreno", "Terreno e documentação",
              "compra do terreno, ITBI, escritura, cartório, registro de incorporação, IPTU"),
    EtapaFixa("projetos", "Projetos e aprovações",
              "projetos, despachante, alvará, aprovação do bombeiro, taxas de ligação de água e luz"),
    EtapaFixa("preliminares", "Serviços preliminares",
              "demolição, limpeza do terreno, locação da obra, tapume, barracão, ligações provisórias"),
    EtapaFixa("fundacao", "Fundação", "estacas, tubulão, sapatas, blocos e vigas baldrame"),
    EtapaFixa("estrutura", "Estrutura e lajes",
              "armação, concreto usinado, formas, escoramento, pilares, vigas, lajes pré-moldadas"),
    EtapaFixa("alvenaria", "Alvenaria e vedações", "tijolo, bloco, paredes, painéis, vergas"),
    EtapaFixa("hidrossanitaria", "Instalações hidrossanitárias", "água fria e quente, esgoto, louças e metais"),
    EtapaFixa("eletrica", "Instalações elétricas", "eletrodutos, fiação, quadros, tomadas, iluminação"),
    EtapaFixa("especiais", "Instalações especiais", "gás, ar-condicionado, incêndio, interfone, CFTV"),
    EtapaFixa("impermeabilizacao", "Impermeabilização", "manta asfáltica, argamassa impermeabilizante"),
    EtapaFixa("cobertura", "Cobertura", "engradamento, telhas, rufos, calhas"),
    EtapaFixa("esquadrias", "Esquadrias", "portas, janelas, guarda-corpo, porta corta-fogo, vidros"),
    EtapaFixa("revestimento_interno", "Revestimentos internos", "chapisco, reboco, azulejo, forro de gesso"),
    EtapaFixa("fachada", "Fachada", "massa externa, revestimento e pintura externa, tela fachadeira"),
    EtapaFixa("pisos", "Pisos", "contrapiso, porcelanato, cerâmica, rodapé"),
    EtapaFixa("bancadas", "Bancadas, soleiras e peitoris", "granito, mármore, bancadas, soleiras, peitoris"),
    EtapaFixa("pintura", "Pintura", "tinta, massa, lixa e mão de obra de pintura interna"),
    EtapaFixa("elevadores", "Elevadores", "compra, instalação e acabamento de elevador"),
    EtapaFixa("equipamentos", "Equipamentos e ferramentas",
              "aluguel e compra de equipamentos e ferramentas, controle tecnológico"),
    EtapaFixa("administracao", "Administração da obra",
              "água, luz e internet do canteiro, mestre de obras, segurança do trabalho, EPI, contador"),
    EtapaFixa("pessoal", "Pessoal e encargos",
              "guias de FGTS e INSS, rescisão, férias, vale-transporte, alimentação, exames, seguro de vida"),
    EtapaFixa("limpeza", "Limpeza e entrega", "limpeza da obra, desmobilização, jardim, placas, letreiro"),
    EtapaFixa("vendas", "Vendas e comercial", "comissão de corretor, propaganda, imposto RET"),
    EtapaFixa("outros", "Outros", "o que não couber em nenhuma outra etapa"),
)

CHAVES_FIXAS = frozenset(e.chave for e in ETAPAS_FIXAS)
_NOMES_FIXOS = {e.chave: e.nome for e in ETAPAS_FIXAS}
OUTROS = "outros"
# Etapa extra é "x-" + o ID do documento: não colide com chave fixa e não muda quando a pessoa renomeia.
PREFIXO_EXTRA = "x-"
MAX_EXTRAS_POR_OBRA = 20
MAX_TOPICOS_POR_ETAPA = 30     # também mantém curta a lista que vai para o leitor de comprovantes
MAX_NOME = 60


def chave_extra(etapa_extra_id: str) -> str:
    return PREFIXO_EXTRA + etapa_extra_id


def normalizar_nome(nome: str) -> str:
    """Para comparar nomes: sem acento, minúsculo, espaços simples. "Elétrica  Bloco B" = "eletrica bloco b"."""
    t = unicodedata.normalize("NFD", nome).encode("ascii", "ignore").decode().lower()
    return " ".join(t.split())


def validar_nome(nome: str, *, ja_usados: Iterable[str] = ()) -> str:
    """Nome de etapa extra ou de tópico, já arrumado. `ja_usados` são os nomes com que ele não pode coincidir (as
    etapas da obra, para etapa extra; os tópicos da mesma etapa, para tópico)."""
    limpo = " ".join(nome.split())
    if not limpo:
        raise RegraViolada("Dê um nome.")
    if len(limpo) > MAX_NOME:
        raise RegraViolada(f"O nome pode ter até {MAX_NOME} caracteres.")
    n = normalizar_nome(limpo)
    for usado in ja_usados:
        if normalizar_nome(usado) == n:
            raise RegraViolada(f"Já existe: {usado}.")
    return limpo


# De onde vem a etapa de um gasto: do orçamento da obra, da lista padrão (obra sem orçamento), extra da obra, ou uma
# chave que não está mais na árvore (gasto lançado na lista padrão antes de a obra importar orçamento, ou etapa de
# uma versão desfeita): esse é o "sem etapa do orçamento", que a pessoa reclassifica.
Origem = Literal["orcamento", "padrao", "extra", "sem_etapa_do_orcamento"]


def _chave_sem_numero(descricao: str) -> str:
    # a mesma normalização que a importação usa para a etapa sem número (a chave dela é o próprio nome)
    t = unicodedata.normalize("NFD", descricao).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())[:60]


def nome_etapa_orcamento(etapa: str, descricao: str) -> str:
    """"18 INSTALAÇÃO ELÉTRICA - 220V": com o código na frente, como na planilha da construtora. Etapa sem número tem
    o próprio nome como chave, e repetir não ajuda."""
    descricao = " ".join(descricao.split())
    if not descricao:
        return f"Etapa {etapa}"
    return descricao if _chave_sem_numero(descricao) == etapa else f"{etapa} {descricao}"


def origem_da_etapa(chave: str, etapas_orcamento: Collection[str]) -> Origem:
    """`etapas_orcamento` = as chaves das etapas do orçamento vigente (vazio = obra sem orçamento)."""
    if chave.startswith(PREFIXO_EXTRA):
        return "extra"
    if chave in etapas_orcamento:
        return "orcamento"
    if not etapas_orcamento and chave in CHAVES_FIXAS:
        return "padrao"
    return "sem_etapa_do_orcamento"


def nome_da_etapa(chave: str, *, orcamento: dict[str, str], extras: dict[str, str]) -> str:
    """Nome para mostrar de qualquer chave gravada num gasto. `orcamento` = {etapa: descrição} vigente; `extras` =
    {id: nome}, inclusive as arquivadas (o gasto antigo continua com nome)."""
    if chave in orcamento:
        return nome_etapa_orcamento(chave, orcamento[chave])
    if chave.startswith(PREFIXO_EXTRA) and chave.removeprefix(PREFIXO_EXTRA) in extras:
        return extras[chave.removeprefix(PREFIXO_EXTRA)]
    return _NOMES_FIXOS.get(chave) or f"Etapa {chave}"


@dataclass(frozen=True)
class TopicoNaArvore:
    id: str
    nome: str


@dataclass(frozen=True)
class EtapaNaArvore:
    chave: str
    nome: str
    inclui: str
    fixa: bool                              # não se renomeia nem se arquiva (padrão e do orçamento)
    topicos: tuple[TopicoNaArvore, ...] = field(default_factory=tuple)
    origem: Origem = "padrao"


def montar_arvore(extras: Iterable[dict], topicos: Iterable[dict],
                  orcamento: Sequence[tuple[str, str]] = ()) -> list[EtapaNaArvore]:
    """A árvore que a tela oferece e o leitor pode sugerir. `orcamento` = as etapas do orçamento vigente, na ordem
    dele, como (código, descrição); vazio = obra sem orçamento.

    Com orçamento: as etapas dele e depois as extras (pela criação). Sem: a lista padrão, com as extras antes de
    "Outros". Os tópicos de cada etapa pela criação, presos à chave da etapa (no orçamento, o código).

    Arquivados ficam de fora. Gasto antigo que aponta para algo arquivado (ou para a lista padrão, numa obra que depois
    importou orçamento) continua valendo nas contas; só não recebe gasto novo.
    """
    def ordem(d: dict):
        # sem data (documento antigo) vai antes; empate pelo ID, que é UUIDv7 e também ordena no tempo
        criado = d.get("criado_em")
        return (criado is not None, criado, d["id"])

    por_etapa: dict[str, list[TopicoNaArvore]] = {}
    for t in sorted((t for t in topicos if not t.get("arquivado")), key=ordem):
        por_etapa.setdefault(t["etapa"], []).append(TopicoNaArvore(t["id"], t["nome"]))

    def etapa(chave: str, nome: str, inclui: str, fixa: bool, origem: Origem) -> EtapaNaArvore:
        return EtapaNaArvore(chave, nome, inclui, fixa, tuple(por_etapa.get(chave, ())), origem)

    da_obra = [etapa(chave_extra(x["id"]), x["nome"], "", False, "extra")
               for x in sorted((x for x in extras if not x.get("arquivado")), key=ordem)]
    base = [(c, d) for c, d in orcamento if c]
    if base:
        vistas: set[str] = set()
        arvore = []
        for c, d in base:
            if c not in vistas:
                vistas.add(c)
                arvore.append(etapa(c, nome_etapa_orcamento(c, d), "", True, "orcamento"))
        return arvore + da_obra
    arvore = [etapa(e.chave, e.nome, e.inclui, True, "padrao") for e in ETAPAS_FIXAS if e.chave != OUTROS]
    outros = next(e for e in ETAPAS_FIXAS if e.chave == OUTROS)
    return arvore + da_obra + [etapa(outros.chave, outros.nome, outros.inclui, True, "padrao")]


# --- índice da árvore: o que está ativo, num documento só por obra ---
#
# Limite (20 extras, 30 tópicos por etapa) e nome sem repetir dependem de TODOS os irmãos ativos, e a transação não
# pode consultar (regra da porta). Por isso cada obra tem um documento com as extras e os tópicos ativos
# (`{"extras": {id: nome}, "topicos": {id: {"etapa", "nome"}}}`): a transação lê esse documento, confere aqui e grava
# a mudança nele junto com a da etapa/tópico. Duas criações ao mesmo tempo gravam o mesmo documento, então uma delas
# é repetida pelo banco e já vê a outra. O tamanho tem teto pelos próprios limites.


def ativar_extra(extras: dict[str, str], *, id_: str | None, nome: str,
                 nomes_base: Iterable[str] | None = None) -> str:
    """Confere que a etapa extra `id_` (None = nova) pode ficar ativa com `nome` e devolve o nome arrumado. Só conta
    no limite quem ainda não está ativa: renomear uma ativa nunca esbarra nele. `nomes_base` = os nomes da base da
    árvore (as etapas do orçamento; padrão = a lista fixa)."""
    if id_ not in extras and len(extras) >= MAX_EXTRAS_POR_OBRA:
        raise RegraViolada(f"A obra já tem {MAX_EXTRAS_POR_OBRA} etapas extras. Arquive uma antes.")
    base = [e.nome for e in ETAPAS_FIXAS] if nomes_base is None else list(nomes_base)
    usados = base + [n for i, n in extras.items() if i != id_]
    return validar_nome(nome, ja_usados=usados)


def nomes_da_base(orcamento: Sequence[tuple[str, str]]) -> list[str]:
    """Os nomes com que uma etapa extra não pode coincidir: a descrição de cada etapa do orçamento, com e sem o
    código, ou a lista padrão na obra sem orçamento."""
    if not any(c for c, _ in orcamento):
        return [e.nome for e in ETAPAS_FIXAS]
    return [n for c, d in orcamento if c for n in (d, nome_etapa_orcamento(c, d)) if n.strip()]


def ativar_topico(extras: dict[str, str], topicos: dict[str, dict], *, id_: str | None, etapa: str,
                  nome: str, chaves_base: Collection[str] | None = CHAVES_FIXAS) -> str:
    """O mesmo para tópico: etapa ativa da obra (só para criar ou reativar), até 30 por etapa, nome único nela.

    `chaves_base` = as chaves da base da árvore (as do orçamento, ou a lista padrão). None = não confere a base, só
    as extras: é o caso do Desfazer, que roda dentro da transação e não pode consultar o orçamento (regra da porta);
    reativar um tópico de etapa que saiu da árvore não faz mal, ele só não aparece."""
    ja_ativo = id_ in topicos
    if not ja_ativo:
        if etapa.startswith(PREFIXO_EXTRA):
            fora = etapa.removeprefix(PREFIXO_EXTRA) not in extras
        else:
            fora = chaves_base is not None and etapa not in chaves_base
        if fora:
            raise RegraViolada("Escolha uma etapa da obra." if id_ is None
                               else "A etapa deste tópico está arquivada: reative a etapa antes.")
        if sum(1 for t in topicos.values() if t["etapa"] == etapa) >= MAX_TOPICOS_POR_ETAPA:
            raise RegraViolada(f"A etapa já tem {MAX_TOPICOS_POR_ETAPA} tópicos. Arquive um antes.")
    usados = [t["nome"] for i, t in topicos.items() if i != id_ and t["etapa"] == etapa]
    return validar_nome(nome, ja_usados=usados)


def indice_da_arvore(extras: Iterable[dict], topicos: Iterable[dict]) -> dict:
    """Índice a partir dos documentos (para a obra que ainda não tem um)."""
    return {"extras": {x["id"]: x["nome"] for x in extras if not x.get("arquivado")},
            "topicos": {t["id"]: {"etapa": t["etapa"], "nome": t["nome"]} for t in topicos if not t.get("arquivado")}}


def validar_classificacao(arvore: list[EtapaNaArvore], etapa: str, topico_id: str | None) -> None:
    """Etapa obrigatória e da árvore da obra; tópico opcional, mas, se vier, tem de ser daquela etapa."""
    no = next((e for e in arvore if e.chave == etapa), None)
    if no is None:
        raise RegraViolada("Escolha uma etapa da obra.")
    if topico_id is not None and all(t.id != topico_id for t in no.topicos):
        raise RegraViolada(f"Esse tópico não está na etapa {no.nome}.")
