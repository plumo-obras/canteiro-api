"""Obras, almoxarifado (insumos com a Conta, movimentos, importação) e pedidos."""
import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import ValidationError

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import Movimento, Obra, Recebimento, StatusPedido, TipoMovimento, TipoVersao

from ..auth import UsuarioAtual, exigir, usuario_atual
from ..esquemas import (
    AvancarCorpo,
    CronogramaCorpo,
    CronogramaObra,
    EtapaRef,
    InsumoComConta,
    InsumoCorpo,
    InsumoSaida,
    LeituraPlanilha,
    Mapeamento,
    MedicaoCorpo,
    MedicaoSaida,
    MovimentoCorpo,
    MovimentoSaida,
    ObraCorpo,
    ObraEdicao,
    ObraSaida,
    OrcamentoObra,
    PedidoCorpo,
    PedidoEdicao,
    PedidoListado,
    PedidoSaida,
    Previa,
    PreviaOrcamento,
    PreviaVersao,
    RecebimentoCorpo,
    RecebimentoSaida,
    ResumoDaObra,
    ResumoObra,
)
from ..modelos import Papel
from ..nucleo import Conflito, ErroNegocio, NaoEncontrado, executar_e_registrar, listar_pagina, obra_do_tenant
from ..servicos import cronograma, estoque, importacao, obras, orcamento, pedidos, resumo, versoes

r = APIRouter(tags=["obras e almoxarifado"])
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


