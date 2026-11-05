"""Propostas (o agente só propõe), aprovação que executa e o desfazer a partir do registro.

Duplicata se evita de dois jeitos, os dois pelo ID do documento (o Firestore não tem índice único nem deixa consultar
dentro da transação):
- `chave_idempotencia`: a proposta ganha um ID derivado da chave. O agente manda (sessão, tool_use): a repetição da
  MESMA chamada de ferramenta cai no mesmo documento.
- Trava de pendência: um documento em `propostas_pendentes` com ID derivado de (tenant, obra, tipo, dados canônicos)
  aponta para a proposta pendente igual. Propor de novo a mesma coisa devolve a que já espera aprovação. Aprovar ou
  recusar solta a trava (na mesma transação), e aí a mesma proposta pode nascer de novo. Uma trava por conteúdo, e
  não um ID determinístico na própria proposta, porque a proposta decidida continua existindo com aquele conteúdo.
"""
import hashlib
import json
from dataclasses import replace

from pydantic import ValidationError

from canteiro_dominio.armazenamento import ConflitoDeId
from canteiro_dominio.modelos import Proposta, StatusPedido, StatusProposta, TipoMovimento, TipoProposta, agora
from canteiro_dominio.regras import (
    texto_da_entrega,
    validar_conferencia,
    validar_entrega,
    validar_transicao,
)
from canteiro_dominio.tipos import fmt_qtd, fmt_reais

from ..auth import UsuarioAtual
from ..esquemas import (
    AJUSTAVEIS,
    DADOS_POR_TIPO,
    AjusteEstoqueDados,
    AvancarPedidoDados,
    PedidoDados,
    PropostaCorpo,
    RegistrarEntregaDados,
)
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    insumo_da_obra,
    obra_do_tenant,
    registrar,
)
from . import cronograma, fornecedores, gastos, obras, pedidos, versoes
from .estoque import (
    id_derivado,
    linhas_de,
    preparar_estorno,
    preparar_movimento,
    preparar_restaurar_insumos,
    recusar_se_ha_pedido_aberto,
)
from .pedidos import (
    id_recebimento,
    preparar_avancar,
    preparar_cancelar_criado,
    preparar_criar_pedido,
    preparar_receber,
    preparar_voltar,
)


def _dados(tipo: TipoProposta, dados: dict):
    try:
        return DADOS_POR_TIPO[tipo].model_validate(dados)
    except ValidationError as e:
        campos = ", ".join(".".join(map(str, err["loc"])) or "dados" for err in e.errors())
        raise ErroNegocio(f"Dados da proposta '{tipo.value}' inválidos ({campos}).") from None


# O que aprovar faz, para o botão da tela dizer a consequência ("Aprovar entrada", "Aprovar encerramento").
EFEITO_POR_TIPO = {TipoProposta.CRIAR_PEDIDO: "compra", TipoProposta.AJUSTE_ESTOQUE: "ajuste",
                   TipoProposta.REGISTRAR_ENTREGA: "entrada"}


def efeito_do_avanco(de: StatusPedido, para: StatusPedido) -> str:
    """"conferido" tem dois sentidos: de parcial ENCERRA o resto (nada entra); de recebido dá entrada do que falta."""
    if para == StatusPedido.CONFERIDO:
        return "encerramento" if de == StatusPedido.PARCIAL else "entrada"
    return "cancelamento" if para == StatusPedido.CANCELADO else "status"


def _entregas(dados: RegistrarEntregaDados) -> list[tuple[int, int]]:
    return [(x.indice, x.quantidade) for x in dados.linhas]


async def _pedido_da_obra(leitor, u: UsuarioAtual, pedido_id: str, obra_id: str) -> dict:
    p = await leitor.obter("pedidos", u.tenant_id, pedido_id)
    if not p or p["obra_id"] != obra_id:
        raise NaoEncontrado("Pedido não encontrado nesta obra.")
    return p


