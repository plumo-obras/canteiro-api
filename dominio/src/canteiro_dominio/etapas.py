"""Árvore de gasto da obra: etapas fixas (iguais em toda obra), etapas extras e tópicos que cada obra cria.

Todo gasto cai numa etapa; o tópico é opcional. As etapas fixas moram no código, não no banco, porque são as mesmas
para todo cliente: o leitor de comprovantes recebe sempre a mesma lista (e o prompt dele fica em cache), e as obras
ficam comparáveis entre si ("Fundação custou X% nesta e Y% naquela").

A lista nasceu da planilha real de um prédio (incorporação), por isso cobre também terreno, encargos e vendas.
Regra para mexer nela: **chave nunca sai nem muda**, porque gastos gravados apontam para ela. Nome e descrição podem
ser melhorados; etapa nova entra antes de "Outros".

Esta é outra árvore que a EAP do orçamento (que vem da planilha de cada construtora). A ligação entre as duas (de-para)
fica para depois.
"""
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class TopicoNaArvore:
    id: str
    nome: str


@dataclass(frozen=True)
class EtapaNaArvore:
    chave: str
    nome: str
    inclui: str
    fixa: bool
    topicos: tuple[TopicoNaArvore, ...] = field(default_factory=tuple)


def montar_arvore(extras: Iterable[dict], topicos: Iterable[dict]) -> list[EtapaNaArvore]:
    """Fixas na ordem da lista, extras (pela criação) antes de "Outros", e os tópicos de cada etapa pela criação.

    Arquivados ficam de fora: é o que a tela oferece e o que o leitor pode sugerir. Gasto antigo que aponta para algo
    arquivado continua válido; só não recebe gasto novo.
    """
    def ordem(d: dict):
        # sem data (documento antigo) vai antes; empate pelo ID, que é UUIDv7 e também ordena no tempo
        criado = d.get("criado_em")
        return (criado is not None, criado, d["id"])

    por_etapa: dict[str, list[TopicoNaArvore]] = {}
    for t in sorted((t for t in topicos if not t.get("arquivado")), key=ordem):
        por_etapa.setdefault(t["etapa"], []).append(TopicoNaArvore(t["id"], t["nome"]))

    def etapa(chave: str, nome: str, inclui: str, fixa: bool) -> EtapaNaArvore:
        return EtapaNaArvore(chave, nome, inclui, fixa, tuple(por_etapa.get(chave, ())))

    arvore = [etapa(e.chave, e.nome, e.inclui, True) for e in ETAPAS_FIXAS if e.chave != OUTROS]
    arvore += [etapa(chave_extra(x["id"]), x["nome"], "", False)
               for x in sorted((x for x in extras if not x.get("arquivado")), key=ordem)]
    outros = next(e for e in ETAPAS_FIXAS if e.chave == OUTROS)
    arvore.append(etapa(outros.chave, outros.nome, outros.inclui, True))
    return arvore


# --- índice da árvore: o que está ativo, num documento só por obra ---
#
# Limite (20 extras, 30 tópicos por etapa) e nome sem repetir dependem de TODOS os irmãos ativos, e a transação não
# pode consultar (regra da porta). Por isso cada obra tem um documento com as extras e os tópicos ativos
# (`{"extras": {id: nome}, "topicos": {id: {"etapa", "nome"}}}`): a transação lê esse documento, confere aqui e grava
# a mudança nele junto com a da etapa/tópico. Duas criações ao mesmo tempo gravam o mesmo documento, então uma delas
# é repetida pelo banco e já vê a outra. O tamanho tem teto pelos próprios limites.


def ativar_extra(extras: dict[str, str], *, id_: str | None, nome: str) -> str:
    """Confere que a etapa extra `id_` (None = nova) pode ficar ativa com `nome` e devolve o nome arrumado. Só conta
    no limite quem ainda não está ativa: renomear uma ativa nunca esbarra nele."""
    if id_ not in extras and len(extras) >= MAX_EXTRAS_POR_OBRA:
        raise RegraViolada(f"A obra já tem {MAX_EXTRAS_POR_OBRA} etapas extras. Arquive uma antes.")
    usados = [e.nome for e in ETAPAS_FIXAS] + [n for i, n in extras.items() if i != id_]
    return validar_nome(nome, ja_usados=usados)


def ativar_topico(extras: dict[str, str], topicos: dict[str, dict], *, id_: str | None, etapa: str,
                  nome: str) -> str:
    """O mesmo para tópico: etapa ativa da obra (só para criar ou reativar), até 30 por etapa, nome único nela."""
    ja_ativo = id_ in topicos
    if not ja_ativo:
        if etapa not in CHAVES_FIXAS and etapa.removeprefix(PREFIXO_EXTRA) not in extras:
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
