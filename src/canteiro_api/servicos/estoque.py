"""Almoxarifado: livro-razão de movimentos + projeção de saldo, sempre gravados juntos."""
import uuid
from dataclasses import asdict
from datetime import UTC
from zoneinfo import ZoneInfo

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Insumo, Movimento, Pedido, Saldo, StatusPedido, TipoInsumo, TipoMovimento, agora
from canteiro_dominio.regras import (
    A_CAMINHO,
    EM_COTACAO,
    RegraViolada,
    conta_de_falta,
    efeito_no_saldo,
    falta_na_linha,
    texto_da_conta,
    validar_saida,
)
from canteiro_dominio.tipos import fmt_qtd

from ..armazenamento.base import normalizar
from ..auth import UsuarioAtual
from ..modelos import doc
from ..nucleo import (
    Conflito,
    NaoEncontrado,
    Operacao,
    evento,
    executar_e_registrar,
    insumo_da_obra,
    obra_do_tenant,
    recusar_se_desfazendo,
    verbo_desfazer,
)

_NS = uuid.UUID("6f1c2a52-3b7e-4c1e-9a55-0c4a1f2b7d10")


def id_derivado(tenant_id: str, *partes: str) -> str:
    """ID determinístico: o mesmo pedido de gravação (mesma chave) cai sempre no mesmo documento."""
    return str(uuid.uuid5(_NS, "|".join([tenant_id, *partes])))


def id_movimento(tenant_id: str, chave: str) -> str:
    return id_derivado(tenant_id, "mov", chave)


_INVERSO = {
    TipoMovimento.ENTRADA: TipoMovimento.SAIDA,
    TipoMovimento.SAIDA: TipoMovimento.ENTRADA,
    TipoMovimento.TRANSFERENCIA_ENTRADA: TipoMovimento.TRANSFERENCIA_SAIDA,
    TipoMovimento.TRANSFERENCIA_SAIDA: TipoMovimento.TRANSFERENCIA_ENTRADA,
}


async def preparar_movimento(tx, u: UsuarioAtual, *, obra_id: str, insumo_id: str, tipo: TipoMovimento,
                             quantidade: int, id: str, destino: str | None = None, etapa: str | None = None,
                             origem_doc: str | None = None,
                             origem: str = "tela", desfaz: str | None = None, conferir_curva: bool = True,
                             insumo: dict | None = None) -> Operacao:
    if not desfaz:
        recusar_se_desfazendo(await obra_do_tenant(tx, u.tenant_id, obra_id))
    insumo = insumo or await insumo_da_obra(tx, u.tenant_id, obra_id, insumo_id)
    if insumo.get("tipo") == TipoInsumo.SERVICO.value:
        raise RegraViolada(f"{insumo['nome']} é serviço: não tem estoque para entrar, sair ou ajustar.")
    saldo = await tx.obter("saldos", u.tenant_id, insumo_id)
    m = Movimento(id=id, tenant_id=u.tenant_id, obra_id=obra_id, insumo_id=insumo_id, tipo=tipo,
                  quantidade=quantidade, destino=destino, etapa=etapa, origem_doc=origem_doc, quem=u.nome,
                  usuario_id=u.id,
                  origem=origem, desfaz=desfaz)
    if tipo == TipoMovimento.AJUSTE and quantidade == 0:
        raise RegraViolada("Ajuste de zero não muda nada.")
    efeito = efeito_no_saldo(m)
    if conferir_curva and tipo in (TipoMovimento.SAIDA, TipoMovimento.TRANSFERENCIA_SAIDA):
        validar_saida(curva=insumo["curva"], destino=destino)
    atual = saldo["saldo"] if saldo else 0

    def aplicar() -> dict:
        d = doc(m)
        tx.criar("movimentos", d)
        tx.salvar("saldos", doc(Saldo(id=insumo_id, tenant_id=u.tenant_id, obra_id=obra_id, saldo=atual + efeito,
                                      atualizado_em=agora())))
        evento(tx, u.tenant_id, "estoque.movimento", obra_id,
               {"movimento_id": m.id, "insumo_id": insumo_id, "tipo": m.tipo.value, "quantidade": quantidade,
                "saldo": atual + efeito})
        return d

    def texto(d: dict) -> str:
        verbo = {"entrada": "Entrada", "saida": "Saída", "ajuste": "Ajuste"}.get(d["tipo"], d["tipo"])
        sufixo = f" ({d['destino']})" if d.get("destino") else ""
        estorno = "Estorno: " if desfaz else ""
        # no ajuste o sinal é a informação ("−20" sumiu, "+20" apareceu); entrada e saída já dizem a direção no verbo
        sinal = ("+" if quantidade > 0 else "-") if d["tipo"] == "ajuste" else ""
        return f"{estorno}{verbo} de {sinal}{fmt_qtd(abs(quantidade))} {insumo['unidade']} de {insumo['nome']}{sufixo}"

    return Operacao(obra_id=obra_id, aplicar=aplicar, texto=texto,
                    desfazer=lambda d: None if desfaz else {"op": "estornar_movimento", "movimento_id": d["id"]})


