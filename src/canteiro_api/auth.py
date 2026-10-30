"""Login próprio: argon2 na senha, JWT curto de acesso e renovação opaca (hash no banco, troca a cada uso, revogável).

O papel NUNCA vem do token: o JWT só diz quem é (sub) e de qual cliente (tid);
o papel é lido do banco a cada requisição.
"""
import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from canteiro_dominio.armazenamento import Consulta, Filtro
from canteiro_dominio.modelos import agora
from canteiro_dominio.tipos import novo_id

from .config import Config
from .modelos import NIVEL, TENANT_SISTEMA, IndiceEmail, Papel, Renovacao, Usuario, doc, id_de_email, normalizar_email

_hasher = PasswordHasher()
# Hash de uma senha qualquer, para gastar o mesmo tempo quando o e-mail não existe (não revela quem tem conta).
_HASH_FANTASMA = _hasher.hash(secrets.token_urlsafe(16))
_ALGORITMO = "HS256"


def hash_senha(senha: str) -> str:
    return _hasher.hash(senha)


def senha_confere(hash_: str, senha: str) -> bool:
    try:
        return _hasher.verify(hash_, senha)
    except (VerificationError, InvalidHashError):
        return False


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode()).hexdigest()


@dataclass(frozen=True)
class UsuarioAtual:
    id: str
    tenant_id: str
    nome: str
    papel: Papel

    def pode(self, minimo: Papel) -> bool:
        return NIVEL[self.papel] >= NIVEL[minimo]


def emitir_acesso(cfg: Config, usuario_id: str, tenant_id: str) -> str:
    momento = agora()
    return jwt.encode({"sub": usuario_id, "tid": tenant_id, "iat": momento,
                       "exp": momento + timedelta(minutes=cfg.acesso_minutos), "typ": "acesso"},
                      cfg.jwt_segredo, algorithm=_ALGORITMO)


def _nova_renovacao(cfg: Config, usuario_id: str, tenant_id: str, familia: str | None = None) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    r = Renovacao(id=_sha(token), familia=familia or novo_id(), usuario_id=usuario_id, tenant_usuario=tenant_id,
                  expira_em=agora() + timedelta(days=cfg.renovacao_dias))
    return token, doc(r)


def _resposta(cfg: Config, u: dict, renovacao: str) -> dict:
    return {"acesso": emitir_acesso(cfg, u["id"], u["tenant_id"]), "renovacao": renovacao,
            "usuario": {"id": u["id"], "nome": u["nome"], "tenant_id": u["tenant_id"], "papel": u["papel"]}}


# --- cadastro ---

async def criar_usuario(arm, *, tenant_id: str, nome: str, email: str, senha: str, papel: Papel) -> dict:
    """Cria o usuário e o índice de e-mail na mesma transação: o ID do índice garante um usuário por e-mail."""
    u = Usuario(tenant_id=tenant_id, nome=nome, email=normalizar_email(email), papel=papel,
                senha_hash=await asyncio.to_thread(hash_senha, senha))
    indice = IndiceEmail(id=id_de_email(email), usuario_id=u.id, tenant_usuario=tenant_id)

    async def corpo(tx):
        tx.criar("emails", doc(indice))           # ConflitoDeId se o e-mail já existe
        tx.criar("usuarios", doc(u))

    await arm.em_transacao(corpo)
    return doc(u)


# --- limite de tentativas ---

def _ids_tentativa(email: str, ip: str) -> list[tuple[str, str]]:
    return [("email", "tent-email-" + _sha(normalizar_email(email))), ("ip", "tent-ip-" + _sha(ip))]


def _ativo(t: dict | None, cfg: Config) -> int:
    if not t or agora() - t["inicio"] > timedelta(minutes=cfg.janela_tentativas_minutos):
        return 0
    return t["contagem"]


async def _bloqueado(arm, cfg: Config, email: str, ip: str) -> bool:
    limites = {"email": cfg.tentativas_por_email, "ip": cfg.tentativas_por_ip}
    for tipo, id_ in _ids_tentativa(email, ip):
        if _ativo(await arm.obter("tentativas", TENANT_SISTEMA, id_), cfg) >= limites[tipo]:
            return True
    return False


async def _anotar_tentativa(arm, cfg: Config, email: str, ip: str, sucesso: bool) -> None:
    ids = _ids_tentativa(email, ip)

    async def corpo(tx):
        atuais = [await tx.obter("tentativas", TENANT_SISTEMA, id_) for _, id_ in ids]
        for (tipo, id_), t in zip(ids, atuais, strict=True):
            if sucesso and tipo == "email":
                tx.salvar("tentativas", {"id": id_, "tenant_id": TENANT_SISTEMA, "inicio": agora(), "contagem": 0})
            elif not sucesso:
                n = _ativo(t, cfg)
                inicio = t["inicio"] if n else agora()
                tx.salvar("tentativas", {"id": id_, "tenant_id": TENANT_SISTEMA, "inicio": inicio, "contagem": n + 1})

    await arm.em_transacao(corpo)


async def consumir_tentativa(arm, cfg: Config, chave: str, limite: int) -> bool:
    """Conta uma tentativa na chave (mesma janela do login) e diz se ainda cabe. Para rota pública sem senha, onde
    toda chamada conta, não só a que falha. A chave entra com hash: o ID não carrega IP nem e-mail cru."""
    id_ = "tent-" + _sha(chave)

    async def corpo(tx):
        t = await tx.obter("tentativas", TENANT_SISTEMA, id_)
        n = _ativo(t, cfg)
        cabe = n < limite
        # grava também quando recusa: a transação escreve o que leu para decidir (mesmo comportamento no Firestore)
        tx.salvar("tentativas", {"id": id_, "tenant_id": TENANT_SISTEMA, "inicio": t["inicio"] if n else agora(),
                                 "contagem": n + 1 if cabe else n})
        return cabe

    return await arm.em_transacao(corpo)


