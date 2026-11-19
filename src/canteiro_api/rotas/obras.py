"""Obras: cadastro, orçamento, resumo, Hoje, cronograma e medição."""
import json
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import ValidationError

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta
from canteiro_dominio.modelos import Obra, TipoVersao

from ..auth import UsuarioAtual, exigir, usuario_atual
from ..esquemas import (
    AprovacaoSaida,
    CronogramaCorpo,
    CronogramaObra,
    DesembolsoObra,
    EstruturaCorpo,
    EtapaRef,
    HojeNaObra,
    ItemEditado,
    ItemOrcamentoEdicao,
    LeituraPlanilha,
    Mapeamento,
    MedicaoCorpo,
    MedicaoSaida,
    ObraCorpo,
    ObraEdicao,
    ObraSaida,
    OrcamentoObra,
    OrdemEtapasCorpo,
    OrdemEtapasSaida,
    PreviaVersao,
    ResumoDaObra,
    ResumoObra,
)
from ..modelos import Papel
from ..nucleo import FUSO, Conflito, ErroNegocio, executar_e_registrar, obra_do_tenant
from ..servicos import (
    cronograma,
    desembolso,
    obras,
    orcamento,
    resumo,
    versoes,
)
from ..servicos import hoje as hoje_servico

r = APIRouter(tags=["obras"])
admin = exigir(Papel.ADMIN)


def _arm(request: Request):
    return request.app.state.arm


