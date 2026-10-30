"""Obras, almoxarifado (insumos com a Conta, movimentos, importação) e pedidos."""
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile, status

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import Movimento, Obra, Pedido, StatusPedido, TipoMovimento

from ..auth import UsuarioAtual, exigir, usuario_atual
from ..esquemas import AvancarCorpo, InsumoComConta, MovimentoCorpo, PedidoCorpo, Previa
from ..modelos import Papel
from ..nucleo import Conflito, ErroNegocio, executar_e_registrar, obra_do_tenant
from ..servicos import estoque, importacao, pedidos

r = APIRouter(tags=["obras e almoxarifado"])
admin = exigir(Papel.ADMIN)


def _arm(request: Request):
    return request.app.state.arm


@r.get("/obras", response_model=list[Obra])
async def listar_obras(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await arm.consultar(Consulta("obras", u.tenant_id, ordem=[("nome", "asc")]))


@r.get("/obras/{obra_id}/insumos", response_model=list[InsumoComConta])
async def insumos(obra_id: str, u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    return await estoque.insumos_com_conta(arm, u.tenant_id, obra_id)


@r.get("/obras/{obra_id}/insumos/{insumo_id}/movimentos", response_model=list[Movimento])
async def movimentos(obra_id: str, insumo_id: str, limite: int = Query(50, ge=1, le=200),
                     u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    return await arm.consultar(Consulta("movimentos", u.tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                    Filtro("insumo_id", "==", insumo_id)],
                                        [("criado_em", "desc")], limite=limite))


@r.post("/obras/{obra_id}/movimentos", response_model=Movimento, status_code=status.HTTP_201_CREATED,
        responses={200: {"description": "repetição da mesma chave: devolve o movimento já gravado"}})
async def lancar_movimento(obra_id: str, corpo: MovimentoCorpo, response: Response,
                           u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    id_ = estoque.id_movimento(u.tenant_id, corpo.chave_idempotencia)

    async def tx_corpo(tx):
        existente = await tx.obter("movimentos", u.tenant_id, id_)
        if existente:
            return existente, False
        await obra_do_tenant(tx, u.tenant_id, obra_id)
        op = await estoque.preparar_movimento(
            tx, u, obra_id=obra_id, insumo_id=corpo.insumo_id, tipo=TipoMovimento(corpo.tipo),
            quantidade=corpo.quantidade, id=id_, destino=corpo.destino, origem_doc=corpo.origem_doc)
        mov, _ = executar_e_registrar(tx, u, op)
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
    return mov


@r.post("/obras/{obra_id}/importar", response_model=Previa)
async def importar(obra_id: str, arquivo: UploadFile = File(...), aplicar: bool = False,
                   u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    conteudo = await arquivo.read(5 * 1024 * 1024 + 1)
    if len(conteudo) > 5 * 1024 * 1024:
        raise ErroNegocio("Arquivo maior que 5 MB.")
    return await importacao.importar(arm, u, obra_id, conteudo, aplicar)


# --- pedidos ---

@r.get("/obras/{obra_id}/pedidos", response_model=list[Pedido], tags=["pedidos"])
async def listar_pedidos(obra_id: str, status_: StatusPedido | None = Query(None, alias="status"),
                         limite: int = Query(100, ge=1, le=500),
                         u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    filtros = [Filtro("obra_id", "==", obra_id)]
    if status_:
        filtros.append(Filtro("status", "==", status_.value))
    return await arm.consultar(Consulta("pedidos", u.tenant_id, filtros, [("criado_em", "desc")], limite=limite))


@r.post("/obras/{obra_id}/pedidos", response_model=Pedido, status_code=status.HTTP_201_CREATED, tags=["pedidos"])
async def criar_pedido(obra_id: str, corpo: PedidoCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    async def tx_corpo(tx):
        op = await pedidos.preparar_criar_pedido(tx, u, obra_id, corpo)
        return executar_e_registrar(tx, u, op)[0]

    return await arm.em_transacao(tx_corpo)


@r.post("/pedidos/{pedido_id}/avancar", response_model=Pedido, tags=["pedidos"])
async def avancar(pedido_id: str, corpo: AvancarCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    async def tx_corpo(tx):
        op = await pedidos.preparar_avancar(tx, u, pedido_id, corpo.para)
        return executar_e_registrar(tx, u, op)[0]

    return await arm.em_transacao(tx_corpo)
