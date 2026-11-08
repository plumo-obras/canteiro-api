"""Gastos da obra: árvore (etapas e tópicos), comprovantes a revisar e gastos confirmados."""
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status

from canteiro_dominio.modelos import StatusComprovante

from .. import vau as tabela_vau
from ..auth import UsuarioAtual, exigir, usuario_atual, usuario_pelo_agente
from ..esquemas import (
    ComprovanteDetalhe,
    ComprovanteSaida,
    Confirmacao,
    ConfirmarCorpo,
    DescartarCorpo,
    EdicaoArvoreCorpo,
    EtapaExtraCorpo,
    EtapaExtraSaida,
    EtapaNaArvoreSaida,
    FiscalGastoCorpo,
    GastoReclassificado,
    GastoSaida,
    LeituraCorpo,
    LinhaVAU,
    ReclassificarCorpo,
    ResumoGastos,
    TopicoCorpo,
    TopicoSaida,
    TributosObra,
)
from ..modelos import Papel
from ..servicos import gastos, tributos

r = APIRouter(tags=["gastos"])
admin = exigir(Papel.ADMIN)


def _arm(request: Request):
    return request.app.state.arm


def _arquivos(request: Request):
    return request.app.state.arquivos


# --- árvore ---

@r.get("/obras/{obra_id}/arvore", response_model=list[EtapaNaArvoreSaida])
async def arvore(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return [asdict(e) for e in await gastos.arvore_da_obra(arm, u.tenant_id, obra_id)]


@r.post("/obras/{obra_id}/etapas-extras", response_model=EtapaExtraSaida, status_code=status.HTTP_201_CREATED)
async def criar_etapa_extra(obra_id: str, corpo: EtapaExtraCorpo, u: UsuarioAtual = Depends(admin),
                            arm=Depends(_arm)):
    return await gastos.criar_etapa_extra(arm, u, obra_id, corpo.nome)


@r.patch("/etapas-extras/{etapa_id}", response_model=EtapaExtraSaida)
async def editar_etapa_extra(etapa_id: str, corpo: EdicaoArvoreCorpo, u: UsuarioAtual = Depends(admin),
                             arm=Depends(_arm)):
    return await gastos.editar_item_arvore(arm, u, "etapas_extras", etapa_id, corpo)


@r.post("/obras/{obra_id}/topicos", response_model=TopicoSaida, status_code=status.HTTP_201_CREATED)
async def criar_topico(obra_id: str, corpo: TopicoCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    return await gastos.criar_topico(arm, u, obra_id, corpo.etapa, corpo.nome)


@r.patch("/topicos/{topico_id}", response_model=TopicoSaida)
async def editar_topico(topico_id: str, corpo: EdicaoArvoreCorpo, u: UsuarioAtual = Depends(admin),
                        arm=Depends(_arm)):
    return await gastos.editar_item_arvore(arm, u, "topicos", topico_id, corpo)


# --- comprovantes ---

@r.post("/obras/{obra_id}/comprovantes", response_model=ComprovanteSaida, status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "o mesmo arquivo já tinha sido enviado (ja_existia)"}})
async def enviar(obra_id: str, response: Response, arquivo: UploadFile = File(...),
                 legenda: str = Form("", max_length=gastos.MAX_LEGENDA), u: UsuarioAtual = Depends(usuario_atual),
                 arm=Depends(_arm), arquivos=Depends(_arquivos)):
    # qualquer papel envia (como qualquer um propõe): enviar não conta em nada; só admin confirma
    conteudo = await arquivo.read(gastos.MAX_ARQUIVO + 1)
    c, ja_existia = await gastos.enviar_comprovante(arm, arquivos, u, obra_id, conteudo, arquivo.content_type or "",
                                                    legenda)
    if ja_existia:
        response.status_code = status.HTTP_200_OK
    return {**c, "ja_existia": ja_existia}


