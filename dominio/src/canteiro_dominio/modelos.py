"""Modelos de documento. Cada classe é um documento de uma coleção, com `tenant_id` sempre presente.

Regras de portabilidade (Mongo agora, Firestore depois):
- nada de lista que cresce sem limite dentro de um documento (movimentos, mensagens e registro são documentos próprios);
- referências por ID (string), sem joins: quem precisa de dado de outra coleção busca pelo ID ou lê uma projeção;
- datas em UTC com fuso (datetime aware).
"""
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .tipos import Centavos, Milesimos, novo_id


def agora() -> datetime:
    return datetime.now(UTC)


class Documento(BaseModel):
    id: str = Field(default_factory=novo_id)
    tenant_id: str
    criado_em: datetime = Field(default_factory=agora)


class Obra(Documento):
    nome: str
    cidade: str = ""
    cep: str = ""
    verba: Centavos = 0
    status: str = "em_andamento"          # em_andamento | em_orcamento | concluida
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
    unidade: str = ""
    quantidade: Milesimos = 0              # negativa só em aditivo (supressão)
    valor_unitario: Centavos = 0
    total: Centavos = 0                    # o total da linha na planilha, quando ela tem: é o número do contrato
    linha: int = 0


class CurvaABC(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class TipoInsumo(StrEnum):
    MATERIAL = "material"                  # tem estoque: entra, sai, conta
    SERVICO = "servico"                    # terraplanagem, empreiteiro: contratado e medido, sem estoque


class Insumo(Documento):
    obra_id: str
    nome: str
    tipo: TipoInsumo = TipoInsumo.MATERIAL
    unidade: str                           # un, sc, kg, m³…
    precisa_ate_o_fim: Milesimos = 0       # quanto a obra ainda vai consumir (orçamento/planejamento)
    curva: CurvaABC = CurvaABC.B           # curva A exige destino na baixa
    valor_unitario: Centavos = 0           # preço unitário do orçamento; 0 = sem preço
    # cadastro desfeito pelo registro: a porta não apaga documento, então ele sai das listas e da Conta. O ID fica
    # reservado: cadastrar o mesmo nome de novo reaproveita este documento.
    arquivado: bool = False


class TipoMovimento(StrEnum):
    ENTRADA = "entrada"
    SAIDA = "saida"
    AJUSTE = "ajuste"                      # contagem: pode ser positivo ou negativo
    TRANSFERENCIA_ENTRADA = "transferencia_entrada"
    TRANSFERENCIA_SAIDA = "transferencia_saida"


class Movimento(Documento):
    """Livro-razão do almoxarifado: só se acrescenta. O ID é a chave de idempotência (evento repetido não duplica)."""
    obra_id: str
    insumo_id: str
    tipo: TipoMovimento
    quantidade: Milesimos                  # sempre positiva, exceto em AJUSTE
    destino: str | None = None             # frente de serviço (obrigatório em saída de curva A)
    etapa: str | None = None               # etapa do orçamento que consumiu (apropriação da saída)
    origem_doc: str | None = None          # PED-…, TRF-…, NF…
    quem: str                              # nome legível de quem lançou (a tela mostra direto, sem buscar usuário)
    usuario_id: str | None = None          # o ID fica à parte, para auditoria (None em dado semeado/importado antigo)
    origem: str = "tela"                   # tela | agente | whatsapp | sistema
    desfaz: str | None = None              # ID do movimento que este estorna


class Saldo(BaseModel):
    """Projeção por insumo, atualizada na mesma transação do movimento. ID = insumo_id."""
    id: str
    tenant_id: str
    obra_id: str
    saldo: Milesimos = 0
    atualizado_em: datetime = Field(default_factory=agora)


class StatusPedido(StrEnum):
    EM_COTACAO = "em_cotacao"
    APROVADO = "aprovado"
    ENVIADO = "enviado"
    EM_TRANSITO = "em_transito"
    RECEBIDO = "recebido"                  # chegou, falta conferir
    PARCIAL = "parcial"                    # recebeu parte: o resto continua a caminho
    CONFERIDO = "conferido"                # tudo entregue (ou o resto encerrado); a tela chama de "Recebido"
    CANCELADO = "cancelado"


class Fornecedor(Documento):
    """Do tenant, vale para todas as obras. O ID sai do CNPJ (ou do nome, sem CNPJ): cadastro repetido colide."""
    nome: str
    cnpj: str = ""                         # só dígitos; "" = não informado
    contato: str = ""
    telefone: str = ""
    email: str = ""


class LinhaPedido(BaseModel):
    """Uma linha do pedido. Nome, unidade e tipo são cópia do insumo na hora do pedido: a folha impressa e a lista de
    entregas não precisam buscar o insumo, e renomear o insumo depois não reescreve o pedido já enviado."""
    insumo_id: str
    nome: str = ""
    unidade: str = ""
    tipo: TipoInsumo = TipoInsumo.MATERIAL
    etapa: str | None = None               # apropriação: etapa do orçamento que paga esta linha
    quantidade: Milesimos
    valor_unitario: Centavos = 0
    total: Centavos = 0
    recebida: Milesimos = 0                # somada a cada entrega; o resto (quantidade − recebida) está a caminho


class Pedido(Documento):
    numero: str                            # PED-0168 (legível; único por tenant via documento de contador)
    obra_id: str
    fornecedor: str
    fornecedor_id: str | None = None
    linhas: list[LinhaPedido]              # até 50 (limite do corpo): a lista não cresce depois de criado
    valor: Centavos                        # soma dos totais das linhas
    status: StatusPedido = StatusPedido.EM_COTACAO
    previsao: str | None = None
    observacao: str = ""
    rastreio: str | None = None
    # Quantas entregas já entraram: numera a próxima e trava o desfazer (só a última entrega pode ser desfeita).
    recebimentos: int = 0
    # Soma dos gastos confirmados como pagamento deste pedido. Só informa: o custo do pedido já conta no realizado
    # pela entrega, e o gasto ligado a ele não soma de novo (`gastos.conta_no_realizado`).
    pago: Centavos = 0

    @model_validator(mode="before")
    @classmethod
    def _linha_unica_antiga(cls, d: Any) -> Any:
        # pedido gravado antes das linhas (MVP-0): um insumo só, com quantidade e valor no próprio pedido
        if isinstance(d, dict) and "linhas" not in d and d.get("insumo_id"):
            recebida = d.get("quantidade_recebida") or (d["quantidade"] if d.get("status") == "conferido" else 0)
            d = {**d, "linhas": [{"insumo_id": d["insumo_id"], "quantidade": d["quantidade"], "total": d["valor"],
                                  "valor_unitario": round(d["valor"] * 1000 / d["quantidade"]) if d["quantidade"]
                                  else 0, "recebida": recebida}]}
        return d


class Recebimento(Documento):
    """Uma entrega (material) ou medição (serviço) de um pedido, com a nota. As entradas no almoxarifado têm ID
    derivado deste documento e são estornadas juntas no desfazer."""
    obra_id: str
    pedido_id: str
    pedido_numero: str
    numero: int                            # 1ª, 2ª… entrega do pedido
    nf_numero: str = ""
    nf_data: str | None = None             # "AAAA-MM-DD"
    nf_valor: Centavos | None = None       # o que a nota diz; a tela compara com `valor`
    valor: Centavos = 0                    # as linhas pelo preço do pedido
    linhas: list[dict[str, Any]]           # {indice, insumo_id, nome, unidade, tipo, etapa, quantidade, valor}
    movimentos: list[str] = Field(default_factory=list)
    quem: str = ""
    usuario_id: str | None = None
    desfeito: bool = False


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
    CRIAR_PEDIDO = "criar_pedido"
    AVANCAR_PEDIDO = "avancar_pedido"
    AJUSTE_ESTOQUE = "ajuste_estoque"


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
    # Quem aprova pode corrigir a proposta antes (ex.: 30 sc em vez de 34). `dados` passa a ser o que foi executado e
    # `dados_originais` guarda o que o agente propôs, para a auditoria mostrar a diferença.
    ajustada: bool = False
    dados_originais: dict[str, Any] | None = None


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
    """Subdivisão de uma etapa (fixa ou extra) dentro de uma obra: "Bloco B", "Mão de obra", "Quadro geral"."""
    obra_id: str
    etapa: str                             # chave fixa ou "x-<id da etapa extra>"
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
    paga entra pelo próprio comprovante.

    Ligado a um pedido (`pedido_id`), o gasto só registra o pagamento daquele pedido: o custo já conta no realizado
    pela entrega, e somar de novo contaria duas vezes (`gastos.conta_no_realizado`)."""
    obra_id: str
    comprovante_id: str | None = None
    valor: Centavos
    data_pagamento: str                    # "AAAA-MM-DD", dia puro, sem hora
    favorecido: str                        # quem recebeu o dinheiro, como no comprovante
    fornecedor_id: str | None = None
    descricao: str
    etapa: str                             # chave fixa ou "x-<id>"; todo gasto tem etapa
    topico_id: str | None = None
    pedido_id: str | None = None
    quem: str = ""
    usuario_id: str | None = None
    estornado: bool = False                # desfeito pelo registro: sai das contas, o documento fica


class UsoIA(Documento):
    """Livro de consumo: uma linha por chamada ao modelo.

    Fonte da fatura (créditos) ou do informativo (chave própria).
    """
    usuario_id: str | None = None
    obra_id: str | None = None
    sessao_id: str | None = None
    rota: str                              # conversa | briefing | whatsapp | nf …
    modelo: str
    tokens_entrada: int = 0
    tokens_saida: int = 0
    tokens_cache_leitura: int = 0
    tokens_cache_escrita: int = 0
    custo_microdolares: int = 0            # inteiro (portável); 1 US$ = 1.000.000
    chave_propria: bool = False
    dia: str = ""                          # AAAA-MM-DD (fuso de São Paulo), para somar por dia sem agregação no banco