async def preparar_estorno(tx, u: UsuarioAtual, movimento_id: str, origem: str = "tela") -> Operacao:
    """Livro-razão não apaga: desfazer um movimento é lançar o oposto, ligado ao original por `desfaz`."""
    m = await tx.obter("movimentos", u.tenant_id, movimento_id)
    if not m:
        raise NaoEncontrado("Movimento não encontrado.")
    id_estorno = id_derivado(u.tenant_id, "estorno", movimento_id)
    if await tx.obter("movimentos", u.tenant_id, id_estorno):
        raise Conflito("Este movimento já foi estornado.")
    tipo = TipoMovimento(m["tipo"])
    if tipo == TipoMovimento.AJUSTE:
        novo_tipo, q = TipoMovimento.AJUSTE, -m["quantidade"]
    else:
        novo_tipo, q = _INVERSO[tipo], m["quantidade"]
    return await preparar_movimento(tx, u, obra_id=m["obra_id"], insumo_id=m["insumo_id"], tipo=novo_tipo,
                                    quantidade=q, id=id_estorno, destino=m.get("destino"), etapa=m.get("etapa"),
                                    origem_doc=m.get("origem_doc"), origem=origem, desfaz=movimento_id,
                                    conferir_curva=False)


def mesmo_movimento(existente: dict, corpo) -> bool:
    return (existente["insumo_id"], existente["tipo"], existente["quantidade"], existente.get("destino")) == (
        corpo.insumo_id, corpo.tipo, corpo.quantidade, corpo.destino)


PEDIDOS_ABERTOS = [s.value for s in A_CAMINHO | EM_COTACAO]


def linhas_de(p: dict) -> list[dict]:
    """As linhas do pedido; o gravado antes delas (um insumo só) ganha a linha montada pelo modelo."""
    if "linhas" in p:
        return p["linhas"]
    return [x.model_dump(mode="json") for x in Pedido.model_validate(p).linhas]


