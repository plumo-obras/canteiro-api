"""Pedidos de compra: número legível por tenant, várias linhas, entregas parciais com nota e transições validadas.

Uma entrega (material) ou medição (serviço) é um Recebimento: soma o que chegou em cada linha, dá entrada no
almoxarifado das linhas de material na mesma transação e deixa o resto "a caminho" até a próxima entrega ou até alguém
encerrar o pedido. Só a última entrega pode ser desfeita: desfazer uma do meio deixaria as seguintes contando em cima de
uma quantidade que não existe mais.
"""
from canteiro_dominio.modelos import LinhaPedido, Pedido, Recebimento, StatusPedido, TipoInsumo, TipoMovimento
from canteiro_dominio.regras import (
    PODE_RECEBER,
    RegraViolada,
    falta_na_linha,
    validar_conferencia,
    validar_transicao,
)
from canteiro_dominio.tipos import fmt_qtd, fmt_reais, novo_id

from ..auth import UsuarioAtual
from ..esquemas import PedidoDados
from ..modelos import doc
from ..nucleo import Conflito, NaoEncontrado, Operacao, evento, insumo_da_obra, obra_do_tenant
from .estoque import id_derivado, linhas_de, preparar_estorno, preparar_movimento


def id_contador(tenant_id: str) -> str:
    # O Firestore não tem sequência nem índice único: o número sai de um documento-contador lido e gravado na transação.
    return f"contador-pedidos-{tenant_id}"


def id_recebimento(tenant_id: str, pedido_id: str, chave: str) -> str:
    return id_derivado(tenant_id, "recebimento", pedido_id, chave)


def _total(quantidade: int, valor_unitario: int) -> int:
    # unitário em centavos por unidade inteira, quantidade em milésimos
    return (quantidade * valor_unitario + 500) // 1000


def _lista(nomes: list[str]) -> str:
    return nomes[0] if len(nomes) == 1 else f"{', '.join(nomes[:-1])} e {nomes[-1]}"


