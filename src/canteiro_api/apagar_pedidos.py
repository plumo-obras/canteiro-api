"""Tira do banco o que sobrou de compras, pedidos e estoque, depois que eles saíram do produto.

    python -m canteiro_api.apagar_pedidos                     # só simula: conta o que mudaria, não grava nada
    python -m canteiro_api.apagar_pedidos --aplicar           # grava a cópia em JSON e só então muda o banco
    python -m canteiro_api.apagar_pedidos --aplicar --copia /caminho/da/pasta

O que muda, por cliente:
- apaga `pedidos`, `recebimentos`, `insumos`, `movimentos`, `saldos` e o contador dos números de pedido;
- o gasto que era pagamento de pedido perde o `pedido_id` e passa a contar como qualquer gasto;
- a proposta de compra ainda pendente é recusada, e a trava de pendência dela é solta;
- a entrada do Histórico cujo Desfazer mexeria em pedido ou estoque perde o Desfazer (a frase fica); a do aditivo que
  gerou Compras perde só a parte de Compras do Desfazer.

A cópia leva tudo o que vai ser apagado ou alterado, como estava, num arquivo só. Ela tem dado de cliente (nomes de
fornecedor, valores): guarde fora do repositório. A saída do script leva só contagens.

Listar os clientes atravessa tenants e apagar sai da porta de armazenamento de propósito: os dois são métodos de
manutenção dos adaptadores (`tenants_da_colecao`, `apagar_ids`), como no `migrar_arquivos`. Rodar de novo é seguro:
o que já foi limpo não aparece mais.
"""
import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from canteiro_dominio.armazenamento import Consulta, Filtro

from .armazenamento import criar_armazenamento
from .config import Config

APAGADAS = ("pedidos", "recebimentos", "insumos", "movimentos", "saldos")
TIPOS_DE_COMPRA = ("criar_pedido", "avancar_pedido", "ajuste_estoque", "registrar_entrega")
# Receitas de Desfazer que só existem para pedido e estoque: depois da limpeza não há o que desfazer
OPS_DE_COMPRA = ("estornar_movimento", "voltar_pedido", "desfazer_recebimento", "editar_pedido", "cancelar_pedido",
                 "restaurar_insumos")
TRAVAS = "propostas_pendentes"
MOTIVO = "Compras saíram do Canteiro."
LOTE = 200


def _id_contador(tenant_id: str) -> str:
    return f"contador-pedidos-{tenant_id}"


@dataclass
class Plano:
    """O que muda num cliente. `copia` guarda os documentos como estavam."""
    apagar: dict[str, list[str]] = field(default_factory=dict)
    gastos: list[str] = field(default_factory=list)
    propostas: list[str] = field(default_factory=list)
    travas: list[str] = field(default_factory=list)
    registro: dict[str, dict | None] = field(default_factory=dict)   # id → desfazer novo
    contador: bool = False
    copia: dict[str, list[dict]] = field(default_factory=dict)

    def vazio(self) -> bool:
        return not (any(self.apagar.values()) or self.gastos or self.propostas or self.travas or self.registro
                    or self.contador)


@dataclass
class Relatorio:
    clientes: int = 0
    apagados: dict[str, int] = field(default_factory=dict)
    gastos_convertidos: int = 0
    propostas_recusadas: int = 0
    desfazer_tirados: int = 0
    copia: str | None = None


def _guardar(plano: Plano, colecao: str, d: dict) -> None:
    plano.copia.setdefault(colecao, []).append(d)


def _receita_sem_compra(receita: dict | None) -> tuple[bool, dict | None]:
    """(muda, receita nova). Também olha a receita de refazer, que repete o `op` da ação original."""
    if not receita:
        return False, receita
    if receita.get("op") in OPS_DE_COMPRA:
        return True, None
    if receita.get("op") == "desligar_versao" and receita.get("compras"):
        return True, {k: v for k, v in receita.items() if k != "compras"}
    return False, receita


async def planejar(arm, tenant: str) -> Plano:
    p = Plano()
    for colecao in APAGADAS:
        docs = await arm.consultar(Consulta(colecao, tenant))
        p.apagar[colecao] = [d["id"] for d in docs]
        for d in docs:
            _guardar(p, colecao, d)
    if contador := await arm.obter("contadores", tenant, _id_contador(tenant)):
        p.contador = True
        _guardar(p, "contadores", contador)
    for g in await arm.consultar(Consulta("gastos", tenant)):
        if g.get("pedido_id"):
            p.gastos.append(g["id"])
            _guardar(p, "gastos", g)
    pendentes = await arm.consultar(Consulta("propostas", tenant, [Filtro("status", "==", "pendente")]))
    for prop in pendentes:
        if prop["tipo"] in TIPOS_DE_COMPRA:
            p.propostas.append(prop["id"])
            _guardar(p, "propostas", prop)
            for t in await arm.consultar(Consulta(TRAVAS, tenant, [Filtro("proposta_id", "==", prop["id"])])):
                p.travas.append(t["id"])
                _guardar(p, TRAVAS, t)
    for e in await arm.consultar(Consulta("registro", tenant)):
        if e.get("desfeito"):
            continue
        muda, nova = _receita_sem_compra(e.get("desfazer"))
        if muda:
            p.registro[e["id"]] = nova
            _guardar(p, "registro", e)
    return p