def valor_em_aberto(p: dict) -> int:
    """O que o pedido ainda vai custar: em cotação, tudo; depois, só o que falta entregar (a parte entregue já virou
    nota). Pelo preço do pedido, proporcional à falta de cada linha."""
    if StatusPedido(p["status"]) in EM_COTACAO:
        return p["valor"]
    return sum((falta_na_linha(x) * x.get("total", 0) + x["quantidade"] // 2) // x["quantidade"]
               for x in linhas_de(p) if x["quantidade"])


def medido_por_insumo(pedidos: list[dict]) -> dict[str, int]:
    """O que já foi medido de cada serviço. Serviço não entra no almoxarifado, então o que chegou não vira saldo:
    sem somar isto, o serviço entregue voltaria inteiro para "Falta comprar" assim que o pedido fechasse."""
    medido: dict[str, int] = {}
    for p in pedidos:
        for x in linhas_de(p):
            if x.get("tipo") == TipoInsumo.SERVICO.value and x.get("recebida"):
                medido[x["insumo_id"]] = medido.get(x["insumo_id"], 0) + x["recebida"]
    return medido


def com_conta(insumos: list[dict], saldos: dict[str, int], pedidos: list[dict],
              medido: dict[str, int] | None = None) -> list[dict]:
    """Junta insumos, saldos e pedidos abertos já lidos. Separado da leitura para o resumo de obras reaproveitar a
    mesma conta lendo o tenant inteiro de uma vez. A caminho é o que falta entregar em cada linha: a parte que já
    chegou está no saldo, e contá-la de novo esconderia uma falta."""
    a_caminho: dict[str, int] = {}
    cotando: dict[str, int] = {}
    for p in pedidos:
        cotacao = StatusPedido(p["status"]) in EM_COTACAO
        alvo = cotando if cotacao else a_caminho
        for x in linhas_de(p):
            q = x["quantidade"] if cotacao else falta_na_linha(x)
            alvo[x["insumo_id"]] = alvo.get(x["insumo_id"], 0) + q
    saida = []
    for i in insumos:
        c = conta_de_falta(precisa=i["precisa_ate_o_fim"], saldo=saldos.get(i["id"], 0),
                           medido=(medido or {}).get(i["id"], 0),
                           a_caminho=a_caminho.get(i["id"], 0), cotando=cotando.get(i["id"], 0), transferencia=0)
        saida.append({"insumo": i, "conta": {**asdict(c), "texto": texto_da_conta(c, i.get("unidade", ""))}})
    return saida


FUSO = ZoneInfo("America/Sao_Paulo")


def dia_local(quando) -> str:
    """O dia de um instante gravado em UTC, no fuso da obra: pedido das 22h de 05/11 é de 05/11, não de 06/11."""
    q = normalizar(quando)
    return (q if q.tzinfo else q.replace(tzinfo=UTC)).astimezone(FUSO).date().isoformat()


def ultimos_precos(pedidos: list[dict]) -> dict[str, dict]:
    """Preço unitário da compra mais nova de cada insumo, entre os pedidos não cancelados. É o que responde "quanto
    custa 10 sacos a mais?" sem o modelo dividir o total do pedido pela quantidade."""
    precos: dict[str, dict] = {}
    for p in sorted(pedidos, key=lambda p: normalizar(p["criado_em"])):
        if p["status"] == StatusPedido.CANCELADO.value:
            continue
        for x in linhas_de(p):
            if not x.get("quantidade"):
                continue
            # pedido antigo sem unitário gravado: o total da linha pelo que foi pedido, arredondado ao centavo
            unitario = x.get("valor_unitario") or (x.get("total", 0) * 1000 + x["quantidade"] // 2) // x["quantidade"]
            precos[x["insumo_id"]] = {"valor_unitario": unitario, "pedido_id": p["id"], "numero": p["numero"],
                                      "fornecedor": p.get("fornecedor", ""), "status": p["status"],
                                      "data": dia_local(p["criado_em"])}
    return precos


async def insumos_com_conta(arm, tenant_id: str, obra_id: str) -> list[dict]:
    """A Conta de cada insumo: saldo da projeção, a caminho e cotando somados dos pedidos abertos da obra; e o preço
    da última compra. Uma consulta de pedidos só (todos os da obra) serve às duas coisas."""
    insumos = await insumos_ativos(arm, tenant_id, obra_id)
    saldos = {s["id"]: s["saldo"] for s in
              await arm.consultar(Consulta("saldos", tenant_id, [Filtro("obra_id", "==", obra_id)]))}
    todos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id)]))
    abertos = [p for p in todos if p["status"] in PEDIDOS_ABERTOS]
    precos = ultimos_precos(todos)
    return [{**x, "ultimo_preco": precos.get(x["insumo"]["id"])}
            for x in com_conta(insumos, saldos, abertos, medido_por_insumo(todos))]