@r.post("/obras/{obra_id}/orcamento/importar", response_model=PreviaOrcamento, tags=["orçamento"])
async def importar_orcamento(obra_id: str, arquivo: UploadFile = File(...), mapeamento: str = Form(...),
                             aplicar: bool = Form(False), u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    try:
        m = Mapeamento.model_validate(json.loads(mapeamento))
    except (ValueError, ValidationError):
        raise ErroNegocio("Mapeamento de colunas inválido.") from None
    conteudo = await _ler_arquivo(arquivo)
    try:
        return await orcamento.importar(arm, u, obra_id, conteudo, arquivo.filename or "", m, aplicar)
    except ConflitoDeId:
        raise Conflito("Alguém cadastrou um destes insumos agora há pouco; envie a planilha de novo.") from None


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
    conteudo = await _ler_arquivo(arquivo)
    try:
        previa, criada = await versoes.importar(arm, u, obra_id, conteudo, arquivo.filename or "", m, tipo,
                                                descricao, data or None, aplicar, chave_idempotencia,
                                                escolhidas)
    except ConflitoDeId:
        raise Conflito("Esta importação já foi gravada agora há pouco; recarregue o orçamento.") from None
    if criada:
        response.status_code = status.HTTP_201_CREATED
    return previa


@r.post("/obras/{obra_id}/orcamento/aprovar", response_model=ObraSaida, tags=["orçamento"])
async def aprovar_orcamento(obra_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    insumos = await obras.insumos_da_obra(arm, u.tenant_id, obra_id)
    vigentes = [v for v in await versoes.versoes_da_obra(arm, u.tenant_id, obra_id) if v["ativa"]]

    async def tx_corpo(tx):
        op = await obras.preparar_aprovar_orcamento(tx, u, obra_id, insumos, vigentes)
        o, entrada = executar_e_registrar(tx, u, op)
        return {**o, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


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
    return await resumo.resumo_da_obra(arm, u.tenant_id, obra_id, hoje or datetime.now(estoque.FUSO).date())


@r.get("/obras/{obra_id}/insumos", response_model=list[InsumoComConta])
async def insumos(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    return await estoque.insumos_com_conta(arm, u.tenant_id, obra_id)


@r.post("/obras/{obra_id}/insumos", response_model=InsumoSaida, status_code=status.HTTP_201_CREATED)
async def criar_insumo(obra_id: str, corpo: InsumoCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    try:
        return await estoque.criar_insumo(arm, u, obra_id, **corpo.model_dump())
    except ConflitoDeId:
        raise Conflito(f"'{corpo.nome}' já está cadastrado nesta obra.") from None


@r.get("/obras/{obra_id}/insumos/{insumo_id}/movimentos", response_model=list[Movimento])
async def movimentos(obra_id: str, insumo_id: str, limite: int = Query(50, ge=1, le=200),
                     antes_de: datetime | None = None, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    filtros = [Filtro("obra_id", "==", obra_id), Filtro("insumo_id", "==", insumo_id)]
    consulta = Consulta("movimentos", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite)
    return await listar_pagina(arm, consulta, "criado_em", antes_de)


@r.get("/obras/{obra_id}/movimentos", response_model=list[Movimento])
async def movimentos_da_obra(obra_id: str, limite: int = Query(50, ge=1, le=200), antes_de: datetime | None = None,
                             u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    # O "histórico do almoxarifado" sem insumo: um insumo por vez obrigava o agente a escolher quais consultar, e ele
    # escolhia só os que já tinham aparecido na conversa (a brita ficou de fora no teste de 05/11).
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    consulta = Consulta("movimentos", u.tenant_id, [Filtro("obra_id", "==", obra_id)], [("criado_em", "desc")],
                        limite=limite)
    return await listar_pagina(arm, consulta, "criado_em", antes_de)


@r.post("/obras/{obra_id}/movimentos", response_model=MovimentoSaida, status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "repetição da mesma chave: devolve o movimento já gravado"}})
async def lancar_movimento(obra_id: str, corpo: MovimentoCorpo, response: Response,
                           u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    # apropriação: a saída diz qual etapa consumiu (obrigatória quando a obra tem orçamento); entrada e ajuste não
    await versoes.validar_etapa(arm, u.tenant_id, obra_id, [corpo.etapa], exigir=corpo.tipo == "saida")
    id_ = estoque.id_movimento(u.tenant_id, corpo.chave_idempotencia)
    # derivado do movimento: a repetição da chave devolve o mesmo `registro_id` sem precisar procurar o registro
    id_registro = estoque.id_derivado(u.tenant_id, "registro", id_)

    async def tx_corpo(tx):
        existente = await tx.obter("movimentos", u.tenant_id, id_)
        if existente:
            return existente, False
        await obra_do_tenant(tx, u.tenant_id, obra_id)
        op = await estoque.preparar_movimento(
            tx, u, obra_id=obra_id, insumo_id=corpo.insumo_id, tipo=TipoMovimento(corpo.tipo),
            quantidade=corpo.quantidade, id=id_, destino=corpo.destino, etapa=corpo.etapa or None,
            origem_doc=corpo.origem_doc)
        mov, _ = executar_e_registrar(tx, u, op, registro_id=id_registro)
        return mov, True

    try:
        mov, criado = await arm.em_transacao(tx_corpo)
    except ConflitoDeId:
        # duas requisições com a mesma chave ao mesmo tempo: a outra gravou primeiro
        mov, criado = await arm.obter("movimentos", u.tenant_id, id_), False
    if not criado:
        if mov["obra_id"] != obra_id or not estoque.mesmo_movimento(mov, corpo):
            raise Conflito("Esta chave de idempotência já foi usada para outro movimento.")
        response.status_code = status.HTTP_200_OK
    return {**mov, "registro_id": id_registro}


@r.post("/obras/{obra_id}/importar", response_model=Previa)
async def importar(obra_id: str, arquivo: UploadFile = File(...), aplicar: bool = False,
                   u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)
    if len(conteudo) > 5 * 1024 * 1024:
        raise ErroNegocio("Arquivo maior que 5 MB.")
    return await importacao.importar(arm, u, obra_id, conteudo, aplicar)


# --- pedidos ---

@r.get("/obras/{obra_id}/pedidos", response_model=list[PedidoListado], tags=["pedidos"])
async def listar_pedidos(obra_id: str, status_: list[StatusPedido] | None = Query(None, alias="status"),
                         limite: int = Query(100, ge=1, le=500), antes_de: datetime | None = None,
                         u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    filtros = [Filtro("obra_id", "==", obra_id)]
    # status repetido (?status=a&status=b) vira "in": o quadro pede só as colunas abertas, o recolhido só as fechadas
    sts = list(dict.fromkeys(st.value for st in status_ or []))
    if len(sts) == 1:
        filtros.append(Filtro("status", "==", sts[0]))
    elif sts:
        filtros.append(Filtro("status", "in", sts))
    return await listar_pagina(arm, Consulta("pedidos", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite),
                               "criado_em", antes_de)


@r.post("/obras/{obra_id}/pedidos", response_model=PedidoSaida, status_code=status.HTTP_201_CREATED,
        tags=["pedidos"])
async def criar_pedido(obra_id: str, corpo: PedidoCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    await versoes.validar_etapa(arm, u.tenant_id, obra_id, [x.etapa for x in corpo.linhas])

    async def tx_corpo(tx):
        op = await pedidos.preparar_criar_pedido(
            tx, u, obra_id, corpo, status=StatusPedido.APROVADO if corpo.aprovado else StatusPedido.EM_COTACAO)
        pedido, entrada = executar_e_registrar(tx, u, op)
        return {**pedido, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


@r.post("/pedidos/{pedido_id}/avancar", response_model=PedidoSaida, tags=["pedidos"])
async def avancar(pedido_id: str, corpo: AvancarCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    async def tx_corpo(tx):
        op = await pedidos.preparar_avancar(tx, u, pedido_id, corpo.para,
                                            quantidade_recebida=corpo.quantidade_recebida, nf=corpo.nf)
        pedido, entrada = executar_e_registrar(tx, u, op)
        return {**pedido, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


@r.patch("/pedidos/{pedido_id}", response_model=PedidoSaida, tags=["pedidos"])
async def editar_pedido(pedido_id: str, corpo: PedidoEdicao, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    mudancas = corpo.model_dump(exclude_unset=True)
    if not mudancas:
        raise ErroNegocio("Nada para mudar.")
    if "observacao" in mudancas:
        mudancas["observacao"] = (mudancas["observacao"] or "").strip()

    async def tx_corpo(tx):
        op = await pedidos.preparar_editar(tx, u, pedido_id, mudancas)
        pedido, entrada = executar_e_registrar(tx, u, op)
        return {**pedido, "registro_id": entrada["id"]}

    return await arm.em_transacao(tx_corpo)


@r.post("/pedidos/{pedido_id}/recebimentos", response_model=RecebimentoSaida, status_code=status.HTTP_201_CREATED,
        tags=["pedidos"], responses={200: {"description": "repetição da mesma chave: devolve a entrega já gravada"}})
async def receber(pedido_id: str, corpo: RecebimentoCorpo, response: Response, u: UsuarioAtual = Depends(admin),
                  arm=Depends(_arm)):
    id_ = pedidos.id_recebimento(u.tenant_id, pedido_id, corpo.chave_idempotencia)
    id_registro = estoque.id_derivado(u.tenant_id, "registro", id_)

    async def tx_corpo(tx):
        ja = await tx.obter("recebimentos", u.tenant_id, id_)
        if ja:
            return ja, await tx.obter("pedidos", u.tenant_id, pedido_id), False
        op = await pedidos.preparar_receber(
            tx, u, pedido_id, [(x.indice, x.quantidade) for x in corpo.linhas], recebimento_id=id_,
            nf_numero=corpo.nf_numero, nf_data=corpo.nf_data, nf_valor=corpo.nf_valor)
        r_, _ = executar_e_registrar(tx, u, op, registro_id=id_registro)
        return r_["recebimento"], r_["pedido"], True

    try:
        rec, pedido, criado = await arm.em_transacao(tx_corpo)
    except ConflitoDeId:
        rec, criado = await arm.obter("recebimentos", u.tenant_id, id_), False
        pedido = await arm.obter("pedidos", u.tenant_id, pedido_id)
    if not criado:
        if [(x["indice"], x["quantidade"]) for x in rec["linhas"]] != [(x.indice, x.quantidade) for x in corpo.linhas]:
            raise Conflito("Esta chave de idempotência já foi usada para outra entrega.")
        response.status_code = status.HTTP_200_OK
    return {"recebimento": rec, "pedido": pedido, "registro_id": id_registro}


@r.get("/pedidos/{pedido_id}/recebimentos", response_model=list[Recebimento], tags=["pedidos"])
async def recebimentos(pedido_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    p = await arm.obter("pedidos", u.tenant_id, pedido_id)
    if not p:
        raise NaoEncontrado("Pedido não encontrado.")
    rs = await arm.consultar(Consulta("recebimentos", u.tenant_id, [Filtro("pedido_id", "==", pedido_id)]))
    return sorted((x for x in rs if not x.get("desfeito")), key=lambda x: x["numero"])


@r.get("/obras/{obra_id}/etapas", response_model=list[EtapaRef])
async def etapas(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    return await versoes.etapas_da_obra(arm, u.tenant_id, obra_id)


# --- cronograma e medição ---

@r.get("/obras/{obra_id}/cronograma", response_model=CronogramaObra, tags=["cronograma"])
async def ver_cronograma(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await cronograma.cronograma_da_obra(arm, u.tenant_id, obra_id)


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
