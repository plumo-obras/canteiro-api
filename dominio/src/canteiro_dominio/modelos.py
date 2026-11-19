"""Modelos de documento. Cada classe é um documento de uma coleção, com `tenant_id` sempre presente.

Regras de portabilidade (Mongo agora, Firestore depois):
- nada de lista que cresce sem limite dentro de um documento (mensagens e registro são documentos próprios);
- referências por ID (string), sem joins: quem precisa de dado de outra coleção busca pelo ID ou lê uma projeção;
- datas em UTC com fuso (datetime aware).
"""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .tipos import Centavos, Milesimos, novo_id


def agora() -> datetime:
    return datetime.now(UTC)


class Documento(BaseModel):
    id: str = Field(default_factory=novo_id)
    tenant_id: str
    criado_em: datetime = Field(default_factory=agora)


# Situação de obra de quando o Desfazer da aprovação rodava em lotes e podia parar no meio. Não nasce mais, mas obra
# gravada nela ainda volta para o orçamento pelo Desfazer.
DESFAZENDO_APROVACAO = "desfazendo_aprovacao"


class Obra(Documento):
    nome: str
    cidade: str = ""
    cep: str = ""
    verba: Centavos = 0
    # em_andamento | em_orcamento | concluida | desfazendo_aprovacao
    status: str = "em_andamento"
    # Na obra em `desfazendo_aprovacao`: o registro cujo Desfazer ficou pela metade, para a tela oferecer retomar.
    desfazendo_registro_id: str | None = None
    # Passou pelo "Aprovar". Obra antiga fica `em_andamento` sem nunca ter tido aprovação, e ela ainda pode importar
    # o contrato: é o `status` sozinho que não distingue as duas.
    aprovada: bool = False
    # Dados do onboarding: todos opcionais, porque a obra nasce com o nome e o resto chega quando a pessoa souber.
    endereco: str = ""
    uf: str = ""
    tipologia: str | None = None           # casa | sobrado | predio | galpao | reforma | comercial | outro
    area_construida: Milesimos = 0         # m² em milésimos (142,5 m² = 142500); 0 = não informada
    pavimentos: int = 0                    # 0 = não informado
    padrao: str | None = None              # baixo | normal | alto
    regime: str | None = None              # empreitada | administracao
    cliente: str = ""
    inicio: str | None = None              # "AAAA-MM-DD"
    prazo_meses: int = 0                   # 0 = não informado
    # Dados do contrato com o cliente. `verba` é o valor do contrato (o nome ficou do MVP-0).
    numero_contrato: str = ""
    bdi: int = 0                           # centésimos de ponto percentual: 22,91% = 2291; 0 = não informado
    data_base: str | None = None           # "AAAA-MM": mês dos preços de referência (SINAPI etc.)
    # Dados fiscais (`tributos`): INSS da aferição (CNO/SERO) e IBS/CBS. Opcionais como o resto do onboarding.
    cno: str = ""                          # matrícula no Cadastro Nacional de Obras (12 dígitos)
    proprietario: str | None = None        # pf | pj: o fator social do INSS só vale para pessoa física
    destinacao: str | None = None          # uma de `tributos.DESTINACOES` (as da tabela VAU do SERO)
    material: str | None = None            # alvenaria | mista | madeira (percentual da mão de obra presumida)
    area_complementar: Milesimos = 0       # m² complementares cobertos (piscina/quadra cobertas, garagem fora da casa)
    area_descoberta: Milesimos = 0         # m² complementares descobertos (piscina, quadra)
    categoria: str | None = None           # obra_nova | acrescimo | reforma | demolicao; None = da tipologia
    dctfweb_em_dia: bool | None = None     # DCTFWeb entregue em todos os meses da obra (fator de ajuste do INSS)
    vau: Centavos = 0                      # VAU à mão, em centavos por m²; 0 = usar a tabela VAU do SERO
    vau_referencia: str | None = None      # "AAAA-MM" do VAU usado
    finalidade: str | None = None          # uso_proprio | venda | prestacao_servico (IBS/CBS)
    valor_terreno: Centavos = 0            # redutor de ajuste do IBS/CBS na venda
    unidades: int = 0                      # imóveis residenciais vendidos (redutor social do IBS/CBS)