def _nome_chave(nome: str) -> str:
    return " ".join(nome.split()).casefold()


def nome_limpo(nome: str) -> str:
    return " ".join(nome.split())


def id_insumo(tenant_id: str, obra_id: str, nome: str) -> str:
    """O nome é a chave: "Areia  média" e "areia média" caem no mesmo documento, venham da tela ou de planilha."""
    return id_derivado(tenant_id, "insumo", obra_id, _nome_chave(nome))


def ativos(insumos: list[dict]) -> list[dict]:
    # filtro em Python, não na consulta: o insumo gravado antes do campo `arquivado` não o tem, e no Firestore a
    # igualdade não casa com campo ausente
    return [i for i in insumos if not i.get("arquivado")]


def mudou_desde_a_aprovacao(i: dict) -> bool:
    return any(i.get(k) != x for k, x in i["aprovacao"]["depois"].items())


def _criado_por_aprovacao(i: dict) -> bool:
    return bool(i.get("aprovacao")) and i["aprovacao"]["antes"] is None


def sem_aprovacao_abandonada(insumos: list[dict]) -> list[dict]:
    """Os insumos de uma obra `em_orcamento`, sem o que uma aprovação que não terminou deixou no caminho. Os lotes
    gravam antes de o status virar, e nessa situação todo insumo com rastro é sobra: aprovação concluída e desfeita
    arquiva o que criou e tira o rastro do que atualizou. O criado some; o atualizado aparece como era."""
    saida = []
    for i in insumos:
        if not i.get("aprovacao"):
            saida.append(i)
        elif not _criado_por_aprovacao(i):
            saida.append(i if mudou_desde_a_aprovacao(i) else {**i, **i["aprovacao"]["antes"]})
    return saida


async def insumos_ativos(leitor, tenant_id: str, obra_id: str) -> list[dict]:
    """Os insumos que a obra tem de verdade, sem os arquivados e sem as sobras de aprovação abandonada."""
    insumos = ativos(await leitor.consultar(Consulta("insumos", tenant_id, [Filtro("obra_id", "==", obra_id)],
                                                     [("nome", "asc")])))
    o = await leitor.obter("obras", tenant_id, obra_id)
    return sem_aprovacao_abandonada(insumos) if o and o["status"] == "em_orcamento" else insumos


def gravar_insumo_novo(tx, d: dict, existente: dict | None) -> None:
    """Grava o insumo de ID derivado do nome. `existente` é o que a transação leu nesse ID: arquivado (cadastro
    desfeito) ou sobra de aprovação abandonada é reaproveitado; ativo quer dizer que alguém cadastrou o mesmo nome
    agora há pouco."""
    if existente is None:
        tx.criar("insumos", d)
    elif existente.get("arquivado") or _criado_por_aprovacao(existente):
        tx.salvar("insumos", d)
    else:
        raise Conflito(f"'{d['nome']}' já está cadastrado nesta obra.")


_CAMPOS_INSUMO = ("nome", "unidade", "tipo", "precisa_ate_o_fim", "valor_unitario", "curva")


def criado(d: dict, saldo: int) -> dict:
    """O que a receita guarda de um insumo recém-criado para saber, no desfazer, que ninguém mexeu nele depois."""
    return {"id": d["id"], "saldo": saldo, "saldo_em": d["criado_em"], "campos": {k: d.get(k) for k in _CAMPOS_INSUMO}}


