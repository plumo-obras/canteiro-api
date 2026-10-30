"""Livro de consumo de IA e créditos. Soma feita aqui, linha a linha: agregação no banco não é portável."""
from datetime import date
from zoneinfo import ZoneInfo

from canteiro_dominio.armazenamento import ConflitoDeId, Consulta, Filtro
from canteiro_dominio.modelos import UsoIA

from ..esquemas import Creditos, RelatorioUso, Soma, SomaDia, SomaObra, SomaRota
from ..modelos import doc
from ..nucleo import NaoEncontrado

_SP = ZoneInfo("America/Sao_Paulo")


def _tokens(u: dict) -> int:
    return u["tokens_entrada"] + u["tokens_saida"] + u["tokens_cache_leitura"] + u["tokens_cache_escrita"]


async def gravar_uso(arm, uso: UsoIA) -> tuple[dict, bool]:
    """Grava a linha e desconta dos créditos na mesma transação. O ID da linha torna a repetição inofensiva."""
    if not uso.dia:
        uso.dia = uso.criado_em.astimezone(_SP).date().isoformat()
    d = doc(uso)

    async def corpo(tx):
        if await tx.obter("uso_ia", uso.tenant_id, uso.id):
            return False
        t = await tx.obter("tenants", uso.tenant_id, uso.tenant_id)
        if not t:
            raise NaoEncontrado("Cliente não encontrado.")
        tx.criar("uso_ia", d)
        if not (uso.chave_propria or t["chave_propria"]):
            tx.atualizar("tenants", uso.tenant_id, uso.tenant_id,
                         {"saldo_microdolares": t["saldo_microdolares"] - uso.custo_microdolares})
        return True

    try:
        criado = await arm.em_transacao(corpo)
    except ConflitoDeId:
        criado = False
    return d, criado


async def relatorio(arm, tenant_id: str, de: date, ate: date) -> RelatorioUso:
    linhas = await arm.consultar(Consulta("uso_ia", tenant_id, [Filtro("dia", ">=", de.isoformat()),
                                                                Filtro("dia", "<=", ate.isoformat())],
                                          [("dia", "asc")]))
    total = Soma()
    por: dict[str, dict] = {"dia": {}, "obra_id": {}, "rota": {}}
    for u in linhas:
        custo, tokens = u["custo_microdolares"], _tokens(u)
        total.custo += custo
        total.tokens += tokens
        for chave, grupo in por.items():
            s = grupo.setdefault(u.get(chave), {"custo": 0, "tokens": 0})
            s["custo"] += custo
            s["tokens"] += tokens
    return RelatorioUso(
        total=total,
        por_dia=[SomaDia(dia=k, **v) for k, v in sorted(por["dia"].items())],
        por_obra=[SomaObra(obra_id=k, **v) for k, v in sorted(por["obra_id"].items(), key=lambda kv: -kv[1]["custo"])],
        por_rota=[SomaRota(rota=k, **v) for k, v in sorted(por["rota"].items(), key=lambda kv: -kv[1]["custo"])])


async def creditos(arm, tenant_id: str, usuario_id: str) -> Creditos:
    t = await arm.obter("tenants", tenant_id, tenant_id)
    if not t:
        raise NaoEncontrado("Cliente não encontrado.")
    return Creditos(saldo_microdolares=t["saldo_microdolares"], franquia=t["franquia_microdolares"],
                    chave_propria=t["chave_propria"], usuario_id=usuario_id, tenant_id=tenant_id)
