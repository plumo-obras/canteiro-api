"""Importação de CSV: separador, BOM, Windows-1252, número ambíguo e prévia antes de gravar."""
from canteiro_dominio.tipos import qtd


async def enviar(vila, conteudo: bytes, aplicar=False):
    return await vila.http.post(f"/obras/{vila.obra}/importar?aplicar={str(aplicar).lower()}", headers=vila.h,
                                files={"arquivo": ("insumos.csv", conteudo, "text/csv")})


async def test_previa_utf8_com_bom_e_ponto_e_virgula(vila):
    csv = "﻿nome;unidade;saldo;precisa_ate_o_fim;curva\nTijolo maciço;un;1.200,0;3000;a\nCal;sc;12,5;;\n"
    r = await enviar(vila, csv.encode("utf-8"))
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["aplicado"] is False and corpo["erros"] == []
    assert [(x["nome"], x["saldo"], x["precisa_ate_o_fim"], x["curva"]) for x in corpo["linhas"]] == [
        ("Tijolo maciço", qtd(1200), qtd(3000), "A"), ("Cal", qtd("12,5"), 0, "B")]
    assert len((await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()) == 4


async def test_windows_1252_e_virgula_como_separador(vila):
    csv = 'nome,unidade,saldo,precisa_ate_o_fim,curva\n"Argamassa AC-III",sc,"7,5",20,C\nPedra britada nº 1,m³,3,10,B\n'
    r = await enviar(vila, csv.encode("cp1252"))
    linhas = r.json()["linhas"]
    assert [x["nome"] for x in linhas] == ["Argamassa AC-III", "Pedra britada nº 1"]
    assert linhas[0]["saldo"] == qtd("7,5") and linhas[1]["unidade"] == "m³"


async def test_erros_por_linha(vila):
    csv = ("nome;unidade;saldo;precisa_ate_o_fim;curva\n"
           "Bloco cerâmico 14x19x29;un;10;10;A\n"     # já existe na obra
           "Prego;kg;2.760;10;B\n"                     # número ambíguo
           "Arame;;abc;10;D\n"                         # unidade vazia, número inválido, curva inválida
           "Telha;un;5;10;B\n")
    r = await enviar(vila, csv.encode())
    erros = r.json()["erros"]
    assert {(e["linha"], e["campo"]) for e in erros} == {(2, "nome"), (3, "saldo"), (4, "unidade"), (4, "saldo"),
                                                          (4, "curva")}
    assert "ambíguo" in next(e["mensagem"] for e in erros if e["linha"] == 3)
    assert [x["nome"] for x in r.json()["linhas"]] == ["Telha"]


async def test_cabecalho_errado(vila):
    r = await enviar(vila, b"item;qtd\nx;1\n")
    assert r.json()["erros"][0]["linha"] == 1 and "Faltam colunas" in r.json()["erros"][0]["mensagem"]


async def test_aplicar_com_erro_nao_grava_nada(vila):
    r = await enviar(vila, b"nome;unidade;saldo;precisa_ate_o_fim;curva\nOk;un;1;1;B\nRuim;un;2.760;1;B\n", True)
    assert r.json()["aplicado"] is False
    assert len((await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()) == 4


async def test_aplicar_cria_insumos_com_ajuste_inicial(vila):
    r = await enviar(vila, b"nome;unidade;saldo;precisa_ate_o_fim;curva\nTelha;un;150;400;B\nCal;sc;0;30;C\n"
, True)
    assert r.json()["aplicado"] is True
    itens = {x["insumo"]["nome"]: x for x in (await vila.http.get(f"/obras/{vila.obra}/insumos",
                                                                    headers=vila.h)).json()}
    assert itens["Telha"]["conta"]["saldo"] == qtd(150) and itens["Telha"]["conta"]["falta"] == qtd(250)
    assert itens["Cal"]["conta"]["saldo"] == 0
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{itens['Telha']['insumo']['id']}/movimentos",
                                headers=vila.h)).json()
    assert [(m["tipo"], m["quantidade"]) for m in movs] == [("ajuste", qtd(150))]
    # reimportar o mesmo arquivo vira erro de nome repetido, não duplicata
    r2 = await enviar(vila, b"nome;unidade;saldo;precisa_ate_o_fim;curva\nTelha;un;150;400;B\n", True)
    assert r2.json()["aplicado"] is False and r2.json()["erros"][0]["campo"] == "nome"


async def test_viewer_nao_importa(vila):
    h, _ = await vila.usuario("viewer")
    r = await vila.http.post(f"/obras/{vila.obra}/importar", headers=h,
                             files={"arquivo": ("a.csv", b"nome;unidade;saldo;precisa_ate_o_fim;curva\n", "text/csv")})
    assert r.status_code == 403


CAB = "nome;unidade;saldo;precisa_ate_o_fim;curva\n"


