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
coisas são métodos de manutenção dos adaptadores (`tenants_da_colecao`, `apagar_ids`), fora da porta, e só este
script os usa.

Depois do `--apagar-do-banco` não há volta para ARQUIVOS=banco sem copiar no sentido contrário: o comprovante migrado
só existe no bucket.
"""
import argparse
import asyncio
import hashlib
from dataclasses import dataclass, field

from canteiro_dominio.armazenamento import Consulta

from .armazenamento import criar_armazenamento
from .arquivos import COLECAO_INDICE, COLECAO_PEDACOS, ArquivosNoBanco, ArquivosSupabase, ErroArquivos, _id_arquivo
from .config import Config


@dataclass
class Relatorio:
    copiados: int = 0          # conferidos no bucket (agora ou numa rodada anterior)
    apagados: int = 0          # tirados do banco (só com --apagar-do-banco)
    bytes: int = 0
    problemas: list[str] = field(default_factory=list)


async def _um(arm, origem, destino, tenant: str, indice: dict, apagar_do_banco: bool, rel: Relatorio) -> str | None:
    """Copia, confere e (se pedido) apaga um arquivo. Devolve o problema, ou None se deu certo."""
    caminho = indice["caminho"]
    conteudo = await origem.ler(tenant, caminho)
    if conteudo is None:
        # índice sem pedaços inteiros: ou o banco estragou, ou uma rodada com --apagar-do-banco parou no meio (os
        # pedaços vão antes do índice). Se o bucket tem o arquivo certo, é o segundo caso e dá para terminar.
        no_bucket = await destino.ler(tenant, caminho)
        if no_bucket is None or hashlib.sha256(no_bucket).hexdigest() != indice["sha256"]:
            return "no banco está incompleto ou não confere; ficou lá"
        conteudo = no_bucket
    else:
        await destino.salvar(tenant, caminho, conteudo)
        volta = await destino.ler(tenant, caminho)
        if volta is None or hashlib.sha256(volta).hexdigest() != indice["sha256"]:
            # no bucket já havia outro conteúdo com esse nome: não sobrescreve, olhe à mão
            return "no bucket não confere com o banco; ficou no banco"
    rel.copiados += 1
    rel.bytes += len(conteudo)
    if apagar_do_banco:
        id_ = _id_arquivo(tenant, caminho)
        # os pedaços primeiro e o índice por último: se parar no meio, o índice fica e a próxima rodada termina
        await arm.apagar_ids(COLECAO_PEDACOS, tenant, [f"{id_}-{n:03d}" for n in range(indice["pedacos"])])
        await arm.apagar_ids(COLECAO_INDICE, tenant, [id_])
        rel.apagados += 1
    return None


async def migrar(arm, destino: ArquivosSupabase, *, apagar_do_banco: bool = False, tenants=None,
                 avisar=print) -> Relatorio:
    origem = ArquivosNoBanco(arm)
    rel = Relatorio()
    for tenant in tenants if tenants is not None else await arm.tenants_da_colecao(COLECAO_INDICE):
        for indice in await arm.consultar(Consulta(COLECAO_INDICE, tenant)):
            onde = f"{tenant}/{indice['caminho']}"
            try:
                problema = await _um(arm, origem, destino, tenant, indice, apagar_do_banco, rel)
            except ErroArquivos as e:
                problema = f"{e} Rode de novo."     # Storage instável: segue com os outros, nada se perde
            if problema:
                rel.problemas.append(f"{onde}: {problema}")
            else:
                avisar(f"ok {onde}" + (" (apagado do banco)" if apagar_do_banco else ""))
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