class TipoVersao(StrEnum):
    CONTRATO = "contrato"
    ADITIVO = "aditivo"


class VersaoOrcamento(Documento):
    """Uma importação do orçamento: o contrato (um vigente por obra) ou um aditivo, que soma ao contrato sem tocá-lo.

    Os itens apontam para a versão e só valem enquanto ela está `ativa`. Desfazer é desligar a versão (e religar a que
    ela substituiu), porque a porta de armazenamento não apaga: um documento por versão vira a chave de tudo.
    """
    obra_id: str
    tipo: TipoVersao
    numero: int = 0                        # 0 no contrato; 1, 2… nos aditivos
    descricao: str = ""
    data: str | None = None                # "AAAA-MM-DD"
    arquivo: str = ""
    itens: int = 0
    etapas: int = 0
    total: Centavos = 0
    ativa: bool = True
    substitui: str | None = None           # contrato que esta versão tirou de vigência
    contrato_id: str | None = None         # no aditivo: o contrato vigente quando ele entrou
    aditivos: int = 0                      # no contrato: aditivos vigentes feitos sobre ele (trava o desfazer)
    assinatura: str = ""                   # hash do arquivo + tipo: a mesma chave com outro arquivo é conflito
    # [{etapa, descricao}] das etapas desta versão: os selects de apropriação leem só as versões, sem os 500 itens
    lista_etapas: list[dict[str, str]] = Field(default_factory=list)
    # [{etapa, id, nome}] dos tópicos que saem da numeração (nível 2): a árvore lê daqui, sem os itens. Documento
    # gravado antes do campo não tem a chave, e a leitura monta pelos itens.
    lista_topicos: list[dict[str, str]] = Field(default_factory=list)
    edicoes: int = 0                       # linhas editadas à mão: aprovar confere que nada mudou no meio dela
    # "venda" = a planilha veio com o preço do cliente (com BDI): os itens guardam o custo (preço ÷ (1 + bdi)) e o
    # preço de venda à parte. "custo" = a planilha já é custo (a de quem orça à mão, ou a do próprio software).
    natureza: Literal["custo", "venda"] = "custo"
    bdi: int = 0                           # o BDI usado para tirar o custo da venda (centésimos de %, 2291 = 22,91%)
    padronizada: bool = False              # nas etapas da lista padrão (a importação encaixou cada linha)


class ItemOrcamento(Documento):
    """Uma linha da EAP como veio da planilha: etapa (título) ou item (serviço com quantidade e preço)."""
    obra_id: str
    versao_id: str
    ordem: int                             # posição na versão
    tipo: str                              # etapa | item
    numero: str = ""                       # "3.2.1", como na coluna item
    nivel: int = 1
    etapa: str = ""                        # número da etapa raiz, para somar por etapa sem join
    codigo: str = ""
    fonte: str = ""
    descricao: str
    complemento: str = ""                  # medida, marca ou detalhe da linha ("0,60 x 2,10"), quando a planilha tem
    unidade: str = ""
    quantidade: Milesimos = 0              # negativa só em aditivo (supressão)
    valor_unitario: Centavos = 0
    total: Centavos = 0                    # o total da linha na planilha, quando ela tem: é o número do contrato
    linha: int = 0
    topico_id: str | None = None           # o tópico do orçamento em que o item conta (de `lista_topicos`)
    # a etapa da planilha de onde o item veio ("ESQUADRIA MADEIRA"), quando ele foi encaixado numa etapa padrão de
    # outro nome: é ela que vira o tópico dentro da etapa, para "Mão de obra" de etapas diferentes não se juntar
    grupo: str = ""
    # só na planilha de venda: o preço do cliente da linha; `valor_unitario`/`total` ficam com o custo
    valor_unitario_venda: Centavos | None = None
    total_venda: Centavos | None = None


class Fornecedor(Documento):
    """Do tenant, vale para todas as obras. O ID sai do CNPJ (ou do nome, sem CNPJ): cadastro repetido colide."""
    nome: str
    cnpj: str = ""                         # só dígitos; "" = não informado
    contato: str = ""
    telefone: str = ""
    email: str = ""