@r.get("/obras", response_model=list[Obra])
async def listar_obras(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await arm.consultar(Consulta("obras", u.tenant_id, ordem=[("nome", "asc")]))


@r.post("/obras", response_model=ObraSaida, status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "repetição da mesma chave: devolve a obra já criada"}})
async def criar_obra(corpo: ObraCorpo, response: Response, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    try:
        o, criada = await obras.criar_obra(arm, u, corpo)
    except ConflitoDeId:
        # duas requisições com a mesma chave ao mesmo tempo: a outra gravou primeiro
        o, criada = await obras.criar_obra(arm, u, corpo)
    if not criada:
        response.status_code = status.HTTP_200_OK
    return o


@r.patch("/obras/{obra_id}", response_model=ObraSaida)
async def editar_obra(obra_id: str, corpo: ObraEdicao, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    campos = corpo.model_dump(exclude_unset=True)
    if campos.get("nome", "") is None:
        raise ErroNegocio("O nome da obra não pode ficar vazio.")

    async def tx_corpo(tx):
        op = await obras.preparar_editar(tx, u, obra_id, campos)
        if op is None:
            return {**await obra_do_tenant(tx, u.tenant_id, obra_id), "registro_id": None}
        o, entrada = executar_e_registrar(tx, u, op)
        return {**o, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


@r.post("/obras/{obra_id}/orcamento/ler", response_model=LeituraPlanilha, tags=["orçamento"])
async def ler_planilha(obra_id: str, arquivo: UploadFile = File(...), planilha: str | None = Form(None),
                       u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    return await orcamento.ler(arm, u, obra_id, await _ler_arquivo(arquivo), arquivo.filename or "", planilha)


@r.post("/obras/{obra_id}/orcamento/estrutura", response_model=PreviaVersao, tags=["orçamento"],
        responses={201: {"model": PreviaVersao}})
async def estrutura_como_orcamento(obra_id: str, corpo: EstruturaCorpo, response: Response,
                                   u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    """Os materiais do projeto viram uma versão do orçamento (etapa "Estrutura"): contrato se a obra ainda não tem um,
    aditivo se já tem. Prévia com `aplicar: false`; `aplicar: true` grava (com chave de idempotência)."""
    try:
        previa, criada = await versoes.importar_estrutura(arm, u, obra_id, corpo)
    except ConflitoDeId:
        raise Conflito("Já existe uma versão com essa chave; mande a lista de novo.") from None
    if criada:
        response.status_code = status.HTTP_201_CREATED
    return previa


@r.post("/obras/{obra_id}/orcamento/etapas/ordem", response_model=OrdemEtapasSaida, tags=["orçamento"],
        responses={201: {"description": "ordem gravada"}, 200: {"description": "repetição da chave"}})
async def ordenar_etapas(obra_id: str, corpo: OrdemEtapasCorpo, response: Response,
                         u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    """Muda a ordem das etapas antes de aprovar: um contrato novo no lugar do vigente, com as etapas renumeradas."""
    try:
        saida, criada = await versoes.reordenar_etapas(arm, u, obra_id, corpo.ordem, corpo.chave_idempotencia,
                                                      corpo.versao_id)
    except ConflitoDeId:
        raise Conflito("Esta mudança já foi gravada agora há pouco; recarregue o orçamento.") from None
    if criada:
        response.status_code = status.HTTP_201_CREATED
    return saida


@r.get("/obras/{obra_id}/orcamento", response_model=OrcamentoObra, tags=["orçamento"])
async def ver_orcamento(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await versoes.orcamento_da_obra(arm, u.tenant_id, obra_id)


@r.post("/obras/{obra_id}/orcamento/versoes", response_model=PreviaVersao, tags=["orçamento"],
        responses={201: {"description": "versão gravada"}, 200: {"description": "prévia, ou repetição da chave"}})
async def importar_versao(obra_id: str, response: Response, arquivo: UploadFile = File(...),
                          mapeamento: str = Form(...), tipo: TipoVersao = Form(TipoVersao.CONTRATO),
                          descricao: str = Form("", max_length=200),
                          data: str | None = Form(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$"),
                          aplicar: bool = Form(False),
                          chave_idempotencia: str | None = Form(None, min_length=8, max_length=64),
                          etapas_itens: str | None = Form(None, max_length=20000),
                          somar: bool = Form(False),
                          ignorar_itens: str | None = Form(None, max_length=20000),
                          padronizar: bool = Form(False),
                          destinos: str | None = Form(None, max_length=20000),
                          natureza: Literal["custo", "venda"] = Form("custo"),
                          bdi: int | None = Form(None, ge=1, le=10_000),
                          u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    try:
        m = Mapeamento.model_validate(json.loads(mapeamento))
    except (ValueError, ValidationError):
        raise ErroNegocio("Mapeamento de colunas inválido.") from None
    try:
        # aditivo: {"ordem do item na prévia": "etapa do contrato"}, para o que a sugestão pelo código não acertou
        escolhidas = {int(k): str(v) for k, v in json.loads(etapas_itens).items()} if etapas_itens else {}
    except (ValueError, AttributeError):
        raise ErroNegocio("etapas_itens inválido: use {\"ordem\": \"etapa\"}.") from None
    try:
        # os itens da prévia (pela `ordem`) que não entram: em geral, os que parecem repetir o que já está no orçamento
        ignorar = [int(x) for x in json.loads(ignorar_itens)] if ignorar_itens else []
    except (ValueError, TypeError):
        raise ErroNegocio("ignorar_itens inválido: use uma lista de `ordem`, como [3, 7].") from None
    try:
        # padronizar: {"etapa da planilha": "etapa padrão"} ou {"linha:N": "etapa padrão"}, o que a pessoa trocou
        destinos_ = {str(k): str(v) for k, v in json.loads(destinos).items()} if destinos else {}
    except (ValueError, AttributeError):
        raise ErroNegocio("destinos inválido: use {\"etapa da planilha\": \"etapa padrão\"}.") from None
    conteudo = await _ler_arquivo(arquivo)
    try:
        previa, criada = await versoes.importar(arm, u, obra_id, conteudo, arquivo.filename or "", m, tipo,
                                                descricao, data or None, aplicar, chave_idempotencia,
                                                escolhidas, somar=somar, ignorar=ignorar, padronizar=padronizar,
                                                natureza=natureza, bdi=bdi, destinos=destinos_)
    except ConflitoDeId:
        raise Conflito("Esta importação já foi gravada agora há pouco; recarregue o orçamento.") from None
    if criada:
        response.status_code = status.HTTP_201_CREATED
    return previa


@r.patch("/obras/{obra_id}/orcamento/itens/{item_id}", response_model=ItemEditado, tags=["orçamento"])
async def editar_item_orcamento(obra_id: str, item_id: str, corpo: ItemOrcamentoEdicao,
                                u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    """Preço e quantidade na própria linha. Com o orçamento aprovado, só o preço de item que estava em R$ 0,00."""
    return await versoes.editar_item(arm, u, obra_id, item_id, corpo.model_dump(exclude_none=True))


@r.post("/obras/{obra_id}/orcamento/organizar-fases", tags=["orçamento"])
async def organizar_fases(obra_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    """Encaixa o orçamento contratado nas etapas padrão (se ainda não está) e prevê as fases de obra que faltam
    (serviços estimados, fora do orçado), para o cronograma ver a obra completa. Com Desfazer."""
    return await versoes.organizar_em_fases(arm, u, obra_id)


@r.post("/obras/{obra_id}/orcamento/aprovar", response_model=AprovacaoSaida, tags=["orçamento"])
async def aprovar_orcamento(obra_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    """Tira a obra de `em_orcamento`: o orçado atual passa a ser o que cronograma e gastos seguem."""
    return await obras.aprovar(arm, u, obra_id)


async def _ler_arquivo(arquivo: UploadFile) -> bytes:
    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)
    if len(conteudo) > 5 * 1024 * 1024:
        raise ErroNegocio("Arquivo maior que 5 MB.")
    return conteudo


@r.get("/obras/resumo", response_model=list[ResumoObra])
async def resumo_obras(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await resumo.resumo_obras(arm, u.tenant_id)


@r.get("/obras/{obra_id}/resumo", response_model=ResumoDaObra)
async def resumo_da_obra(obra_id: str, hoje: date | None = Query(None, description="AAAA-MM-DD; padrão: hoje em São "
                                                                                  "Paulo"),
                         u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await resumo.resumo_da_obra(arm, u.tenant_id, obra_id, hoje or datetime.now(FUSO).date())


@r.get("/obras/{obra_id}/hoje", response_model=HojeNaObra)
async def hoje_na_obra(obra_id: str,
                       hoje: date | None = Query(None, description="AAAA-MM-DD; padrão: hoje em São Paulo"),
                       u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    """O que importa hoje na obra, calculado na API: nenhuma chamada ao modelo."""
    return await hoje_servico.hoje_da_obra(arm, u.tenant_id, obra_id, hoje or datetime.now(FUSO).date())


@r.get("/obras/{obra_id}/etapas", response_model=list[EtapaRef])
async def etapas(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    return await versoes.etapas_da_obra(arm, u.tenant_id, obra_id)


# --- cronograma e medição ---

@r.get("/obras/{obra_id}/cronograma", response_model=CronogramaObra, tags=["cronograma"])
async def ver_cronograma(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await cronograma.cronograma_da_obra(arm, u.tenant_id, obra_id)


@r.get("/obras/{obra_id}/desembolso", response_model=DesembolsoObra, tags=["cronograma"])
async def ver_desembolso(obra_id: str, hoje: date | None = Query(None, description="AAAA-MM-DD; padrão: hoje em São "
                                                                                  "Paulo"),
                         u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    agora = datetime.now(FUSO).date()
    # a série vai do 1º pagamento até `hoje`: sem teto, ?hoje=9999-12-31 viraria 95 mil meses de resposta
    if hoje and abs(hoje.year - agora.year) > 10:
        raise ErroNegocio("A data de referência tem de estar a até 10 anos de hoje.")
    return await desembolso.desembolso_da_obra(arm, u.tenant_id, obra_id, hoje or agora)


@r.put("/obras/{obra_id}/cronograma", response_model=CronogramaObra, tags=["cronograma"])
async def salvar_cronograma(obra_id: str, corpo: CronogramaCorpo, u: UsuarioAtual = Depends(admin),
                            arm=Depends(_arm)):
    return await cronograma.salvar(arm, u, obra_id, corpo)


@r.post("/obras/{obra_id}/medicoes", response_model=MedicaoSaida, status_code=status.HTTP_201_CREATED,
        tags=["cronograma"], responses={200: {"description": "repetição da mesma chave: devolve a medição já gravada"}})
async def medir(obra_id: str, corpo: MedicaoCorpo, response: Response, u: UsuarioAtual = Depends(admin),
                arm=Depends(_arm)):
    try:
        med, criada, registro_id = await cronograma.medir(arm, u, obra_id, corpo)
    except ConflitoDeId:
        med, criada, registro_id = await cronograma.medir(arm, u, obra_id, corpo)
    if not criada:
        response.status_code = status.HTTP_200_OK
    return {"medicao": med, "cronograma": await cronograma.cronograma_da_obra(arm, u.tenant_id, obra_id),
            "registro_id": registro_id}