def receita_insumos(obra_id: str, rotulo: str, *, arquivar: list[dict] = (), restaurar: list[dict] = ()) -> dict:
    """Receita de desfazer de cadastro/importação de insumos.

    `arquivar`: [`criado(...)`] — insumos criados; desfazer tira das listas e devolve o saldo inicial (ajuste oposto),
    só se nada mudou desde então: `saldo_em` é o `atualizado_em` que a criação gravou no saldo (qualquer movimento o
    troca) e `campos` o que ela gravou no insumo (uma importação posterior o troca).
    `restaurar`: [{id, antes, depois}] — campos que a importação sobrescreveu.
    """
    return {"op": "restaurar_insumos", "obra_id": obra_id, "rotulo": rotulo, "arquivar": list(arquivar),
            "reativar": [], "restaurar": list(restaurar)}


async def recusar_se_ha_pedido_aberto(arm, tenant_id: str, obra_id: str, insumo_ids: list[str]) -> None:
    """Arquivar um insumo com pedido aberto deixaria o pedido sem ter onde dar entrada (receber daria 404).

    Fica fora da transação porque a porta não consulta dentro dela (o Firestore só lê por ID ali): sobra a janela de
    alguém criar um pedido entre esta leitura e o desfazer, que é pequena e recuperável com Refazer.
    """
    ids = set(insumo_ids)
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                  Filtro("status", "in", PEDIDOS_ABERTOS)]))
    presos = sorted({p["numero"] for p in pedidos if any(linha["insumo_id"] in ids for linha in p["linhas"])})
    if presos:
        raise Conflito(f"Há pedido aberto com este insumo ({', '.join(presos)}); cancele-o antes de desfazer.")


