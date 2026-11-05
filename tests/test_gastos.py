"""Gastos da obra: árvore com etapas fixas, extras e tópicos; comprovante → leitura → revisão → gasto, com Desfazer."""
import secrets

from canteiro_api.servicos.gastos import hoje

JPEG = b"\xff\xd8\xff\xe0" + b"comprovante de teste "


def _foto() -> bytes:
    return JPEG + secrets.token_bytes(16)    # arquivo novo a cada teste (o mesmo arquivo é o mesmo comprovante)


def _agente(vila, config, h=None):
    return {"Authorization": f"Bearer {config.token_servico_agente}",
            "X-Canteiro-Usuario": (h or vila.h)["Authorization"]}


async def _enviar(vila, conteudo=None, tipo="image/jpeg", legenda="eletricista, quadro", h=None):
    return await vila.http.post(f"/obras/{vila.obra}/comprovantes", headers=h or vila.h,
                                files={"arquivo": ("pix.jpg", conteudo or _foto(), tipo)}, data={"legenda": legenda})


def _campo(certeza, *valores):
    return {"certeza": certeza, "opcoes": [{"valor": v, "motivo": "lido"} for v in valores]}


def _leitura(**kw):
    return {"tipo_documento": "comprovante_pix", "situacao_pagamento": "pago",
            "valor": _campo("alta", "350,00"), "data_pagamento": _campo("alta", hoje().isoformat()),
            "favorecido": _campo("alta", "João da Silva"), "cnpj_favorecido": None,
            "descricao": _campo("alta", "mão de obra do eletricista"),
            "classificacao": {"certeza": "alta", "opcoes": [{"etapa": "eletrica", "topico_id": None,
                                                             "motivo": "legenda"}]},
            "observacoes": None, **kw}


def _confirmacao(**kw):
    return {"valor": 35000, "data_pagamento": hoje().isoformat(), "favorecido": "João da Silva",
            "descricao": "mão de obra do eletricista", "etapa": "eletrica", **kw}


async def _desfazer(vila, registro_id):
    r = await vila.http.post(f"/registro/{registro_id}/desfazer", headers=vila.h)
    assert r.status_code == 200, r.text
    return r.json()


# --- árvore ---

async def test_toda_obra_nasce_com_as_etapas_fixas(vila):
    r = await vila.http.get(f"/obras/{vila.obra}/arvore", headers=vila.h)
    assert r.status_code == 200, r.text
    a = r.json()
    assert len(a) == 24 and a[0]["chave"] == "terreno" and a[-1]["chave"] == "outros"
    assert all(e["fixa"] and e["topicos"] == [] for e in a)


async def test_etapa_extra_e_topico_entram_na_arvore_com_desfazer(vila):
    h, obra = vila.h, vila.obra
    r = await vila.http.post(f"/obras/{obra}/etapas-extras", headers=h, json={"nome": " Muro  de arrimo "})
    assert r.status_code == 201, r.text
    extra = r.json()
    assert extra["nome"] == "Muro de arrimo" and extra["chave"] == f"x-{extra['id']}" and extra["registro_id"]
    r = await vila.http.post(f"/obras/{obra}/topicos", headers=h, json={"etapa": extra["chave"], "nome": "Mão de obra"})
    assert r.status_code == 201, r.text
    r = await vila.http.post(f"/obras/{obra}/topicos", headers=h, json={"etapa": "eletrica", "nome": "Quadro geral"})
    topico = r.json()

    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=h)).json()
    assert [e["chave"] for e in a][-2:] == [extra["chave"], "outros"]
    assert next(e for e in a if e["chave"] == "eletrica")["topicos"] == [{"id": topico["id"], "nome": "Quadro geral"}]

    # nome repetido (sem diferenciar acento e maiúscula) e etapa que não existe
    assert (await vila.http.post(f"/obras/{obra}/etapas-extras", headers=h,
                                 json={"nome": "instalacoes ELETRICAS"})).status_code == 422
    assert (await vila.http.post(f"/obras/{obra}/topicos", headers=h,
                                 json={"etapa": "marcenaria", "nome": "X"})).status_code == 422

    # desfazer a criação arquiva; refazer reativa
    desfeito = await _desfazer(vila, topico["registro_id"])
    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=h)).json()
    assert next(e for e in a if e["chave"] == "eletrica")["topicos"] == []
    await _desfazer(vila, desfeito["id"])
    a = (await vila.http.get(f"/obras/{obra}/arvore", headers=h)).json()
    assert len(next(e for e in a if e["chave"] == "eletrica")["topicos"]) == 1


