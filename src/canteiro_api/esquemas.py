"""Corpos de requisição e respostas que não são documentos. Quantidade em milésimos e dinheiro em centavos."""
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from canteiro_dominio.modelos import (
    EntradaRegistro,
    Fornecedor,
    Insumo,
    ItemOrcamento,
    Medicao,
    Movimento,
    Obra,
    Pedido,
    Proposta,
    Recebimento,
    StatusPedido,
    TipoProposta,
    TipoVersao,
    VersaoOrcamento,
)


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


class InsumoCorpo(Entrada):
    """Insumo cadastrado pela tela: o que chegou ou foi comprado sem estar na planilha do orçamento."""
    nome: str = Field(min_length=1, max_length=120)
    unidade: str = Field(min_length=1, max_length=12)
    tipo: Literal["material", "servico"] = "material"
    curva: Literal["A", "B", "C"] = "C"
    precisa_ate_o_fim: int = Field(0, ge=0,
                                   description="milésimos; 0 = fora do orçamento (não entra na conta de falta)")
    valor_unitario: int = Field(0, ge=0, description="centavos; 0 = sem preço")


class InsumoSaida(Insumo):
    registro_id: str | None = Field(None, description="a EntradaRegistro do cadastro")


class MovimentoCorpo(Entrada):
    insumo_id: str
    # transferência entre obras ainda não existe no MVP-0
    tipo: Literal["entrada", "saida", "ajuste"]
    quantidade: int = Field(description="milésimos; positiva, exceto em ajuste (que carrega o sinal)")
    destino: str | None = Field(None, max_length=200)
    etapa: str | None = Field(None, max_length=60, description="etapa do orçamento (obrigatória em saída quando a obra "
                                                              "tem orçamento)")
    origem_doc: str | None = Field(None, max_length=100)
    chave_idempotencia: str = Field(min_length=8, max_length=200)


_DATA = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"


class LinhaPedidoCorpo(Entrada):
    insumo_id: str
    quantidade: int = Field(gt=0, description="milésimos")
    valor_unitario: int = Field(0, ge=0, description="centavos por unidade")
    total: int | None = Field(None, ge=0, description="centavos; padrão = quantidade × unitário. Informe quando o "
                                                      "total combinado não fecha com o unitário arredondado")
    etapa: str | None = Field(None, max_length=60)


