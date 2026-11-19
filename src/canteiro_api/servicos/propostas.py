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
from functools import partial

from pydantic import ValidationError

from canteiro_dominio.armazenamento import ConflitoDeId
from canteiro_dominio.modelos import Proposta, StatusProposta, TipoProposta, agora

from ..auth import UsuarioAtual
from ..esquemas import DADOS_POR_TIPO, PropostaCorpo
from ..modelos import doc
from ..nucleo import (
    COMPRAS_SAIRAM,
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    Retirado,
    evento,
    executar_e_registrar,
    id_derivado,
    obra_do_tenant,
    registrar,
)
from . import cronograma, fornecedores, gastos, obras, versoes


def _dados(tipo: TipoProposta, dados: dict):
    try:
        return DADOS_POR_TIPO[tipo].model_validate(dados)
    except ValidationError as e:
        campos = ", ".join(".".join(map(str, err["loc"])) or "dados" for err in e.errors())
        raise ErroNegocio(f"Dados da proposta '{tipo.value}' inválidos ({campos}).") from None


# O que aprovar faz, para o botão da tela dizer a consequência ("Aprovar cronograma").
EFEITO_POR_TIPO = {TipoProposta.CRONOGRAMA: "cronograma"}


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
    if corpo.tipo not in EFEITO_POR_TIPO:
        raise Retirado(COMPRAS_SAIRAM)
    dados = _dados(corpo.tipo, corpo.dados)
    await obra_do_tenant(arm, u.tenant_id, corpo.obra_id)
    conta, efeito = corpo.conta, EFEITO_POR_TIPO[corpo.tipo]
    # valida contra o orçamento aqui (com arm, fora de transação): a execução depois só grava
    await cronograma.validar_para_proposta(arm, u.tenant_id, corpo.obra_id, dados)
    chave = corpo.chave_idempotencia
    p = Proposta(tenant_id=u.tenant_id, obra_id=corpo.obra_id, tipo=corpo.tipo, titulo=corpo.titulo,
                 conta=conta, justificativa=corpo.justificativa, fonte=corpo.fonte, efeito=efeito,
                 dados=dados.model_dump(mode="json"),
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
    # aprovar grava o cronograma proposto; já validado na criação, aqui só se grava (dentro da transação)
    return await cronograma.preparar_salvar(tx, u, prop["obra_id"], _dados(TipoProposta(prop["tipo"]), prop["dados"]))


async def _proposta_pendente(tx, u: UsuarioAtual, proposta_id: str) -> dict:
    prop = await tx.obter("propostas", u.tenant_id, proposta_id)
    if not prop:
        raise NaoEncontrado("Proposta não encontrada.")
    if prop["status"] != StatusProposta.PENDENTE.value:
        raise Conflito(f"Esta proposta já foi {prop['status']}.")
    return prop


async def aprovar(arm, u: UsuarioAtual, proposta_id: str, ajustes: dict | None = None) -> tuple[dict, dict]:
    async def corpo(tx):
        prop = await _proposta_pendente(tx, u, proposta_id)
        # a de compra que ficou pendente só pode ser recusada: aprovar criaria pedido ou movimento de estoque
        if prop["tipo"] not in EFEITO_POR_TIPO:
            raise Retirado(COMPRAS_SAIRAM)
        if ajustes:
            raise ErroNegocio("A proposta de cronograma não se ajusta campo a campo: aprove ou recuse.")
        trava = await _ler_trava(tx, u, prop)
        campos: dict = {}
        op = await _preparar_proposta(tx, u, prop)
        _, entrada = executar_e_registrar(tx, u, op, origem="agente", proposta_id=proposta_id)
        campos |= {"status": StatusProposta.APROVADA.value, "decidido_por": u.id,
                   "decidido_por_nome": u.nome, "decidido_em": agora(), "registro_id": entrada["id"]}
        # a proposta grava com os dados que foram executados, na mesma transação da execução
        tx.atualizar("propostas", u.tenant_id, proposta_id, campos)
        _soltar_trava(tx, u, trava)
        evento(tx, u.tenant_id, "proposta.aprovada", prop["obra_id"], {"proposta_id": proposta_id})
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
    "editar_fornecedor": fornecedores.preparar_desfazer_edicao,
    "restaurar_cronograma": cronograma.preparar_restaurar,
    "desfazer_medicao": cronograma.preparar_desfazer_medicao,
    "refazer_medicao": cronograma.preparar_refazer_medicao,
    "restaurar_obra": obras.preparar_restaurar,
    "reabrir_orcamento": obras.preparar_reabrir,
    "desligar_versao": versoes.preparar_desligar,
    "editar_item_orcamento": versoes.preparar_desfazer_item,
    "restaurar_etapas": versoes.preparar_restaurar_etapas,
    "editar_arvore": gastos.preparar_desfazer_arvore,
    "estornar_gasto": gastos.preparar_estorno_gasto,
    "reabrir_comprovante": gastos.preparar_reabrir,
    "reclassificar_gasto": gastos.preparar_reclassificar,
    "fiscal_gasto": gastos.preparar_desfazer_fiscal,
}


def _com_texto(receita: dict | None, texto: str) -> dict | None:
    if receita and receita.get("refazer"):
        return {**receita, "texto": receita.get("texto") or texto.removeprefix("Refeito: ")}
    return receita


async def desfazer(arm, u: UsuarioAtual, entrada_id: str) -> dict:
    previa = await arm.obter("registro", u.tenant_id, entrada_id)
    receita_previa = (previa or {}).get("desfazer") or {}
    if receita_previa.get("op") == "desligar_versao" and receita_previa.get("renumeradas") and not previa["desfeito"]:
        await versoes.recusar_se_renumeradas_em_uso(arm, u.tenant_id, previa["obra_id"], receita_previa["renumeradas"])
    # a árvore de gasto depende do orçamento da obra, e consultar o orçamento não pode ser dentro da transação
    base_arvore = (await gastos.base_para_desfazer_arvore(arm, u.tenant_id, receita_previa)
                   if receita_previa.get("op") == "editar_arvore" else None)

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
        if receita["op"] not in _DESFAZER:
            # receita de pedido ou estoque gravada antes de Compras sair (a limpeza do banco tira o Desfazer delas)
            raise Retirado(COMPRAS_SAIRAM)
        preparar = _DESFAZER[receita["op"]]
        if receita["op"] == "editar_arvore":
            preparar = partial(preparar, base=base_arvore)
        op = await preparar(tx, u, receita)
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