async def test_renomear_e_arquivar_topico(vila):
    h = vila.h
    t = (await vila.http.post(f"/obras/{vila.obra}/topicos", headers=h,
                              json={"etapa": "pisos", "nome": "Térreo"})).json()
    r = await vila.http.patch(f"/topicos/{t['id']}", headers=h, json={"nome": "Térreo e garagem"})
    assert r.status_code == 200 and r.json()["nome"] == "Térreo e garagem"
    r = await vila.http.patch(f"/topicos/{t['id']}", headers=h, json={"arquivado": True})
    assert r.status_code == 200 and r.json()["arquivado"] is True
    assert (await vila.http.patch(f"/topicos/{t['id']}", headers=h, json={"arquivado": True})).status_code == 422
    await _desfazer(vila, r.json()["registro_id"])
    a = (await vila.http.get(f"/obras/{vila.obra}/arvore", headers=h)).json()
    assert next(e for e in a if e["chave"] == "pisos")["topicos"][0]["nome"] == "Térreo e garagem"


async def test_viewer_ve_a_arvore_mas_nao_cria(vila):
    hv, _ = await vila.usuario("viewer")
    assert (await vila.http.get(f"/obras/{vila.obra}/arvore", headers=hv)).status_code == 200
    assert (await vila.http.post(f"/obras/{vila.obra}/topicos", headers=hv,
                                 json={"etapa": "pisos", "nome": "X"})).status_code == 403


# --- comprovante: envio, arquivo e leitura ---

async def test_envio_fica_a_revisar_e_o_mesmo_arquivo_nao_duplica(vila):
    foto = _foto()
    r = await _enviar(vila, foto, legenda="  empreiteiro,   alvenaria ")
    assert r.status_code == 201, r.text
    c = r.json()
    assert (c["status"], c["legenda"], c["tipo_conteudo"], c["ja_existia"]) == (
        "a_revisar", "empreiteiro, alvenaria", "image/jpeg", False)
    de_novo = await _enviar(vila, foto)
    assert de_novo.status_code == 200 and de_novo.json()["id"] == c["id"] and de_novo.json()["ja_existia"]

    r = await vila.http.get(f"/comprovantes/{c['id']}/arquivo", headers=vila.h)
    assert r.status_code == 200 and r.content == foto
    assert r.headers["content-type"] == "image/jpeg" and "no-store" in r.headers["cache-control"]

    fila = (await vila.http.get(f"/obras/{vila.obra}/comprovantes?status=a_revisar", headers=vila.h)).json()
    assert [x["id"] for x in fila] == [c["id"]]


async def test_formato_e_tamanho_do_arquivo(vila):
    assert (await _enviar(vila, b"oi", tipo="text/plain")).status_code == 422
    assert (await _enviar(vila, b"x" * (5 * 1024 * 1024 + 1))).status_code == 422
    assert (await _enviar(vila, b"%PDF-1.7 teste", tipo="application/pdf")).status_code == 201


async def test_comprovante_de_outro_cliente_nao_aparece(vila, http, arm, config):
    from conftest import nova_vila

    c = (await _enviar(vila)).json()
    outra = await nova_vila(http, arm, config)
    assert (await http.get(f"/comprovantes/{c['id']}", headers=outra.h)).status_code == 404
    assert (await http.get(f"/comprovantes/{c['id']}/arquivo", headers=outra.h)).status_code == 404


async def test_qualquer_papel_envia_mas_so_admin_confirma(vila):
    hv, _ = await vila.usuario("viewer")
    r = await _enviar(vila, h=hv)
    assert r.status_code == 201, r.text
    c = r.json()
    assert (await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=hv,
                                 json=_confirmacao())).status_code == 403


