"""Almoxarifado: livro-razão de movimentos + projeção de saldo, sempre gravados juntos."""
import uuid
from dataclasses import asdict

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Insumo, Movimento, Saldo, StatusPedido, TipoMovimento, agora
from canteiro_dominio.regras import A_CAMINHO, EM_COTACAO, RegraViolada, conta_de_falta, efeito_no_saldo, validar_saida
from canteiro_dominio.tipos import fmt_qtd

from ..auth import UsuarioAtual
from ..modelos import doc
from ..nucleo import Conflito, NaoEncontrado, Operacao, evento, insumo_da_obra, obra_do_tenant, registrar

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
                             quantidade: int, id: str, destino: str | None = None, origem_doc: str | None = None,
                             origem: str = "tela", desfaz: str | None = None, conferir_curva: bool = True,
                             insumo: dict | None = None) -> Operacao:
    insumo = insumo or await insumo_da_obra(tx, u.tenant_id, obra_id, insumo_id)
    saldo = await tx.obter("saldos", u.tenant_id, insumo_id)
    m = Movimento(id=id, tenant_id=u.tenant_id, obra_id=obra_id, insumo_id=insumo_id, tipo=tipo,
                  quantidade=quantidade, destino=destino, origem_doc=origem_doc, quem=u.nome, usuario_id=u.id,
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
                                    quantidade=q, id=id_estorno, destino=m.get("destino"),
                                    origem_doc=m.get("origem_doc"), origem=origem, desfaz=movimento_id,
                                    conferir_curva=False)


def mesmo_movimento(existente: dict, corpo) -> bool:
    return (existente["insumo_id"], existente["tipo"], existente["quantidade"], existente.get("destino")) == (
        corpo.insumo_id, corpo.tipo, corpo.quantidade, corpo.destino)


PEDIDOS_ABERTOS = [s.value for s in A_CAMINHO | EM_COTACAO]


def com_conta(insumos: list[dict], saldos: dict[str, int], pedidos: list[dict]) -> list[dict]:
    """Junta insumos, saldos e pedidos abertos já lidos. Separado da leitura para o resumo de obras reaproveitar a
    mesma conta lendo o tenant inteiro de uma vez."""
    a_caminho: dict[str, int] = {}
    cotando: dict[str, int] = {}
    for p in pedidos:
        alvo = cotando if StatusPedido(p["status"]) in EM_COTACAO else a_caminho
        alvo[p["insumo_id"]] = alvo.get(p["insumo_id"], 0) + p["quantidade"]
    return [{"insumo": i, "conta": asdict(conta_de_falta(
        precisa=i["precisa_ate_o_fim"], saldo=saldos.get(i["id"], 0), a_caminho=a_caminho.get(i["id"], 0),
        cotando=cotando.get(i["id"], 0), transferencia=0))} for i in insumos]


async def insumos_com_conta(arm, tenant_id: str, obra_id: str) -> list[dict]:
    """A Conta de cada insumo: saldo da projeção, a caminho e cotando somados dos pedidos abertos da obra."""
    insumos = await arm.consultar(Consulta("insumos", tenant_id, [Filtro("obra_id", "==", obra_id)], [("nome", "asc")]))
    saldos = {s["id"]: s["saldo"] for s in
              await arm.consultar(Consulta("saldos", tenant_id, [Filtro("obra_id", "==", obra_id)]))}
    pedidos = await arm.consultar(Consulta("pedidos", tenant_id, [Filtro("obra_id", "==", obra_id),
                                                                  Filtro("status", "in", PEDIDOS_ABERTOS)]))
    return com_conta(insumos, saldos, pedidos)


def _nome_chave(nome: str) -> str:
    return " ".join(nome.split()).casefold()


async def criar_insumo(arm, u: UsuarioAtual, obra_id: str, *, nome: str, unidade: str, curva: str,
                       precisa_ate_o_fim: int, valor_unitario: int = 0) -> dict:
    """Cadastra um insumo na obra. O nome é a chave: "Areia  média" e "areia média" são o mesmo insumo.

    A checagem contra os que já existem (inclusive os importados, de ID aleatório) é uma consulta antes da transação;
    o ID derivado do nome fecha a corrida de dois cadastros iguais ao mesmo tempo (o segundo cai em ConflitoDeId).
    """
    nome = " ".join(nome.split())
    await obra_do_tenant(arm, u.tenant_id, obra_id)
    existentes = await arm.consultar(Consulta("insumos", u.tenant_id, [Filtro("obra_id", "==", obra_id)]))
    if any(_nome_chave(i["nome"]) == _nome_chave(nome) for i in existentes):
        raise Conflito(f"'{nome}' já está cadastrado nesta obra.")
    ins = Insumo(id=id_derivado(u.tenant_id, "insumo", obra_id, _nome_chave(nome)), tenant_id=u.tenant_id,
                 obra_id=obra_id, nome=nome, unidade=unidade.strip(), curva=curva, precisa_ate_o_fim=precisa_ate_o_fim,
                 valor_unitario=valor_unitario)
    d = doc(ins)

    async def corpo(tx):
        tx.criar("insumos", d)
        tx.criar("saldos", doc(Saldo(id=ins.id, tenant_id=u.tenant_id, obra_id=obra_id, saldo=0)))
        reg = registrar(tx, u, obra_id=obra_id, origem="tela",
                        texto=f"Cadastrou o insumo {nome} ({ins.unidade}, curva {curva})")
        evento(tx, u.tenant_id, "estoque.insumo_criado", obra_id, {"insumo_id": ins.id})
        return {**d, "registro_id": reg["id"]}

    return await arm.em_transacao(corpo)
