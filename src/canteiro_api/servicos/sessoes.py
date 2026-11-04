"""Sessões de conversa portáveis: um documento por sessão e um por mensagem (nada de lista crescendo no documento).

A mensagem tem ID = `{sessao_id}-{seq:06d}`: ordena pelo ID, e reenviar o mesmo lote cai nos mesmos documentos
(idempotente sem índice único). A sessão guarda `total_mensagens`, que é a próxima sequência esperada.
"""
import json
from datetime import datetime

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import agora
from canteiro_dominio.tipos import novo_id

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..esquemas import MensagensCorpo, SessaoAlteracao, SessaoCorpo
from ..nucleo import Conflito, ErroNegocio, GrandeDemais, NaoEncontrado, listar_pagina, obra_do_tenant

# O Firestore limita o documento a 1 MiB; sobra margem para os outros campos.
MAX_BYTES_MENSAGEM = 800_000
# O lote inteiro é gravado numa transação, e o Firestore recusa commit acima de 10 MiB (50 mensagens de 800 KB dariam
# 40 MB). 9 MiB de conteúdo deixa folga para os outros campos, os IDs e a atualização da sessão no mesmo commit.
MAX_BYTES_LOTE = 9 * 1024 * 1024
SESSAO_NAO_ENCONTRADA = "Conversa não encontrada."


def id_mensagem(sessao_id: str, seq: int) -> str:
    return f"{sessao_id}-{seq:06d}"


async def _sessao_do_usuario(leitor, u: UsuarioAtual, sessao_id: str) -> dict:
    s = await leitor.obter("sessoes", u.tenant_id, sessao_id)
    # sessão de outra pessoa do mesmo cliente é tratada como inexistente: conversa é privada
    if not s or s["dono"] != u.id:
        raise NaoEncontrado(SESSAO_NAO_ENCONTRADA)
    return s


async def criar(arm, u: UsuarioAtual, corpo: SessaoCorpo) -> tuple[dict, bool]:
    if corpo.obra_id:
        await obra_do_tenant(arm, u.tenant_id, corpo.obra_id)
    momento = agora()
    d = normalizar({"id": corpo.id or novo_id(), "tenant_id": u.tenant_id, "dono": u.id, "obra_id": corpo.obra_id,
                    "titulo": corpo.titulo, "abas": corpo.abas, "total_mensagens": 0, "criado_em": momento,
                    "atualizado_em": momento})

    async def tx_corpo(tx):
        existente = await tx.obter("sessoes", u.tenant_id, d["id"])
        if existente:
            if existente["dono"] != u.id:
                # Mesma resposta da sessão que não existe: um 409 aqui diria a quem chuta IDs que a conversa existe.
                raise NaoEncontrado(SESSAO_NAO_ENCONTRADA)
            return existente, False
        tx.criar("sessoes", d)
        return d, True

    try:
        return await arm.em_transacao(tx_corpo)
    except ConflitoDeId:
        # O ID é único na coleção inteira (Mongo e Firestore), não por cliente: a leitura acima, filtrada pelo tenant,
        # não vê a sessão de outro cliente, e a gravação colide. Responder 409 revelaria que ela existe em algum lugar.
        raise NaoEncontrado(SESSAO_NAO_ENCONTRADA) from None


async def listar(arm, u: UsuarioAtual, obra_id: str | None, limite: int,
                 antes_de: datetime | None = None) -> list[dict]:
    # a lista anda pela última atividade, então a página seguinte também: antes_de compara com atualizado_em
    filtros = [Filtro("dono", "==", u.id)]
    if obra_id:
        filtros.append(Filtro("obra_id", "==", obra_id))
    consulta = Consulta("sessoes", u.tenant_id, filtros, [("atualizado_em", "desc")], limite=limite)
    return await listar_pagina(arm, consulta, "atualizado_em", antes_de)


async def obter(arm, u: UsuarioAtual, sessao_id: str) -> dict:
    return await _sessao_do_usuario(arm, u, sessao_id)


async def alterar(arm, u: UsuarioAtual, sessao_id: str, corpo: SessaoAlteracao) -> dict:
    campos = corpo.model_dump(exclude_unset=True)
    if not campos:
        raise ErroNegocio("Nada para alterar.")
    if campos.get("obra_id"):
        await obra_do_tenant(arm, u.tenant_id, campos["obra_id"])
    campos["atualizado_em"] = agora()

    async def tx_corpo(tx):
        s = await _sessao_do_usuario(tx, u, sessao_id)
        tx.atualizar("sessoes", u.tenant_id, sessao_id, campos)
        return normalizar({**s, **campos})

    return await arm.em_transacao(tx_corpo)


async def mensagens(arm, u: UsuarioAtual, sessao_id: str, depois_de: int, limite: int) -> list[dict]:
    await _sessao_do_usuario(arm, u, sessao_id)
    return await arm.consultar(Consulta("mensagens", u.tenant_id, [Filtro("sessao_id", "==", sessao_id),
                                                                   Filtro("seq", ">", depois_de)],
                                        [("seq", "asc")], limite=limite))


async def acrescentar(arm, u: UsuarioAtual, sessao_id: str, corpo: MensagensCorpo) -> tuple[dict, bool]:
    """Só acrescenta. `seq_inicial` tem de ser o `total_mensagens` atual; reenvio do mesmo lote devolve 200."""
    momento = agora()
    novas = []
    total_bytes = 0
    for i, m in enumerate(corpo.mensagens):
        tamanho = len(json.dumps(m.conteudo, ensure_ascii=False).encode())
        if tamanho > MAX_BYTES_MENSAGEM:
            raise ErroNegocio(f"Mensagem {corpo.seq_inicial + i} grande demais para guardar.")
        total_bytes += tamanho
        if total_bytes > MAX_BYTES_LOTE:
            raise GrandeDemais(f"Lote passa de {MAX_BYTES_LOTE // 2**20} MiB e não cabe numa gravação; divida em lotes "
                               f"menores (a mensagem {corpo.seq_inicial + i} já fica de fora deste).")
        seq = corpo.seq_inicial + i
        novas.append(normalizar({"id": id_mensagem(sessao_id, seq), "tenant_id": u.tenant_id, "sessao_id": sessao_id,
                                 "seq": seq, "papel": m.papel, "conteudo": m.conteudo, "criado_em": momento}))

    async def tx_corpo(tx):
        s = await _sessao_do_usuario(tx, u, sessao_id)
        total = s["total_mensagens"]
        if corpo.seq_inicial < total:
            # reenvio (a resposta anterior se perdeu?): aceita só se for exatamente o que já está gravado
            gravadas = [await tx.obter("mensagens", u.tenant_id, m["id"]) for m in novas]
            if all(g and (g["papel"], g["conteudo"]) == (m["papel"], m["conteudo"])
                   for g, m in zip(gravadas, novas, strict=True)):
                return {**s, "mensagens": gravadas}, False
            raise Conflito(f"A conversa já tem {total} mensagens; envie a partir de seq_inicial={total}.")
        if corpo.seq_inicial > total:
            raise Conflito(f"Faltam mensagens antes desta; envie a partir de seq_inicial={total}.")
        for m in novas:
            tx.criar("mensagens", m)
        campos = {"total_mensagens": total + len(novas), "atualizado_em": momento}
        tx.atualizar("sessoes", u.tenant_id, sessao_id, campos)
        return normalizar({**s, **campos, "mensagens": novas}), True

    return await arm.em_transacao(tx_corpo)
