"""Propostas, aprovação, registro, consumo de IA e créditos."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import StatusProposta, UsoIA

from ..auth import UsuarioAtual, exigir, usuario_atual, usuario_pelo_agente
from ..esquemas import Aprovacao, Creditos, PropostaCorpo, PropostaSaida, RecusarCorpo, RegistroSaida, RelatorioUso
from ..modelos import Papel
from ..nucleo import ErroNegocio
from ..servicos import propostas, uso

r = APIRouter()
admin = exigir(Papel.ADMIN)


def _arm(request: Request):
    return request.app.state.arm


@r.post("/propostas", response_model=PropostaSaida, status_code=status.HTTP_201_CREATED, tags=["propostas"])
async def criar(corpo: PropostaCorpo, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    # Qualquer papel pode propor (o agente age com o token de quem pergunta); só admin+ aprova.
    return await propostas.criar_proposta(arm, u, corpo)


@r.get("/propostas", response_model=list[PropostaSaida], tags=["propostas"])
async def listar(status_: StatusProposta | None = Query(None, alias="status"), obra_id: str | None = None,
                 limite: int = Query(100, ge=1, le=500), u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    filtros = []
    if status_:
        filtros.append(Filtro("status", "==", status_.value))
    if obra_id:
        filtros.append(Filtro("obra_id", "==", obra_id))
    return await arm.consultar(Consulta("propostas", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite))


@r.post("/propostas/{proposta_id}/aprovar", response_model=Aprovacao, tags=["propostas"])
async def aprovar(proposta_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    prop, registro = await propostas.aprovar(arm, u, proposta_id)
    return {"proposta": prop, "registro": registro}


@r.post("/propostas/{proposta_id}/recusar", response_model=PropostaSaida, tags=["propostas"])
async def recusar(proposta_id: str, corpo: RecusarCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    return await propostas.recusar(arm, u, proposta_id, corpo.motivo)


@r.get("/registro", response_model=list[RegistroSaida], tags=["registro"])
async def registro(obra_id: str | None = None, limite: int = Query(50, ge=1, le=200),
                   u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    filtros = [Filtro("obra_id", "==", obra_id)] if obra_id else []
    return await arm.consultar(Consulta("registro", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite))


@r.post("/registro/{entrada_id}/desfazer", response_model=RegistroSaida, tags=["registro"])
async def desfazer(entrada_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    return await propostas.desfazer(arm, u, entrada_id)


# --- consumo de IA ---

@r.post("/uso", response_model=UsoIA, status_code=status.HTTP_201_CREATED, tags=["consumo de IA"])
async def gravar_uso(corpo: UsoIA, response: Response, u: UsuarioAtual = Depends(usuario_pelo_agente(60)),
                     arm=Depends(_arm)):
    # o corpo diz de quem é o consumo; o token do usuário (conferido) tem de dizer o mesmo
    if (corpo.tenant_id, corpo.usuario_id) != (u.tenant_id, u.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "tenant_id/usuario_id do corpo não batem com o usuário.")
    d, criado = await uso.gravar_uso(arm, corpo)
    if not criado:
        response.status_code = status.HTTP_200_OK
    return d


@r.get("/uso", response_model=RelatorioUso, tags=["consumo de IA"])
async def relatorio_uso(de: date, ate: date, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    if ate < de:
        raise ErroNegocio("'ate' antes de 'de'.")
    if (ate - de).days > 366:
        raise ErroNegocio("Período maior que um ano.")
    return await uso.relatorio(arm, u.tenant_id, de, ate)


@r.get("/creditos", response_model=Creditos, tags=["consumo de IA"],
       responses={402: {"description": "sem créditos e sem chave própria (o corpo traz o saldo e a mensagem)"}})
async def creditos(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    c = await uso.creditos(arm, u.tenant_id, u.id)
    if c.saldo_microdolares <= 0 and not c.chave_propria:
        return JSONResponse(status_code=status.HTTP_402_PAYMENT_REQUIRED, content={
            "detail": "Os créditos de IA acabaram. Peça ao responsável da conta para comprar mais créditos "
                      "ou conectar a chave própria.", **c.model_dump()})
    return c