class Cronograma(Documento):
    """Cronograma físico-financeiro da obra: % de cada etapa por mês (centésimos; cada etapa soma 10000 ou fica fora).
    Um por obra, ID derivado da obra. Guarda também a posição das medições (quantas, até que mês e o acumulado de cada
    etapa), para a medição nova se conferir lendo um documento só na transação."""
    obra_id: str
    inicio: str                            # "AAAA-MM"
    meses: int
    etapas: list[dict[str, Any]] = Field(default_factory=list)   # [{etapa, percentuais: [int]}]
    versao: int = 1                        # sobe a cada edição: o desfazer de uma edição velha é recusado
    # Linha de base: o cronograma como foi assinado ({inicio, meses, etapas}). Reprogramar (aditivo de prazo) muda o
    # vigente e mantém a base, senão o atraso some no dia em que se reprograma. Só troca se a pessoa pedir.
    base: dict[str, Any] | None = None
    revisoes: int = 0                      # reprogramações desde a linha de base
    medicoes: int = 0
    ultima_medicao: str | None = None      # "AAAA-MM"
    acumulado: dict[str, int] = Field(default_factory=dict)      # etapa → % executado acumulado


class Medicao(Documento):
    """Boletim de medição mensal por etapa. Só a última pode ser desfeita (as seguintes partem do acumulado dela)."""
    obra_id: str
    numero: int
    mes: str                               # "AAAA-MM"
    etapas: list[dict[str, Any]]           # [{etapa, pct_acumulado, pct_periodo, valor_periodo}]
    valor: Centavos = 0
    pct_obra_acumulado: int = 0            # centésimos de %, ponderado pelo orçado atual de cada etapa
    observacao: str = ""
    anterior: dict[str, int] = Field(default_factory=dict)       # o acumulado antes desta: é o que o desfazer volta
    mes_anterior: str | None = None
    quem: str = ""
    usuario_id: str | None = None
    desfeito: bool = False


class StatusProposta(StrEnum):
    PENDENTE = "pendente"
    APROVADA = "aprovada"
    RECUSADA = "recusada"
    DESFEITA = "desfeita"


class TipoProposta(StrEnum):
    CRONOGRAMA = "cronograma"                 # a IA propõe o cronograma (etapas × meses); aprovar grava-o
    # De compras, que saíram do produto: só para ler o histórico. Ninguém cria nem aprova (a API responde 410).
    CRIAR_PEDIDO = "criar_pedido"
    AVANCAR_PEDIDO = "avancar_pedido"
    AJUSTE_ESTOQUE = "ajuste_estoque"
    REGISTRAR_ENTREGA = "registrar_entrega"


class Proposta(Documento):
    """O que o agente quer fazer. Nada é gravado até alguém aprovar."""
    obra_id: str
    tipo: TipoProposta
    titulo: str
    conta: str = ""                        # a linha de conta que justifica ("precisa … = falta …")
    justificativa: str = ""                # por que o agente propõe, em uma frase (aparece no cartão)
    fonte: str = ""                        # de onde veio o dado
    dados: dict[str, Any]                  # payload validado pela API conforme o tipo
    sugerido_por: str = "agente"
    status: StatusProposta = StatusProposta.PENDENTE
    decidido_por: str | None = None
    decidido_em: datetime | None = None
    motivo_recusa: str | None = None
    # a EntradaRegistro da execução: é o que o Desfazer usa (sobrevive a recarregar a tela)
    registro_id: str | None = None
    # Na proposta de compra (histórico), quem aprovava podia corrigir antes: `dados` é o que foi executado e
    # `dados_originais` o que o agente propôs. A de cronograma não se ajusta.
    ajustada: bool = False
    dados_originais: dict[str, Any] | None = None
    # O que aprovar faz, decidido pela API ao criar ("cronograma"; no histórico, os de compra). O botão diz por ele.
    efeito: str | None = None


class EntradaRegistro(Documento):
    """Auditoria: quem fez o quê, quando e de onde veio. `desfazer` guarda o necessário para a operação inversa."""
    obra_id: str | None = None
    quem: str
    origem: str                            # tela | agente | whatsapp | sistema
    texto: str
    proposta_id: str | None = None
    desfazer: dict[str, Any] | None = None
    desfeito: bool = False