async def lista(vila):
    return {x["insumo"]["nome"]: x for x in
            (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()}


async def test_nome_igual_a_menos_de_espaco_e_caixa_e_o_mesmo_insumo(vila):
    # no mesmo arquivo: a segunda grafia é erro de linha, não um segundo insumo
    r = await enviar(vila, (CAB + "Areia  média fina;m³;1;1;B\nareia média FINA;m³;1;1;B\n").encode())
    assert [(e["linha"], e["campo"]) for e in r.json()["erros"]] == [(3, "nome")]
    # contra o que já está na obra (seed: "Areia média"), com espaço sobrando
    r = await enviar(vila, (CAB + " AREIA   média ;m³;1;1;B\n").encode())
    assert [(e["linha"], e["campo"]) for e in r.json()["erros"]] == [(2, "nome")]
    # o nome gravado sai com os espaços arrumados, e o ID é o mesmo do cadastro pela tela
    r = await enviar(vila, (CAB + "Pedra   rachão;m³;0;5;C\n").encode(), True)
    assert r.json()["aplicado"] is True
    pedra = (await lista(vila))["Pedra rachão"]["insumo"]
    dup = await vila.http.post(f"/obras/{vila.obra}/insumos", headers=vila.h,
                               json={"nome": "pedra rachão", "unidade": "m³"})
    assert dup.status_code == 409
    from canteiro_api.servicos.estoque import id_insumo
    assert pedra["id"] == id_insumo(vila.tenant_id, vila.obra, "PEDRA  RACHÃO")


async def test_importacoes_simultaneas_nao_duplicam(vila):
    import asyncio
    conteudo = (CAB + "Telha colonial;un;10;100;B\n").encode()
    rs = await asyncio.gather(*(enviar(vila, conteudo, True) for _ in range(3)), return_exceptions=True)
    assert sum(1 for r in rs if not isinstance(r, Exception) and r.status_code == 200) >= 1
    assert all(isinstance(r, Exception) or r.status_code in (200, 409) for r in rs)
    itens = [x for x in (await vila.http.get(f"/obras/{vila.obra}/insumos", headers=vila.h)).json()
             if x["insumo"]["nome"] == "Telha colonial"]
    assert len(itens) == 1 and itens[0]["conta"]["saldo"] == qtd(10)


async def test_desfazer_importacao_arquiva_e_devolve_o_saldo(vila):
    r = await enviar(vila, (CAB + "Telha;un;150;400;B\nCal;sc;0;30;C\n").encode(), True)
    (reg_id,) = r.json()["registro_ids"]
    telha = (await lista(vila))["Telha"]["insumo"]
    desfeito = await vila.http.post(f"/registro/{reg_id}/desfazer", headers=vila.h)
    assert desfeito.status_code == 200, desfeito.text
    assert desfeito.json()["texto"] == "Desfeito: importação de 2 insumo(s) da planilha"
    assert {"Telha", "Cal"}.isdisjoint(await lista(vila))
    # o livro-razão fecha: ajuste inicial + ajuste oposto
    movs = (await vila.http.get(f"/obras/{vila.obra}/insumos/{telha['id']}/movimentos", headers=vila.h)).json()
    assert sorted(m["quantidade"] for m in movs) == [-qtd(150), qtd(150)]

    refeito = await vila.http.post(f"/registro/{desfeito.json()['id']}/desfazer", headers=vila.h)
    assert refeito.status_code == 200, refeito.text
    assert refeito.json()["texto"] == "Refeito: Importou 2 insumo(s) da planilha"
    itens = await lista(vila)
    assert itens["Telha"]["conta"]["saldo"] == qtd(150) and "Cal" in itens

    # desfeita de novo, a mesma planilha volta a importar (os documentos arquivados são reaproveitados)
    assert (await vila.http.post(f"/registro/{refeito.json()['id']}/desfazer", headers=vila.h)).status_code == 200
    r = await enviar(vila, (CAB + "telha;un;7;400;B\n").encode(), True)
    assert r.json()["aplicado"] is True, r.text
    itens = await lista(vila)
    assert itens["telha"]["insumo"]["id"] == telha["id"] and itens["telha"]["conta"]["saldo"] == qtd(7)


async def test_desfazer_importacao_recusa_se_um_insumo_andou(vila):
    r = await enviar(vila, (CAB + "Prego 17x21;kg;5;10;C\nArame recozido;kg;0;10;C\n").encode(), True)
    prego = (await lista(vila))["Prego 17x21"]["insumo"]
    m = await vila.http.post(f"/obras/{vila.obra}/movimentos", headers=vila.h,
                             json={"insumo_id": prego["id"], "tipo": "saida", "quantidade": 1000,
                                   "chave_idempotencia": "prego-saida-1"})
    assert m.status_code == 201, m.text
    d = await vila.http.post(f"/registro/{r.json()['registro_ids'][0]}/desfazer", headers=vila.h)
    assert d.status_code == 409 and "Prego 17x21" in d.json()["detail"]
    # tudo ou nada: o arame, que não andou, continua na lista
    assert {"Prego 17x21", "Arame recozido"} <= set(await lista(vila))