async def test_sem_leitura_a_revisao_abre_em_branco(vila):
    c = (await _enviar(vila)).json()
    rev = (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["revisao"]
    assert rev["valor"]["estado"] == "vazio" and rev["classificacao"]["estado"] == "vazio"


async def test_leitura_so_pelo_servico_do_agente(vila, config):
    c = (await _enviar(vila)).json()
    url = f"/interno/comprovantes/{c['id']}/leitura"
    assert (await vila.http.put(url, headers=vila.h, json={"leitura": _leitura()})).status_code == 401
    r = await vila.http.put(url, headers=_agente(vila, config), json={"leitura": _leitura(), "modelo": "claude-x"})
    assert r.status_code == 200, r.text

    rev = (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["revisao"]
    assert rev["valor"] == {"estado": "lido", "escolhido": 0,
                            "opcoes": [{"valor": 35000, "rotulo": "R$ 350,00", "motivo": "lido",
                                        "fornecedor_id": None}]}
    assert rev["classificacao"]["opcoes"][0]["rotulo"] == "Instalações elétricas"


async def test_leitura_que_falha_vira_aviso_e_nao_apaga_a_anterior(vila, config):
    c = (await _enviar(vila)).json()
    url = f"/interno/comprovantes/{c['id']}/leitura"
    h = _agente(vila, config)
    erro = "A leitura automática falhou agora. Preencha à mão ou tente ler de novo."
    assert (await vila.http.put(url, headers=h, json={"erro": erro})).status_code == 200
    rev = (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["revisao"]
    assert rev["avisos"] == [erro] and rev["valor"]["estado"] == "vazio"

    await vila.http.put(url, headers=h, json={"leitura": _leitura()})
    await vila.http.put(url, headers=h, json={"erro": erro})
    rev = (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["revisao"]
    assert rev["valor"]["estado"] == "lido"
    # leitura e erro juntos (ou nenhum dos dois) → 422
    assert (await vila.http.put(url, headers=h, json={})).status_code == 422


# --- confirmar, descartar e desfazer ---

async def test_confirmar_cria_gasto_que_soma_na_etapa_e_desfazer_volta_para_a_fila(vila):
    h, obra = vila.h, vila.obra
    t = (await vila.http.post(f"/obras/{obra}/topicos", headers=h, json={"etapa": "eletrica", "nome": "Quadro"})).json()
    c = (await _enviar(vila)).json()
    r = await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=h,
                             json=_confirmacao(favorecido=" João  da Silva ", topico_id=t["id"]))
    assert r.status_code == 201, r.text
    conf = r.json()
    assert conf["gasto"]["favorecido"] == "João da Silva" and conf["comprovante"]["status"] == "confirmado"

    resumo = (await vila.http.get(f"/obras/{obra}/gastos/resumo", headers=h)).json()
    assert resumo == {"total": 35000, "pagamentos_de_pedidos": 0, "a_revisar": 0, "etapas": [
        {"etapa": "eletrica", "nome": "Instalações elétricas", "total": 35000,
         "topicos": [{"topico_id": t["id"], "nome": "Quadro", "total": 35000}]}]}
    gastos = (await vila.http.get(f"/obras/{obra}/gastos?etapa=eletrica", headers=h)).json()
    assert [g["id"] for g in gastos] == [conf["gasto"]["id"]]
    entrada = next(e for e in (await vila.http.get(f"/registro?obra_id={obra}", headers=h)).json()
                   if e["id"] == conf["registro_id"])
    assert entrada["texto"] == ("Gasto de R$ 350,00 em Instalações elétricas › Quadro: mão de obra do eletricista "
                                "(João da Silva)")

    # confirmar de novo → 409
    assert (await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=h,
                                 json=_confirmacao())).status_code == 409

    await _desfazer(vila, conf["registro_id"])
    resumo = (await vila.http.get(f"/obras/{obra}/gastos/resumo", headers=h)).json()
    assert (resumo["total"], resumo["a_revisar"], resumo["etapas"]) == (0, 1, [])
    assert (await vila.http.get(f"/obras/{obra}/gastos", headers=h)).json() == []
    # voltou para a fila: dá para corrigir e confirmar de novo
    r = await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=h, json=_confirmacao(valor=34000))
    assert r.status_code == 201, r.text


async def test_gasto_de_pedido_nao_soma_no_realizado_e_marca_o_pedido(vila):
    h = vila.h
    pedido_id = vila.pedido["PED-0158"]       # aço em trânsito (semeado)
    c = (await _enviar(vila)).json()
    r = await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=h,
                             json=_confirmacao(etapa="estrutura", pedido_id=pedido_id))
    assert r.status_code == 201, r.text
    resumo = (await vila.http.get(f"/obras/{vila.obra}/gastos/resumo", headers=h)).json()
    assert (resumo["total"], resumo["pagamentos_de_pedidos"]) == (0, 35000)
    pedidos = (await vila.http.get(f"/obras/{vila.obra}/pedidos", headers=h)).json()
    assert next(p for p in pedidos if p["id"] == pedido_id)["pago"] == 35000

    await _desfazer(vila, r.json()["registro_id"])
    pedidos = (await vila.http.get(f"/obras/{vila.obra}/pedidos", headers=h)).json()
    assert next(p for p in pedidos if p["id"] == pedido_id)["pago"] == 0


