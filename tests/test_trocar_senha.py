"""Troca da própria senha: confere a atual no mesmo limite do login, derruba as outras sessões e devolve uma nova."""
import secrets

from canteiro_api.modelos import TENANT_SISTEMA


def _corpo(atual, nova):
    return {"senha_atual": atual, "senha_nova": nova}


async def _trocar(vila, atual, nova, h=None):
    return await vila.http.post("/auth/senha", json=_corpo(atual, nova), headers=h or vila.h)


async def test_troca_devolve_sessao_nova_que_funciona(vila):
    nova = secrets.token_urlsafe(16)
    r = await _trocar(vila, vila.senha, nova)
    assert r.status_code == 200, r.text
    s = r.json()
    assert set(s) == {"acesso", "renovacao", "usuario"} and s["usuario"] == vila.sessao["usuario"]
    h = {"Authorization": f"Bearer {s['acesso']}"}
    assert (await vila.http.get("/obras", headers=h)).status_code == 200
    assert (await vila.http.post("/auth/renovar", json={"renovacao": s["renovacao"]})).status_code == 200
    # a senha velha deixou de entrar; a nova entra
    assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})).status_code == 401
    assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": nova})).status_code == 200


async def test_troca_derruba_as_outras_sessoes(vila):
    outra = (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})).json()
    # a outra sessão já renovou uma vez: a ponta viva da família é a sucessora, não o token do login
    outra = (await vila.http.post("/auth/renovar", json={"renovacao": outra["renovacao"]})).json()
    r = await _trocar(vila, vila.senha, secrets.token_urlsafe(16))
    assert r.status_code == 200
    for token in (outra["renovacao"], vila.sessao["renovacao"]):
        assert (await vila.http.post("/auth/renovar", json={"renovacao": token})).status_code == 401
    # o acesso de 15 min das outras sessões também cai, sem esperar vencer
    for acesso in (outra["acesso"], vila.sessao["acesso"]):
        assert (await vila.http.get("/obras", headers={"Authorization": f"Bearer {acesso}"})).status_code == 401
    # e o banco diz o mesmo: nenhuma ponta viva de antes da troca ficou sem revogar
    import hashlib
    for token in (outra["renovacao"], vila.sessao["renovacao"]):
        d = await vila.arm.obter("renovacoes", TENANT_SISTEMA, hashlib.sha256(token.encode()).hexdigest())
        assert d["revogado"] or d["usado"]


async def test_renovacao_de_familia_antiga_cai_mesmo_sem_revogada(vila):
    """A garantia é a marca no usuário, não a revogação por consulta (que perderia uma renovação concorrente)."""
    import hashlib
    velho = vila.sessao["renovacao"]
    assert (await _trocar(vila, vila.senha, secrets.token_urlsafe(16))).status_code == 200
    await vila.arm.atualizar("renovacoes", TENANT_SISTEMA, hashlib.sha256(velho.encode()).hexdigest(),
                             {"revogado": False})
    assert (await vila.http.post("/auth/renovar", json={"renovacao": velho})).status_code == 401


async def test_senha_atual_errada_401_e_conta_no_limite(vila, config):
    for _ in range(config.tentativas_por_email):
        r = await _trocar(vila, "errada-" + secrets.token_hex(4), secrets.token_urlsafe(16))
        assert r.status_code == 401 and r.json() == {"detail": "Senha atual incorreta."}
        assert "www-authenticate" not in r.headers
    # estourou: nem a senha certa passa, e o login por e-mail também está bloqueado (mesmo contador)
    r = await _trocar(vila, vila.senha, secrets.token_urlsafe(16))
    assert r.status_code == 429
    assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})).status_code == 429
    # a sessão segue de pé: senha errada não desconecta ninguém
    assert (await vila.http.get("/obras", headers=vila.h)).status_code == 200


async def test_erros_do_login_contam_na_troca(vila, config):
    for _ in range(config.tentativas_por_email):
        await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": "x"})
    assert (await _trocar(vila, vila.senha, secrets.token_urlsafe(16))).status_code == 429


async def test_troca_certa_zera_o_contador(vila, config):
    for _ in range(config.tentativas_por_email - 1):
        await _trocar(vila, "errada", secrets.token_urlsafe(16))
    nova = secrets.token_urlsafe(16)
    r = await _trocar(vila, vila.senha, nova)
    assert r.status_code == 200
    h = {"Authorization": f"Bearer {r.json()['acesso']}"}
    assert (await _trocar(vila, "errada", secrets.token_urlsafe(16), h)).status_code == 401   # não 429


async def test_regras_da_senha_nova(vila):
    casos = [
        ("curta", "A nova senha precisa ter de 8 a 256 caracteres."),
        ("x" * 257, "A nova senha precisa ter de 8 a 256 caracteres."),
        (vila.senha, "A nova senha precisa ser diferente da atual."),
        (" " + vila.email.upper(), "A nova senha não pode ser o seu e-mail."),
    ]
    for nova, msg in casos:
        r = await _trocar(vila, vila.senha, nova)
        assert r.status_code == 422 and r.json() == {"detail": msg}, (nova, r.text)
    r = await _trocar(vila, "", secrets.token_urlsafe(16))
    assert r.status_code == 422 and r.json() == {"detail": "Informe a senha atual."}
    # nada mudou: a senha de antes continua entrando
    assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})).status_code == 200


async def test_regra_violada_nao_gasta_tentativa(vila, config):
    for _ in range(config.tentativas_por_email + 2):
        assert (await _trocar(vila, vila.senha, "curta")).status_code == 422
    assert (await _trocar(vila, vila.senha, secrets.token_urlsafe(16))).status_code == 200


async def test_exige_sessao(vila):
    r = await vila.http.post("/auth/senha", json=_corpo(vila.senha, secrets.token_urlsafe(16)))
    assert r.status_code == 401


async def test_qualquer_papel_troca_e_nao_derruba_os_outros_usuarios(vila):
    from canteiro_api.auth import criar_usuario
    from canteiro_api.modelos import Papel
    email, senha = f"v-{secrets.token_hex(4)}@teste.exemplo", secrets.token_urlsafe(16)
    await criar_usuario(vila.arm, tenant_id=vila.tenant_id, nome="Leitor", email=email, senha=senha,
                        papel=Papel.VIEWER)
    s = (await vila.http.post("/auth/entrar", json={"email": email, "senha": senha})).json()
    r = await _trocar(vila, senha, secrets.token_urlsafe(16), {"Authorization": f"Bearer {s['acesso']}"})
    assert r.status_code == 200 and r.json()["usuario"]["papel"] == "viewer"
    assert (await vila.http.get("/obras", headers=vila.h)).status_code == 200
    assert (await vila.http.post("/auth/renovar", json={"renovacao": vila.sessao["renovacao"]})).status_code == 200


async def test_registro_sem_desfazer_e_sem_segredo(vila):
    nova = "Nova-" + secrets.token_urlsafe(16)
    r = await _trocar(vila, vila.senha, nova)
    assert r.status_code == 200
    h = {"Authorization": f"Bearer {r.json()['acesso']}"}
    entradas = (await vila.http.get("/registro", headers=h)).json()
    e = next(x for x in entradas if x["texto"] == "Trocou a própria senha")
    assert e["desfazer"] is None and e["obra_id"] is None
    u = await vila.arm.obter("usuarios", vila.tenant_id, vila.sessao["usuario"]["id"])
    bruto = repr(e) + repr(entradas)
    for segredo in (vila.senha, nova, u["senha_hash"]):
        assert segredo not in bruto
    assert u["senha_hash"].startswith("$argon2id$") and nova not in u["senha_hash"]