async def aplicar(arm, tenant: str, p: Plano) -> None:
    """Primeiro o que muda, por último o que se apaga: se parar no meio, rodar de novo acha o resto."""
    agora = datetime.now(UTC)
    for id_ in p.gastos:
        await arm.atualizar("gastos", tenant, id_, {"pedido_id": None})
    for id_ in p.propostas:
        await arm.atualizar("propostas", tenant, id_, {"status": "recusada", "motivo_recusa": MOTIVO,
                                                      "decidido_por": None, "decidido_por_nome": "Canteiro",
                                                      "decidido_em": agora})
    for id_ in p.travas:
        await arm.atualizar(TRAVAS, tenant, id_, {"proposta_id": None, "atualizado_em": agora})
    for id_, receita in p.registro.items():
        await arm.atualizar("registro", tenant, id_, {"desfazer": receita})
    for colecao, ids in p.apagar.items():
        for i in range(0, len(ids), LOTE):
            await arm.apagar_ids(colecao, tenant, ids[i:i + LOTE])
    if p.contador:
        await arm.apagar_ids("contadores", tenant, [_id_contador(tenant)])


def _gravar_copia(pasta: Path, conteudo: str) -> Path:
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / f"copia-pedidos-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    # "x": duas rodadas no mesmo segundo não sobrescrevem a cópia da primeira
    with arquivo.open("x", encoding="utf-8") as f:
        f.write(conteudo)
    return arquivo


def _json(v):
    if isinstance(v, datetime):
        return v.isoformat()
    raise TypeError(f"{type(v).__name__} não vira JSON")


async def limpar(arm, *, gravar: bool, pasta_copia: Path | None = None, tenants=None, avisar=print) -> Relatorio:
    """Sem `gravar`, só conta. Com `gravar`, a cópia de todos os clientes vai para o disco antes da primeira escrita
    no banco: se a cópia falhar, nada mudou."""
    if gravar and pasta_copia is None:
        raise ValueError("Aplicar sem pasta para a cópia.")
    if tenants is None:
        vistos: set[str] = set()
        for colecao in (*APAGADAS, "gastos", "propostas", "registro"):
            vistos.update(await arm.tenants_da_colecao(colecao))
        tenants = sorted(vistos)
    planos = {t: await planejar(arm, t) for t in tenants}
    planos = {t: p for t, p in planos.items() if not p.vazio()}
    rel = Relatorio(clientes=len(planos))
    for p in planos.values():
        for colecao, ids in p.apagar.items():
            rel.apagados[colecao] = rel.apagados.get(colecao, 0) + len(ids)
        rel.apagados["contadores"] = rel.apagados.get("contadores", 0) + int(p.contador)
        rel.gastos_convertidos += len(p.gastos)
        rel.propostas_recusadas += len(p.propostas)
        rel.desfazer_tirados += len(p.registro)
    if not gravar or not planos:
        return rel
    conteudo = json.dumps({t: p.copia for t, p in planos.items()}, default=_json, ensure_ascii=False, indent=1)
    rel.copia = str(await asyncio.to_thread(_gravar_copia, pasta_copia, conteudo))
    for t, p in planos.items():
        await aplicar(arm, t, p)
        avisar(f"ok {t}")
    return rel


def _imprimir(rel: Relatorio, gravar: bool) -> None:
    print("Aplicado." if gravar else "Simulação: nada foi gravado. Rode com --aplicar para valer.")
    print(f"{rel.clientes} cliente(s) com o que limpar.")
    for colecao, n in sorted(rel.apagados.items()):
        print(f"  {colecao}: {n} apagado(s)")
    print(f"  gastos que eram pagamento de pedido: {rel.gastos_convertidos} (passam a contar como gasto)")
    print(f"  propostas de compra pendentes: {rel.propostas_recusadas} (recusadas)")
    print(f"  entradas do Histórico sem o Desfazer de compra: {rel.desfazer_tirados}")
    if rel.copia:
        print(f"Cópia: {rel.copia}")


async def _principal(gravar: bool, pasta: Path) -> int:
    config = Config()
    arm = criar_armazenamento("mongo", mongo_url=config.mongo_url, mongo_db=config.mongo_db)
    try:
        rel = await limpar(arm, gravar=gravar, pasta_copia=pasta)
    finally:
        await arm.fechar()
    _imprimir(rel, gravar)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aplicar", action="store_true", help="grava a cópia e muda o banco (sem isto, só simula)")
    p.add_argument("--copia", type=Path, default=Path("."), help="pasta da cópia em JSON (padrão: a atual)")
    a = p.parse_args()
    raise SystemExit(asyncio.run(_principal(a.aplicar, a.copia)))


if __name__ == "__main__":
    main()