async def test_pedido_em_cotacao_nao_recebe_pagamento(vila):
    c = (await _enviar(vila)).json()
    r = await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=vila.h,
                             json=_confirmacao(pedido_id=vila.pedido["PED-0167"]))
    assert r.status_code == 422 and "em cotação" in r.json()["detail"]


async def test_confirmacao_invalida_deixa_na_fila(vila):
    c = (await _enviar(vila)).json()
    url = f"/comprovantes/{c['id']}/confirmar"
    amanha = hoje().toordinal() + 1
    from datetime import date
    for corpo, trecho in [(_confirmacao(etapa="marcenaria"), "Escolha uma etapa"),
                          (_confirmacao(data_pagamento=date.fromordinal(amanha).isoformat()), "depois de hoje"),
                          (_confirmacao(descricao="  "), "o que foi pago"),
                          (_confirmacao(fornecedor_id="nao-existe"), "Fornecedor")]:
        r = await vila.http.post(url, headers=vila.h, json=corpo)
        assert r.status_code == 422 and trecho in r.json()["detail"], r.text
    assert (await vila.http.post(url, headers=vila.h, json=_confirmacao(valor=0))).status_code == 422
    assert (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["status"] == "a_revisar"


async def test_descartar_guarda_o_arquivo_e_desfazer_reabre(vila):
    foto = _foto()
    c = (await _enviar(vila, foto)).json()
    r = await vila.http.post(f"/comprovantes/{c['id']}/descartar", headers=vila.h, json={"motivo": "foto repetida"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "descartado" and r.json()["registro_id"]
    assert (await vila.http.get(f"/comprovantes/{c['id']}/arquivo", headers=vila.h)).content == foto
    assert (await vila.http.post(f"/comprovantes/{c['id']}/confirmar", headers=vila.h,
                                 json=_confirmacao())).status_code == 409
    await _desfazer(vila, r.json()["registro_id"])
    assert (await vila.http.get(f"/comprovantes/{c['id']}", headers=vila.h)).json()["status"] == "a_revisar"


# --- o arquivo pelo que os bytes dizem ---

def _png(largura, altura):
    import struct
    ihdr = b"IHDR" + struct.pack(">II", largura, altura) + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + ihdr


async def test_tipo_vem_dos_bytes_nao_do_que_o_cliente_diz(vila):
    pdf = b"%PDF-1.7 comprovante " + secrets.token_bytes(8)
    r = await _enviar(vila, pdf, tipo="image/jpeg")
    assert r.status_code == 201 and r.json()["tipo_conteudo"] == "application/pdf"
    disfarçado = await _enviar(vila, b"MZ\x90\x00 executavel" + secrets.token_bytes(8), tipo="image/png")
    assert disfarçado.status_code == 422


async def test_limites_por_tipo_e_imagem_comprida(vila):
    grande = JPEG + b"x" * 3_500_001
    assert "3,5 MB" in (await _enviar(vila, grande)).json()["detail"]
    pdf_grande = b"%PDF-1.7" + b"x" * (4 * 1024 * 1024)
    assert "4 MB" in (await _enviar(vila, pdf_grande, tipo="application/pdf")).json()["detail"]
    r = await _enviar(vila, _png(1080, 9000), tipo="image/png")
    assert r.status_code == 422 and "comprida" in r.json()["detail"]
    assert (await _enviar(vila, _png(1080, 7999) + secrets.token_bytes(8), tipo="image/png")).status_code == 201


async def test_corpo_acima_do_limite_da_rota_e_413(vila):
    r = await _enviar(vila, JPEG + b"x" * (7 * 1024 * 1024))
    assert r.status_code == 413


async def test_legenda_ate_300(vila):
    assert (await _enviar(vila, legenda="x" * 301)).status_code == 422
