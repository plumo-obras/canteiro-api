"""Corpos de requisição e respostas que não são documentos. Quantidade em milésimos e dinheiro em centavos."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from canteiro_dominio.modelos import EntradaRegistro, Insumo, Obra, Proposta, StatusPedido, TipoProposta


class Entrada(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntrarCorpo(Entrada):
    email: str = Field(min_length=3, max_length=254)
    senha: str = Field(min_length=1, max_length=256)


class RenovarCorpo(Entrada):
    renovacao: str = Field(min_length=10, max_length=200)


class UsuarioResumo(BaseModel):
    id: str
    nome: str
    tenant_id: str
    papel: str


class Sessao(BaseModel):
    acesso: str
    renovacao: str
    usuario: UsuarioResumo


class ContaSaida(BaseModel):
    precisa: int
    saldo: int
    a_caminho: int
    cotando: int
    transferencia: int
    falta: int
    comprar: int


class InsumoComConta(BaseModel):
    insumo: Insumo
    conta: ContaSaida


class MovimentoCorpo(Entrada):
    insumo_id: str
    # transferência entre obras ainda não existe no MVP-0
    tipo: Literal["entrada", "saida", "ajuste"]
    quantidade: int = Field(description="milésimos; positiva, exceto em ajuste (que carrega o sinal)")
    destino: str | None = Field(None, max_length=200)
    origem_doc: str | None = Field(None, max_length=100)
    chave_idempotencia: str = Field(min_length=8, max_length=200)


class PedidoCorpo(Entrada):
    insumo_id: str
    quantidade: int = Field(gt=0, description="milésimos")
    fornecedor: str = Field(min_length=1, max_length=200)
    valor: int = Field(ge=0, description="centavos")
    previsao: str | None = Field(None, max_length=50)


class AvancarCorpo(Entrada):
    para: StatusPedido


class AvancarPedidoDados(Entrada):
    pedido_id: str
    para: StatusPedido


class AjusteEstoqueDados(Entrada):
    insumo_id: str
    quantidade: int = Field(description="milésimos, com sinal (+ sobrou na contagem, − faltou)")
    motivo: str = Field(min_length=1, max_length=500)


DADOS_POR_TIPO: dict[TipoProposta, type[Entrada]] = {
    TipoProposta.CRIAR_PEDIDO: PedidoCorpo,
    TipoProposta.AVANCAR_PEDIDO: AvancarPedidoDados,
    TipoProposta.AJUSTE_ESTOQUE: AjusteEstoqueDados,
}


class PropostaCorpo(Entrada):
    obra_id: str
    tipo: TipoProposta
    titulo: str = Field(min_length=1, max_length=300)
    conta: str = Field("", max_length=1000)
    justificativa: str = Field("", max_length=1000)
    fonte: str = Field("", max_length=1000)
    dados: dict


class RecusarCorpo(Entrada):
    motivo: str = Field(min_length=1, max_length=1000)


class LinhaImportada(BaseModel):
    linha: int
    nome: str
    unidade: str
    saldo: int
    precisa_ate_o_fim: int
    curva: str


class ErroLinha(BaseModel):
    linha: int
    campo: str | None = None
    mensagem: str


class Previa(BaseModel):
    aplicado: bool
    linhas: list[LinhaImportada]
    erros: list[ErroLinha]


class Soma(BaseModel):
    custo: int = Field(0, description="microdólares")
    tokens: int = 0


class SomaDia(Soma):
    dia: str


class SomaObra(Soma):
    obra_id: str | None


class SomaRota(Soma):
    rota: str


class RelatorioUso(BaseModel):
    total: Soma
    por_dia: list[SomaDia]
    por_obra: list[SomaObra]
    por_rota: list[SomaRota]


class Creditos(BaseModel):
    saldo_microdolares: int
    franquia: int
    chave_propria: bool
    usuario_id: str
    tenant_id: str


# --- chave própria da Anthropic ---

class ChaveCorpo(Entrada):
    chave: str = Field(min_length=20, max_length=300, pattern=r"^sk-ant-")


class ChaveSituacao(BaseModel):
    chave_propria: bool
    final: str | None = Field(None, description="só os 4 últimos caracteres, para a pessoa reconhecer")


class ChaveInterna(BaseModel):
    chave: str


class ChaveEstado(BaseModel):
    """Situação da chave própria para a tela. Nunca carrega a chave, nem cifrada."""
    ativa: bool
    final: str | None = Field(None, description="só os 4 últimos caracteres")
    cadastrada_em: datetime | None = None


# --- conta (perfil + cliente + créditos) ---

class UsuarioConta(BaseModel):
    id: str
    nome: str
    email: str
    papel: str


class TenantConta(BaseModel):
    id: str
    nome: str


class MinhaConta(BaseModel):
    usuario: UsuarioConta
    tenant: TenantConta
    chave_propria: ChaveEstado | None = Field(None, description="null quando não há chave conectada")
    creditos: Creditos


# --- página inicial ---

class ResumoObra(BaseModel):
    obra: Obra
    pendencias: int = Field(description="propostas pendentes")
    falta_comprar: int = Field(description="insumos com Conta.comprar > 0")
    pedidos_abertos: int = Field(description="pedidos que ainda não foram conferidos nem cancelados")
    valor_pedidos_abertos: int = Field(description="centavos")
    nf_para_conferir: int = Field(description="pedidos recebidos esperando conferência")
    ultima_atividade: datetime | None = Field(None, description="última entrada do registro da obra")


# --- sessões de conversa ---

_ID = r"^[A-Za-z0-9_-]{8,64}$"
Aba = dict[str, Any]


class SessaoCorpo(Entrada):
    id: str | None = Field(None, pattern=_ID, description="opcional: o agente pode gerar o UUIDv7 (idempotente)")
    obra_id: str | None = None
    titulo: str = Field("", max_length=200)
    abas: list[Aba] = Field(default_factory=list, max_length=20)


class SessaoAlteracao(Entrada):
    obra_id: str | None = None
    titulo: str | None = Field(None, max_length=200)
    abas: list[Aba] | None = Field(None, max_length=20)


class SessaoConversa(BaseModel):
    id: str
    tenant_id: str
    dono: str
    obra_id: str | None
    titulo: str
    abas: list[Aba]
    total_mensagens: int
    criado_em: datetime
    atualizado_em: datetime


class MensagemCorpo(Entrada):
    papel: Literal["user", "assistant"]
    conteudo: str | list[dict[str, Any]]


class MensagensCorpo(Entrada):
    seq_inicial: int = Field(ge=0, description="tem de ser o total_mensagens atual da sessão")
    mensagens: list[MensagemCorpo] = Field(min_length=1, max_length=50)


class Mensagem(BaseModel):
    id: str
    sessao_id: str
    seq: int
    papel: str
    conteudo: str | list[dict[str, Any]]
    criado_em: datetime


class SessaoComMensagens(SessaoConversa):
    mensagens: list[Mensagem]


# --- saídas com nome legível (a tela não mostra ID de usuário) ---

class PropostaSaida(Proposta):
    decidido_por_nome: str | None = None


class RegistroSaida(EntradaRegistro):
    quem_nome: str | None = None
    usuario_id: str | None = None


class Aprovacao(BaseModel):
    proposta: PropostaSaida
    registro: RegistroSaida = Field(description="o `registro.id` é o que o Desfazer usa")
