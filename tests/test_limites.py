"""Limites que protegem o banco e o servidor: vencimento (TTL) das coleções de passagem, corrida no limite de login,
tamanho do lote de mensagens e tamanho do corpo da requisição."""
import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta

from canteiro_api.auth import _ids_tentativa
from canteiro_api.limite_corpo import LIMITE_PADRAO, LIMITE_UPLOAD
from canteiro_api.modelos import RETENCAO_OUTBOX, TENANT_SISTEMA
from canteiro_api.servicos.sessoes import MAX_BYTES_LOTE
from canteiro_dominio.armazenamento import Consulta, Filtro

# --- TTL: todo documento das coleções de passagem sai com a data de vencimento (data, não texto) ---


async def test_colecoes_de_passagem_gravam_data_de_vencimento(vila, config):
    id_renovacao = hashlib.sha256(vila.sessao["renovacao"].encode()).hexdigest()
    r = await vila.arm.obter("renovacoes", TENANT_SISTEMA, id_renovacao)
    assert isinstance(r["expira_em"], datetime) and r["expira_em"] > r["criado_em"]

    assert (await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": "x"})).status_code == 401
    t = await vila.arm.obter("tentativas", TENANT_SISTEMA, _ids_tentativa(vila.email, "")[0][1])
    assert t["expira_em"] == t["inicio"] + timedelta(minutes=config.janela_tentativas_minutos)

    assert (await vila.http.post("/obras", json={"nome": "Obra do TTL", "chave_idempotencia": uuid.uuid4().hex},
                                 headers=vila.h)).status_code == 201
    eventos = await vila.arm.consultar(Consulta("outbox", vila.tenant_id, [Filtro("publicado", "==", False)]))
    assert eventos
    assert all(e["expira_em"] == e["criado_em"] + RETENCAO_OUTBOX for e in eventos)


# --- login: N tentativas em paralelo não furam o limite ---


async def test_tentativas_paralelas_respeitam_o_limite(vila, config):
    n = config.tentativas_por_email * 4
    rs = await asyncio.gather(*[vila.http.post("/auth/entrar", json={"email": vila.email, "senha": f"errada{i}"})
                                for i in range(n)])
    codigos = [r.status_code for r in rs]
    # cada uma que passou da reserva gastou um argon2; nenhuma além do limite
    assert codigos.count(401) == config.tentativas_por_email, codigos
    assert codigos.count(429) == n - config.tentativas_por_email
    r = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})
    assert r.status_code == 429


async def test_login_certo_nao_gasta_tentativa(vila, config):
    # a reserva conta antes da senha; o acerto devolve a vaga do IP e zera a do e-mail, como antes da reserva existir
    # (passar do limite do IP prova os dois: ele é o maior)
    for _ in range(max(config.tentativas_por_email, config.tentativas_por_ip) + 1):
        r = await vila.http.post("/auth/entrar", json={"email": vila.email, "senha": vila.senha})
        assert r.status_code == 200, r.text


# --- lote de mensagens: o commit inteiro tem de caber no limite do Firestore ---


async def test_lote_de_mensagens_grande_demais_e_recusado(vila):
    s = (await vila.http.post("/sessoes", json={"titulo": "grande"}, headers=vila.h)).json()
    parte = 750_000                                   # cada uma abaixo do limite por mensagem
    qtd = MAX_BYTES_LOTE // parte + 1                 # juntas, acima do limite do lote
    corpo = {"seq_inicial": 0, "mensagens": [{"papel": "user", "conteudo": "a" * parte} for _ in range(qtd)]}
    r = await vila.http.post(f"/sessoes/{s['id']}/mensagens", json=corpo, headers=vila.h)
    assert r.status_code == 413, r.text
    assert "divida" in r.json()["detail"]
    s2 = (await vila.http.get(f"/sessoes/{s['id']}", headers=vila.h)).json()
    assert s2["total_mensagens"] == 0

    corpo["mensagens"] = corpo["mensagens"][: qtd - 1]
    r = await vila.http.post(f"/sessoes/{s['id']}/mensagens", json=corpo, headers=vila.h)
    assert r.status_code == 201, r.text


# --- tamanho do corpo, antes de autenticar ---


async def test_corpo_grande_recusado_pelo_content_length_antes_do_login(http):
    r = await http.post("/obras/x/orcamento/ler", content=b"0" * (LIMITE_UPLOAD + 1),
                        headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413          # e não 401: nem chegou a ler o corpo
    r = await http.post("/auth/entrar", content=b"{" + b" " * LIMITE_PADRAO + b"}",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 413


async def test_upload_aceita_mais_que_o_limite_padrao(vila):
    # 2 MiB passa do limite geral mas cabe no de upload: chega na rota (e a planilha inválida dá 422, não 413)
    r = await vila.http.post(f"/obras/{vila.obra}/orcamento/ler", headers=vila.h,
                             files={"arquivo": ("orcamento.csv", b"a;b\n" + b"1;2\n" * (LIMITE_PADRAO // 2))})
    assert r.status_code != 413, r.text


async def test_corpo_em_partes_sem_content_length_para_de_ser_lido(http):
    enviados = 0
    pedaco = b"0" * 65536

    async def fluxo():
        nonlocal enviados
        # começa como um multipart válido: o parser segue lendo o arquivo em vez de recusar o formato logo de cara
        yield b'--x\r\nContent-Disposition: form-data; name="arquivo"; filename="a.csv"\r\n\r\n'
        for _ in range(1000):             # 62,5 MiB se alguém lesse tudo
            enviados += 1
            yield pedaco

    r = await http.post("/obras/x/orcamento/ler", content=fluxo(),
                        headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
    # parou logo depois do limite de upload, sem consumir o resto
    assert enviados * len(pedaco) <= LIMITE_UPLOAD + 2 * len(pedaco)

    enviados = 0
    r = await http.post("/lista-espera", content=fluxo(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert enviados * len(pedaco) <= LIMITE_PADRAO + 2 * len(pedaco)
