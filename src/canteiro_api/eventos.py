"""Relay da outbox. A publicação fica atrás de `Publicador` para o Pub/Sub entrar depois sem mexer no domínio."""
from typing import Any, Protocol

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import agora


class Publicador(Protocol):
    async def publicar(self, topico: str, evento: dict[str, Any], *, chave_ordenacao: str) -> None: ...


class PublicadorEmMemoria:
    """Para testes e desenvolvimento local enquanto o relay do Pub/Sub não existe."""

    def __init__(self) -> None:
        self.publicados: list[tuple[str, dict[str, Any], str]] = []

    async def publicar(self, topico: str, evento: dict[str, Any], *, chave_ordenacao: str) -> None:
        self.publicados.append((topico, evento, chave_ordenacao))


TOPICO_DOMINIO = "dominio.eventos"


async def publicar_pendentes(arm, publicador: Publicador, tenant_id: str, *, lote: int = 100) -> int:
    """Publica o que a outbox ainda não publicou, do mais antigo ao mais novo.

    Entrega é pelo menos uma vez (cair entre publicar e marcar repete o evento): o consumidor deduplica pelo `id`.
    """
    pendentes = await arm.consultar(Consulta("outbox", tenant_id, [Filtro("publicado", "==", False)],
                                             [("criado_em", "asc")], limite=lote))
    for e in pendentes:
        await publicador.publicar(TOPICO_DOMINIO, e, chave_ordenacao=e["chave_ordenacao"])
        await arm.atualizar("outbox", tenant_id, e["id"], {"publicado": True, "publicado_em": agora()})
    return len(pendentes)
