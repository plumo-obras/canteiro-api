"""Leitura do comprovante pela IA e como ela vira a tela de revisão.

O comprovante não tem padrão (NF, Pix, boleto, recibo, papel à mão). O agente lê e devolve `LeituraComprovante`; a
API guarda e, a cada abertura da revisão, `montar_revisao` transforma a leitura no formulário: cada campo em um de
três estados, sempre com espaço para a resposta própria da pessoa no mesmo destaque das opções.

- **lido**: a IA leu com certeza; a opção já vem marcada (`escolhido = 0`) e pode ser editada.
- **conferir**: a IA ficou em dúvida; até 3 opções, cada uma com o motivo, e **nenhuma marcada**. Um valor errado com
  cara de certo é o maior risco da feature: na dúvida, quem escolhe é a pessoa.
- **vazio**: não deu para ler; só o campo livre. A IA nunca chuta.

A leitura é recalculada a cada abertura (e não gravada já pronta) porque a árvore da obra muda: tópico arquivado
depois da leitura deixa de ser oferecido.
"""
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .etapas import EtapaNaArvore
from .regras import cnpj_valido
from .tipos import fmt_reais, reais

MAX_OPCOES = 3
MAX_FAVORECIDO = 120
MAX_DESCRICAO = 200
# data de pagamento mais velha que isto é suspeita (comprovante antigo, ano lido errado): vai para conferir
DIAS_DATA_ANTIGA = 365

Certeza = Literal["alta", "baixa"]
Estado = Literal["lido", "conferir", "vazio"]
TipoDocumento = Literal["nota_fiscal", "comprovante_pix", "comprovante_transferencia", "boleto", "recibo",
                        "anotacao_a_mao", "outro"]
SituacaoPagamento = Literal["pago", "a_pagar", "incerto"]


# --- o que o agente devolve (saída estruturada do modelo) ---
# Sem limites de tamanho ou número no esquema: a saída estruturada do modelo não aceita todos, e quem aplica regra é
# `montar_revisao`, que também descarta o que vier fora do esperado.

class OpcaoLida(BaseModel):
    valor: str = Field(description="o que está escrito no comprovante, sem completar nem deduzir")
    motivo: str = Field(description="de onde saiu este valor, em uma frase curta (ex.: 'valor do Pix')")


class CampoLido(BaseModel):
    certeza: Certeza = Field(description="'alta' só se legível e sem ambiguidade; senão 'baixa'")
    opcoes: list[OpcaoLida] = Field(description="vazia se não deu para ler; uma se 'alta'; até 3, a mais provável "
                                                "primeiro, se 'baixa'")


class OpcaoEtapa(BaseModel):
    etapa: str = Field(description="a chave exata de uma etapa da lista da obra")
    topico_id: str | None = Field(description="o id exato de um tópico dessa etapa, ou null")
    motivo: str = Field(description="por que este gasto entra aqui, em uma frase")


class Classificacao(BaseModel):
    certeza: Certeza
    opcoes: list[OpcaoEtapa] = Field(description="até 3 etapas (com tópico, se houver), a mais provável primeiro")


class LeituraComprovante(BaseModel):
    tipo_documento: TipoDocumento
    situacao_pagamento: SituacaoPagamento = Field(
        description="o documento comprova dinheiro que já saiu? 'a_pagar' para nota a prazo e boleto em aberto")
    valor: CampoLido = Field(description="valor efetivamente pago, como está escrito (ex.: '1.234,56')")
    data_pagamento: CampoLido = Field(description="data do pagamento, AAAA-MM-DD")
    favorecido: CampoLido = Field(description="quem recebeu o dinheiro (no Pix, o recebedor, não o pagador)")
    cnpj_favorecido: str | None = Field(description="CNPJ de quem recebeu, se aparecer inteiro; senão null")
    descricao: CampoLido = Field(description="o que foi comprado ou pago, em poucas palavras")
    classificacao: Classificacao
    observacoes: str | None = Field(description="o que atrapalhou a leitura ou o que a pessoa precisa saber")


# --- o que a tela de revisão recebe ---

class OpcaoRevisao(BaseModel):
    valor: int | str                       # centavos (valor), "AAAA-MM-DD" (data) ou texto
    rotulo: str                            # como a tela mostra ("R$ 1.234,56", "03/11/2026")
    motivo: str = ""
    fornecedor_id: str | None = None       # só no favorecido, quando a opção é um fornecedor cadastrado


