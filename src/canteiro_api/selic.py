"""Selic acumulada no mês (Banco Central, série SGS 4390): a correção que o SERO aplica à remuneração já comprovada da
obra (IN RFB 2.021/2021, art. 31).

Dado público e igual para todo cliente: no tenant de sistema, um documento por ano ("selic:2026", com no máximo 12
meses: o documento não cresce sem limite), {"AAAA-MM": taxa em centésimos de ponto} (1,07% = 107). Só entram meses
fechados; o do mês corrente ainda é parcial.

Importar: `python -m canteiro_api.selic` baixa do Banco Central; `python -m canteiro_api.selic ARQUIVO` lê o JSON de
`dados/` (ou a resposta da API do BCB salva em arquivo).
"""
import asyncio
import json
import re
import sys
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from .modelos import TENANT_SISTEMA, agora

COLECAO = "indices"
DADOS = Path(__file__).parent / "dados" / "selic.json"
URL_BCB = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.4390/dados?formato=json&dataInicial=01/01/2015"


class SelicInvalida(ValueError):
    pass


def ler_bcb(linhas: list[dict], mes_corrente: str) -> dict[str, int]:
    """A resposta da API do BCB ([{"data": "01/10/2026", "valor": "1.07"}]) → {"2026-10": 107}, sem o mês corrente."""
    meses: dict[str, int] = {}
    for x in linhas:
        m = re.fullmatch(r"01/(\d{2})/(\d{4})", str(x.get("data", "")))
        try:
            taxa = Decimal(str(x.get("valor")))
        except InvalidOperation:
            taxa = None
        if not m or taxa is None or not 0 <= taxa < 10:
            raise SelicInvalida(f"Linha da Selic inesperada: {x}.")
        mes = f"{m[2]}-{m[1]}"
        if mes < mes_corrente:
            meses[mes] = int(taxa * 100)
    if not meses:
        raise SelicInvalida("Nenhum mês fechado da Selic na resposta.")
    return meses


def _id(ano: str | int) -> str:
    return f"selic:{ano}"


async def tabela(arm, desde: str, ate: str) -> dict[str, int]:
    """Os meses dos anos de `desde` a `ate` ("AAAA-MM"): a conta da obra lê só os anos da obra, por ID."""
    meses: dict[str, int] = {}
    for ano in range(int(desde[:4]), int(ate[:4]) + 1):
        doc = await arm.obter(COLECAO, TENANT_SISTEMA, _id(ano))
        meses.update(doc["meses"] if doc else {})
    return meses


async def importar(arm, meses: dict[str, int], fonte: str) -> dict:
    """Junta os meses à tabela (o mês que já existe é atualizado). Devolve quantos eram novos e quantos mudaram."""
    for mes, taxa in meses.items():
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mes) or not isinstance(taxa, int) or not 0 <= taxa < 1000:
            raise SelicInvalida(f"Mês inválido: {mes} = {taxa}.")
    por_ano: dict[str, dict[str, int]] = {}
    for mes, taxa in meses.items():
        por_ano.setdefault(mes[:4], {})[mes] = taxa

    async def corpo(tx):
        atuais = {ano: await tx.obter(COLECAO, TENANT_SISTEMA, _id(ano)) for ano in por_ano}
        novos = mudaram = 0
        for ano, doc in atuais.items():
            antes = doc["meses"] if doc else {}
            novos += sum(m not in antes for m in por_ano[ano])
            mudaram += sum(m in antes and antes[m] != t for m, t in por_ano[ano].items())
            campos = {"ano": ano, "meses": {**antes, **por_ano[ano]}, "fonte": fonte, "atualizado_em": agora()}
            if doc:
                tx.atualizar(COLECAO, TENANT_SISTEMA, _id(ano), campos)
            else:
                tx.criar(COLECAO, {"id": _id(ano), "tenant_id": TENANT_SISTEMA, **campos})
        return {"meses": len(meses), "novos": novos, "mudaram": mudaram}

    return await arm.em_transacao(corpo)


def carregar(caminho: Path) -> tuple[dict[str, int], str]:
    d = json.loads(caminho.read_text(encoding="utf-8"))
    if isinstance(d, list):
        return ler_bcb(d, f"{datetime.now(ZoneInfo('America/Sao_Paulo')):%Y-%m}"), f"BCB, de {caminho.name}"
    return d["meses"], d.get("fonte", caminho.name)


def baixar_bcb() -> tuple[dict[str, int], str]:
    with urllib.request.urlopen(URL_BCB, timeout=30) as r:  # noqa: S310 (URL fixa, https)
        linhas = json.loads(r.read())
    hoje = datetime.now(ZoneInfo("America/Sao_Paulo"))
    return ler_bcb(linhas, f"{hoje:%Y-%m}"), f"Banco Central, SGS série 4390, baixado em {hoje:%Y-%m-%d}"


async def principal(caminho: str | None) -> int:
    from .armazenamento import criar_armazenamento
    from .config import Config

    meses, fonte = carregar(Path(caminho)) if caminho else baixar_bcb()
    cfg = Config()
    arm = criar_armazenamento(cfg.armazenamento, mongo_url=cfg.mongo_url, mongo_db=cfg.mongo_db)
    try:
        r = await importar(arm, meses, fonte)
    finally:
        await arm.fechar()
    print(f"Selic: {r['meses']} meses ({r['novos']} novos, {r['mudaram']} com taxa nova), até {max(meses)}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal(sys.argv[1] if len(sys.argv) > 1 else None)))
