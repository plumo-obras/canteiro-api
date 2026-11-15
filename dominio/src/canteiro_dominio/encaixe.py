"""O encaixe da planilha da pessoa na lista padrão de etapas (`ETAPAS_FIXAS`): o orçamento padronizado.

Cada construtora escreve a planilha do seu jeito ("7 PAREDES E PAINÉIS", "INFRAESTRUTURA", "ACABAMENTOS"), e as
linhas de quem orça à mão são genéricas ("Mão de obra", "Materiais diversos", uma verba). Encaixada na lista padrão, a
obra fica comparável com as outras ("Fundação custou X% nesta e Y% naquela"), o gasto lançado na lista padrão antes da
planilha continua valendo, e o leitor de comprovantes vê a mesma lista em toda obra.

A sugestão sai por palavras; a pessoa confere na prévia e troca o que estiver errado. Palavra nunca decide calada.
"""
import re
import unicodedata

from .etapas import ETAPAS_FIXAS, OUTROS, normalizar_nome

# Palavras por etapa fixa, sobre o texto sem acento, em começo de palavra; com espaço no fim, só a palavra inteira
# ("gas " não é "gasto"). A ordem importa, porque a primeira que casa decide: o mais específico vem antes ("limpeza do
# terreno" é preliminar, não terreno; "revestimento de parede" não é alvenaria; "pintura de parede" é pintura; "bloco de
# concreto" é alvenaria, não estrutura; "impermeabilização do baldrame" é impermeabilização, não fundação).
PALAVRAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("preliminares", ("limpeza do terreno", "limpeza de terreno", "limpeza e preparo do terreno", "preliminar",
                      "demolic", "mobilizac", "tapume", "barracao", "canteiro", "locacao da obra", "locacao de obra",
                      "placa de obra", "provisori")),
    ("terreno", ("terreno", "itbi", "escritura", "documentac", "cartorio", "iptu", "registro de incorporac")),
    ("projetos", ("projeto", "aprovac", "alvara", "licenc", "despachante", "habite-se", "habite se")),
    ("limpeza", ("limpeza", "encerramento", "desmobiliz", "jardin", "jardim", "paisagis", "entrega da obra",
                 "letreiro")),
    ("impermeabilizacao", ("impermeab", "manta asfalt")),
    ("fachada", ("fachada", "revestimento extern", "pintura extern", "massa extern", "tela fachadeira")),
    ("pintura", ("pintur", "tinta", "latex", "massa corrida", "selador", "verniz", "textura", "grafiato")),
    ("fundacao", ("infraestrutura", "infra estrutura", "infra-estrutura", "fundac", "estaca", "sapata", "baldrame",
                  "radier", "tubulao", "movimento de terra", "movimentacao de terra", "escavac", "aterro", "reaterro",
                  "terraplen", "bota-fora", "bota fora", "compactac", "lastro", "concreto magro")),
    ("pisos", ("revestimento de piso", "revestimento ceramico de piso", "piso ceramico", "piso porcelanato")),
    ("revestimento_interno", ("revestimento de parede", "revestimento interno", "revestimentos internos", "reboco",
                              "emboco", "chapisco", "massa unica", "azulej", "forro", "gesso", "revestiment")),
    ("alvenaria", ("alvenaria", "parede", "vedac", "paineis", "painel", "tijolo", "bloco ceramico",
                   "bloco de concreto", "verga", "contraverga", "muro", "drywall", "divisori")),
    ("estrutura", ("superestrutura", "estrutura", "laje", "viga", "pilar", "concreto", "armac", "armadura", "aco ca",
                   "ferragem", "forma ", "formas", "escoramento", "cimbramento", "escada")),
    ("bancadas", ("bancada", "soleira", "peitori", "granito", "marmore", "tampo")),
    ("pisos", ("contrapiso", "piso", "porcelanat", "rodape", "regularizac", "ceramic", "laminado", "vinilic", "calcada",
               "pavimentac")),
    ("especiais", ("instalacoes especiais", "instalacao especial", "gas ", "ar condicionado", "ar-condicionado",
                   "climatiz", "incendio", "interfone", "cftv", "logica", "telefon", "automac", "alarme")),
    ("eletrica", ("eletric", "eletrodut", "fiac", "cabo", "quadro de distribuic", "disjuntor", "tomada",
                  "interruptor", "luminar", "iluminac", "spda", "para-raio", "para raio", "fotovolt", "energia solar",
                  "conduite")),
    ("hidrossanitaria", ("hidro", "sanitar", "esgot", "agua fria", "agua quente", "pluvia", "louca", "metais",
                         "tubulac", "tubo", "registro", "caixa d", "reservatorio", "drenag", "aquecedor",
                         "aquecimento", "bacia", "lavatorio", "torneira", "chuveiro")),
    ("cobertura", ("cobertura", "telhad", "telha", "calha", "rufo", "cumeeira", "madeiramento", "engradamento")),
    ("esquadrias", ("esquadri", "janela", "porta", "portao", "portoes", "vidro", "serralheri", "guarda-corpo",
                    "guarda corpo", "box ", "caixilho", "batente", "fechadura")),
    ("elevadores", ("elevador",)),
    ("equipamentos", ("ferrament", "equipament", "controle tecnologico", "betoneira", "andaime")),
    ("administracao", ("administrac", "indireta", "despesas gerais", "mestre de obra", "seguranca do trabalho",
                       "epi ", "vigia", "vigilancia")),
    ("pessoal", ("pessoal", "encargo", "folha de pagamento", "fgts", "inss", "ferias", "rescis", "vale-transporte",
                 "vale transporte")),
    ("vendas", ("venda", "comercial", "corretag", "marketing", "propaganda", "comissao")),
    (OUTROS, ("diversos", "outros", "outras", "gerais")),
)

