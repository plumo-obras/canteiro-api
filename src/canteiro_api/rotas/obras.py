"""Obras, almoxarifado (insumos com a Conta, movimentos, importação) e pedidos."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from pydantic import ValidationError

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import Movimento, Obra, Pedido, StatusPedido, TipoMovimento

from ..auth import UsuarioAtual, exigir, usuario_atual
from ..esquemas import (
    AvancarCorpo,
    InsumoComConta,
    InsumoCorpo,
    InsumoSaida,
    LeituraPlanilha,
    Mapeamento,
    MovimentoCorpo,
    MovimentoSaida,
    ObraCorpo,
    ObraEdicao,
    ObraSaida,
    PedidoCorpo,
    PedidoSaida,
    Previa,
    PreviaOrcamento,
    ResumoObra,
)
from ..modelos import Papel
from ..nucleo import Conflito, ErroNegocio, executar_e_registrar, listar_pagina, obra_do_tenant
from ..servicos import estoque, importacao, obras, orcamento, pedidos, resumo

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


@r.post("/obras/{obra_id}/orcamento/aprovar", response_model=ObraSaida, tags=["orçamento"])
async def aprovar_orcamento(obra_id: str, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    insumos = await obras.insumos_da_obra(arm, u.tenant_id, obra_id)

    async def tx_corpo(tx):
        o, entrada = executar_e_registrar(tx, u, await obras.preparar_aprovar_orcamento(tx, u, obra_id, insumos))
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


@r.post("/obras/{obra_id}/movimentos", response_model=MovimentoSaida, status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "repetição da mesma chave: devolve o movimento já gravado"}})
async def lancar_movimento(obra_id: str, corpo: MovimentoCorpo, response: Response,
                           u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
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
            quantidade=corpo.quantidade, id=id_, destino=corpo.destino, origem_doc=corpo.origem_doc)
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

@r.get("/obras/{obra_id}/pedidos", response_model=list[Pedido], tags=["pedidos"])
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