# --- fluxos ---

def _negado(msg: str = "E-mail ou senha incorretos.") -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, msg, headers={"WWW-Authenticate": "Bearer"})


async def entrar(arm, cfg: Config, email: str, senha: str, ip: str) -> dict:
    if await _bloqueado(arm, cfg, email, ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Muitas tentativas de entrar. Espere alguns minutos e tente de novo.")
    indice = await arm.obter("emails", TENANT_SISTEMA, id_de_email(email))
    u = indice and await arm.obter("usuarios", indice["tenant_usuario"], indice["usuario_id"])
    # argon2 é lento de propósito: fora do laço de eventos para não travar as outras requisições
    ok = await asyncio.to_thread(senha_confere, u["senha_hash"] if u else _HASH_FANTASMA, senha)
    ok = ok and bool(u) and u["ativo"]
    await _anotar_tentativa(arm, cfg, email, ip, ok)
    if not ok:
        raise _negado()
    token, renovacao = _nova_renovacao(cfg, u["id"], u["tenant_id"])
    await arm.criar("renovacoes", renovacao)
    return _resposta(cfg, u, token)


async def _revogar_familia(arm, familia: str) -> None:
    for r in await arm.consultar(Consulta("renovacoes", TENANT_SISTEMA, [Filtro("familia", "==", familia)])):
        if not r["revogado"]:
            await arm.atualizar("renovacoes", TENANT_SISTEMA, r["id"], {"revogado": True})


async def renovar(arm, cfg: Config, token: str) -> dict:
    id_ = _sha(token)

    async def corpo(tx):
        r = await tx.obter("renovacoes", TENANT_SISTEMA, id_)
        if r is None or r["revogado"] or r["expira_em"] < agora():
            return None, r
        if r["usado"]:
            return "reuso", r
        u = await tx.obter("usuarios", r["tenant_usuario"], r["usuario_id"])
        if not u or not u["ativo"]:
            return None, r
        novo, doc_novo = _nova_renovacao(cfg, u["id"], u["tenant_id"], r["familia"])
        tx.atualizar("renovacoes", TENANT_SISTEMA, id_, {"usado": True})
        tx.criar("renovacoes", doc_novo)
        return _resposta(cfg, u, novo), r

    resultado, r = await arm.em_transacao(corpo)
    if resultado == "reuso":
        # Token já trocado apareceu de novo: provavelmente vazou. Derruba a família inteira.
        await _revogar_familia(arm, r["familia"])
        raise _negado("Sessão encerrada por segurança. Entre de novo.")
    if resultado is None:
        raise _negado("Sessão expirada. Entre de novo.")
    return resultado


async def sair(arm, token: str) -> None:
    r = await arm.obter("renovacoes", TENANT_SISTEMA, _sha(token))
    if r:
        await _revogar_familia(arm, r["familia"])


# --- dependências FastAPI ---

_bearer = HTTPBearer(auto_error=False)
CABECALHO_USUARIO = "X-Canteiro-Usuario"


async def resolver_usuario(request: Request, token: str | None, folga: timedelta = timedelta(0)) -> UsuarioAtual:
    if not token:
        raise _negado("Entre para continuar.")
    cfg: Config = request.app.state.config
    try:
        claims = jwt.decode(token, cfg.jwt_segredo, algorithms=[_ALGORITMO], leeway=folga,
                            options={"require": ["exp", "sub", "tid"]})
    except jwt.PyJWTError:
        raise _negado("Sessão expirada ou inválida. Entre de novo.") from None
    if claims.get("typ") != "acesso":
        raise _negado("Token inválido.")
    # papel e status vêm do banco a cada requisição: rebaixar ou desativar alguém vale na hora
    u = await request.app.state.arm.obter("usuarios", claims["tid"], claims["sub"])
    if not u or not u["ativo"]:
        raise _negado("Usuário inativo.")
    return UsuarioAtual(id=u["id"], tenant_id=u["tenant_id"], nome=u["nome"], papel=Papel(u["papel"]))


async def usuario_atual(request: Request,
                        cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> UsuarioAtual:
    return await resolver_usuario(request, cred.credentials if cred else None)


def exigir(minimo: Papel):
    async def dep(u: UsuarioAtual = Depends(usuario_atual)) -> UsuarioAtual:
        if not u.pode(minimo):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Seu papel ({u.papel.value}) não permite esta ação.")
        return u
    return dep


def servico_agente(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
    esperado = request.app.state.config.token_servico_agente
    if not esperado or cred is None or not hmac.compare_digest(cred.credentials.encode(), esperado.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Rota exclusiva do serviço do agente.")


def usuario_pelo_agente(folga_minutos: int = 0):
    """Rotas internas: o agente prova que é o serviço (Authorization) e por quem age (X-Canteiro-Usuario).

    Tenant e usuário saem do token do usuário, conferido aqui; nunca de um campo que o agente informe.
    `folga_minutos` aceita token recém-vencido: a conversa pode passar dos 15 min do acesso e o consumo já gasto
    tem de ser gravado mesmo assim (a assinatura continua sendo conferida).
    """
    async def dep(request: Request, _: None = Depends(servico_agente)) -> UsuarioAtual:
        valor = request.headers.get(CABECALHO_USUARIO, "")
        esquema, _, token = valor.partition(" ")
        if esquema.lower() != "bearer":
            raise _negado(f"Falta o cabeçalho {CABECALHO_USUARIO} com o token do usuário.")
        return await resolver_usuario(request, token.strip(), timedelta(minutes=folga_minutos))
    return dep
