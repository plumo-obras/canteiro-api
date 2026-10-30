"""Login próprio, renovação com troca, limite de tentativas e papel lido do banco a cada requisição."""
from datetime import timedelta

import jwt

from canteiro_api.modelos import TENANT_SISTEMA
from canteiro_dominio.modelos import agora


async def test_entrar_devolve_par_e_usuario(vila):
    s = vila.sessao
    assert s["usuario"]["papel"] == "admin" and s["usuario"]["tenant_id"] == vila.tenant_id
    claims = jwt.decode(s["acesso"], options={"verify_signature": False})
    assert "papel" not in claims                       # o papel nunca vai no token
    assert claims["exp"] - claims["iat"] <= 15 * 60


async def test_email_e_normalizado(vila):
    r = await vila.http.post("/auth/entrar", json={"email": "  " + vila.email.upper(), "senha": vila.senha})
    assert r.status_code == 200


async def test_senha_errada_e_email_inexistente_dao_a_mesma_resposta(vila):
    a = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": "errada"})
    b = await vila.http.post("/auth/entrar", json={"email": "ninguem@teste.exemplo", "senha": "errada"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


async def test_senha_guardada_com_argon2(vila):
    u = await vila.arm.obter("usuarios", vila.tenant_id, vila.sessao["usuario"]["id"])
    assert u["senha_hash"].startswith("$argon2id$") and vila.senha not in u["senha_hash"]


async def test_limite_de_tentativas_por_email(vila, config):
    for _ in range(config.tentativas_por_email):
        assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": "x"})).status_code == 401
    r = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})
    assert r.status_code == 429


async def test_limite_de_tentativas_por_ip(vila, config):
    for i in range(config.tentativas_por_ip):
        await vila.http.post("/auth/entrar", json={"email": f"chute{i}@teste.exemplo", "senha": "x"})
    r = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})
    assert r.status_code == 429


async def test_janela_de_tentativas_expira(vila, config):
    for _ in range(config.tentativas_por_email):
        await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": "x"})
    from canteiro_api.auth import _ids_tentativa
    id_email = _ids_tentativa(vila.email, "")[0][1]
    antigo = agora() - timedelta(minutes=config.janela_tentativas_minutos + 1)
    await vila.arm.atualizar("tentativas", TENANT_SISTEMA, id_email, {"inicio": antigo})
    r = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})
    assert r.status_code == 200


async def test_renovar_troca_o_token_e_reuso_derruba_a_familia(vila):
    velho = vila.sessao["renovacao"]
    r = await vila.http.post("/auth/renovar", json={"renovacao": velho})
    assert r.status_code == 200
    novo = r.json()["renovacao"]
    assert r.json()["usuario"] == vila.sessao["usuario"]
    assert novo != velho
    # o token velho reaparece (vazou?): recusa e revoga também o novo
    assert (await vila.http.post("/auth/renovar", json={"renovacao": velho})).status_code == 401
    assert (await vila.http.post("/auth/renovar", json={"renovacao": novo})).status_code == 401


async def test_renovacao_guardada_so_como_hash(vila):
    import hashlib
    token = vila.sessao["renovacao"]
    assert await vila.arm.obter("renovacoes", TENANT_SISTEMA, token) is None
    assert await vila.arm.obter("renovacoes", TENANT_SISTEMA, hashlib.sha256(token.encode()).hexdigest())


async def test_sair_revoga(vila):
    assert (await vila.http.post("/auth/sair", json={"renovacao": vila.sessao["renovacao"]})).status_code == 204
    assert (await vila.http.post("/auth/renovar", json={"renovacao": vila.sessao["renovacao"]})).status_code == 401


async def test_rota_sem_token_ou_token_invalido(vila, config):
    assert (await vila.http.get("/obras")).status_code == 401
    forjado = jwt.encode({"sub": "x", "tid": vila.tenant_id, "exp": 9999999999, "typ": "acesso"}, "outro-segredo" * 4)
    assert (await vila.http.get("/obras", headers={"Authorization": f"Bearer {forjado}"})).status_code == 401
    vencido = jwt.encode({"sub": vila.sessao["usuario"]["id"], "tid": vila.tenant_id, "typ": "acesso",
                          "exp": agora() - timedelta(minutes=1)}, config.jwt_segredo)
    assert (await vila.http.get("/obras", headers={"Authorization": f"Bearer {vencido}"})).status_code == 401


async def test_papel_vem_do_banco_a_cada_requisicao(vila):
    corpo = {"insumo_id": vila.insumo["areia"], "tipo": "entrada", "quantidade": 1000,
             "chave_idempotencia": "papel-teste-1"}
    uid = vila.sessao["usuario"]["id"]
    await vila.arm.atualizar("usuarios", vila.tenant_id, uid, {"papel": "viewer"})
    # mesmo token de admin, mas o banco diz viewer: vale o banco
    r = await vila.http.post(f"/obras/{vila.obra}/movimentos", json=corpo, headers=vila.h)
    assert r.status_code == 403
    assert (await vila.http.get("/obras", headers=vila.h)).status_code == 200
    await vila.arm.atualizar("usuarios", vila.tenant_id, uid, {"ativo": False})
    assert (await vila.http.get("/obras", headers=vila.h)).status_code == 401