async def _linhas_com_nome(leitor, u: UsuarioAtual, p: dict) -> list[dict]:
    """As linhas do pedido com nome e unidade: o gravado antes das linhas não guardava a cópia do insumo, e a conta da
    entrega sem eles sairia ": recebido 0 + …"."""
    linhas = [dict(x) for x in linhas_de(p)]
    for x in linhas:
        if not x.get("nome"):
            ins = await insumo_da_obra(leitor, u.tenant_id, p["obra_id"], x["insumo_id"])
            x |= {"nome": ins["nome"], "unidade": ins["unidade"]}
    return linhas


def _regras_dos_dados(dados) -> None:
    """Regras que não dependem do banco: valem ao criar e de novo depois de um ajuste na aprovação."""
    if isinstance(dados, AjusteEstoqueDados) and dados.quantidade == 0:
        raise ErroNegocio("Ajuste de zero não muda nada.")
    if isinstance(dados, AvancarPedidoDados):
        validar_conferencia(dados.para, quantidade_recebida=dados.quantidade_recebida, nf=dados.nf)


TRAVAS = "propostas_pendentes"


def id_trava(tenant_id: str, obra_id: str, tipo: str, dados: dict) -> str:
    """O mesmo conteúdo dá sempre o mesmo ID. `dados` é o já validado e normalizado (o que fica gravado): "34" e
    "34,0" viram o mesmo inteiro antes de chegar aqui, e a ordem das chaves não importa."""
    canonico = json.dumps(dados, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return id_derivado(tenant_id, "proposta-pendente", obra_id, tipo, hashlib.sha256(canonico.encode()).hexdigest())


def _trava_de(prop: dict) -> str:
    # a trava foi feita com o que o agente propôs; um ajuste na aprovação muda `dados`, não o que estava travado
    return id_trava(prop["tenant_id"], prop["obra_id"], prop["tipo"], prop.get("dados_originais") or prop["dados"])


async def _ler_trava(tx, u: UsuarioAtual, prop: dict) -> dict | None:
    t = await tx.obter(TRAVAS, u.tenant_id, _trava_de(prop))
    return t if t and t.get("proposta_id") == prop["id"] else None


def _soltar_trava(tx, u: UsuarioAtual, trava: dict | None) -> None:
    if trava is not None:
        tx.atualizar(TRAVAS, u.tenant_id, trava["id"], {"proposta_id": None, "atualizado_em": agora()})


async def criar_proposta(arm, u: UsuarioAtual, corpo: PropostaCorpo) -> tuple[dict, bool]:
    """Só valida e guarda. Conferir as referências agora evita cartão de decisão que nunca poderia ser aprovado.

    Devolve (proposta, já_existia): já_existia quando a chave já tinha sido usada ou quando uma proposta igual ainda
    espera aprovação nesta obra; nesses casos nada é gravado."""
    dados = _dados(corpo.tipo, corpo.dados)
    _regras_dos_dados(dados)
    await obra_do_tenant(arm, u.tenant_id, corpo.obra_id)
    conta, efeito = corpo.conta, EFEITO_POR_TIPO.get(corpo.tipo)
    match dados:
        case PedidoDados():
            for x in dados.linhas:
                await insumo_da_obra(arm, u.tenant_id, corpo.obra_id, x.insumo_id)
            await versoes.validar_etapa(arm, u.tenant_id, corpo.obra_id, [x.etapa for x in dados.linhas],
                                        exigir=False)
        case AjusteEstoqueDados():
            await insumo_da_obra(arm, u.tenant_id, corpo.obra_id, dados.insumo_id)
        case AvancarPedidoDados():
            p = await _pedido_da_obra(arm, u, dados.pedido_id, corpo.obra_id)
            de = StatusPedido(p["status"])
            validar_transicao(de, dados.para)
            validar_conferencia(dados.para, quantidade_recebida=dados.quantidade_recebida, nf=dados.nf, de=de)
            efeito = efeito_do_avanco(de, dados.para)
        case RegistrarEntregaDados():
            p = await _pedido_da_obra(arm, u, dados.pedido_id, corpo.obra_id)
            previa = validar_entrega(StatusPedido(p["status"]), await _linhas_com_nome(arm, u, p), _entregas(dados),
                                     confirmar_excesso=dados.confirmar_excesso)
            # a conta da entrega sai do domínio, não de quem propõe: "recebido 18 + esta entrega 7 = 25 de 25"
            conta = f"{p['numero']} · {texto_da_entrega(previa)}"
    chave = corpo.chave_idempotencia
    p = Proposta(tenant_id=u.tenant_id, obra_id=corpo.obra_id, tipo=corpo.tipo, titulo=corpo.titulo,
                 conta=conta, justificativa=corpo.justificativa, fonte=corpo.fonte, efeito=efeito,
                 dados=_guardado(corpo.tipo, dados.model_dump(mode="json")),
                 **({"id": id_derivado(u.tenant_id, "proposta-chave", chave)} if chave else {}))
    d = doc(p)
    trava_id = _trava_de(d)

    async def corpo_tx(tx):
        if chave:
            mesma = await tx.obter("propostas", u.tenant_id, d["id"])
            if mesma:
                if (mesma["obra_id"], mesma["tipo"], mesma.get("dados_originais") or mesma["dados"]) != (
                        d["obra_id"], d["tipo"], d["dados"]):
                    raise Conflito("Esta chave de idempotência já foi usada com outra proposta.")
                return mesma, True
        trava = await tx.obter(TRAVAS, u.tenant_id, trava_id)
        if trava and trava.get("proposta_id"):
            igual = await tx.obter("propostas", u.tenant_id, trava["proposta_id"])
            if igual and igual["status"] == StatusProposta.PENDENTE.value:
                return igual, True
        # A trava é o documento que decide, e por isso é gravado sempre (no Firestore a leitura dela entra no
        # controle de conflito; no Mongo a escrita é que colide): duas propostas iguais ao mesmo tempo não passam.
        if trava:
            tx.atualizar(TRAVAS, u.tenant_id, trava_id, {"proposta_id": d["id"], "atualizado_em": agora()})
        else:
            tx.criar(TRAVAS, {"id": trava_id, "tenant_id": u.tenant_id, "proposta_id": d["id"],
                              "atualizado_em": agora()})
        tx.criar("propostas", d)
        evento(tx, u.tenant_id, "proposta.criada", corpo.obra_id, {"proposta_id": p.id, "tipo": p.tipo.value})
        return d, False

    try:
        return await arm.em_transacao(corpo_tx)
    except ConflitoDeId:
        # Corrida: a outra criou a trava (ou a proposta da mesma chave) entre a leitura e a gravação desta. A segunda
        # volta lê o que ela gravou e devolve a existente.
        return await arm.em_transacao(corpo_tx)


async def _preparar_proposta(tx, u: UsuarioAtual, prop: dict) -> Operacao:
    tipo = TipoProposta(prop["tipo"])
    dados = _dados(tipo, prop["dados"])
    match tipo:
        case TipoProposta.CRIAR_PEDIDO:
            # aprovar a proposta É a aprovação da compra: o pedido nasce aprovado, sem pedir um segundo "sim"
            return await preparar_criar_pedido(tx, u, prop["obra_id"], dados, status=StatusPedido.APROVADO)
        case TipoProposta.AVANCAR_PEDIDO:
            atual = await _pedido_da_obra(tx, u, dados.pedido_id, prop["obra_id"])
            # o cartão prometeu uma consequência; se o pedido andou e "conferido" passou a significar outra coisa
            # (era entrada, virou encerramento), aprovar faria o que ninguém viu
            if prop.get("efeito") and efeito_do_avanco(StatusPedido(atual["status"]), dados.para) != prop["efeito"]:
                raise Conflito(f"{atual['numero']} mudou desde a proposta (está {atual['status']}); recuse e peça "
                               "outra.")
            return await preparar_avancar(tx, u, dados.pedido_id, dados.para, origem="agente",
                                          quantidade_recebida=dados.quantidade_recebida, nf=dados.nf)
        case TipoProposta.REGISTRAR_ENTREGA:
            atual = await _pedido_da_obra(tx, u, dados.pedido_id, prop["obra_id"])
            # revalida contra o pedido de agora: outra entrega no meio pode ter tornado esta um excesso
            validar_entrega(StatusPedido(atual["status"]), linhas_de(atual), _entregas(dados),
                            confirmar_excesso=dados.confirmar_excesso)
            # o mesmo serviço do "Registrar entrega" da tela; o ID da entrega sai da proposta (aprovar de novo depois
            # de desfazer é outra proposta, outra entrega)
            op = await preparar_receber(
                tx, u, dados.pedido_id, _entregas(dados), nf_numero=dados.nf_numero, nf_data=dados.nf_data,
                nf_valor=dados.nf_valor, origem="agente",
                recebimento_id=id_recebimento(u.tenant_id, dados.pedido_id, f"proposta-{prop['id']}"))
            receber, texto_rec, desfazer_rec = op.aplicar, op.texto, op.desfazer
            # o registro guarda a entrega; quem aprova recebe o pedido como nas outras propostas de pedido
            return replace(op, aplicar=lambda: (lambda r: {**r["pedido"], "_entrega": r})(receber()),
                           texto=lambda d: texto_rec(d["_entrega"]), desfazer=lambda d: desfazer_rec(d["_entrega"]))
        case TipoProposta.AJUSTE_ESTOQUE:
            op = await preparar_movimento(
                tx, u, obra_id=prop["obra_id"], insumo_id=dados.insumo_id, tipo=TipoMovimento.AJUSTE,
                quantidade=dados.quantidade, id=id_derivado(u.tenant_id, "proposta", prop["id"]), origem="agente")
            texto = op.texto
            op.texto = lambda d: f"{texto(d)}: {dados.motivo}"
            return op


async def _proposta_pendente(tx, u: UsuarioAtual, proposta_id: str) -> dict:
    prop = await tx.obter("propostas", u.tenant_id, proposta_id)
    if not prop:
        raise NaoEncontrado("Proposta não encontrada.")
    if prop["status"] != StatusProposta.PENDENTE.value:
        raise Conflito(f"Esta proposta já foi {prop['status']}.")
    return prop


def _mesclar_ajustes(prop: dict, ajustes: dict) -> tuple[dict, dict]:
    """Devolve (dados mesclados e validados, só o que mudou). Revalida com o esquema do tipo: um ajuste não pode
    produzir o que a criação recusaria."""
    tipo = TipoProposta(prop["tipo"])
    desconhecidas = sorted(set(ajustes) - AJUSTAVEIS[tipo])
    if desconhecidas:
        raise ErroNegocio(f"Não dá para ajustar {', '.join(desconhecidas)} numa proposta '{tipo.value}' "
                          f"(ajustáveis: {', '.join(sorted(AJUSTAVEIS[tipo]))}).")
    base = prop["dados"]
    if tipo == TipoProposta.CRIAR_PEDIDO:
        base = _pedido_plano(base)
    dados = _dados(tipo, {**base, **ajustes})
    _regras_dos_dados(dados)
    mesclados = _guardado(tipo, dados.model_dump(mode="json"))
    mudou = {k: (base.get(k), mesclados[k]) for k in ajustes if base.get(k) != mesclados[k]}
    return mesclados, mudou


def _guardado(tipo: TipoProposta, dados: dict) -> dict:
    """Como a proposta fica gravada: a de pedido com uma linha só fica no formato plano (insumo, quantidade, valor),
    que é o que o cartão de aprovação mostra e o que o ajuste edita."""
    if tipo == TipoProposta.CRIAR_PEDIDO and len(dados.get("linhas", [])) == 1:
        return _pedido_plano(dados)
    return dados


def _pedido_plano(dados: dict) -> dict:
    """A proposta de pedido do agente tem uma linha só: o ajuste fala de quantidade e valor como antes das linhas."""
    if "linhas" not in dados:
        return dados
    if len(dados["linhas"]) != 1:
        raise ErroNegocio("Esta proposta tem várias linhas; recuse e peça outra em vez de ajustar.")
    x = dados["linhas"][0]
    resto = {k: v for k, v in dados.items() if k != "linhas"}
    total = x.get("total")
    if total is None:
        total = (x["quantidade"] * x.get("valor_unitario", 0) + 500) // 1000
    return {**resto, "insumo_id": x["insumo_id"], "quantidade": x["quantidade"], "valor": total,
            "etapa": x.get("etapa")}


async def _unidade(tx, u: UsuarioAtual, prop: dict) -> str:
    """Só para escrever o ajuste no registro com a unidade ("34 → 30 sc")."""
    dados = prop["dados"]
    insumo_id = dados.get("insumo_id") or (dados.get("linhas") or [{}])[0].get("insumo_id")
    if not insumo_id and dados.get("pedido_id"):
        p = await tx.obter("pedidos", u.tenant_id, dados["pedido_id"])
        insumo_id = p and linhas_de(p)[0]["insumo_id"]
    i = await tx.obter("insumos", u.tenant_id, insumo_id) if insumo_id else None
    return i["unidade"] if i else ""


def _descrever_ajuste(mudou: dict, unidade: str) -> str:
    def fmt(campo: str, v) -> str:
        if v is None:
            return "—"
        if campo in ("quantidade", "quantidade_recebida"):
            return fmt_qtd(v)
        if campo in ("valor", "nf_valor"):
            return fmt_reais(v)
        if campo == "linhas":
            return ", ".join(f"linha {x['indice'] + 1}: {fmt_qtd(x['quantidade'])}" for x in v)
        return str(v)

    partes = []
    for campo, (antes, depois) in mudou.items():
        un = f" {unidade}" if campo in ("quantidade", "quantidade_recebida") and unidade else ""
        partes.append(f"{campo.replace('_', ' ')} {fmt(campo, antes)} → {fmt(campo, depois)}{un}")
    return "; ".join(partes)


async def aprovar(arm, u: UsuarioAtual, proposta_id: str, ajustes: dict | None = None) -> tuple[dict, dict]:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        trava = await _ler_trava(tx, u, prop)
        campos: dict = {}
        if ajustes:
            dados, mudou = _mesclar_ajustes(prop, ajustes)
            if mudou:
                campos = {"dados": dados, "dados_originais": prop["dados"], "ajustada": True}
                unidade = await _unidade(tx, u, prop)
        op = await _preparar_proposta(tx, u, {**prop, **campos})
        if campos:
            texto = op.texto
            op.texto = lambda d: f"{texto(d)} · ajustado: {_descrever_ajuste(mudou, unidade)}"
        _, entrada = executar_e_registrar(tx, u, op, origem="agente", proposta_id=proposta_id)
        campos |= {"status": StatusProposta.APROVADA.value, "decidido_por": u.id,
                   "decidido_por_nome": u.nome, "decidido_em": agora(), "registro_id": entrada["id"]}
        # a proposta grava com os dados que foram executados, na mesma transação da execução
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        _soltar_trava(tx, u, trava)
        evento(tx, u.tenant_id, "proposta.aprovada", prop["obra_id"], {"proposta_id": proposta_id,
                                                                      "ajustada": bool(campos.get("ajustada"))})
        return {**prop, **campos}, entrada

    return await arm.em_transacao(corpo)


async def recusar(arm, u: UsuarioAtual, proposta_id: str, motivo: str) -> dict:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        trava = await _ler_trava(tx, u, prop)
        campos = {"status": StatusProposta.RECUSADA.value, "decidido_por": u.id,
                  "decidido_por_nome": u.nome, "decidido_em": agora(),
                  "motivo_recusa": motivo}
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        _soltar_trava(tx, u, trava)
        registrar(tx, u, obra_id=prop["obra_id"], texto=f"Recusou: {prop['titulo']} ({motivo})", origem="tela",
                  proposta_id=proposta_id)
        evento(tx, u.tenant_id, "proposta.recusada", prop["obra_id"], {"proposta_id": proposta_id})
        return {**prop, **campos}

    return await arm.em_transacao(corpo)


_DESFAZER = {
    "estornar_movimento": lambda tx, u, r: preparar_estorno(tx, u, r["movimento_id"]),
    "voltar_pedido": preparar_voltar,
    "desfazer_recebimento": pedidos.preparar_desfazer_recebimento,
    "editar_pedido": pedidos.preparar_desfazer_edicao,
    "editar_fornecedor": fornecedores.preparar_desfazer_edicao,
    "restaurar_cronograma": cronograma.preparar_restaurar,
    "desfazer_medicao": cronograma.preparar_desfazer_medicao,
    "refazer_medicao": cronograma.preparar_refazer_medicao,
    "cancelar_pedido": preparar_cancelar_criado,
    "restaurar_obra": obras.preparar_restaurar,
    "reabrir_orcamento": obras.preparar_reabrir,
    "desligar_versao": versoes.preparar_desligar,
    "restaurar_insumos": preparar_restaurar_insumos,
    "editar_arvore": gastos.preparar_desfazer_arvore,
    "estornar_gasto": gastos.preparar_estorno_gasto,
    "reabrir_comprovante": gastos.preparar_reabrir,
}


def _com_texto(receita: dict | None, texto: str) -> dict | None:
    if receita and receita.get("refazer"):
        return {**receita, "texto": receita.get("texto") or texto.removeprefix("Refeito: ")}
    return receita


async def desfazer(arm, u: UsuarioAtual, entrada_id: str) -> dict:
    previa = await arm.obter("registro", u.tenant_id, entrada_id)
    receita_previa = (previa or {}).get("desfazer") or {}
    if receita_previa.get("op") == "restaurar_insumos" and receita_previa.get("arquivar"):
        await recusar_se_ha_pedido_aberto(arm, u.tenant_id, receita_previa["obra_id"],
                                          [x["id"] for x in receita_previa["arquivar"]])

    async def corpo(tx):
        e = await tx.obter("registro", u.tenant_id, entrada_id)
        if not e:
            raise NaoEncontrado("Entrada do registro não encontrada.")
        if e["desfeito"]:
            raise Conflito("Isto já foi desfeito.")
        if not e.get("desfazer"):
            raise ErroNegocio("Esta entrada não tem como ser desfeita.")
        prop = await tx.obter("propostas", u.tenant_id, e["proposta_id"]) if e.get("proposta_id") else None
        receita = e["desfazer"]
        op = await _DESFAZER[receita["op"]](tx, u, receita)
        if receita.get("refazer") and receita.get("texto"):
            op = replace(op, texto=lambda d: f"Refeito: {receita['texto']}")
        # a receita de refazer guarda o texto da ação original: "Refeito: Atualizou a obra X: cliente" diz o que
        # voltou a valer, e não repete o texto do desfeito
        desfazer_op = op.desfazer
        op = replace(op, desfazer=lambda d: _com_texto(desfazer_op(d), e["texto"]))
        _, nova = executar_e_registrar(tx, u, op, origem="tela")
        tx.atualizar("registro", u.tenant_id, entrada_id, {"desfeito": True})
        if prop and prop["status"] == StatusProposta.APROVADA.value:
            tx.atualizar("propostas", u.tenant_id, prop["id"], {"status": StatusProposta.DESFEITA.value})
        evento(tx, u.tenant_id, "registro.desfeito", e.get("obra_id"), {"registro_id": entrada_id})
        return nova

    return await arm.em_transacao(corpo)
