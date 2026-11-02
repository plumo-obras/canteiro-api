"""Modelos de documento. Cada classe é um documento de uma coleção, com `tenant_id` sempre presente.

Regras de portabilidade (Mongo agora, Firestore depois):
- nada de lista que cresce sem limite dentro de um documento (movimentos, mensagens e registro são documentos próprios);
- referências por ID (string), sem joins: quem precisa de dado de outra coleção busca pelo ID ou lê uma projeção;
- datas em UTC com fuso (datetime aware).
"""
from datetime import datetime, UTC
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

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


class Insumo(Documento):
    obra_id: str
    nome: str
    unidade: str                           # un, sc, kg, m³…
    precisa_ate_o_fim: Milesimos = 0       # quanto a obra ainda vai consumir (orçamento/planejamento)
    curva: CurvaABC = CurvaABC.B           # curva A exige destino na baixa
    valor_unitario: Centavos = 0           # preço unitário do orçamento; 0 = sem preço


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
    CONFERIDO = "conferido"                # deu entrada no almoxarifado
    CANCELADO = "cancelado"


class Pedido(Documento):
    numero: str                            # PED-0168 (legível; único por tenant via documento de contador)
    obra_id: str
    insumo_id: str
    quantidade: Milesimos
    fornecedor: str
    valor: Centavos
    status: StatusPedido = StatusPedido.EM_COTACAO
    previsao: str | None = None
    rastreio: str | None = None
    # Preenchidos ao conferir: o que chegou de fato pode diferir do pedido (entrega parcial ou a mais) e a NF amarra a
    # entrada ao documento do fornecedor. O desfazer da conferência limpa os dois.
    quantidade_recebida: Milesimos | None = None
    nf: str | None = None
    # Quantas vezes já foi conferido: entra no ID da entrada no almoxarifado. Conferir, desfazer e conferir de novo é
    # uma segunda entrada (a primeira foi estornada), não a repetição da mesma gravação.
    conferencias: int = 0


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
    registro_id: str | None = None         # a EntradaRegistro da execução: é o que o Desfazer usa (sobrevive a recarregar a tela)
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


class UsoIA(Documento):
    """Livro de consumo: uma linha por chamada ao modelo. Fonte da fatura (créditos) ou do informativo (chave própria)."""
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