async def test_um_usuario_por_email(vila):
    import pytest

    from canteiro_api.auth import criar_usuario
    from canteiro_api.modelos import Papel
    from canteiro_dominio.armazenamento import ConflitoDeId
    with pytest.raises(ConflitoDeId):
        await criar_usuario(vila.arm, tenant_id="outro-tenant", nome="X", email=vila.email.upper(), senha="y" * 12,
                            papel=Papel.VIEWER)


async def test_openapi_exposto(http):
    r = await http.get("/openapi.json")
    assert r.status_code == 200
    assert "/obras/{obra_id}/insumos" in r.json()["paths"]


async def _tentativas(arm, config, proxies, cliente_ip, xff, n):
    import httpx

    from canteiro_api.main import create_app
    app = create_app(config.model_copy(update={"trusted_proxies": proxies}), arm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(cliente_ip, 1)),
                                 base_url="http://teste") as c:
        return [(await c.post("/auth/entrar", json={"email": f"x{i}@teste.exemplo", "senha": "x"},
                              headers={"X-Forwarded-For": xff() if callable(xff) else xff})).status_code
                for i in range(n)]


async def test_xff_de_qualquer_um_e_ignorado(arm, config):
    import itertools
    import secrets
    # sem proxy de confiança, trocar o X-Forwarded-For a cada tentativa não escapa do limite por IP
    ip = f"192.0.2.{secrets.randbelow(250)}"
    seq = itertools.count()
    r = await _tentativas(arm, config, "", ip, lambda: f"198.51.100.{next(seq)}", config.tentativas_por_ip + 1)
    assert r[-1] == 429


async def test_xff_vale_atras_de_proxy_confiavel(arm, config):
    import secrets
    proxy = f"10.200.{secrets.randbelow(250)}.{secrets.randbelow(250)}"
    # clientes diferentes atrás do Next: cada um tem o próprio limite
    a = await _tentativas(arm, config, "10.200.0.0/16", proxy, f"203.0.113.{secrets.randbelow(250)}",
                          config.tentativas_por_ip)
    b = await _tentativas(arm, config, "10.200.0.0/16", proxy, f"2001:db8::{secrets.randbelow(9999):x}", 1)
    assert set(a) == {401} and b == [401]
    # o que o cliente escreve à esquerda não conta: vale o último endereço que não é proxy nosso
    cliente = f"198.18.{secrets.randbelow(250)}.{secrets.randbelow(250)}"
    forjado = await _tentativas(arm, config, "10.200.0.0/16", proxy, f"1.2.3.4, {cliente}, {proxy}", 1)
    assert forjado == [401]


def test_ip_da_cadeia():
    from types import SimpleNamespace

    from canteiro_api.rotas.auth import _ip

    def req(cliente, xff, proxies="10.0.0.0/8", segredo="", headers=None):
        h = {"x-forwarded-for": xff, **(headers or {})}
        cfg = SimpleNamespace(trusted_proxies=proxies, proxy_segredo=segredo)
        return SimpleNamespace(client=SimpleNamespace(host=cliente), headers=h,
                               app=SimpleNamespace(state=SimpleNamespace(config=cfg)))

    assert _ip(req("10.0.0.5", "1.1.1.1, 2.2.2.2, 10.0.0.9")) == "2.2.2.2"
    assert _ip(req("8.8.8.8", "1.1.1.1")) == "8.8.8.8"                  # quem chamou não é proxy nosso
    assert _ip(req("10.0.0.5", "1.1.1.1", proxies="")) == "10.0.0.5"    # nada configurado
    assert _ip(req("10.0.0.5", "lixo")) == "10.0.0.5"
    assert _ip(req("10.0.0.5", "")) == "10.0.0.5"


def test_ip_informado_pelo_next_com_segredo():
    """Na Vercel não há IP fixo: o Next prova quem é pelo segredo e informa o IP do visitante."""
    from types import SimpleNamespace

    from canteiro_api.rotas.auth import _ip

    def req(headers, segredo="s3gredo-do-next"):
        cfg = SimpleNamespace(trusted_proxies="", proxy_segredo=segredo)
        return SimpleNamespace(client=SimpleNamespace(host="76.76.21.9"), headers=headers,
                               app=SimpleNamespace(state=SimpleNamespace(config=cfg)))

    ok = {"x-canteiro-proxy": "s3gredo-do-next", "x-canteiro-ip-cliente": "200.1.2.3"}
    assert _ip(req(ok)) == "200.1.2.3"
    assert _ip(req({**ok, "x-canteiro-proxy": "errado"})) == "76.76.21.9"        # segredo errado: IP da conexão
    assert _ip(req({"x-canteiro-ip-cliente": "200.1.2.3"})) == "76.76.21.9"      # sem segredo: ignora o cabeçalho
    assert _ip(req(ok, segredo="")) == "76.76.21.9"                               # desligado na API
    assert _ip(req({**ok, "x-canteiro-ip-cliente": "lixo"})) == "76.76.21.9"     # IP inválido não vale