_POSICAO = {e.chave: i for i, e in enumerate(ETAPAS_FIXAS)}
_NOME = {e.chave: e.nome for e in ETAPAS_FIXAS}
_PELO_NOME = {normalizar_nome(e.nome): e.chave for e in ETAPAS_FIXAS}
_REGEX = tuple((chave, re.compile("|".join(r"\b" + re.escape(p.rstrip()) + (r"\b" if p.endswith(" ") else "")
                                           for p in palavras)))
               for chave, palavras in PALAVRAS)


def _texto(t: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9-]+", " ", sem_acento.lower()).split())


def etapa_pelo_nome(texto: str) -> str | None:
    """A etapa fixa que o texto (título de etapa ou linha) indica, ou None quando nada indica. O nome igual ao de uma
    etapa fixa ("Pisos") decide antes das palavras."""
    t = _texto(texto)
    if not t:
        return None
    if (chave := _PELO_NOME.get(normalizar_nome(texto))) is not None:
        return chave
    for chave, rx in _REGEX:
        if rx.search(t):
            return chave
    return None


def etapa_no_contexto(descricao: str, titulo: str = "") -> str:
    """A etapa fixa de uma linha do orçamento, sabendo a etapa da planilha em que ela está (`titulo`).

    A linha que diz o que é ("Porta 0,60", "Pintura látex") vai para a etapa dela, mesmo dentro de uma etapa mista
    ("ACABAMENTOS"). A linha genérica ("Mão de obra", "Materiais diversos", uma verba) herda a etapa do título: é o
    título que diz que aquela mão de obra é de parede. Concreto, fôrma e armação dentro de uma etapa de fundação são
    fundação. Sem nada que indique, Outros."""
    do_titulo = etapa_pelo_nome(titulo) if titulo else None
    do_item = etapa_pelo_nome(descricao)
    if do_item in (None, OUTROS):
        return do_titulo or OUTROS
    if do_item == "estrutura" and do_titulo == "fundacao":
        return "fundacao"
    return do_item


def ordem_da_etapa(chave: str) -> int:
    """Posição da etapa fixa na lista (a ordem da tela e do orçamento encaixado)."""
    return _POSICAO.get(chave, len(_POSICAO))


def nome_da_etapa(chave: str) -> str:
    return _NOME.get(chave, chave)


# --- a previsão da obra (cronograma): fases de obra ↔ etapas fixas ---
#
# A previsão estima as fases de obra que faltam (`cronograma.estimar_fases_faltantes`, a distribuição típica de custo
# por fase). O orçamento encaixado é por etapa fixa: estas duas tabelas levam de um para o outro.

_FASE_DA_ETAPA = {
    "preliminares": "Serviços preliminares", "fundacao": "Fundação", "estrutura": "Estrutura",
    "alvenaria": "Alvenaria", "cobertura": "Cobertura", "hidrossanitaria": "Instalações", "eletrica": "Instalações",
    "especiais": "Instalações", "revestimento_interno": "Revestimento", "pisos": "Revestimento",
    "fachada": "Revestimento", "bancadas": "Revestimento", "impermeabilizacao": "Revestimento",
    "esquadrias": "Esquadrias", "pintura": "Pintura", "limpeza": "Entrega",
}
_ETAPA_DA_FASE = {"Serviços preliminares": "preliminares", "Fundação": "fundacao", "Estrutura": "estrutura",
                  "Alvenaria": "alvenaria", "Cobertura": "cobertura", "Instalações": "hidrossanitaria",
                  "Revestimento": "revestimento_interno", "Esquadrias": "esquadrias", "Pintura": "pintura",
                  "Entrega": "limpeza"}


def fase_da_etapa(chave: str) -> str | None:
    """A fase de obra de uma etapa fixa, para a previsão saber quais fases já têm orçamento; None nas que não são
    obra física (terreno, projetos, administração…)."""
    return _FASE_DA_ETAPA.get(chave)


def etapa_da_estimativa(fase: str, descricao: str) -> str:
    """A etapa fixa de um serviço que a previsão estimou para uma fase: a das palavras do serviço quando ela é da mesma
    fase (a instalação elétrica vai para Elétrica, o contrapiso para Pisos), senão a etapa principal da fase."""
    pela_linha = etapa_pelo_nome(descricao)
    if pela_linha is not None and _FASE_DA_ETAPA.get(pela_linha) == fase:
        return pela_linha
    return _ETAPA_DA_FASE.get(fase, OUTROS)