@r.get("/obras/{obra_id}/comprovantes", response_model=list[ComprovanteSaida])
async def listar_comprovantes(obra_id: str, status_: StatusComprovante | None = Query(None, alias="status"),
                              limite: int = Query(100, ge=1, le=500), antes_de: datetime | None = None,
                              u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await gastos.listar_comprovantes(arm, u, obra_id, status_, limite, antes_de)


@r.get("/comprovantes/{comprovante_id}", response_model=ComprovanteDetalhe)
async def detalhe(comprovante_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await gastos.detalhe(arm, u, comprovante_id)


@r.get("/comprovantes/{comprovante_id}/arquivo", response_class=Response,
       responses={200: {"content": {"image/jpeg": {}, "application/pdf": {}}}})
async def arquivo(comprovante_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm),
                  arquivos=Depends(_arquivos)):
    conteudo, tipo = await gastos.arquivo_do_comprovante(arm, arquivos, u, comprovante_id)
    # dado pessoal: nada de cache compartilhado; nosniff para o navegador não tratar o arquivo como outra coisa
    return Response(conteudo, media_type=tipo, headers={"Cache-Control": "private, no-store",
                                                        "X-Content-Type-Options": "nosniff",
                                                        "Content-Disposition": "inline"})


@r.put("/interno/comprovantes/{comprovante_id}/leitura", response_model=ComprovanteSaida)
async def gravar_leitura(comprovante_id: str, corpo: LeituraCorpo,
                         u: UsuarioAtual = Depends(usuario_pelo_agente(60)), arm=Depends(_arm)):
    # só o serviço do agente grava leitura, agindo por quem enviou ou pediu "ler de novo"
    return await gastos.gravar_leitura(arm, u, comprovante_id, corpo)


@r.post("/comprovantes/{comprovante_id}/confirmar", response_model=Confirmacao, status_code=status.HTTP_201_CREATED)
async def confirmar(comprovante_id: str, corpo: ConfirmarCorpo, u: UsuarioAtual = Depends(admin),
                    arm=Depends(_arm)):
    return await gastos.confirmar(arm, u, comprovante_id, corpo)


@r.post("/comprovantes/{comprovante_id}/descartar", response_model=ComprovanteSaida)
async def descartar(comprovante_id: str, corpo: DescartarCorpo | None = None, u: UsuarioAtual = Depends(admin),
                    arm=Depends(_arm)):
    return await gastos.descartar(arm, u, comprovante_id, corpo.motivo if corpo else "")


# --- gastos ---

@r.get("/obras/{obra_id}/gastos", response_model=list[GastoSaida])
async def listar_gastos(obra_id: str, etapa: str | None = Query(None, max_length=80),
                        limite: int = Query(100, ge=1, le=500), antes_de: datetime | None = None,
                        u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await gastos.listar_gastos(arm, u, obra_id, etapa, limite, antes_de)


@r.patch("/gastos/{gasto_id}", response_model=GastoReclassificado)
async def reclassificar(gasto_id: str, corpo: ReclassificarCorpo, u: UsuarioAtual = Depends(admin),
                        arm=Depends(_arm)):
    return await gastos.reclassificar(arm, u, gasto_id, corpo)


@r.get("/obras/{obra_id}/gastos/resumo", response_model=ResumoGastos)
async def resumo(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await gastos.resumo(arm, u, obra_id)



@r.patch("/gastos/{gasto_id}/fiscal", response_model=GastoReclassificado, tags=["tributos"])
async def classificar_fiscal(gasto_id: str, corpo: FiscalGastoCorpo, u: UsuarioAtual = Depends(admin),
                             arm=Depends(_arm)):
    return await gastos.classificar_fiscal(arm, u, gasto_id, corpo)


@r.get("/obras/{obra_id}/tributos", response_model=TributosObra, tags=["tributos"])
async def tributos_da_obra(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await tributos.tributos_da_obra(arm, u, obra_id)


@r.get("/vau", response_model=list[LinhaVAU], tags=["tributos"])
async def tabela_vau_da_uf(uf: str = Query(min_length=2, max_length=2), destinacao: str = Query(max_length=40),
                           u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    # tabela pública da Receita, a mesma para todo cliente: qualquer papel lê, mais novo primeiro
    linhas = await tabela_vau.linhas_de(arm, uf.upper(), destinacao)
    return sorted(linhas, key=lambda x: x["mes"], reverse=True)