class PedidoDados(Entrada):
    """O pedido em si. É também o `dados` da proposta `criar_pedido`, que não aceita `aprovado`: aprovar a proposta já
    é a aprovação."""
    fornecedor: str = Field(min_length=1, max_length=200)
    fornecedor_id: str | None = None
    linhas: list[LinhaPedidoCorpo] = Field(min_length=1, max_length=50)
    previsao: str | None = Field(None, max_length=50)
    observacao: str = Field("", max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _linha_unica(cls, d: Any) -> Any:
        # o formato de um insumo só (agente e propostas antigas): vira um pedido de uma linha com o total combinado
        if isinstance(d, dict) and "linhas" not in d and "insumo_id" in d:
            d = dict(d)
            d["linhas"] = [{"insumo_id": d.pop("insumo_id"), "quantidade": d.pop("quantidade", 0),
                            "total": d.pop("valor", 0), "etapa": d.pop("etapa", None)}]
        return d

    @field_validator("linhas")
    @classmethod
    def _sem_repetir(cls, v: list[LinhaPedidoCorpo]) -> list[LinhaPedidoCorpo]:
        # duas linhas do mesmo insumo gravariam o mesmo saldo duas vezes na mesma transação da entrega
        vistos = [x.insumo_id for x in v]
        if len(vistos) != len(set(vistos)):
            raise ValueError("o mesmo insumo aparece em duas linhas; junte as quantidades numa linha só")
        return v


class PedidoCorpo(PedidoDados):
    aprovado: bool = Field(False, description="true: nasce aprovado (compra já decidida); false: em cotação")


class PedidoEdicao(Entrada):
    previsao: str | None = Field(None, max_length=50)
    observacao: str | None = Field(None, max_length=500)


class LinhaEntrega(Entrada):
    indice: int = Field(ge=0, description="posição da linha no pedido")
    quantidade: int = Field(gt=0, description="milésimos")


class RecebimentoCorpo(Entrada):
    nf_numero: str | None = Field("", max_length=60)
    nf_data: str | None = Field(None, pattern=_DATA)
    nf_valor: int | None = Field(None, ge=0, description="centavos, como está na nota")
    linhas: list[LinhaEntrega] = Field(min_length=1, max_length=50)
    chave_idempotencia: str = Field(min_length=8, max_length=200)

    @field_validator("nf_numero")
    @classmethod
    def _nf(cls, v: str | None) -> str:
        # null e em branco = entrega sem nota (o formulário manda o campo vazio como null)
        return (v or "").strip()

    @field_validator("linhas")
    @classmethod
    def _uma_vez(cls, v: list[LinhaEntrega]) -> list[LinhaEntrega]:
        if len({x.indice for x in v}) != len(v):
            raise ValueError("a mesma linha do pedido aparece duas vezes na entrega")
        return v


class RecebimentoSaida(BaseModel):
    recebimento: Recebimento
    pedido: Pedido
    registro_id: str | None = None


class _DadosFornecedor(Entrada):
    contato: str | None = Field(None, max_length=120)
    telefone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=254)

    @field_validator("contato", "telefone", "email")
    @classmethod
    def _aparar(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None


class FornecedorCorpo(_DadosFornecedor):
    nome: str = Field(min_length=1, max_length=200)
    cnpj: str = Field("", max_length=20, description="com ou sem pontuação; vazio = não informado")


class FornecedorEdicao(_DadosFornecedor):
    nome: str | None = Field(None, min_length=1, max_length=200)
    cnpj: str | None = Field(None, max_length=20)


class FornecedorSaida(Fornecedor):
    registro_id: str | None = None


class EtapaRef(BaseModel):
    etapa: str
    descricao: str


class Conferencia(Entrada):
    """Só fazem sentido com `para = conferido` (regras.validar_conferencia recusa no resto)."""
    quantidade_recebida: int | None = Field(None, gt=0, description="milésimos; padrão = a quantidade do pedido")
    nf: str | None = Field(None, max_length=60, description="número da nota fiscal da entrega")

    @field_validator("nf")
    @classmethod
    def _nf_sem_espaco(cls, v: str | None) -> str | None:
        # NF em branco é "não informada", não uma NF vazia que apareceria como "NF " no documento de origem
        return (v.strip() or None) if v is not None else None


class AvancarCorpo(Conferencia):
    para: StatusPedido


class AvancarPedidoDados(Conferencia):
    pedido_id: str
    para: StatusPedido


class AjusteEstoqueDados(Entrada):
    insumo_id: str
    quantidade: int = Field(description="milésimos, com sinal (+ sobrou na contagem, − faltou)")
    motivo: str = Field(min_length=1, max_length=500)


DADOS_POR_TIPO: dict[TipoProposta, type[Entrada]] = {
    TipoProposta.CRIAR_PEDIDO: PedidoDados,
    TipoProposta.AVANCAR_PEDIDO: AvancarPedidoDados,
    TipoProposta.AJUSTE_ESTOQUE: AjusteEstoqueDados,
}

# O que quem aprova pode corrigir antes de aprovar. Fora daqui (insumo, pedido, destino do status) já é outra proposta.
AJUSTAVEIS: dict[TipoProposta, set[str]] = {
    TipoProposta.CRIAR_PEDIDO: {"quantidade", "fornecedor", "valor", "previsao"},
    TipoProposta.AVANCAR_PEDIDO: {"quantidade_recebida", "nf"},
    TipoProposta.AJUSTE_ESTOQUE: {"quantidade", "motivo"},
}


class PropostaCorpo(Entrada):
    obra_id: str
    tipo: TipoProposta
    titulo: str = Field(min_length=1, max_length=300)
    conta: str = Field("", max_length=1000)
    justificativa: str = Field("", max_length=1000)
    fonte: str = Field("", max_length=1000)
    dados: dict


class AprovarCorpo(Entrada):
    ajustes: dict[str, Any] | None = Field(None, description="campos de `dados` corrigidos antes de aprovar")


class RecusarCorpo(Entrada):
    motivo: str = Field(min_length=1, max_length=1000)


# --- obra (onboarding) ---

Tipologia = Literal["casa", "sobrado", "predio", "galpao", "reforma", "comercial", "outro"]
Padrao = Literal["baixo", "normal", "alto"]
Regime = Literal["empreitada", "administracao"]


class _DadosObra(Entrada):
    """Os campos da obra que a pessoa preenche. Texto chega aparado: espaço sobrando viraria diferença no PATCH."""
    cidade: str = Field("", max_length=80)
    cep: str = Field("", max_length=9)
    uf: str = Field("", max_length=2)
    endereco: str = Field("", max_length=200)
    tipologia: Tipologia | None = None
    area_construida: int = Field(0, ge=0, le=10_000_000_000, description="m² em milésimos")
    pavimentos: int = Field(0, ge=0, le=200)
    padrao: Padrao | None = None
    regime: Regime | None = None
    cliente: str = Field("", max_length=120)
    inicio: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="AAAA-MM-DD")
    prazo_meses: int = Field(0, ge=0, le=240)
    verba: int = Field(0, ge=0, description="valor do contrato, em centavos")
    numero_contrato: str = Field("", max_length=40)
    bdi: int = Field(0, ge=0, le=10_000, description="centésimos de ponto percentual (22,91% = 2291)")
    data_base: str | None = Field(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="AAAA-MM")

    @field_validator("cidade", "cep", "endereco", "cliente", "numero_contrato")
    @classmethod
    def _aparar(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("uf")
    @classmethod
    def _uf(cls, v: str) -> str:
        return v.strip().upper()


class ObraCorpo(_DadosObra):
    nome: str = Field(min_length=1, max_length=120)
    chave_idempotencia: str = Field(min_length=8, max_length=64)

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Nome vazio.")
        return v


class ObraEdicao(_DadosObra):
    """Todos opcionais: só o que veio no corpo muda (exclude_unset)."""
    nome: str | None = Field(None, min_length=1, max_length=120)

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = " ".join(v.split())
        if not v:
            raise ValueError("Nome vazio.")
        return v


class ObraSaida(Obra):
    registro_id: str | None = Field(None, description="a EntradaRegistro da mudança (null quando nada mudou)")


# --- orçamento por planilha ---

class ColunaPlanilha(BaseModel):
    indice: int
    titulo: str


class Mapeamento(Entrada):
    """Qual coluna (0-based) é o quê. A tela recebe a sugestão, a pessoa corrige e devolve na importação."""
    planilha: str = Field(max_length=120)
    linha_cabecalho: int = Field(ge=1, le=10_000)
    item: int | None = Field(None, ge=0, le=500)
    codigo: int | None = Field(None, ge=0, le=500)
    fonte: int | None = Field(None, ge=0, le=500)
    nome: int | None = Field(None, ge=0, le=500)
    unidade: int | None = Field(None, ge=0, le=500)
    quantidade: int | None = Field(None, ge=0, le=500)
    valor_unitario: int | None = Field(None, ge=0, le=500)
    valor_total: int | None = Field(None, ge=0, le=500)
    saldo: int | None = Field(None, ge=0, le=500)
    curva: int | None = Field(None, ge=0, le=500)


class LeituraPlanilha(BaseModel):
    planilhas: list[str]
    planilha: str
    linha_cabecalho: int
    colunas: list[ColunaPlanilha]
    amostra: list[list[str]]
    sugestao: Mapeamento


class LinhaOrcamento(BaseModel):
    linhas: list[int] = Field(description="linhas da planilha que formam este insumo (mais de uma quando juntou)")
    nome: str
    unidade: str
    quantidade: int
    valor_unitario: int
    total: int
    curva: str
    saldo: int
    situacao: Literal["novo", "atualiza"]
    precisa_antes: int | None = None
    valor_antes: int | None = None


class LinhaIgnorada(BaseModel):
    linha: int
    texto: str
    motivo: str


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


class PreviaOrcamento(BaseModel):
    aplicado: bool
    linhas: list[LinhaOrcamento]
    ignoradas: list[LinhaIgnorada]
    erros: list[ErroLinha]
    total: int = Field(description="centavos")
    novos: int
    atualizados: int
    juntadas: int = Field(description="linhas da planilha absorvidas em outra (mesmo insumo repetido)")


class ItemPrevia(BaseModel):
    ordem: int
    tipo: Literal["etapa", "item"]
    numero: str
    nivel: int
    etapa: str
    codigo: str
    fonte: str
    descricao: str
    unidade: str
    quantidade: int
    valor_unitario: int
    total: int
    linha: int
    # só no aditivo: de onde veio a etapa do item. "planilha" = como veio; "codigo" = o mesmo código está nesta etapa
    # do contrato; "escolhida" = a pessoa apontou na prévia. `etapa_planilha` guarda a da planilha (para voltar).
    etapa_origem: Literal["planilha", "codigo", "escolhida"] = "planilha"
    etapa_planilha: str = ""


class Aviso(BaseModel):
    linha: int
    mensagem: str


class TotalEtapa(BaseModel):
    etapa: str
    descricao: str
    total: int


class Substituida(BaseModel):
    versao_id: str
    total: int
    itens: int


class PreviaVersao(BaseModel):
    aplicado: bool
    tipo: TipoVersao
    itens: list[ItemPrevia]
    ignoradas: list[LinhaIgnorada]
    erros: list[ErroLinha]
    avisos: list[Aviso]
    total: int = Field(description="centavos")
    n_itens: int
    n_etapas: int
    etapas: list[TotalEtapa]
    substitui: Substituida | None = None
    versao_id: str | None = None
    registro_id: str | None = None


class ItemComCurva(ItemOrcamento):
    curva: str | None = None


class _Andamento(BaseModel):
    """Previsto × realizado, em centavos. Cotando = pedidos em cotação; comprometido = o que já foi contratado com
    fornecedor (pedido aprovado em diante; o encerrado conta só o que foi entregue); realizado = entregas e medições
    lançadas, pelo preço do pedido."""
    cotando: int = 0
    comprometido: int = 0
    realizado: int = 0


class EtapaOrcamento(_Andamento):
    etapa: str
    descricao: str
    contratado: int
    aditivos: int
    atual: int


class TotaisOrcamento(_Andamento):
    contratado: int
    aditivos: int
    atual: int
    # gasto sem etapa (obra sem orçamento na hora do pedido); já está somado nos três campos acima
    sem_etapa: _Andamento = Field(default_factory=_Andamento)
    n_itens: int
    n_etapas: int


class VersaoComRegistro(VersaoOrcamento):
    registro_id: str = Field(description="a EntradaRegistro da importação: é o que o Desfazer da versão usa")


class OrcamentoObra(BaseModel):
    versoes: list[VersaoComRegistro]
    itens: list[ItemComCurva]
    etapas: list[EtapaOrcamento]
    totais: TotaisOrcamento


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


class PedidoSaida(Pedido):
    registro_id: str | None = Field(None, description="a EntradaRegistro desta escrita: é o que o Desfazer usa")


class MovimentoSaida(Movimento):
    registro_id: str | None = Field(None, description="a EntradaRegistro desta escrita; null na repetição da chave")


class Aprovacao(BaseModel):
    proposta: PropostaSaida
    registro: RegistroSaida = Field(description="o `registro.id` é o que o Desfazer usa")


# --- lista de espera (landing pública, sem login) ---

# Conferência de forma, não de existência: sem dependência nova e sem consulta a DNS. Um "@", nada de espaço, domínio
# com ponto. O resto (caixa postal que existe) só o e-mail de confirmação prova.
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")


class ListaEsperaCorpo(Entrada):
    email: str = Field(min_length=3, max_length=254)
    nome: str | None = Field(None, max_length=120)
    empresa: str | None = Field(None, max_length=120)
    papel: Literal["engenheiro", "mestre", "dono", "comprador", "outro"] | None = None
    obras_ativas: int | None = Field(None, ge=0, le=999)
    origem: str | None = Field(None, max_length=80)
    # isca para robô: invisível na página; gente de verdade deixa em branco
    site: str | None = Field(None, max_length=200)

    @field_validator("email")
    @classmethod
    def _email_valido(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL.match(v):
            raise ValueError("E-mail inválido.")
        return v

    @field_validator("nome", "empresa", "origem")
    @classmethod
    def _sem_espaco_sobrando(cls, v: str | None) -> str | None:
        return (v.strip() or None) if v is not None else None


class ListaEsperaResposta(BaseModel):
    ok: bool = True
    ja_estava: bool


# --- cronograma e medição ---

_MES = r"^\d{4}-(0[1-9]|1[0-2])$"


class EtapaCronogramaCorpo(Entrada):
    etapa: str = Field(min_length=1, max_length=60)
    percentuais: list[int] = Field(max_length=60, description="centésimos de % por mês; soma 10000 ou tudo zero")


class CronogramaCorpo(Entrada):
    inicio: str = Field(pattern=_MES)
    meses: int = Field(ge=1, le=60)
    etapas: list[EtapaCronogramaCorpo] = Field(max_length=200)
    atualizar_prazo_da_obra: bool = Field(False, description="aditivo de prazo: grava `meses` no prazo da obra na "
                                                             "mesma transação (e o desfazer volta os dois)")
    nova_linha_base: bool = Field(False, description="troca a linha de base por este cronograma (o atraso passa a "
                                                     "ser medido contra ele)")


class EtapaMedicaoCorpo(Entrada):
    etapa: str = Field(min_length=1, max_length=60)
    pct_acumulado: int = Field(ge=0, le=10000)


class MedicaoCorpo(Entrada):
    mes: str = Field(pattern=_MES)
    etapas: list[EtapaMedicaoCorpo] = Field(max_length=200)
    observacao: str = Field("", max_length=500)
    chave_idempotencia: str = Field(min_length=8, max_length=200)


class EtapaCronograma(BaseModel):
    etapa: str
    descricao: str
    atual: int
    percentuais: list[int]
    medido_acumulado: int = 0


class MesCronograma(BaseModel):
    mes: str
    previsto: int
    previsto_acumulado: int
    pct_previsto_acumulado: int
    medido: int | None = None
    medido_acumulado: int | None = None
    pct_medido_acumulado: int | None = None
    pct_base_acumulado: int = 0


class Prazo(BaseModel):
    termino_contratual: str | None = None
    termino_projetado: str | None = None
    atraso_meses: int | None = None
    atraso_decimos: int | None = Field(None, description="atraso em décimos de mês (1,4 mês = 14)")
    pct_previsto_na_medicao: int | None = None
    pct_medido: int | None = None
    mes_referencia: str | None = None
    # contra a linha de base (o cronograma assinado): é o atraso que a reprogramação não apaga
    termino_base: str | None = None
    atraso_base_meses: int | None = None
    atraso_base_decimos: int | None = None


class LinhaBase(BaseModel):
    inicio: str
    meses: int


class CronogramaObra(BaseModel):
    inicio: str | None = None
    meses: int = 0
    base: LinhaBase | None = None
    revisoes: int = 0
    etapas: list[EtapaCronograma] = Field(default_factory=list)
    mensal: list[MesCronograma] = Field(default_factory=list)
    medicoes: list[Medicao] = Field(default_factory=list)
    prazo: Prazo = Field(default_factory=Prazo)
    registro_id: str | None = None


class MedicaoSaida(BaseModel):
    medicao: Medicao
    cronograma: CronogramaObra
    registro_id: str | None = None
