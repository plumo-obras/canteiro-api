"""Obra: criar (onboarding), corrigir os dados e aprovar o orçamento, que tira a obra de `em_orcamento`."""
from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import Obra
from canteiro_dominio.tipos import fmt_reais

from ..auth import UsuarioAtual
from ..esquemas import ObraCorpo
from ..modelos import doc
from ..nucleo import (
    Conflito,
    ErroNegocio,
    NaoEncontrado,
    Operacao,
    evento,
    obra_do_tenant,
    receita_inversa,
    registrar,
    verbo_desfazer,
)
from .estoque import ativos, id_derivado

# Como cada campo aparece no texto do registro ("Atualizou a obra X: área, padrão").
ROTULOS = {
    "nome": "nome", "cidade": "cidade", "cep": "CEP", "uf": "UF", "endereco": "endereço", "tipologia": "tipo de obra",
    "area_construida": "área", "pavimentos": "pavimentos", "padrao": "padrão", "regime": "regime",
    "cliente": "cliente", "inicio": "início", "prazo_meses": "prazo", "verba": "valor do contrato",
    "numero_contrato": "nº do contrato", "bdi": "BDI", "data_base": "data-base",
    "cno": "CNO", "proprietario": "proprietário", "destinacao": "destinação", "material": "material",
    "area_complementar": "área complementar", "area_descoberta": "área descoberta", "vau": "VAU",
    "vau_referencia": "mês do VAU", "finalidade": "finalidade", "valor_terreno": "valor do terreno",
    "unidades": "unidades", "categoria": "categoria", "dctfweb_em_dia": "DCTFWeb em dia",
}


def id_obra(tenant_id: str, chave: str) -> str:
    return id_derivado(tenant_id, "obra", chave)


async def criar_obra(arm, u: UsuarioAtual, corpo: ObraCorpo) -> tuple[dict, bool]:
    """Devolve (obra com registro_id, criada?). A mesma chave repetida devolve a obra já gravada: o duplo clique no
    "Criar" do onboarding, ou a rede que caiu depois de gravar, não viram duas obras."""
    id_ = id_obra(u.tenant_id, corpo.chave_idempotencia)
    id_registro = id_derivado(u.tenant_id, "registro", id_)
    dados = corpo.model_dump(exclude={"chave_idempotencia"})

    async def tx_corpo(tx):
        existente = await tx.obter("obras", u.tenant_id, id_)
        if existente:
            if existente["nome"] != corpo.nome:
                raise Conflito("Esta chave de idempotência já foi usada para outra obra.")
            return existente, False
        o = doc(Obra(id=id_, tenant_id=u.tenant_id, status="em_orcamento", **dados))
        tx.criar("obras", o)
        # sem desfazer: a porta de armazenamento não apaga documento, e arquivar a obra (como o insumo desfeito) ainda
        # não tem onde aparecer: tiraria da lista uma obra com orçamento, pedidos e registro
        registrar(tx, u, obra_id=id_, texto=f"Criou a obra {o['nome']}", origem="tela", id=id_registro)
        evento(tx, u.tenant_id, "obra.criada", id_, {"nome": o["nome"]})
        return o, True

    o, criada = await arm.em_transacao(tx_corpo)
    return {**o, "registro_id": id_registro}, criada


async def preparar_editar(tx, u: UsuarioAtual, obra_id: str, campos: dict) -> Operacao | None:
    """Só o que de fato muda entra: reenviar o formulário inteiro sem mexer não gera registro "Atualizou"."""
    o = await obra_do_tenant(tx, u.tenant_id, obra_id)
    novos = {k: v for k, v in campos.items() if o.get(k) != v}
    if not novos:
        return None
    antes = {k: o.get(k) for k in novos}
    nome = novos.get("nome", o["nome"])

    def aplicar():
        tx.atualizar("obras", u.tenant_id, obra_id, novos)
        return {**o, **novos}

    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: f"Atualizou a obra {nome}: {', '.join(ROTULOS[k] for k in novos)}",
                    desfazer=lambda d: {"op": "restaurar_obra", "obra_id": obra_id, "antes": antes, "depois": novos})


