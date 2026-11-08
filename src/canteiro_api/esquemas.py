"""Corpos de requisição e respostas que não são documentos. Quantidade em milésimos e dinheiro em centavos."""
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from canteiro_dominio.etapas import Origem
from canteiro_dominio.leitura import LeituraComprovante, Revisao
from canteiro_dominio.modelos import (
    Comprovante,
    EntradaRegistro,
    EtapaExtra,
    Fornecedor,
    Gasto,
    Insumo,
    ItemOrcamento,
    LinhaPedido,
    Medicao,
    Movimento,
    Obra,
    Pedido,
    Proposta,
    Recebimento,
    StatusPedido,
    TipoProposta,
    TipoVersao,
    Topico,
    VersaoOrcamento,
)
from canteiro_dominio.regras import falta_na_linha


class Entrada(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntrarCorpo(Entrada):
    email: str = Field(min_length=3, max_length=254)
    senha: str = Field(min_length=1, max_length=256)


class TrocarSenhaCorpo(Entrada):
    # Os limites de verdade (8 a 256, diferente da atual, não é o e-mail) são conferidos em auth.trocar_senha, com
    # mensagem em português; aqui só o teto que impede mandar meio megabyte para o argon2.
    senha_atual: str = Field(max_length=1024)
    senha_nova: str = Field(max_length=1024)


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
    sobra: int = Field(0, description="milésimos que já tem ou vêm além do que precisa (0 sem previsão)")
    sem_previsao: bool = Field(False, description="precisa = 0: ninguém lançou a previsão; NÃO quer dizer que não "
                                                  "precisa")
    texto: str = Field("", description="a linha de conta pronta (regras.texto_da_conta), sempre aritmética verdadeira")


class UltimoPreco(BaseModel):
    """Preço unitário da última compra do insumo nesta obra (pedido não cancelado mais novo)."""
    valor_unitario: int = Field(description="centavos por unidade do insumo")
    pedido_id: str
    numero: str
    fornecedor: str
    data: str = Field(description="AAAA-MM-DD em que o pedido foi criado")
    status: StatusPedido


class InsumoComConta(BaseModel):
    insumo: Insumo
    conta: ContaSaida
    ultimo_preco: UltimoPreco | None = Field(None, description="null = nunca comprado nesta obra")


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
    # Com a conta das linhas (falta_entregar): a tela troca o pedido por este depois da entrega.
    pedido: PedidoListado
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


class RegistrarEntregaDados(Entrada):
    """A entrega proposta: o mesmo corpo do `POST /pedidos/{id}/recebimentos`, sem a chave (a aprovação deriva uma do
    ID da proposta) e com a confirmação de "chegou a mais" que a tela pede."""
    pedido_id: str
    linhas: list[LinhaEntrega] = Field(min_length=1, max_length=50)
    nf_numero: str = Field("", max_length=60)
    nf_data: str | None = Field(None, pattern=_DATA)
    nf_valor: int | None = Field(None, ge=0, description="centavos, como está na nota")
    confirmar_excesso: bool = Field(False, description="a pessoa confirmou que chegou mais do que faltava")

    @field_validator("nf_numero")
    @classmethod
    def _nf(cls, v: str | None) -> str:
        return (v or "").strip()

    @field_validator("linhas")
    @classmethod
    def _uma_vez(cls, v: list[LinhaEntrega]) -> list[LinhaEntrega]:
        if len({x.indice for x in v}) != len(v):
            raise ValueError("a mesma linha do pedido aparece duas vezes na entrega")
        return v


DADOS_POR_TIPO: dict[TipoProposta, type[Entrada]] = {
    TipoProposta.CRIAR_PEDIDO: PedidoDados,
    TipoProposta.AVANCAR_PEDIDO: AvancarPedidoDados,
    TipoProposta.AJUSTE_ESTOQUE: AjusteEstoqueDados,
    TipoProposta.REGISTRAR_ENTREGA: RegistrarEntregaDados,
}

# O que quem aprova pode corrigir antes de aprovar. Fora daqui (insumo, pedido, destino do status) já é outra proposta.
AJUSTAVEIS: dict[TipoProposta, set[str]] = {
    TipoProposta.CRIAR_PEDIDO: {"quantidade", "fornecedor", "valor", "previsao"},
    TipoProposta.AVANCAR_PEDIDO: {"quantidade_recebida", "nf"},
    TipoProposta.AJUSTE_ESTOQUE: {"quantidade", "motivo"},
    TipoProposta.REGISTRAR_ENTREGA: {"linhas", "nf_numero", "nf_data", "nf_valor", "confirmar_excesso"},
}


class PropostaCorpo(Entrada):
    obra_id: str
    tipo: TipoProposta
    titulo: str = Field(min_length=1, max_length=300)
    conta: str = Field("", max_length=1000)
    justificativa: str = Field("", max_length=1000)
    fonte: str = Field("", max_length=1000)
    dados: dict
    chave_idempotencia: str | None = Field(
        None, min_length=1, max_length=200,
        description="opcional: repetir a mesma chave devolve a proposta já criada (200) em vez de criar outra")


class AprovarCorpo(Entrada):
    ajustes: dict[str, Any] | None = Field(None, description="campos de `dados` corrigidos antes de aprovar")


class RecusarCorpo(Entrada):
    motivo: str = Field(min_length=1, max_length=1000)


# --- obra (onboarding) ---

Tipologia = Literal["casa", "sobrado", "predio", "galpao", "reforma", "comercial", "outro"]
Padrao = Literal["baixo", "normal", "alto"]
Regime = Literal["empreitada", "administracao"]
Proprietario = Literal["pf", "pj"]
Destinacao = Literal["residencial_unifamiliar", "residencial_multifamiliar", "casa_popular", "conjunto_habitacional",
                     "comercial", "edificio_garagens", "galpao"]
Material = Literal["alvenaria", "mista", "madeira"]
Finalidade = Literal["uso_proprio", "venda", "prestacao_servico"]
NaturezaFiscal = Literal["folha", "mao_de_obra_nf", "gps", "autonomo_mei", "pre_moldado", "concreto_usinado",
                         "mao_de_obra_sem_nf", "material", "equipamento", "outros"]
CategoriaObra = Literal["obra_nova", "acrescimo", "reforma", "demolicao"]


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
    # dados fiscais (tributos)
    cno: str = Field("", max_length=15, description="matrícula CNO: 12 dígitos, com ou sem pontuação")
    proprietario: Proprietario | None = None
    destinacao: Destinacao | None = None
    material: Material | None = None
    area_complementar: int = Field(0, ge=0, le=10_000_000_000, description="m² em milésimos")
    area_descoberta: int = Field(0, ge=0, le=10_000_000_000, description="m² em milésimos")
    categoria: CategoriaObra | None = Field(None, description="null = da tipologia (reforma) ou obra nova")
    dctfweb_em_dia: bool | None = Field(None, description="DCTFWeb entregue em todos os meses da obra")
    vau: int = Field(0, ge=0, le=10_000_000, description="VAU à mão, em centavos por m²; 0 = o da tabela do SERO")
    vau_referencia: str | None = Field(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="AAAA-MM")
    finalidade: Finalidade | None = None
    valor_terreno: int = Field(0, ge=0, description="centavos")
    unidades: int = Field(0, ge=0, le=100_000)

    @field_validator("cno")
    @classmethod
    def _cno(cls, v: str) -> str:
        # guardado só com os dígitos: "90.012.34567/89" e "900123456789" são a mesma obra
        v = v.strip()
        digitos = re.sub(r"[.\-/ ]", "", v)
        if v and not re.fullmatch(r"\d{12}", digitos):
            raise ValueError("O CNO tem 12 dígitos.")
        return digitos

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
    registro_ids: list[str] = Field(default_factory=list,
                                    description="uma EntradaRegistro por lote gravado: é o que o Desfazer usa")


class PreviaOrcamento(BaseModel):
    aplicado: bool
    linhas: list[LinhaOrcamento]
    ignoradas: list[LinhaIgnorada]
    erros: list[ErroLinha]
    total: int = Field(description="centavos")
    novos: int
    atualizados: int
    juntadas: int = Field(description="linhas da planilha absorvidas em outra (mesmo insumo repetido)")
    registro_ids: list[str] = Field(default_factory=list,
                                    description="uma EntradaRegistro por lote gravado: é o que o Desfazer usa")


class MaterialDoProjeto(BaseModel):
    nome: str = Field(min_length=1, max_length=120, description="como vai aparecer no almoxarifado e em Compras")
    unidade: str = Field(min_length=1, max_length=12)
    quantidade: int = Field(gt=0, description="milésimos: o quanto o projeto pede (vira o precisa_ate_o_fim)")
    valor_unitario: int = Field(0, ge=0, description="centavos; 0 = sem preço de referência")


class MateriaisDoProjeto(BaseModel):
    """A lista de materiais que o tradutor tirou das pranchas (o \"precisa\" da conta de compras)."""
    origem: str = Field(min_length=1, max_length=80, description="vai no registro: \"projeto estrutural\"")
    itens: list[MaterialDoProjeto] = Field(min_length=1, max_length=500)
    aplicar: bool = False


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
    # Calculados aqui, não em quem lê: o agente só cita, e a tela não refaz a conta.
    saldo: int = Field(0, description="saldo a comprometer = atual − comprometido (negativo = estourou)")
    estourou: bool = Field(False, description="comprometido passou do orçado atual")
    gastos_avulsos: int = Field(0, description="gastos confirmados por comprovante, sem pedido, nesta etapa; já "
                                               "somados no comprometido e no realizado")


class GastosNoOrcamentoSaida(BaseModel):
    """Os gastos avulsos (comprovante confirmado, sem pedido) da obra, todos já somados no comprometido e no
    realizado dos totais. Pagamento de pedido não entra: o custo do pedido já conta pela entrega."""
    total: int = 0
    nas_etapas: int = Field(0, description="nas etapas do orçamento (cada etapa traz o seu em gastos_avulsos)")
    fora_do_orcamento: int = Field(0, description="nas etapas extras da obra: não engordam etapa do orçamento")
    na_lista_padrao: int = Field(0, description="obra sem orçamento: gastos na lista padrão de etapas")
    sem_etapa_do_orcamento: int = Field(0, description="numa etapa que não é do orçamento vigente nem extra (lista "
                                                       "padrão de antes do orçamento): falta reclassificar")


class TotaisOrcamento(_Andamento):
    contratado: int
    aditivos: int
    atual: int
    saldo: int = Field(0, description="saldo a comprometer da obra = atual − comprometido")
    aditivos_pct: int | None = Field(None, description="aditivos sobre o contratado, em centésimos de % (290 = "
                                                       "2,9%); null sem contrato")
    # gasto sem etapa (obra sem orçamento na hora do pedido); já está somado nos três campos acima
    sem_etapa: _Andamento = Field(default_factory=_Andamento)
    gastos: GastosNoOrcamentoSaida = Field(default_factory=GastosNoOrcamentoSaida)
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
    prazo: Prazo | None = Field(None, description="o mesmo do GET /obras/{id}/cronograma; null sem cronograma")



# --- resumo de UMA obra ("como está a obra?"): tudo pronto para citar, cada bloco diz se o dado existe ---

class EtapaNoMes(BaseModel):
    etapa: str
    descricao: str
    pct_no_mes: int = Field(description="% da etapa previsto no mês (centésimos)")
    valor_no_mes: int = Field(description="R$ previsto da etapa no mês (centavos)")
    previsto_ate_referencia: int = 0
    medido_acumulado: int = 0
    situacao: str | None = None
    comeca: bool = Field(False, description="começa neste mês: sem medição e sem % previsto antes dele")


class EtapaAtrasada(BaseModel):
    etapa: str
    descricao: str
    medido_acumulado: int
    previsto_ate_referencia: int
    atraso_pct: int


class ResumoCronograma(BaseModel):
    cadastrado: bool
    mes_corrente: str = Field(description="AAAA-MM de hoje, no fuso de São Paulo")
    termino_contratual: str | None = None
    termino_projetado: str | None = None
    termino_original: str | None = None
    atraso_decimos: int | None = Field(None, description="contra o cronograma atual; null sem medição")
    atraso_base_decimos: int | None = Field(None, description="contra o original (linha de base)")
    revisoes: int = 0
    mes_ultima_medicao: str | None = None
    medicoes: int = 0
    pct_previsto_na_medicao: int | None = None
    pct_medido: int | None = None
    previsto_no_mes: int | None = Field(None, description="R$ previsto no mês corrente (null fora do cronograma)")
    pct_previsto_acumulado_no_mes: int | None = None
    etapas_do_mes: list[EtapaNoMes] = Field(default_factory=list, description="com % previsto no mês corrente, "
                                                                              "maior valor primeiro")
    proximo_mes: str | None = Field(None, description="AAAA-MM do mês seguinte ao corrente (null fora do cronograma)")
    previsto_no_proximo_mes: int | None = None
    pct_previsto_acumulado_no_proximo_mes: int | None = None
    etapas_do_proximo_mes: list[EtapaNoMes] = Field(
        default_factory=list,
        description="todas com % previsto no mês seguinte (as que continuam e as que começam), maior valor primeiro")
    proximo_mes_com_inicio: str | None = Field(None, description="o próximo mês em que alguma etapa começa (sem "
                                                                 "medição: a que já foi medida não começa de novo)")
    proximas_etapas: list[EtapaNoMes] = Field(default_factory=list, description="as que começam nesse mês")
    etapas_atrasadas: list[EtapaAtrasada] = Field(default_factory=list, description="maior atraso primeiro")


class EtapaEstourada(BaseModel):
    etapa: str
    descricao: str
    saldo: int


class ResumoFinanceiro(BaseModel):
    tem_orcamento: bool
    contratado: int = 0
    aditivos: int = 0
    aditivos_pct: int | None = None
    orcado_atual: int = 0
    cotando: int = 0
    comprometido: int = 0
    realizado: int = 0
    saldo: int = 0
    etapas_estouradas: list[EtapaEstourada] = Field(default_factory=list)
    gastos: GastosNoOrcamentoSaida = Field(default_factory=GastosNoOrcamentoSaida)


class ItemEntrega(BaseModel):
    nome: str
    unidade: str
    falta_entregar: int


class EntregaResumo(BaseModel):
    pedido_id: str
    numero: str
    fornecedor: str
    status: StatusPedido
    previsao: str | None
    itens: list[ItemEntrega]


class ResumoEntregas(BaseModel):
    hoje: list[EntregaResumo] = Field(default_factory=list)
    atrasadas: list[EntregaResumo] = Field(default_factory=list, description="previsão antes de hoje e ainda a caminho")
    proximos_7_dias: list[EntregaResumo] = Field(default_factory=list)
    sem_previsao: int = Field(0, description="pedidos a caminho sem data (ou com data fora de AAAA-MM-DD)")
    nf_para_conferir: int = 0


class FaltaResumo(BaseModel):
    insumo_id: str
    nome: str
    unidade: str
    falta: int
    texto: str


class ResumoAlmoxarifado(BaseModel):
    insumos: int = 0
    falta_comprar: list[FaltaResumo] = Field(default_factory=list)
    sem_previsao: int = Field(0, description="insumos sem quantidade prevista: a falta deles não é calculada")
    consumo_projetado: bool = Field(False, description="o sistema ainda não projeta consumo por semana")


class ResumoPendencias(BaseModel):
    propostas: int = 0
    titulos: list[str] = Field(default_factory=list, description="até 5, as mais novas primeiro")


class ResumoDaObra(BaseModel):
    obra: Obra
    hoje: str = Field(description="AAAA-MM-DD, fuso de São Paulo")
    cronograma: ResumoCronograma
    financeiro: ResumoFinanceiro
    entregas: ResumoEntregas
    almoxarifado: ResumoAlmoxarifado
    pendencias: ResumoPendencias

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


class PropostaCriada(PropostaSaida):
    ja_existia: bool = Field(False, description="true = nada novo: a mesma chave já fora usada, ou uma proposta igual "
                                                "desta obra ainda espera aprovação (é ela que volta)")


class RegistroSaida(EntradaRegistro):
    quem_nome: str | None = None
    usuario_id: str | None = None


class LinhaPedidoSaida(LinhaPedido):
    falta_entregar: int = Field(0, description="milésimos que ainda faltam chegar: quantidade − recebida, nunca "
                                               "negativo; 0 em pedido encerrado (conferido) ou cancelado")


# Pedido encerrado com sobra não espera mais nada: "falta entregar" ali induziria a cobrar o fornecedor à toa.
_FECHADOS = {StatusPedido.CONFERIDO, StatusPedido.CANCELADO}


class PedidoListado(Pedido):
    """O pedido como sai para leitura, com a conta de cada linha pronta (o agente cita, não subtrai)."""
    linhas: list[LinhaPedidoSaida]

    @model_validator(mode="after")
    def _falta_entregar(self):
        for x in self.linhas:
            x.falta_entregar = 0 if self.status in _FECHADOS else falta_na_linha(x.model_dump())
        return self


class PedidoSaida(PedidoListado):
    registro_id: str | None = Field(None, description="a EntradaRegistro desta escrita: é o que o Desfazer usa")


RecebimentoSaida.model_rebuild()


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
    # Contas prontas para a tela e o agente (o Gantt calculava a situação no navegador; o agente não tinha como).
    valores_mes: list[int] = Field(default_factory=list, description="R$ previsto de cada mês (centavos), o mesmo "
                                                                     "arredondamento do mensal")
    medido_mes: list[int] = Field(default_factory=list, description="% medido em cada mês (centésimos), alinhado "
                                                                    "com `mensal`; 0 em mês sem medição")
    previsto_ate_referencia: int = Field(0, description="% previsto acumulado até o mês da última medição")
    situacao: Literal["concluida", "atrasada", "adiantada", "em_dia", "nao_comecou"] | None = Field(
        None, description="no mês da última medição (regras.situacao_etapa); null sem cronograma")
    atraso_pct: int = Field(0, description="centésimos que o medido está abaixo do previsto (0 se não atrasada)")


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


class MesDesembolso(BaseModel):
    mes: str
    pago: int = Field(0, description="gastos confirmados (avulsos e pagamentos de pedido) pagos neste mês")
    previsto: int = Field(0, description="o cronograma vigente para o mês (o mesmo `previsto` do mensal)")
    projetado: int = Field(0, description="o que ainda vai sair neste mês, pelo cronograma (empurrado pelo atraso); "
                                          "0 antes do mês corrente")
    projetado_no_ritmo: int = Field(0, description="o mesmo, no ritmo medido até aqui (igual ao `projetado` sem "
                                                   "medição ou com a obra no ritmo do plano)")


class EtapaDesembolso(BaseModel):
    etapa: str
    descricao: str
    atual: int
    realizado: int
    executado_pct: int = Field(description="avanço usado na conta (centésimos): o medido, ou realizado ÷ orçado se a "
                                           "obra não tem medição")
    falta: int = Field(description="orçado atual − realizado, nunca negativo")
    devido_agora: int = Field(0, description="executado pela medição e ainda não realizado: cai no mês corrente")
    sem_cronograma: bool = Field(False, description="a etapa não está no cronograma: o que falta vai para `sem_mes`")


class TotaisDesembolso(BaseModel):
    orcado: int
    realizado: int
    pago: int
    falta: int = Field(description="Σ projetado + sem_mes: o que ainda vai sair (o mesmo nos dois cenários)")
    custo_final: int = Field(description="realizado + o que falta nas etapas")
    desvio: int = Field(description="custo_final − orçado (positivo = acima do orçado, pelo que já estourou)")
    entregue_sem_pagamento: int = Field(0, description="realizado − pago: pedido entregue sem pagamento registrado; "
                                                       "está no projetado do mês corrente")
    sem_mes: int = Field(0, description="o que falta nas etapas fora do cronograma")


class DesembolsoObra(BaseModel):
    """`GET /obras/{id}/desembolso`: a projeção de desembolso (servicos/desembolso.py explica a conta)."""
    mes_atual: str
    base: Literal["medicao", "gastos", "sem_cronograma"] = Field(
        description="de onde vem o avanço: a última medição; os gastos (obra sem medição); ou nada (sem cronograma, "
                    "tudo em sem_mes)")
    mes_referencia: str | None = Field(None, description="mês da última medição")
    atraso_meses: int = Field(0, description="quanto o resto do cronograma foi empurrado (o atraso do prazo projetado; "
                                             "0 sem medição ou com a obra adiantada)")
    ritmo_pct: int = Field(10000, description="% medido ÷ % previsto na última medição (centésimos, entre 1000 e "
                                              "10000); 10000 sem medição")
    termino_projetado: str | None = Field(None, description="último mês com desembolso, pelo cronograma")
    termino_no_ritmo: str | None = Field(None, description="último mês com desembolso, no ritmo atual")
    meses: list[MesDesembolso] = Field(default_factory=list)
    etapas: list[EtapaDesembolso] = Field(default_factory=list)
    totais: TotaisDesembolso


class MedicaoSaida(BaseModel):
    medicao: Medicao
    cronograma: CronogramaObra
    registro_id: str | None = None


# --- gastos: árvore da obra, comprovantes e gastos ---

class TopicoNaArvoreSaida(BaseModel):
    id: str
    nome: str


class EtapaNaArvoreSaida(BaseModel):
    chave: str = Field(description="chave fixa (ex.: 'eletrica') ou 'x-<id>' para etapa extra da obra")
    nome: str
    inclui: str = Field(description="o que entra nela (vazio em etapa extra)")
    fixa: bool = Field(description="não se renomeia nem se arquiva (etapa do orçamento ou da lista padrão)")
    topicos: list[TopicoNaArvoreSaida] = Field(default_factory=list)
    origem: Origem = Field(description="'orcamento' (etapa do orçamento vigente; chave = o código), 'padrao' (lista "
                                       "padrão, só em obra sem orçamento) ou 'extra'")


class EtapaExtraCorpo(Entrada):
    nome: str = Field(min_length=1, max_length=120)


class TopicoCorpo(Entrada):
    etapa: str = Field(min_length=1, max_length=80, description="chave da etapa (fixa ou extra) da árvore da obra")
    nome: str = Field(min_length=1, max_length=120)


class EdicaoArvoreCorpo(Entrada):
    """Renomear e/ou arquivar (arquivado = sai da árvore e não recebe gasto novo; os antigos continuam)."""
    nome: str | None = Field(None, min_length=1, max_length=120)
    arquivado: bool | None = None


class EtapaExtraSaida(EtapaExtra):
    chave: str = ""
    registro_id: str | None = None


class TopicoSaida(Topico):
    registro_id: str | None = None


class ComprovanteSaida(Comprovante):
    ja_existia: bool = Field(False, description="no envio: o mesmo arquivo já tinha sido enviado nesta obra")
    registro_id: str | None = Field(None, description="no descarte: a EntradaRegistro (o Desfazer usa)")


class ComprovanteDetalhe(Comprovante):
    revisao: Revisao = Field(description="o formulário de revisão pronto: estado e opções de cada campo")


class LeituraCorpo(Entrada):
    """O que o serviço do agente grava depois de ler: a leitura, ou o erro (frase para a pessoa)."""
    leitura: LeituraComprovante | None = None
    erro: str | None = Field(None, min_length=1, max_length=300)
    modelo: str | None = Field(None, max_length=80)

    @model_validator(mode="after")
    def _um_dos_dois(self):
        if (self.leitura is None) == (self.erro is None):
            raise ValueError("Mande a leitura ou o erro (um dos dois).")
        return self


class ConfirmarCorpo(Entrada):
    valor: int = Field(ge=1, description="centavos")
    data_pagamento: str = Field(max_length=10, description="AAAA-MM-DD")
    favorecido: str = Field(max_length=300)
    descricao: str = Field(max_length=400)
    etapa: str = Field(max_length=80)
    topico_id: str | None = Field(None, max_length=80)
    fornecedor_id: str | None = Field(None, max_length=80)
    pedido_id: str | None = Field(None, max_length=80, description="o gasto é o pagamento deste pedido")


class DescartarCorpo(Entrada):
    motivo: str = Field("", max_length=200)


class GastoSaida(Gasto):
    etapa_nome: str | None = None
    etapa_origem: Origem | None = None


class ReclassificarCorpo(Entrada):
    etapa: str = Field(min_length=1, max_length=80, description="chave de uma etapa da árvore de hoje")
    topico_id: str | None = Field(None, max_length=80)


class GastoReclassificado(GastoSaida):
    registro_id: str


class FiscalGastoCorpo(Entrada):
    natureza_fiscal: NaturezaFiscal | None = Field(description="o que o pagamento comprova no INSS; null = a "
                                                               "classificar")
    ibs_cbs: int = Field(0, ge=0, description="IBS/CBS destacado na nota, em centavos (vira crédito)")
    nf_cno: bool | None = Field(None, description="a nota de mão de obra traz o CNO e o endereço da obra")
    retencao: int = Field(0, ge=0, description="retenção de 11% destacada na nota, em centavos")


# --- tributos (estimativa) ---

class INSSObra(BaseModel):
    situacao: Literal["calculado", "isenta", "sem_dados"]
    motivos: list[str]
    area_equivalente: int = Field(description="área para cálculo do SERO, m² em milésimos")
    custo_obra: int = Field(description="COD: VAU × área para cálculo, centavos")
    rmt: int = Field(description="mão de obra presumida não decadente, centavos, com todas as reduções")
    fator_social: int = Field(description="centésimos de ponto (10000 = sem redução)")
    percentual_rmt: int
    percentual_categoria: int
    pre_moldado: Literal["reduziu", "virou_mista"] | None
    nao_decadente: int = Field(description="parte não decadente dos meses da obra, centésimos de ponto")
    comprovado: int = Field(description="o que abate da RMT: remunerações corrigidas pela Selic + concreto")
    comprovado_por_natureza: dict[str, int]
    credito_concreto: int
    credito_autonomo: int = Field(description="20% do autônomo/MEI declarado, abatido do débito")
    base: int
    inss_sem_fator_ajuste: int
    inss: int
    fator_ajuste: Literal["nao_se_aplica", "aplicado", "atingido", "nao_atingido"]
    fator_ajuste_minimo: int
    creditos_fator_ajuste: int
    falta_fator_ajuste: int = Field(description="remuneração declarada que falta para o INSS da aferição zerar")
    economia_com_notas: int = Field(description="quanto o INSS cairia se a mão de obra sem nota (ou com nota sem o "
                                                "CNO) fosse comprovada")
    mao_de_obra_sem_nf: int
    notas_sem_cno: int
    selic_faltando: list[str]
    avisos: list[str]
    linhas: list[str]


class IBSCBSObra(BaseModel):
    situacao: Literal["calculado", "nao_incide", "sem_dados"]
    motivos: list[str]
    aliquota_efetiva: int
    valor_operacao: int
    redutor_ajuste: int
    redutor_social: int
    base: int
    debito: int
    creditos: int
    a_pagar: int
    transicao: str
    linhas: list[str]


class GastoAClassificar(BaseModel):
    id: str
    valor: int
    data_pagamento: str
    favorecido: str
    descricao: str
    natureza_fiscal: str | None
    nf_cno: bool | None = None


class LinhaVAU(BaseModel):
    uf: str
    mes: str = Field(description="AAAA-MM")
    destinacao: Destinacao
    vau: int = Field(description="centavos por m²")
    fonte: str


class TributosObra(BaseModel):
    obra_id: str
    cno: str
    vau_referencia: str | None = Field(description="AAAA-MM do VAU usado na conta")
    vau_usado: int = Field(0, description="centavos por m²; 0 = sem VAU (a conta fica sem_dados)")
    vau_fonte: Literal["informado", "tabela_sero"] | None = Field(
        None, description="informado = o VAU à mão da obra; tabela_sero = o vigente da tabela VAU para a UF e a "
                          "destinação da obra")
    destinacao: str | None = Field(None, description="a destinação usada (a da obra, ou a que sai da tipologia)")
    categoria: str = Field("obra_nova", description="a categoria usada (a da obra, ou reforma pela tipologia)")
    afericao: str = Field(description="AAAA-MM: o mês em que a conta supõe a aferição (hoje)")
    selic_ate: str | None = Field(None, description="AAAA-MM da última Selic carregada")
    inss: INSSObra
    ibs_cbs: IBSCBSObra
    a_classificar: int = Field(description="gastos avulsos sem natureza fiscal (contam zero no INSS)")
    valor_a_classificar: int
    sem_nota: list[GastoAClassificar] = Field(description="até 20 pagamentos de mão de obra sem nota (ou com nota "
                                                          "sem o CNO da obra), maiores primeiro: o que mais reduz o "
                                                          "INSS se for comprovado")
    pendentes: list[GastoAClassificar] = Field(description="até 20 gastos a classificar, maiores primeiro")
    parametros: dict[str, Any] = Field(description="percentuais usados (inss, ibs_cbs), com a fonte")
    aviso: str


class Confirmacao(BaseModel):
    gasto: GastoSaida
    comprovante: Comprovante
    registro_id: str


class TopicoNoResumo(BaseModel):
    topico_id: str | None = Field(description="null = gasto da etapa sem tópico")
    nome: str
    total: int


class EtapaNoResumo(BaseModel):
    etapa: str
    nome: str
    origem: Origem
    total: int = Field(description="centavos; só gastos avulsos (os de pedido ficam em pagamentos_de_pedidos)")
    topicos: list[TopicoNoResumo]


class ResumoGastos(BaseModel):
    total: int = Field(description="gastos avulsos confirmados (centavos): é o que soma no realizado do orçamento")
    pagamentos_de_pedidos: int = Field(description="gastos ligados a pedido: o custo já conta pela entrega")
    a_revisar: int = Field(description="comprovantes esperando revisão (não contam em nada)")
    tem_orcamento: bool = Field(False, description="a árvore da obra vem do orçamento")
    fora_do_orcamento: int = Field(0, description="parte do total nas etapas extras")
    sem_etapa_do_orcamento: int = Field(0, description="parte do total em etapa que não é do orçamento nem extra")
    etapas: list[EtapaNoResumo]