async def preparar_criar_pedido(tx, u: UsuarioAtual, obra_id: str, corpo: PedidoDados,
                                status: StatusPedido = StatusPedido.EM_COTACAO) -> Operacao:
    """`status` = aprovado quando a compra já foi decidida (proposta aprovada, ou a pessoa marcou na tela): pedir uma
    segunda aprovação do mesmo pedido seria burocracia sem informação nova. A etapa de cada linha é conferida contra o
    orçamento antes da transação (é consulta, e a transação só lê por ID)."""
    await obra_do_tenant(tx, u.tenant_id, obra_id)
    if corpo.fornecedor_id and not await tx.obter("fornecedores", u.tenant_id, corpo.fornecedor_id):
        raise NaoEncontrado("Fornecedor não encontrado.")
    linhas: list[LinhaPedido] = []
    for x in corpo.linhas:
        ins = await insumo_da_obra(tx, u.tenant_id, obra_id, x.insumo_id)
        total = x.total if x.total is not None else _total(x.quantidade, x.valor_unitario)
        unitario = x.valor_unitario or ((total * 1000 + x.quantidade // 2) // x.quantidade)
        linhas.append(LinhaPedido(insumo_id=x.insumo_id, nome=ins["nome"], unidade=ins["unidade"],
                                  tipo=ins.get("tipo", TipoInsumo.MATERIAL), etapa=x.etapa or None,
                                  quantidade=x.quantidade, valor_unitario=unitario, total=total))
    contador = await tx.obter("contadores", u.tenant_id, id_contador(u.tenant_id))
    n = (contador["ultimo"] if contador else 0) + 1
    p = Pedido(tenant_id=u.tenant_id, numero=f"PED-{n:04d}", obra_id=obra_id, fornecedor=corpo.fornecedor.strip(),
               fornecedor_id=corpo.fornecedor_id, linhas=linhas, valor=sum(x.total for x in linhas),
               previsao=corpo.previsao, observacao=corpo.observacao.strip(), status=status)

    def aplicar() -> dict:
        tx.salvar("contadores", {"id": id_contador(u.tenant_id), "tenant_id": u.tenant_id, "ultimo": n})
        d = doc(p)
        tx.criar("pedidos", d)
        evento(tx, u.tenant_id, "pedido.criado", obra_id, {"pedido_id": p.id, "numero": p.numero,
                                                           "status": status.value})
        return d

    def texto(d: dict) -> str:
        aprovado = " · aprovado" if status == StatusPedido.APROVADO else ""
        if len(linhas) == 1:
            x = linhas[0]
            o_que = f"{fmt_qtd(x.quantidade)} {x.unidade} de {x.nome}"
        else:
            o_que = f"{len(linhas)} itens ({_lista([x.nome for x in linhas])})"
        return f"{d['numero']} criado: {o_que} com {d['fornecedor']} ({fmt_reais(d['valor'])}){aprovado}"

    # o status de nascimento vai na receita: desfazer só cancela se o pedido não andou desde então
    return Operacao(obra_id=obra_id, aplicar=aplicar, texto=texto,
                    desfazer=lambda d: {"op": "cancelar_pedido", "pedido_id": d["id"], "status_criado": status.value})


async def _pedido(tx, tenant_id: str, pedido_id: str) -> dict:
    p = await tx.obter("pedidos", tenant_id, pedido_id)
    if not p:
        raise NaoEncontrado("Pedido não encontrado.")
    return p


def _normalizado(p: dict) -> dict:
    """O pedido sempre com `linhas` (o gravado antes delas ganha uma linha montada dos campos antigos)."""
    return p if "linhas" in p else {**p, "linhas": linhas_de(p), "recebimentos": p.get("recebimentos", 0)}


async def preparar_receber(tx, u: UsuarioAtual, pedido_id: str, entregas: list[tuple[int, int]], *,
                           recebimento_id: str, nf_numero: str = "", nf_data: str | None = None,
                           nf_valor: int | None = None, origem: str = "tela") -> Operacao:
    """Registra uma entrega: `entregas` = [(índice da linha, quantidade)]. Material entra no almoxarifado; serviço só
    soma o medido. Status vira conferido quando nada mais falta, senão parcial."""
    p = _normalizado(await _pedido(tx, u.tenant_id, pedido_id))
    de = StatusPedido(p["status"])
    if de not in PODE_RECEBER:
        raise RegraViolada(f"{p['numero']} está {de.value}: não recebe entrega.")
    linhas = [dict(x) for x in p["linhas"]]
    numero = p.get("recebimentos", 0) + 1
    rec_linhas: list[dict] = []
    movimentos: list[Operacao] = []
    origem_doc = f"{p['numero']} · NF {nf_numero}" if nf_numero else p["numero"]
    for indice, q in entregas:
        if not 0 <= indice < len(linhas):
            raise RegraViolada(f"{p['numero']} não tem a linha {indice + 1}.")
        x = linhas[indice]
        if not x.get("nome"):
            # pedido de antes das linhas não guardava a cópia do insumo: lê agora para o texto e a entrega
            ins = await insumo_da_obra(tx, u.tenant_id, p["obra_id"], x["insumo_id"])
            x |= {"nome": ins["nome"], "unidade": ins["unidade"], "tipo": ins.get("tipo", TipoInsumo.MATERIAL.value)}
        x["recebida"] = x.get("recebida", 0) + q
        rec_linhas.append({"indice": indice, "insumo_id": x["insumo_id"], "nome": x.get("nome", ""),
                           "unidade": x.get("unidade", ""), "tipo": x.get("tipo", TipoInsumo.MATERIAL.value),
                           "etapa": x.get("etapa"), "quantidade": q, "valor": _total(q, x.get("valor_unitario", 0))})
        if x.get("tipo", TipoInsumo.MATERIAL.value) == TipoInsumo.MATERIAL.value:
            movimentos.append(await preparar_movimento(
                tx, u, obra_id=p["obra_id"], insumo_id=x["insumo_id"], tipo=TipoMovimento.ENTRADA, quantidade=q,
                id=id_derivado(u.tenant_id, "entrega", recebimento_id, str(indice)), origem_doc=origem_doc,
                etapa=x.get("etapa"), origem=origem))
    para = StatusPedido.CONFERIDO if all(falta_na_linha(x) == 0 for x in linhas) else StatusPedido.PARCIAL
    rec = Recebimento(id=recebimento_id, tenant_id=u.tenant_id, obra_id=p["obra_id"], pedido_id=pedido_id,
                      pedido_numero=p["numero"], numero=numero, nf_numero=nf_numero, nf_data=nf_data,
                      nf_valor=nf_valor, valor=sum(x["valor"] for x in rec_linhas), linhas=rec_linhas,
                      quem=u.nome, usuario_id=u.id)

    def aplicar() -> dict:
        rec.movimentos = [m.aplicar()["id"] for m in movimentos]
        campos = {"linhas": linhas, "status": para.value, "recebimentos": numero}
        tx.atualizar("pedidos", u.tenant_id, pedido_id, campos)
        tx.criar("recebimentos", doc(rec))
        evento(tx, u.tenant_id, "pedido.recebimento", p["obra_id"],
               {"pedido_id": pedido_id, "numero": p["numero"], "recebimento_id": rec.id, "de": de.value,
                "para": para.value})
        return {"recebimento": doc(rec), "pedido": {**p, **campos}}

    def texto(d: dict) -> str:
        servico = all(x["tipo"] == TipoInsumo.SERVICO.value for x in rec_linhas)
        nf = f" (NF {nf_numero})" if nf_numero else ""
        partes = []
        for e in rec_linhas:
            x = linhas[e["indice"]]
            # entrega menor que o pedido diz "18 de 25": é o que o comprador cobra do fornecedor depois
            de_total = f" de {fmt_qtd(x['quantidade'])}" if x["recebida"] != x["quantidade"] else ""
            partes.append(f"{fmt_qtd(e['quantidade'])}{de_total} {e['unidade']} de {e['nome']}")
        t = f"{p['numero']} · {numero}ª {'medição' if servico else 'entrega'}{nf}: {'; '.join(partes)}"
        faltam = [f"{fmt_qtd(falta_na_linha(x))} {x.get('unidade', '')} de {x.get('nome', '')}"
                  for x in linhas if falta_na_linha(x)]
        return t + (f" · faltam {_lista(faltam)}" if faltam else " · pedido completo")

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar, texto=texto,
                    desfazer=lambda d: {"op": "desfazer_recebimento", "recebimento_id": rec.id,
                                        "status_antes": de.value, "status_depois": para.value})


async def preparar_desfazer_recebimento(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    rec = await tx.obter("recebimentos", u.tenant_id, receita["recebimento_id"])
    if not rec or rec.get("desfeito"):
        raise Conflito("Esta entrega já foi desfeita.")
    p = await _pedido(tx, u.tenant_id, rec["pedido_id"])
    if p.get("recebimentos", 0) != rec["numero"]:
        raise Conflito(f"{p['numero']} recebeu outra entrega depois desta; desfaça a mais recente primeiro.")
    if p["status"] != receita["status_depois"]:
        raise Conflito(f"{p['numero']} já mudou de status desde então; não dá para desfazer.")
    estornos = [await preparar_estorno(tx, u, m) for m in rec.get("movimentos", [])]
    linhas = [dict(x) for x in p["linhas"]]
    for e in rec["linhas"]:
        linhas[e["indice"]]["recebida"] -= e["quantidade"]
    voltou = StatusPedido.PARCIAL.value if any(x["recebida"] for x in linhas) else receita["status_antes"]

    def aplicar() -> dict:
        for e in estornos:
            e.aplicar()
        campos = {"linhas": linhas, "status": voltou, "recebimentos": rec["numero"] - 1}
        tx.atualizar("pedidos", u.tenant_id, p["id"], campos)
        tx.atualizar("recebimentos", u.tenant_id, rec["id"], {"desfeito": True})
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": p["id"], "numero": p["numero"], "de": p["status"], "para": voltou})
        return {**p, **campos}

    nf = f" (NF {rec['nf_numero']})" if rec.get("nf_numero") else ""
    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {rec['numero']}ª entrega do {p['numero']}{nf} retirada",
                    desfazer=lambda d: None)


async def preparar_avancar(tx, u: UsuarioAtual, pedido_id: str, para: StatusPedido, origem: str = "tela", *,
                           quantidade_recebida: int | None = None, nf: str | None = None) -> Operacao:
    """Avança o status. `conferido` tem dois sentidos: vindo de `recebido`, é receber tudo o que falta numa entrega só
    (o fluxo de antes das entregas parciais, que o agente ainda usa); vindo de `parcial`, é encerrar o resto."""
    p = _normalizado(await _pedido(tx, u.tenant_id, pedido_id))
    de = StatusPedido(p["status"])
    validar_transicao(de, para)
    validar_conferencia(para, quantidade_recebida=quantidade_recebida, nf=nf)
    if para == StatusPedido.CONFERIDO and de != StatusPedido.PARCIAL:
        if quantidade_recebida is not None and len(p["linhas"]) > 1:
            raise RegraViolada(f"{p['numero']} tem {len(p['linhas'])} linhas: registre a entrega linha a linha.")
        entregas = [(i, quantidade_recebida if quantidade_recebida is not None else falta_na_linha(x))
                    for i, x in enumerate(p["linhas"])]
        # sem chave de idempotência neste caminho: desfazer e conferir de novo é outra entrega, com outro ID
        n = p.get("recebimentos", 0)
        op = await preparar_receber(
            tx, u, pedido_id, [(i, q) for i, q in entregas if q > 0], nf_numero=nf or "", origem=origem,
            recebimento_id=id_recebimento(u.tenant_id, pedido_id, f"conferir-{n}-{novo_id()}"))
        # quem avança espera o pedido de volta; a entrega fica guardada para o texto e o desfazer
        receber, texto_rec, desfazer_rec = op.aplicar, op.texto, op.desfazer
        return Operacao(obra_id=op.obra_id, aplicar=lambda: (lambda r: {**r["pedido"], "_entrega": r})(receber()),
                        texto=lambda d: texto_rec(d["_entrega"]), desfazer=lambda d: desfazer_rec(d["_entrega"]))
    campos: dict = {"status": para.value}

    def aplicar() -> dict:
        tx.atualizar("pedidos", u.tenant_id, pedido_id, campos)
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": pedido_id, "numero": p["numero"], "de": de.value, "para": para.value})
        return {**p, **campos}

    def texto(d: dict) -> str:
        if de == StatusPedido.PARCIAL:
            resto = [f"{fmt_qtd(falta_na_linha(x))} {x.get('unidade', '')} de {x.get('nome', '')}"
                     for x in p["linhas"] if falta_na_linha(x)]
            return f"{p['numero']} encerrado: o resto não vem ({_lista(resto)})" if resto else \
                f"{p['numero']} encerrado"
        return f"{p['numero']}: {de.value} → {para.value}"

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar, texto=texto,
                    desfazer=lambda d: {"op": "voltar_pedido", "pedido_id": pedido_id, "de": de.value,
                                        "para": para.value})


