"""Sessões de conversa (histórico do agente) e chave própria da Anthropic."""
from fastapi import APIRouter, Depends, Query, Request, Response, status

from canteiro_dominio.modelos import agora

from ..auth import UsuarioAtual, exigir, usuario_atual, usuario_pelo_agente
from ..esquemas import (
    ChaveCorpo,
    ChaveEstado,
    ChaveInterna,
    ChaveSituacao,
    Mensagem,
    MensagensCorpo,
    MinhaConta,
    SessaoAlteracao,
    SessaoComMensagens,
    SessaoConversa,
    SessaoCorpo,
)
from ..modelos import Papel
from ..nucleo import NaoEncontrado, evento, registrar
from ..servicos import sessoes, uso

r = APIRouter()


def _arm(request: Request):
    return request.app.state.arm


@r.post("/sessoes", response_model=SessaoConversa, status_code=status.HTTP_201_CREATED, tags=["sessões"])
async def criar_sessao(corpo: SessaoCorpo, response: Response, u: UsuarioAtual = Depends(usuario_atual),
                       arm=Depends(_arm)):
    s, criada = await sessoes.criar(arm, u, corpo)
    if not criada:
        response.status_code = status.HTTP_200_OK
    return s


@r.get("/sessoes", response_model=list[SessaoConversa], tags=["sessões"])
async def listar_sessoes(obra_id: str | None = None, limite: int = Query(30, ge=1, le=100),
                         u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await sessoes.listar(arm, u, obra_id, limite)


@r.get("/sessoes/{sessao_id}", response_model=SessaoConversa, tags=["sessões"])
async def obter_sessao(sessao_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await sessoes.obter(arm, u, sessao_id)


@r.patch("/sessoes/{sessao_id}", response_model=SessaoConversa, tags=["sessões"])
async def alterar_sessao(sessao_id: str, corpo: SessaoAlteracao, u: UsuarioAtual = Depends(usuario_atual),
                         arm=Depends(_arm)):
    return await sessoes.alterar(arm, u, sessao_id, corpo)


@r.get("/sessoes/{sessao_id}/mensagens", response_model=list[Mensagem], tags=["sessões"])
async def listar_mensagens(sessao_id: str, depois_de: int = Query(-1, ge=-1), limite: int = Query(200, ge=1, le=500),
                           u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await sessoes.mensagens(arm, u, sessao_id, depois_de, limite)


@r.post("/sessoes/{sessao_id}/mensagens", response_model=SessaoComMensagens, status_code=status.HTTP_201_CREATED,
        tags=["sessões"], responses={200: {"description": "reenvio do mesmo lote: nada novo gravado"},
                                     409: {"description": "seq_inicial fora de ordem (a mensagem diz o esperado)"}})
async def acrescentar_mensagens(sessao_id: str, corpo: MensagensCorpo, response: Response,
                                u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    s, criadas = await sessoes.acrescentar(arm, u, sessao_id, corpo)
    if not criadas:
        response.status_code = status.HTTP_200_OK
    return s


# --- chave própria da Anthropic (o segredo só volta pela rota interna, para o serviço do agente) ---

def _id_segredo(tenant_id: str) -> str:
    return f"anthropic-{tenant_id}"


async def _chave_estado(arm, tenant_id: str) -> ChaveEstado:
    t = await arm.obter("tenants", tenant_id, tenant_id)
    s = await arm.obter("segredos", tenant_id, _id_segredo(tenant_id))
    if not t or not t["chave_propria"] or not s or not s.get("cifrado"):
        return ChaveEstado(ativa=False)
    return ChaveEstado(ativa=True, final=s.get("final"), cadastrada_em=s.get("atualizado_em"))


@r.get("/conta", response_model=MinhaConta, tags=["conta"])
async def minha_conta(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    usuario = await arm.obter("usuarios", u.tenant_id, u.id)
    t = await arm.obter("tenants", u.tenant_id, u.tenant_id)
    if not usuario or not t:
        raise NaoEncontrado("Cliente não encontrado.")
    chave = await _chave_estado(arm, u.tenant_id)
    # sem o 402 do /creditos: a tela da conta é justamente onde a pessoa vê que o saldo acabou
    return MinhaConta(usuario={"id": u.id, "nome": usuario["nome"], "email": usuario["email"], "papel": u.papel.value},
                      tenant={"id": t["id"], "nome": t["nome"]}, chave_propria=chave if chave.ativa else None,
                      creditos=await uso.creditos(arm, u.tenant_id, u.id))


@r.get("/conta/chave-anthropic", response_model=ChaveEstado, tags=["conta"])
async def situacao_chave(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await _chave_estado(arm, u.tenant_id)


@r.put("/conta/chave-anthropic", response_model=ChaveSituacao, tags=["conta"])
async def conectar_chave(corpo: ChaveCorpo, request: Request, u: UsuarioAtual = Depends(exigir(Papel.MASTER)),
                         arm=Depends(_arm)):
    cifrado = request.app.state.cofre.cifrar(corpo.chave, tenant_id=u.tenant_id)
    final = corpo.chave[-4:]

    async def tx_corpo(tx):
        if not await tx.obter("tenants", u.tenant_id, u.tenant_id):
            raise NaoEncontrado("Cliente não encontrado.")
        tx.salvar("segredos", {"id": _id_segredo(u.tenant_id), "tenant_id": u.tenant_id, "tipo": "anthropic",
                               "cifrado": cifrado, "final": final, "atualizado_em": agora(), "por": u.id})
        tx.atualizar("tenants", u.tenant_id, u.tenant_id, {"chave_propria": True})
        registrar(tx, u, obra_id=None, texto=f"Conectou a chave própria da Anthropic (…{final})")
        evento(tx, u.tenant_id, "conta.chave_propria", None, {"ativa": True})

    await arm.em_transacao(tx_corpo)
    return ChaveSituacao(chave_propria=True, final=final)


@r.delete("/conta/chave-anthropic", response_model=ChaveSituacao, tags=["conta"])
async def desconectar_chave(u: UsuarioAtual = Depends(exigir(Papel.MASTER)), arm=Depends(_arm)):
    async def tx_corpo(tx):
        if not await tx.obter("tenants", u.tenant_id, u.tenant_id):
            raise NaoEncontrado("Cliente não encontrado.")
        # a porta não apaga documento: o segredo é sobrescrito sem o valor
        tx.salvar("segredos", {"id": _id_segredo(u.tenant_id), "tenant_id": u.tenant_id, "tipo": "anthropic",
                               "cifrado": None, "final": None, "atualizado_em": agora(), "por": u.id})
        tx.atualizar("tenants", u.tenant_id, u.tenant_id, {"chave_propria": False})
        registrar(tx, u, obra_id=None, texto="Desconectou a chave própria da Anthropic")
        evento(tx, u.tenant_id, "conta.chave_propria", None, {"ativa": False})

    await arm.em_transacao(tx_corpo)
    return ChaveSituacao(chave_propria=False)


@r.get("/interno/chave-anthropic", response_model=ChaveInterna, tags=["interno"])
async def chave_para_o_agente(request: Request, response: Response, u: UsuarioAtual = Depends(usuario_pelo_agente()),
                              arm=Depends(_arm)):
    t = await arm.obter("tenants", u.tenant_id, u.tenant_id)
    s = await arm.obter("segredos", u.tenant_id, _id_segredo(u.tenant_id))
    if not t or not t["chave_propria"] or not s or not s.get("cifrado"):
        raise NaoEncontrado("Este cliente não tem chave própria conectada.")
    response.headers["Cache-Control"] = "no-store"
    return ChaveInterna(chave=request.app.state.cofre.decifrar(s["cifrado"], tenant_id=u.tenant_id))