class EtapaExtra(Documento):
    """Etapa que a obra cria além das fixas (`etapas.ETAPAS_FIXAS`). Na árvore e no gasto ela aparece como
    `etapas.chave_extra(id)`. Não se apaga (a porta não apaga): arquivada, sai da árvore e não recebe gasto novo."""
    obra_id: str
    nome: str
    arquivado: bool = False
    atualizado_em: datetime = Field(default_factory=agora)


class Topico(Documento):
    """Subdivisão de uma etapa (do orçamento, padrão ou extra) dentro de uma obra: "Bloco B", "Quadro geral"."""
    obra_id: str
    etapa: str                             # código da etapa do orçamento, chave da lista padrão ou "x-<id>"
    nome: str
    arquivado: bool = False
    atualizado_em: datetime = Field(default_factory=agora)


class StatusComprovante(StrEnum):
    A_REVISAR = "a_revisar"                # enviado; não conta em nenhuma conta até alguém confirmar
    CONFIRMADO = "confirmado"              # virou `Gasto`
    DESCARTADO = "descartado"              # não era gasto da obra, foto repetida… O arquivo continua guardado.


class Comprovante(Documento):
    """Arquivo de um gasto (foto, PDF) esperando revisão. A IA lê e sugere; só a confirmação de uma pessoa cria o
    `Gasto`. A leitura é ajuda, não condição: sem ela (erro, recusa, sem crédito) a revisão abre em branco."""
    obra_id: str
    arquivo: str                           # caminho no armazenamento de arquivos privado; só sai por rota com login
    tipo_conteudo: str                     # image/jpeg, image/png, image/webp, application/pdf
    tamanho: int                           # bytes
    sha256: str                            # o mesmo arquivo enviado duas vezes vira o mesmo comprovante
    # "Do que é?" escrito por quem enviou ("empreiteiro, alvenaria"): o Pix sozinho não diz para que foi
    legenda: str = ""
    quem: str = ""
    usuario_id: str | None = None
    status: StatusComprovante = StatusComprovante.A_REVISAR
    leitura: dict[str, Any] | None = None  # `leitura.LeituraComprovante` como o agente devolveu (já validada)
    erro_leitura: str | None = None        # por que a leitura não saiu, em frase para a pessoa
    lido_em: datetime | None = None
    modelo_leitura: str | None = None
    gasto_id: str | None = None
    motivo_descarte: str = ""
    atualizado_em: datetime = Field(default_factory=agora)


class Gasto(Documento):
    """Dinheiro que saiu, numa data, numa etapa da obra. Nota a prazo ou boleto em aberto não é gasto: cada parcela
    paga entra pelo próprio comprovante."""
    obra_id: str
    comprovante_id: str | None = None
    valor: Centavos
    data_pagamento: str                    # "AAAA-MM-DD", dia puro, sem hora
    favorecido: str                        # quem recebeu o dinheiro, como no comprovante
    fornecedor_id: str | None = None
    descricao: str
    # código da etapa do orçamento, chave da lista padrão ou "x-<id>" (extra); None = "ver depois", ainda a definir
    etapa: str | None = None
    topico_id: str | None = None
    quem: str = ""
    usuario_id: str | None = None
    estornado: bool = False                # desfeito pelo registro: sai das contas, o documento fica
    # Para os tributos: o que este pagamento comprova no INSS da obra (`tributos.NATUREZAS`; None = a classificar)
    # e o IBS/CBS destacado na nota, que vira crédito.
    natureza_fiscal: str | None = None
    ibs_cbs: Centavos = 0
    # Na nota de mão de obra: traz o CNO da obra (sem ele a Receita não liga a nota à obra) e a retenção de 11%.
    nf_cno: bool | None = None
    retencao: Centavos = 0


class UsoIA(Documento):
    """Livro de consumo: uma linha por chamada ao modelo.

    Fonte da fatura (créditos) ou do informativo (chave própria).
    """
    usuario_id: str | None = None
    obra_id: str | None = None
    sessao_id: str | None = None
    rota: str                              # conversa | briefing | whatsapp | comprovante …
    modelo: str
    tokens_entrada: int = 0
    tokens_saida: int = 0
    tokens_cache_leitura: int = 0
    tokens_cache_escrita: int = 0
    custo_microdolares: int = 0            # inteiro (portável); 1 US$ = 1.000.000
    chave_propria: bool = False
    dia: str = ""                          # AAAA-MM-DD (fuso de São Paulo), para somar por dia sem agregação no banco