class CampoRevisao(BaseModel):
    estado: Estado
    opcoes: list[OpcaoRevisao] = Field(default_factory=list)
    escolhido: int | None = None           # índice da opção já marcada; só no estado "lido"


class OpcaoClassificacao(BaseModel):
    etapa: str
    topico_id: str | None = None
    rotulo: str                            # "Instalações elétricas › Quadro geral"
    motivo: str = ""


class ClassificacaoRevisao(BaseModel):
    estado: Estado
    opcoes: list[OpcaoClassificacao] = Field(default_factory=list)
    escolhido: int | None = None


class Revisao(BaseModel):
    valor: CampoRevisao
    data_pagamento: CampoRevisao
    favorecido: CampoRevisao
    descricao: CampoRevisao
    classificacao: ClassificacaoRevisao
    tipo_documento: TipoDocumento | None = None
    situacao_pagamento: SituacaoPagamento | None = None
    avisos: list[str] = Field(default_factory=list)
    observacoes: str | None = None


VAZIO = CampoRevisao(estado="vazio")


def centavos_lidos(texto: str) -> int | None:
    """Valor como aparece em comprovante brasileiro → centavos. "1.234" é mil e duzentos e trinta e quatro (no
    comprovante o ponto com três casas é milhar, sem a ambiguidade da planilha). Zero, negativo ou ilegível → None."""
    t = texto.replace("R$", "").replace(" ", "").strip()
    partes = t.split(".")
    if "," not in t and len(partes) > 1 and all(len(p) == 3 for p in partes[1:]) and partes[0].isdigit():
        t = t.replace(".", "")
    try:
        c = reais(t)
    except (ValueError, ArithmeticError):
        return None
    return c if c > 0 else None


def data_lida(texto: str) -> date | None:
    try:
        return date.fromisoformat(texto.strip())
    except ValueError:
        return None


def _texto(texto: str, limite: int) -> str | None:
    limpo = " ".join(texto.split())[:limite]
    return limpo or None


def _estado(certeza: Certeza, lidas: int, validas: list) -> tuple[Estado, int | None]:
    """'lido' só com certeza alta e uma opção só, válida. Certeza alta com várias opções é contradição: conferir."""
    if not validas:
        return "vazio", None
    if certeza == "alta" and lidas == 1 and len(validas) == 1:
        return "lido", 0
    return "conferir", None


def _campo(campo: CampoLido, converter) -> CampoRevisao:
    opcoes: list[OpcaoRevisao] = []
    for o in campo.opcoes:
        convertido = converter(o.valor)
        if convertido is None or any(x.valor == convertido[0] for x in opcoes):
            continue
        opcoes.append(OpcaoRevisao(valor=convertido[0], rotulo=convertido[1], motivo=o.motivo.strip()))
    opcoes = opcoes[:MAX_OPCOES]
    estado, escolhido = _estado(campo.certeza, len(campo.opcoes), opcoes)
    return CampoRevisao(estado=estado, opcoes=opcoes, escolhido=escolhido)


def _valor(texto: str):
    c = centavos_lidos(texto)
    return None if c is None else (c, fmt_reais(c))


def _data(texto: str):
    d = data_lida(texto)
    return None if d is None else (d.isoformat(), d.strftime("%d/%m/%Y"))


def _favorecido(texto: str):
    t = _texto(texto, MAX_FAVORECIDO)
    return None if t is None else (t, t)


def _descricao(texto: str):
    t = _texto(texto, MAX_DESCRICAO)
    return None if t is None else (t, t)


def _fmt_cnpj(c: str) -> str:
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def _com_fornecedor(campo: CampoRevisao, cnpj: str | None, fornecedores: Iterable[dict]) -> CampoRevisao:
    """CNPJ lido que bate com um fornecedor cadastrado vira a opção marcada: os dois dígitos verificadores tornam
    improvável um CNPJ mal lido coincidir com outro cadastrado. O nome lido continua como alternativa (razão social
    e nome fantasia costumam diferir)."""
    digitos = "".join(ch for ch in (cnpj or "") if ch.isdigit())
    if not cnpj_valido(digitos):
        return campo
    f = next((f for f in fornecedores if f.get("cnpj") == digitos), None)
    if f is None:
        return campo
    do_cadastro = OpcaoRevisao(valor=f["nome"], rotulo=f["nome"], fornecedor_id=f["id"],
                               motivo=f"CNPJ {_fmt_cnpj(digitos)}, fornecedor cadastrado")
    resto = [o for o in campo.opcoes if o.valor != f["nome"]]
    return CampoRevisao(estado="lido", opcoes=[do_cadastro, *resto][:MAX_OPCOES], escolhido=0)