async def preparar_voltar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz um avanço de status (sem passar pelas transições, é a operação inversa). Receita antiga de conferência
    (de antes das entregas) traz o movimento a estornar."""
    p = await _pedido(tx, u.tenant_id, receita["pedido_id"])
    if p["status"] != receita["para"]:
        raise Conflito(f"{p['numero']} já mudou de status desde então; não dá para desfazer.")
    estorno = await preparar_estorno(tx, u, receita["movimento_id"]) if receita.get("movimento_id") else None

    def aplicar() -> dict:
        if estorno:
            estorno.aplicar()
        campos: dict = {"status": receita["de"]}
        if estorno:
            campos |= {"quantidade_recebida": None, "nf": None}
        tx.atualizar("pedidos", u.tenant_id, p["id"], campos)
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": p["id"], "numero": p["numero"], "de": receita["para"], "para": receita["de"]})
        return {**p, **campos}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {p['numero']} voltou para {receita['de']}", desfazer=lambda d: None)


async def preparar_cancelar_criado(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    p = await _pedido(tx, u.tenant_id, receita["pedido_id"])
    # receita antiga (de antes do pedido poder nascer aprovado) não tem o campo: nascia sempre em cotação
    if p["status"] != receita.get("status_criado", StatusPedido.EM_COTACAO.value):
        raise Conflito(f"{p['numero']} já andou ({p['status']}); cancele pelo fluxo do pedido.")

    def aplicar() -> dict:
        tx.atualizar("pedidos", u.tenant_id, p["id"], {"status": StatusPedido.CANCELADO.value})
        evento(tx, u.tenant_id, "pedido.status", p["obra_id"],
               {"pedido_id": p["id"], "numero": p["numero"], "de": p["status"], "para": "cancelado"})
        return {**p, "status": StatusPedido.CANCELADO.value}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {p['numero']} cancelado", desfazer=lambda d: None)


_ROTULO_EDICAO = {"previsao": "previsão", "observacao": "observação"}


async def preparar_editar(tx, u: UsuarioAtual, pedido_id: str, mudancas: dict) -> Operacao:
    """Previsão e observação mudam a qualquer momento (o fornecedor remarca a entrega); o resto do pedido não."""
    p = await _pedido(tx, u.tenant_id, pedido_id)
    antes = {k: p.get(k) for k in mudancas}
    mudou = {k: v for k, v in mudancas.items() if antes[k] != v}
    if not mudou:
        raise RegraViolada("Nada mudou.")

    def aplicar() -> dict:
        tx.atualizar("pedidos", u.tenant_id, pedido_id, mudou)
        return {**p, **mudou}

    def texto(d: dict) -> str:
        partes = [f"{_ROTULO_EDICAO[k]} {antes[k] or '—'} → {v or '—'}" for k, v in mudou.items()]
        return f"{p['numero']}: {', '.join(partes)}"

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar, texto=texto,
                    desfazer=lambda d: {"op": "editar_pedido", "pedido_id": pedido_id,
                                        "antes": {k: antes[k] for k in mudou}, "depois": mudou})


async def preparar_desfazer_edicao(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    p = await _pedido(tx, u.tenant_id, receita["pedido_id"])
    if any(p.get(k) != v for k, v in receita["depois"].items()):
        raise Conflito(f"{p['numero']} foi editado de novo depois; não dá para desfazer esta edição.")

    def aplicar() -> dict:
        tx.atualizar("pedidos", u.tenant_id, p["id"], receita["antes"])
        return {**p, **receita["antes"]}

    return Operacao(obra_id=p["obra_id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: {p['numero']} voltou à edição anterior", desfazer=lambda d: None)
