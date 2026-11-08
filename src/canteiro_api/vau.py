"""Tabela VAU do SERO (Receita Federal): o valor do m² por UF, mês e destinação que a aferição do INSS da obra usa.

É dado público e o mesmo para todo cliente, então mora no tenant de sistema, um documento por UF, mês e destinação
(id derivado: reimportar o mesmo mês só atualiza). A obra que não informa o VAU à mão usa o vigente desta tabela.

Importar: `python -m canteiro_api.vau ARQUIVO` com o JSON de `dados/` ou o texto da tela "Consultar Tabela VAU" do
SERO copiado como está (cabeçalho "Mês/Ano  UF  Casa popular …", uma linha por mês, separado por tab).
"""
import asyncio
import json
import re
import sys
from pathlib import Path

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.tipos import reais
from canteiro_dominio.tributos import DESTINACOES, vau_vigente

from .modelos import TENANT_SISTEMA, agora

COLECAO = "vau"
DADOS = Path(__file__).parent / "dados" / "vau-sero.json"
# Os títulos das colunas como o SERO escreve → a destinação do contrato.
COLUNAS_SERO = {
    "casa popular": "casa_popular", "comercial salas e lojas": "comercial",
    "conjunto habitacional popular": "conjunto_habitacional", "edifício de garagens": "edificio_garagens",
    "galpão industrial": "galpao", "residencial multifamiliar": "residencial_multifamiliar",
    "residencial unifamiliar": "residencial_unifamiliar",
}
UFS = frozenset("AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split())
POR_TRANSACAO = 400       # abaixo do teto de 500 escritas da porta


class TabelaInvalida(ValueError):
    pass


def ler_tabela_sero(texto: str) -> list[dict]:
    """O texto da tela do SERO → linhas {uf, mes "AAAA-MM", destinacao, vau (centavos)}. Aceita várias tabelas
    coladas uma depois da outra (uma por UF). Coluna desconhecida ou valor que não é dinheiro → recusa tudo: um
    VAU errado muda o INSS de toda obra daquela UF."""
    linhas: list[dict] = []
    destinos: list[str] | None = None
    for n, bruta in enumerate(texto.splitlines(), 1):
        partes = [p.strip() for p in bruta.split("\t")]
        if not any(partes):
            continue
        if partes[:2] == ["Mês/Ano", "UF"]:
            try:
                destinos = [COLUNAS_SERO[c.lower()] for c in partes[2:]]
            except KeyError as e:
                raise TabelaInvalida(f"Linha {n}: coluna que o SERO não tinha: {e}.") from None
            continue
        m = re.fullmatch(r"(\d{2})/(\d{4})", partes[0])
        if not m:
            continue
        if destinos is None:
            raise TabelaInvalida(f"Linha {n}: valores antes do cabeçalho da tabela.")
        uf = partes[1].upper()
        if uf not in UFS or len(partes) - 2 != len(destinos):
            raise TabelaInvalida(f"Linha {n}: UF ou número de colunas inesperado.")
        for destinacao, valor in zip(destinos, partes[2:], strict=True):
            try:
                v = reais(valor)
            except (ValueError, ArithmeticError):
                raise TabelaInvalida(f"Linha {n}: '{valor}' não é um valor em reais.") from None
            if v <= 0:
                raise TabelaInvalida(f"Linha {n}: VAU tem de ser maior que zero.")
            linhas.append({"uf": uf, "mes": f"{m[2]}-{m[1]}", "destinacao": destinacao, "vau": v})
    if not linhas:
        raise TabelaInvalida("Nenhuma linha de VAU encontrada no texto.")
    return linhas


def _id(x: dict) -> str:
    return f"{x['uf']}:{x['mes']}:{x['destinacao']}"


async def importar(arm, linhas: list[dict], fonte: str) -> dict:
    """Grava (ou atualiza) as linhas. Devolve quantas eram novas e quantas mudaram de valor."""
    for x in linhas:
        if x["destinacao"] not in DESTINACOES or x["uf"] not in UFS or not re.fullmatch(r"\d{4}-\d{2}", x["mes"]):
            raise TabelaInvalida(f"Linha inválida: {x}.")
    novas = mudaram = 0
    for i in range(0, len(linhas), POR_TRANSACAO):
        lote = linhas[i:i + POR_TRANSACAO]

        async def corpo(tx, lote=lote):
            n = m = 0
            atuais = [await tx.obter(COLECAO, TENANT_SISTEMA, _id(x)) for x in lote]
            for x, atual in zip(lote, atuais, strict=True):
                campos = {**x, "fonte": fonte, "atualizado_em": agora()}
                if atual is None:
                    tx.criar(COLECAO, {"id": _id(x), "tenant_id": TENANT_SISTEMA, **campos})
                    n += 1
                else:
                    # gravada mesmo sem mudar: o que a transação leu, ela escreve (regra da porta)
                    m += atual["vau"] != x["vau"]
                    tx.atualizar(COLECAO, TENANT_SISTEMA, _id(x), campos)
            return n, m

        n, m = await arm.em_transacao(corpo)
        novas, mudaram = novas + n, mudaram + m
    return {"linhas": len(linhas), "novas": novas, "mudaram": mudaram}


async def linhas_de(arm, uf: str, destinacao: str) -> list[dict]:
    return await arm.consultar(Consulta(COLECAO, TENANT_SISTEMA, [Filtro("uf", "==", uf),
                                                                  Filtro("destinacao", "==", destinacao)]))


async def vigente(arm, uf: str, destinacao: str, ate: str) -> dict | None:
    if uf not in UFS or destinacao not in DESTINACOES:
        return None
    return vau_vigente(await linhas_de(arm, uf, destinacao), ate)


def carregar(caminho: Path) -> tuple[list[dict], str]:
    texto = caminho.read_text(encoding="utf-8")
    if caminho.suffix == ".json":
        d = json.loads(texto)
        return d["linhas"], d.get("fonte", caminho.name)
    return ler_tabela_sero(texto), f"SERO, importado de {caminho.name}"


async def principal(caminho: str) -> int:
    from .armazenamento import criar_armazenamento
    from .config import Config

    cfg = Config()
    linhas, fonte = carregar(Path(caminho))
    arm = criar_armazenamento(cfg.armazenamento, mongo_url=cfg.mongo_url, mongo_db=cfg.mongo_db)
    try:
        r = await importar(arm, linhas, fonte)
    finally:
        await arm.fechar()
    print(f"Tabela VAU: {r['linhas']} linhas ({r['novas']} novas, {r['mudaram']} com valor novo).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal(sys.argv[1] if len(sys.argv) > 1 else str(DADOS))))