async def preparar_restaurar_insumos(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz (ou refaz) cadastro e importação de insumos, tudo ou nada.

    Arquivar em vez de apagar porque a porta não apaga documento; o saldo inicial sai por um ajuste oposto, como no
    estorno, para o livro-razão continuar fechando com a projeção. Recusa se qualquer insumo andou desde então:
    desfazer em cima de um movimento ou de uma edição posterior apagaria trabalho de outra pessoa.
    """
    obra_id = receita["obra_id"]
    lidos: dict[str, tuple[dict, dict | None]] = {}
    for x in [*receita["arquivar"], *receita["reativar"], *receita["restaurar"]]:
        i = await tx.obter("insumos", u.tenant_id, x["id"])
        if not i or i["obra_id"] != obra_id:
            raise NaoEncontrado("Insumo não encontrado nesta obra.")
        lidos[x["id"]] = (i, await tx.obter("saldos", u.tenant_id, x["id"]))
    for x, arquivado, saldo_esperado in [*((x, False, x["saldo"]) for x in receita["arquivar"]),
                                         *((x, True, 0) for x in receita["reativar"])]:
        i, saldo = lidos[x["id"]]
        intocado = (saldo and saldo["atualizado_em"] == normalizar(x["saldo_em"]) and saldo["saldo"] == saldo_esperado
                    and all(i.get(k) == v for k, v in x.get("campos", {}).items()))
        if bool(i.get("arquivado")) != arquivado or not intocado:
            raise Conflito(f"{i['nome']} teve movimento ou mudou desde então; não dá para desfazer.")
    for x in receita["restaurar"]:
        i, _ = lidos[x["id"]]
        if i.get("arquivado") or any(i.get(k) != v for k, v in x["depois"].items()):
            raise Conflito(f"{i['nome']} foi editado de novo depois; não dá para desfazer.")
    quando = agora()

    def ajustar(i: dict, saldo: dict, quantidade: int, novo: int) -> None:
        if quantidade:
            tx.criar("movimentos", doc(Movimento(
                tenant_id=u.tenant_id, obra_id=obra_id, insumo_id=i["id"], tipo=TipoMovimento.AJUSTE,
                quantidade=quantidade, quem=u.nome, usuario_id=u.id, origem="tela",
                origem_doc="refazer" if receita.get("refazer") else "desfazer")))
        tx.salvar("saldos", doc(Saldo(id=i["id"], tenant_id=u.tenant_id, obra_id=obra_id, saldo=novo,
                                      atualizado_em=quando)))

    def aplicar() -> dict:
        for x in receita["arquivar"]:
            i, saldo = lidos[x["id"]]
            tx.atualizar("insumos", u.tenant_id, i["id"], {"arquivado": True})
            ajustar(i, saldo, -x["saldo"], 0)
        for x in receita["reativar"]:
            i, saldo = lidos[x["id"]]
            tx.atualizar("insumos", u.tenant_id, i["id"], {"arquivado": False})
            ajustar(i, saldo, x["saldo"], x["saldo"])
        for x in receita["restaurar"]:
            tx.atualizar("insumos", u.tenant_id, x["id"], x["antes"])
        evento(tx, u.tenant_id, "estoque.insumos_restaurados", obra_id,
               {"arquivados": [x["id"] for x in receita["arquivar"]],
                "reativados": [x["id"] for x in receita["reativar"]],
                "restaurados": [x["id"] for x in receita["restaurar"]]})
        return {"obra_id": obra_id}

    # o refazer é a mesma receita com os papéis trocados; `saldo_em` passa a ser o instante gravado agora
    inversa = {**receita, "arquivar": [{**x, "saldo_em": quando} for x in receita["reativar"]],
               "reativar": [{**x, "saldo_em": quando} for x in receita["arquivar"]],
               "restaurar": [{**x, "antes": x["depois"], "depois": x["antes"]} for x in receita["restaurar"]],
               "refazer": not receita.get("refazer")}
    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: f"{verbo_desfazer(receita)}: {receita['rotulo']}",
                    desfazer=lambda d: inversa)


async def criar_insumo(arm, u: UsuarioAtual, obra_id: str, *, nome: str, unidade: str, curva: str,
                       precisa_ate_o_fim: int, valor_unitario: int = 0, tipo: str = "material") -> dict:
    """Cadastra um insumo na obra. O nome é a chave: "Areia  média" e "areia média" são o mesmo insumo.

    A checagem contra os que já existem (inclusive os importados antigos, de ID aleatório) é uma consulta antes da
    transação; o ID derivado do nome fecha a corrida de dois cadastros iguais ao mesmo tempo. Um cadastro desfeito
    (arquivado) com o mesmo nome é reaproveitado.
    """
    nome = nome_limpo(nome)
    recusar_se_desfazendo(await obra_do_tenant(arm, u.tenant_id, obra_id))
    existentes = await insumos_ativos(arm, u.tenant_id, obra_id)
    if any(_nome_chave(i["nome"]) == _nome_chave(nome) for i in existentes):
        raise Conflito(f"'{nome}' já está cadastrado nesta obra.")
    ins = Insumo(id=id_insumo(u.tenant_id, obra_id, nome), tenant_id=u.tenant_id,
                 obra_id=obra_id, nome=nome, tipo=tipo, unidade=unidade.strip(), curva=curva,
                 precisa_ate_o_fim=precisa_ate_o_fim,
                 valor_unitario=valor_unitario)
    d = doc(ins)
    o_que = "serviço" if tipo == "servico" else "insumo"

    async def corpo(tx):
        gravar_insumo_novo(tx, d, await tx.obter("insumos", u.tenant_id, ins.id))
        # a projeção nasce com o instante do cadastro: é por ele que o desfazer sabe que nada se mexeu desde então
        tx.salvar("saldos", doc(Saldo(id=ins.id, tenant_id=u.tenant_id, obra_id=obra_id, saldo=0,
                                      atualizado_em=ins.criado_em)))
        op = Operacao(obra_id=obra_id, aplicar=lambda: d,
                      texto=lambda _: f"Cadastrou o {o_que} {nome} ({ins.unidade}, curva {curva})",
                      desfazer=lambda _: receita_insumos(obra_id, f"cadastro do {o_que} {nome}",
                                                         arquivar=[criado(d, 0)]))
        _, reg = executar_e_registrar(tx, u, op)
        evento(tx, u.tenant_id, "estoque.insumo_criado", obra_id, {"insumo_id": ins.id})
        return {**d, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo)
