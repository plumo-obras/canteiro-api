"""Fixtures comuns. Todo teste que recebe `arm` (ou `cliente`) roda contra a memória E contra o Mongo do compose.

Nada de segredo aqui: o segredo do JWT e as senhas são gerados na hora.
"""
import os
import secrets
import uuid

import pytest

from canteiro_api.armazenamento import ArmazenamentoMemoria

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017/?replicaSet=rs0&directConnection=true")
ADAPTADORES = ["memoria", "mongo"]


@pytest.fixture(scope="session")
async def _mongo_sessao():
    """Um banco descartável por execução; cada teste usa tenants e IDs novos, então não há limpeza entre testes."""

    from canteiro_api.armazenamento.mongo import ArmazenamentoMongo

    arm = ArmazenamentoMongo(MONGO_URL + "&serverSelectionTimeoutMS=2000", f"canteiro_teste_{uuid.uuid4().hex[:8]}")
    try:
        await arm.garantir_indices()
    except Exception as e:     # sem importar pymongo fora do adaptador
        await arm.fechar()
        if os.environ.get("EXIGIR_MONGO"):
            raise
        pytest.skip(f"Mongo do compose fora do ar ({e.__class__.__name__}); suba com docker compose.")
    yield arm
    await arm.apagar_banco()
    await arm.fechar()


@pytest.fixture(params=ADAPTADORES)
def arm(request):
    if request.param == "memoria":
        return ArmazenamentoMemoria()
    return request.getfixturevalue("_mongo_sessao")


@pytest.fixture
def tenant() -> str:
    return f"t-{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def config():
    import base64

    from canteiro_api.config import Config
    return Config(jwt_segredo=secrets.token_urlsafe(48), token_servico_agente=secrets.token_urlsafe(32),
                  chave_cifra=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), armazenamento="memoria",
                  _env_file=None)


# --- app + cliente HTTP (ASGI) ---

class Vila:
    """Um cliente novo com a Casa Vila Nova semeada e um admin logado."""

    def __init__(self, http, arm, tenant_id, docs, email, senha, sessao):
        self.http, self.arm, self.tenant_id, self.email, self.senha = http, arm, tenant_id, email, senha
        self.sessao = sessao
        self.h = {"Authorization": f"Bearer {sessao['acesso']}"}
        self.obra = docs["obras"][0]["id"]
        self.insumo = {i["nome"].split()[0].lower(): i["id"] for i in docs["insumos"]}   # bloco, aço, cimento, areia
        self.pedido = {p["numero"]: p["id"] for p in docs["pedidos"]}

    async def conta(self, chave):
        r = await self.http.get(f"/obras/{self.obra}/insumos", headers=self.h)
        assert r.status_code == 200, r.text
        return next(x["conta"] for x in r.json() if x["insumo"]["id"] == self.insumo[chave])

    async def usuario(self, papel, tenant_id=None):
        """Cria outro usuário e devolve os cabeçalhos dele já logado."""
        from canteiro_api.auth import criar_usuario
        from canteiro_api.modelos import Papel

        email, senha = f"{uuid.uuid4().hex[:8]}@teste.exemplo", secrets.token_urlsafe(16)
        u = await criar_usuario(self.arm, tenant_id=tenant_id or self.tenant_id, nome=f"Pessoa {papel}", email=email,
                                senha=senha, papel=Papel(papel))
        r = await self.http.post("/auth/entrar", json={"email": email, "senha": senha})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['acesso']}"}, u


@pytest.fixture
async def http(arm, config):
    import httpx

    from canteiro_api.main import create_app

    app = create_app(config, arm)
    ip = f"10.{secrets.randbelow(255)}.{secrets.randbelow(255)}.{secrets.randbelow(255)}"
    transporte = httpx.ASGITransport(app=app, client=(ip, 5555))
    async with httpx.AsyncClient(transport=transporte, base_url="http://teste") as c:
        yield c


async def nova_vila(http, arm, config) -> Vila:
    from canteiro_api.seed import semear

    tenant_id = f"t-{uuid.uuid4().hex[:12]}"
    email, senha = f"admin-{uuid.uuid4().hex[:8]}@teste.exemplo", secrets.token_urlsafe(16)
    docs = await semear(arm, config, senha_admin=senha, tenant_id=tenant_id, email=email)
    r = await http.post("/auth/entrar", json={"email": email, "senha": senha})
    assert r.status_code == 200, r.text
    return Vila(http, arm, tenant_id, docs, email, senha, r.json())


@pytest.fixture
async def vila(http, arm, config) -> Vila:
    return await nova_vila(http, arm, config)
