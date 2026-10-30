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
    quem: str
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
