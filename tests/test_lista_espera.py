"""Lista de espera da landing: pública, idempotente pelo e-mail, com isca para robô, limite por IP e CORS restrito."""
import logging
import secrets
import uuid

import httpx

from canteiro_api.modelos import TENANT_SISTEMA, id_de_inscrito

ORIGEM_OK = "http://localhost:4321"


def _email() -> str:
    return f"pessoa-{uuid.uuid4().hex[:10]}@obra.exemplo"


async def _cliente(arm, config, **extra):
    from canteiro_api.main import create_app

    app = create_app(config.model_copy(update=extra), arm)
    ip = f"10.{secrets.randbelow(255)}.{secrets.randbelow(255)}.{secrets.randbelow(255)}"
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5555)), base_url="http://teste")


async def test_inscreve_e_grava_no_tenant_do_sistema(http, arm):
    email = _email()
    corpo = {"email": "  " + email.upper(), "nome": " Ana ", "empresa": "Construtora X", "papel": "engenheiro",
             "obras_ativas": 3, "origem": "utm_source=ig"}
    r = await http.post("/lista-espera", json=corpo)
    assert r.status_code == 201, r.text
    assert r.json() == {"ok": True, "ja_estava": False}
    d = await arm.obter("lista_espera", TENANT_SISTEMA, id_de_inscrito(email))
    assert d["email"] == email and d["nome"] == "Ana" and d["papel"] == "engenheiro" and d["obras_ativas"] == 3
    assert email not in d["id"]                          # o ID é hash, não carrega o e-mail


async def test_repetir_e_idempotente_e_nao_sobrescreve(http, arm):
    email = _email()
    assert (await http.post("/lista-espera", json={"email": email, "nome": "Ana"})).status_code == 201
    r = await http.post("/lista-espera", json={"email": email.upper(), "nome": "Outro Nome", "empresa": "Y"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "ja_estava": True}   # nada além disso: nem posição, nem dado guardado
    d = await arm.obter("lista_espera", TENANT_SISTEMA, id_de_inscrito(email))
    assert d["nome"] == "Ana" and d["empresa"] is None


async def test_corpo_invalido_da_422(http):
    for corpo in ({"email": "sem-arroba"}, {"email": "a@b"}, {"email": "a b@c.com"}, {},
                  {"email": _email(), "papel": "presidente"}, {"email": _email(), "obras_ativas": 1000},
                  {"email": _email(), "obras_ativas": -1}, {"email": _email(), "nome": "x" * 121},
                  {"email": "x" * 250 + "@a.com"}, {"email": _email(), "campo_estranho": 1}):
        r = await http.post("/lista-espera", json=corpo)
        assert r.status_code == 422, corpo


async def test_isca_preenchida_finge_sucesso_e_nao_grava(http, arm):
    email = _email()
    r = await http.post("/lista-espera", json={"email": email, "site": "http://spam.exemplo"})
    assert r.status_code == 201 and r.json() == {"ok": True, "ja_estava": False}
    assert await arm.obter("lista_espera", TENANT_SISTEMA, id_de_inscrito(email)) is None


async def test_limite_por_ip(arm, config):
    async with await _cliente(arm, config, lista_espera_por_ip=3) as c:
        for _ in range(3):
            assert (await c.post("/lista-espera", json={"email": _email()})).status_code == 201
        email = _email()
        r = await c.post("/lista-espera", json={"email": email})
        assert r.status_code == 429
        assert await arm.obter("lista_espera", TENANT_SISTEMA, id_de_inscrito(email)) is None
        # a isca também conta: robô não escapa do limite
        assert (await c.post("/lista-espera", json={"email": _email(), "site": "x"})).status_code == 429


async def test_cors_libera_so_origem_configurada(http):
    preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    ok = await http.options("/lista-espera", headers={"Origin": ORIGEM_OK, **preflight})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGEM_OK
    assert "access-control-allow-credentials" not in ok.headers

    negada = await http.options("/lista-espera", headers={"Origin": "https://malicioso.exemplo", **preflight})
    assert negada.status_code == 400 and "access-control-allow-origin" not in negada.headers

    r = await http.post("/lista-espera", json={"email": _email()}, headers={"Origin": ORIGEM_OK})
    assert r.headers["access-control-allow-origin"] == ORIGEM_OK
    r = await http.post("/lista-espera", json={"email": _email()}, headers={"Origin": "https://malicioso.exemplo"})
    assert "access-control-allow-origin" not in r.headers


async def test_cors_vem_da_config_e_nao_vaza_para_outras_rotas(arm, config):
    async with await _cliente(arm, config, cors_origens="https://plumo.vercel.app, *") as c:
        h = {"Origin": "https://plumo.vercel.app", "Access-Control-Request-Method": "POST"}
        assert (await c.options("/lista-espera", headers=h)).headers["access-control-allow-origin"] == h["Origin"]
        local = {**h, "Origin": ORIGEM_OK}                 # o padrão foi trocado pela env
        assert "access-control-allow-origin" not in (await c.options("/lista-espera", headers=local)).headers
        # "*" na lista é ignorado
        qualquer = {**h, "Origin": "https://qualquer.exemplo"}
        assert "access-control-allow-origin" not in (await c.options("/lista-espera", headers=qualquer)).headers
        # o resto da API não fala com navegador de outro domínio
        r = await c.post("/auth/entrar", json={"email": _email(), "senha": "x"}, headers={"Origin": h["Origin"]})
        assert "access-control-allow-origin" not in r.headers


async def test_email_nao_aparece_em_log(http, caplog):
    email = _email()
    with caplog.at_level(logging.DEBUG):
        await http.post("/lista-espera", json={"email": email})
        await http.post("/lista-espera", json={"email": email})
        await http.post("/lista-espera", json={"email": email, "papel": "invalido"})
    assert email not in caplog.text
