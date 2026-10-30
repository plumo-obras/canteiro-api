"""Dados de exemplo: a Casa Vila Nova do protótipo.  Uso: `python -m canteiro_api.seed`

A senha do admin vem de SEED_ADMIN_SENHA no `.env` local; nunca de código (o repositório é público).
Rodar de novo não duplica nada: os IDs são fixos e o seed para se o cliente já existe.
"""
import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta

from canteiro_dominio.modelos import Insumo, Movimento, Obra, Pedido, Saldo, StatusPedido, TipoMovimento
from canteiro_dominio.regras import saldo_de
from canteiro_dominio.tipos import qtd, reais

from .armazenamento import criar_armazenamento
from .auth import criar_usuario
from .config import Config
from .modelos import Papel, Tenant, doc
from .servicos.pedidos import id_contador

_NS = uuid.UUID("0b6f5f0e-6a8d-4f53-9a37-2f1f6c1b9e21")


def _id(nome: str) -> str:
    return str(uuid.uuid5(_NS, nome))


TENANT_EXEMPLO = _id("tenant:construtora-exemplo")


def documentos(cfg: Config, t: str = TENANT_EXEMPLO) -> dict[str, list[dict]]:
    """IDs derivados do tenant: os testes semeiam um cliente novo por teste sem colidir no mesmo banco."""
    def _id(nome: str) -> str:
        return str(uuid.uuid5(_NS, f"{t}|{nome}"))

    o = _id("obra:casa-vila-nova")
    base = datetime(2026, 10, 1, 10, tzinfo=UTC)
    tenant = Tenant(id=t, nome="Construtora Exemplo", saldo_microdolares=cfg.franquia_padrao_microdolares,
                    franquia_microdolares=cfg.franquia_padrao_microdolares)
    obra = Obra(id=o, tenant_id=t, nome="Casa Vila Nova", cidade="Campinas/SP", cep="13000-000",
                verba=reais("480.000,00"))

    def insumo(chave, nome, unidade, precisa, curva):
        return Insumo(id=_id(f"insumo:{chave}"), tenant_id=t, obra_id=o, nome=nome, unidade=unidade,
                      precisa_ate_o_fim=qtd(precisa), curva=curva)

    bloco = insumo("bloco", "Bloco cerâmico 14x19x29", "un", "4.800,0", "A")
    aco = insumo("aco", "Aço CA-50 10 mm", "kg", "3.940,0", "A")
    cimento = insumo("cimento", "Cimento CP-II 50 kg", "sc", 200, "A")
    areia = insumo("areia", "Areia média", "m³", 60, "B")

    movs: list[Movimento] = []

    def mov(ins, tipo, q, dias, **kw):
        movs.append(Movimento(id=_id(f"mov:{ins.id}:{len(movs)}"), tenant_id=t, obra_id=o, insumo_id=ins.id,
                              tipo=tipo, quantidade=qtd(q), quem="seed", origem="sistema",
                              criado_em=base + timedelta(days=dias), **kw))

    # bloco: entrou 4.000, saiu 1.050 para a alvenaria e 190 de quebra = 2.760
    mov(bloco, TipoMovimento.ENTRADA, "4.000,0", 1, origem_doc="NF 4471")
    mov(bloco, TipoMovimento.SAIDA, "1.050,0", 8, destino="Alvenaria do térreo")
    mov(bloco, TipoMovimento.SAIDA, 190, 9, destino="Quebra no descarregamento")
    mov(aco, TipoMovimento.AJUSTE, 140, 2, origem_doc="contagem inicial")
    mov(cimento, TipoMovimento.AJUSTE, 46, 2, origem_doc="contagem inicial")
    mov(areia, TipoMovimento.AJUSTE, 12, 2, origem_doc="contagem inicial")

    insumos = [bloco, aco, cimento, areia]
    saldos = [Saldo(id=i.id, tenant_id=t, obra_id=o, saldo=saldo_de([m for m in movs if m.insumo_id == i.id]))
              for i in insumos]

    def pedido(numero, ins, q, fornecedor, valor, status, dias, **kw):
        return Pedido(id=_id(f"pedido:{numero}"), tenant_id=t, numero=numero, obra_id=o, insumo_id=ins.id,
                      quantidade=qtd(q), fornecedor=fornecedor, valor=reais(valor), status=status,
                      criado_em=base + timedelta(days=dias), **kw)

    pedidos = [
        pedido("PED-0157", areia, 40, "Areial Rio Claro", "4.800,00", StatusPedido.RECEBIDO, 10,
               previsao="2026-10-28"),
        pedido("PED-0158", aco, "3.200,0", "Aço Forte Distribuidora", "20.800,00", StatusPedido.EM_TRANSITO, 11,
               previsao="2026-11-03", rastreio="BR123456789"),
        pedido("PED-0160", cimento, 120, "Depósito São Jorge", "4.560,00", StatusPedido.ENVIADO, 12,
               previsao="2026-11-01"),
        pedido("PED-0167", aco, 600, "Aço Forte Distribuidora", "3.960,00", StatusPedido.EM_COTACAO, 20),
    ]
    return {
        "tenants": [doc(tenant)],
        "obras": [doc(obra)],
        "insumos": [doc(i) for i in insumos],
        "movimentos": [doc(m) for m in movs],
        "saldos": [doc(s) for s in saldos],
        "pedidos": [doc(p) for p in pedidos],
        "contadores": [{"id": id_contador(t), "tenant_id": t, "ultimo": 167}],
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
    finally:
        await arm.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(principal()))
