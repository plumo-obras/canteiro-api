"""Rotas de compras, pedidos e estoque que saíram do produto.

Ficam respondendo 410 em vez de sumir: o cliente antigo (aba aberta, versão anterior do agente) recebe a frase de por
que não adianta tentar de novo, e não um 404 que parece erro de digitação.
"""
from fastapi import APIRouter

from ..nucleo import COMPRAS_SAIRAM, Retirado

r = APIRouter(tags=["retiradas"], include_in_schema=False)

RETIRADAS = [
    ("GET", "/obras/{obra_id}/insumos"),
    ("POST", "/obras/{obra_id}/insumos"),
    ("GET", "/obras/{obra_id}/insumos/{insumo_id}/movimentos"),
    ("GET", "/obras/{obra_id}/movimentos"),
    ("POST", "/obras/{obra_id}/movimentos"),
    ("POST", "/obras/{obra_id}/importar"),
    ("POST", "/obras/{obra_id}/orcamento/importar"),
    ("POST", "/obras/{obra_id}/orcamento/materiais"),
    ("GET", "/obras/{obra_id}/pedidos"),
    ("POST", "/obras/{obra_id}/pedidos"),
    ("POST", "/pedidos/{pedido_id}/avancar"),
    ("PATCH", "/pedidos/{pedido_id}"),
    ("GET", "/pedidos/{pedido_id}/recebimentos"),
    ("POST", "/pedidos/{pedido_id}/recebimentos"),
]


async def _retirada():
    raise Retirado(COMPRAS_SAIRAM)


for metodo, caminho in RETIRADAS:
    r.add_api_route(caminho, _retirada, methods=[metodo], status_code=410)
