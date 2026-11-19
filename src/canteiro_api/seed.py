"""Dados de exemplo: a Casa Vila Nova do protótipo.  Uso: `python -m canteiro_api.seed`

A senha do admin vem de SEED_ADMIN_SENHA no `.env` local; nunca de código (o repositório é público).
Rodar de novo não duplica nada: os IDs são fixos e o seed para se o cliente já existe.
"""
import asyncio
import sys
import uuid

from canteiro_dominio.modelos import Obra
from canteiro_dominio.tipos import qtd, reais

from . import selic, vau
from .armazenamento import criar_armazenamento
from .auth import criar_usuario
from .config import Config
from .modelos import Papel, Tenant, doc

_NS = uuid.UUID("0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21")


def _id(nome: str) -> str:
    return str(uuid.uuid5(_NS, nome))


TENANT_EXEMPLO = _id("tenant:construtora-exemplo")


def documentos(cfg: Config, t: str = TENANT_EXEMPLO) -> dict[str, list[dict]]:
    """IDs derivados do tenant: os testes semeiam um cliente novo por teste sem colidir no mesmo banco."""
    def _id(nome: str) -> str:
        return str(uuid.uuid5(_NS, f"{t}|{nome}"))

    o = _id("obra:casa-vila-nova")
    tenant = Tenant(id=t, nome="Construtora Exemplo", saldo_microdolares=cfg.franquia_padrao_microdolares,
                    franquia_microdolares=cfg.franquia_padrao_microdolares)
    obra = Obra(id=o, tenant_id=t, nome="Casa Vila Nova", cidade="Campinas/SP", cep="13000-000",
                verba=reais("480.000,00"), uf="SP", tipologia="casa", padrao="normal", area_construida=qtd("180"),
                # dados fiscais para a aba Tributos
                # o VAU vem da tabela do SERO (UF + destinação); nada à mão
                area_complementar=qtd("30"), proprietario="pf", material="alvenaria", finalidade="uso_proprio")
    return {
        "tenants": [doc(tenant)],
        "obras": [doc(obra)],
    }


async def semear(arm, cfg: Config, *, senha_admin: str, tenant_id: str = TENANT_EXEMPLO,
                 email: str | None = None) -> dict | None:
    """Devolve os documentos criados, ou None se o exemplo já estava lá."""
    if await arm.obter("tenants", tenant_id, tenant_id):
        return None
    colecoes = documentos(cfg, tenant_id)

    async def corpo(tx):
        for colecao, docs in colecoes.items():
            for d in docs:
                tx.criar(colecao, d)

    await arm.em_transacao(corpo)
    await criar_usuario(arm, tenant_id=tenant_id, nome="Admin da Vila Nova", email=email or cfg.seed_admin_email,
                        senha=senha_admin, papel=Papel.ADMIN)
    return colecoes


async def principal() -> int:
    cfg = Config()
    if len(cfg.seed_admin_senha) < 12:
        print("Defina SEED_ADMIN_SENHA (12+ caracteres) no .env local antes de semear.", file=sys.stderr)
        return 1
    arm = criar_armazenamento(cfg.armazenamento, mongo_url=cfg.mongo_url, mongo_db=cfg.mongo_db)
    try:
        if hasattr(arm, "garantir_indices"):
            await arm.garantir_indices()
        if await semear(arm, cfg, senha_admin=cfg.seed_admin_senha):
            print(f"Casa Vila Nova semeada. Entre com {cfg.seed_admin_email} e a senha do .env.")
        else:
            print("O exemplo já existe; nada a fazer.")
        # a tabela VAU é pública e idempotente: vai sempre, para a obra de exemplo já sair com o VAU do SERO
        linhas, fonte = vau.carregar(vau.DADOS)
        r = await vau.importar(arm, linhas, fonte)
        print(f"Tabela VAU: {r['linhas']} linhas ({r['novas']} novas).")
        meses, fonte = selic.carregar(selic.DADOS)
        r = await selic.importar(arm, meses, fonte)
        print(f"Selic: {r['meses']} meses ({r['novos']} novos), até {max(meses)}.")
    finally:
        await arm.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
