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
