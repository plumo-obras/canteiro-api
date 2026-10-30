"""Sessões de conversa portáveis: um documento por sessão e um por mensagem (nada de lista crescendo no documento).

A mensagem tem ID = `{sessao_id}-{seq:06d}`: ordena pelo ID, e reenviar o mesmo lote cai nos mesmos documentos
(idempotente sem índice único). A sessão guarda `total_mensagens`, que é a próxima sequência esperada.
"""
import json

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import agora
from canteiro_dominio.tipos import novo_id

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..esquemas import MensagensCorpo, SessaoAlteracao, SessaoCorpo
from ..nucleo import Conflito, ErroNegocio, NaoEncontrado, obra_do_tenant

# O Firestore limita o documento a 1 MiB; sobra margem para os outros campos.
MAX_BYTES_MENSAGEM = 800_000


def id_mensagem(sessao_id: str, seq: int) -> str:
    return f"{sessao_id}-{seq:06d}"


async def _sessao_do_usuario(leitor, u: UsuarioAtual, sessao_id: str) -> dict:
    s = await leitor.obter("sessoes", u.tenant_id, sessao_id)
    # sessão de outra pessoa do mesmo cliente é tratada como inexistente: conversa é privada
    if not s or s["dono"] != u.id:
        raise NaoEncontrado("Conversa não encontrada.")
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
                raise Conflito("Já existe uma conversa com este ID.")
            return existente, False
        tx.criar("sessoes", d)
        return d, True

    return await arm.em_transacao(tx_corpo)


async def listar(arm, u: UsuarioAtual, obra_id: str | None, limite: int) -> list[dict]:
    filtros = [Filtro("dono", "==", u.id)]
    if obra_id:
        filtros.append(Filtro("obra_id", "==", obra_id))
    return await arm.consultar(Consulta("sessoes", u.tenant_id, filtros, [("atualizado_em", "desc")], limite=limite))


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
    for i, m in enumerate(corpo.mensagens):
        if len(json.dumps(m.conteudo, ensure_ascii=False).encode()) > MAX_BYTES_MENSAGEM:
            raise ErroNegocio(f"Mensagem {corpo.seq_inicial + i} grande demais para guardar.")
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