def _classificacao(c: Classificacao, arvore: list[EtapaNaArvore]) -> ClassificacaoRevisao:
    """Etapa fora da árvore é descartada. Tópico inventado (ou arquivado, ou de outra etapa) cai, mas a etapa fica: a
    sugestão da etapa continua útil."""
    nos = {e.chave: e for e in arvore}
    opcoes: list[OpcaoClassificacao] = []
    for o in c.opcoes:
        no = nos.get(o.etapa)
        if no is None:
            continue
        topico = next((t for t in no.topicos if t.id == o.topico_id), None)
        topico_id = topico.id if topico else None
        if any(x.etapa == no.chave and x.topico_id == topico_id for x in opcoes):
            continue
        rotulo = f"{no.nome} › {topico.nome}" if topico else no.nome
        opcoes.append(OpcaoClassificacao(etapa=no.chave, topico_id=topico_id, rotulo=rotulo, motivo=o.motivo.strip()))
    opcoes = opcoes[:MAX_OPCOES]
    estado, escolhido = _estado(c.certeza, len(c.opcoes), opcoes)
    return ClassificacaoRevisao(estado=estado, opcoes=opcoes, escolhido=escolhido)


def _avisos_da_data(campo: CampoRevisao, hoje: date) -> tuple[CampoRevisao, list[str]]:
    """Data no futuro ou muito antiga vai para conferir, mesmo que a IA tenha lido com certeza."""
    avisos = []
    datas = [date.fromisoformat(str(o.valor)) for o in campo.opcoes]
    if any(d > hoje for d in datas):
        avisos.append("A data lida é depois de hoje: confira.")
    if any(d < hoje - timedelta(days=DIAS_DATA_ANTIGA) for d in datas):
        avisos.append("A data lida tem mais de um ano: confira.")
    if avisos and campo.estado == "lido":
        campo = campo.model_copy(update={"estado": "conferir", "escolhido": None})
    return campo, avisos


AVISO_A_PAGAR = ("Este documento não comprova pagamento (nota a prazo ou boleto em aberto). Gasto é o dinheiro que "
                 "já saiu: lance quando pagar, pelo comprovante do pagamento.")
AVISO_INCERTO = "Não dá para saber pelo documento se já foi pago: confira antes de confirmar."


def montar_revisao(leitura: LeituraComprovante | None, *, arvore: list[EtapaNaArvore], hoje: date,
                   fornecedores: Iterable[dict] = ()) -> Revisao:
    """O formulário de revisão a partir da leitura. Sem leitura (falhou, recusou, sem crédito): tudo vazio, e a
    pessoa preenche à mão. `fornecedores` = os do tenant ({id, nome, cnpj}), para casar o CNPJ lido."""
    if leitura is None:
        return Revisao(valor=VAZIO, data_pagamento=VAZIO, favorecido=VAZIO, descricao=VAZIO,
                       classificacao=ClassificacaoRevisao(estado="vazio"))
    data, avisos = _avisos_da_data(_campo(leitura.data_pagamento, _data), hoje)
    if leitura.situacao_pagamento == "a_pagar":
        avisos.insert(0, AVISO_A_PAGAR)
    elif leitura.situacao_pagamento == "incerto":
        avisos.insert(0, AVISO_INCERTO)
    return Revisao(
        valor=_campo(leitura.valor, _valor),
        data_pagamento=data,
        favorecido=_com_fornecedor(_campo(leitura.favorecido, _favorecido), leitura.cnpj_favorecido, fornecedores),
        descricao=_campo(leitura.descricao, _descricao),
        classificacao=_classificacao(leitura.classificacao, arvore),
        tipo_documento=leitura.tipo_documento,
        situacao_pagamento=leitura.situacao_pagamento,
        avisos=avisos,
        observacoes=_texto(leitura.observacoes or "", 500),
    )
