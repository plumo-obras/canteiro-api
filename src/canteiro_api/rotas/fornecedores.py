"""Cadastro de fornecedores do tenant (valem para todas as obras)."""
from fastapi import APIRouter, Depends, Request, status

from canteiro_dominio.armazenamento import ConflitoDeId

from ..auth import UsuarioAtual, exigir, usuario_atual
from ..esquemas import FornecedorCorpo, FornecedorEdicao, FornecedorSaida
from ..modelos import Papel
from ..nucleo import Conflito
from ..servicos import fornecedores

r = APIRouter(tags=["fornecedores"])
admin = exigir(Papel.ADMIN)


def _arm(request: Request):
    return request.app.state.arm


@r.get("/fornecedores", response_model=list[FornecedorSaida])
async def listar(u: UsuarioAtual = Depends(usuario_atual), arm=Depends(_arm)):
    return await fornecedores.listar(arm, u.tenant_id)


@r.post("/fornecedores", response_model=FornecedorSaida, status_code=status.HTTP_201_CREATED)
async def criar(corpo: FornecedorCorpo, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    try:
        return await fornecedores.criar(arm, u, corpo)
    except ConflitoDeId:
        raise Conflito(f"Já cadastrado: {corpo.nome}.") from None


@r.patch("/fornecedores/{fornecedor_id}", response_model=FornecedorSaida)
async def editar(fornecedor_id: str, corpo: FornecedorEdicao, u: UsuarioAtual = Depends(admin), arm=Depends(_arm)):
    return await fornecedores.editar(arm, u, fornecedor_id, corpo)
