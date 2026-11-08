"""Copia os comprovantes do banco (ARQUIVOS=banco, pedaços no Mongo) para o bucket do Supabase (ARQUIVOS=supabase).

    python -m canteiro_api.migrar_arquivos                    # copia e confere; não apaga nada
    python -m canteiro_api.migrar_arquivos --apagar-do-banco  # depois da troca: tira do Mongo o que já está no bucket

Ordem da troca (docs/deploy.md, seção 9): copiar → trocar ARQUIVOS para supabase no Render → copiar de novo (pega o
que chegou no meio) → só então `--apagar-do-banco`. Rodar de novo é seguro: o bucket não sobrescreve, e o que já está
lá conta como copiado depois de conferido.

Cada arquivo só conta como copiado depois de lido DE VOLTA do bucket com o mesmo sha256 do índice; e só esse sai do
banco. O que não confere fica onde está e aparece no relatório. A saída leva caminhos (IDs nossos) e contagens: nunca
a chave nem o conteúdo.

Listar os clientes atravessa tenants, o que a porta de armazenamento proíbe de propósito; e ela não apaga. As duas
coisas ficam aqui, só para os dois adaptadores que existem, e só neste script de manutenção.
"""
import argparse
import asyncio
import hashlib
from dataclasses import dataclass, field

from canteiro_dominio.armazenamento import Consulta

from .armazenamento import criar_armazenamento
from .arquivos import COLECAO_INDICE, COLECAO_PEDACOS, ArquivosNoBanco, ArquivosSupabase, _id_arquivo
from .config import Config


@dataclass
class Relatorio:
    copiados: int = 0          # conferidos no bucket (agora ou numa rodada anterior)
    apagados: int = 0          # tirados do banco (só com --apagar-do-banco)
    bytes: int = 0
    problemas: list[str] = field(default_factory=list)


async def _tenants(arm) -> list[str]:
    if hasattr(arm, "_db"):
        return sorted(await arm._db[COLECAO_INDICE].distinct("tenant_id"))
    return sorted({d["tenant_id"] for (col, _), d in arm._docs.items() if col == COLECAO_INDICE})


async def _apagar(arm, colecao: str, tenant_id: str, ids: list[str]) -> None:
    if hasattr(arm, "_db"):
        await arm._db[colecao].delete_many({"_id": {"$in": ids}, "tenant_id": tenant_id})
        return
    for id_ in ids:
        if (d := arm._docs.get((colecao, id_))) and d["tenant_id"] == tenant_id:
            del arm._docs[(colecao, id_)]


async def migrar(arm, destino: ArquivosSupabase, *, apagar_do_banco: bool = False, tenants=None,
                 avisar=print) -> Relatorio:
    origem = ArquivosNoBanco(arm)
    rel = Relatorio()
    for tenant in tenants if tenants is not None else await _tenants(arm):
        for indice in await arm.consultar(Consulta(COLECAO_INDICE, tenant)):
            caminho = indice["caminho"]
            conteudo = await origem.ler(tenant, caminho)
            if conteudo is None:
                rel.problemas.append(f"{tenant}/{caminho}: no banco está incompleto ou não confere; ficou lá")
                continue
            await destino.salvar(tenant, caminho, conteudo)
            volta = await destino.ler(tenant, caminho)
            if volta is None or hashlib.sha256(volta).hexdigest() != indice["sha256"]:
                # no bucket já havia outro conteúdo com esse nome: não sobrescreve, olhe à mão
                rel.problemas.append(f"{tenant}/{caminho}: no bucket não confere com o banco; ficou no banco")
                continue
            rel.copiados += 1
            rel.bytes += len(conteudo)
            if apagar_do_banco:
                id_ = _id_arquivo(tenant, caminho)
                # o índice primeiro: sem ele o arquivo não existe no banco, nunca pela metade
                await _apagar(arm, COLECAO_INDICE, tenant, [id_])
                await _apagar(arm, COLECAO_PEDACOS, tenant, [f"{id_}-{n:03d}" for n in range(indice["pedacos"])])
                rel.apagados += 1
            avisar(f"ok {tenant}/{caminho}" + (" (apagado do banco)" if apagar_do_banco else ""))
    return rel


async def _principal(apagar_do_banco: bool) -> int:
    config = Config()
    if not (config.supabase_url.startswith("https://") and config.supabase_chave_servico):
        print("Defina SUPABASE_URL (https) e SUPABASE_CHAVE_SERVICO no ambiente.")
        return 2
    arm = criar_armazenamento("mongo", mongo_url=config.mongo_url, mongo_db=config.mongo_db)
    destino = ArquivosSupabase(config.supabase_url, config.supabase_chave_servico, config.supabase_bucket)
    try:
        await destino.conferir()
        rel = await migrar(arm, destino, apagar_do_banco=apagar_do_banco)
    finally:
        await arm.fechar()
    print(f"\n{rel.copiados} arquivo(s) conferido(s) no bucket ({rel.bytes / 1_048_576:.1f} MiB), "
          f"{rel.apagados} apagado(s) do banco, {len(rel.problemas)} problema(s).")
    for p in rel.problemas:
        print(f"  - {p}")
    return 1 if rel.problemas else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apagar-do-banco", action="store_true",
                   help="tira do Mongo o que já está conferido no bucket (só depois de trocar ARQUIVOS no Render)")
    raise SystemExit(asyncio.run(_principal(p.parse_args().apagar_do_banco)))


if __name__ == "__main__":
    main()