async def preparar_restaurar(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    """Desfaz uma edição só se ninguém mexeu nesses campos depois: senão apagaria a correção de outra pessoa."""
    o = await tx.obter("obras", u.tenant_id, receita["obra_id"])
    if not o:
        raise NaoEncontrado("Obra não encontrada.")
    if any(o.get(k) != v for k, v in receita["depois"].items()):
        raise Conflito("Os dados da obra mudaram de novo desde então; não dá para desfazer.")
    antes = receita["antes"]

    def aplicar():
        tx.atualizar("obras", u.tenant_id, o["id"], antes)
        return {**o, **antes}

    return Operacao(obra_id=o["id"], aplicar=aplicar,
                    texto=lambda d: f"{verbo_desfazer(receita)}: dados da obra {d['nome']} restaurados",
                    desfazer=lambda d: receita_inversa(receita))


async def insumos_da_obra(arm, tenant_id: str, obra_id: str) -> list[dict]:
    # a transação portável só lê por ID; a lista de insumos vem antes, por consulta
    return ativos(await arm.consultar(Consulta("insumos", tenant_id, [Filtro("obra_id", "==", obra_id)])))


async def preparar_aprovar_orcamento(tx, u: UsuarioAtual, obra_id: str, insumos: list[dict],
                                     vigentes: list[dict] | None = None) -> Operacao:
    """Com contrato importado (EAP), aprova o orçado atual (contrato + aditivos vigentes), o mesmo número que a tela
    confirma; sem contrato, a lista de materiais (o caminho do MVP-0)."""
    o = await obra_do_tenant(tx, u.tenant_id, obra_id)
    if o["status"] != "em_orcamento":
        raise Conflito("O orçamento já foi aprovado.")
    lidas = [await tx.obter("versoes_orcamento", u.tenant_id, v["id"]) for v in vigentes or []]
    # relidas na transação: se alguma importação ou desfazer mudou as versões nesse meio-tempo, o total seria outro
    if any(not v or not v["ativa"] for v in lidas):
        raise Conflito("O orçamento mudou agora há pouco; confira e aprove de novo.")
    if any(v["tipo"] == "contrato" for v in lidas):
        n, total, oque = sum(v["itens"] for v in lidas), sum(v["total"] for v in lidas), "item"
    else:
        lidas = []
        orcados = [i for i in insumos if i.get("precisa_ate_o_fim", 0) > 0]
        if not orcados:
            raise ErroNegocio("Importe ou cadastre o orçamento antes de aprovar.")
        n, oque = len(orcados), "insumo"
        total = sum(i["precisa_ate_o_fim"] * i.get("valor_unitario", 0) // 1000 for i in orcados)

    def aplicar():
        tx.atualizar("obras", u.tenant_id, obra_id, {"status": "em_andamento"})
        for v in lidas:
            # o que a transação leu para decidir ela também grava (regra da porta: conflito igual no Mongo e Firestore)
            tx.atualizar("versoes_orcamento", u.tenant_id, v["id"], {"aditivos": v.get("aditivos", 0)})
        evento(tx, u.tenant_id, "orcamento.aprovado", obra_id, {oque + "s": n, "total": total})
        return {**o, "status": "em_andamento"}

    qtd_itens = f"{n} " + (oque if n == 1 else {"item": "itens", "insumo": "insumos"}[oque])
    return Operacao(obra_id=obra_id, aplicar=aplicar,
                    texto=lambda d: f"Aprovou o orçamento de {o['nome']} ({qtd_itens}, {fmt_reais(total)})",
                    desfazer=lambda d: {"op": "reabrir_orcamento", "obra_id": obra_id})


async def preparar_reabrir(tx, u: UsuarioAtual, receita: dict) -> Operacao:
    o = await tx.obter("obras", u.tenant_id, receita["obra_id"])
    if not o:
        raise NaoEncontrado("Obra não encontrada.")
    if o["status"] != "em_andamento":
        raise Conflito("A obra já mudou de situação desde então; não dá para desfazer.")

    def aplicar():
        tx.atualizar("obras", u.tenant_id, o["id"], {"status": "em_orcamento"})
        return {**o, "status": "em_orcamento"}

    return Operacao(obra_id=o["id"], aplicar=aplicar,
                    texto=lambda d: f"Desfeito: orçamento de {o['nome']} reaberto", desfazer=lambda d: None)
