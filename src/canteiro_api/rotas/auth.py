import ipaddress

from fastapi import APIRouter, Request, status

from .. import auth
from ..esquemas import EntrarCorpo, RenovarCorpo, Sessao

r = APIRouter(prefix="/auth", tags=["autenticação"])


def _redes(texto: str) -> list:
    return [ipaddress.ip_network(x.strip(), strict=False) for x in texto.split(",") if x.strip()]


def _confiavel(ip: str, redes: list) -> bool:
    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(endereco in rede for rede in redes)


def _ip(request: Request) -> str:
    """IP real de quem tenta entrar, para o limite de tentativas.

    X-Forwarded-For só vale se quem nos chamou é um proxy de confiança (TRUSTED_PROXIES); senão qualquer um forjaria
    o cabeçalho para escapar do limite. Lê da direita para a esquerda e para no primeiro endereço que não é proxy nosso:
    o que está à esquerda dele foi escrito pelo próprio cliente.
    """
    direto = request.client.host if request.client else "desconhecido"
    redes = _redes(request.app.state.config.trusted_proxies)
    if not redes or not _confiavel(direto, redes):
        return direto
    cadeia = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    for ip in reversed(cadeia):
        if not _confiavel(ip, redes):
            return ip if _valido(ip) else direto
    return cadeia[0] if cadeia and _valido(cadeia[0]) else direto


def _valido(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


@r.post("/entrar", response_model=Sessao)
async def entrar(corpo: EntrarCorpo, request: Request):
    return await auth.entrar(request.app.state.arm, request.app.state.config, corpo.email, corpo.senha, _ip(request))


@r.post("/renovar", response_model=Sessao)
async def renovar(corpo: RenovarCorpo, request: Request):
    return await auth.renovar(request.app.state.arm, request.app.state.config, corpo.renovacao)


@r.post("/sair", status_code=status.HTTP_204_NO_CONTENT)
async def sair(corpo: RenovarCorpo, request: Request) -> None:
    await auth.sair(request.app.state.arm, corpo.renovacao)
