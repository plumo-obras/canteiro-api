"""Lista de espera da landing (Aprumo). Rota pública: sem login, chamada direto do navegador (CORS em main.py).

Nada aqui pode virar oráculo de quem está inscrito além do próprio e-mail informado: a resposta só diz se aquele
e-mail já estava na lista, nunca posição na fila nem dado de outra pessoa. E o e-mail não vai para log.
"""
from fastapi import APIRouter, HTTPException, Request, Response, status

from canteiro_dominio.armazenamento import ConflitoDeId

from ..auth import consumir_tentativa
from ..config import Config
from ..esquemas import ListaEsperaCorpo, ListaEsperaResposta
from ..modelos import InscritoEspera, doc, id_de_inscrito, normalizar_email
from .auth import _ip

r = APIRouter(tags=["lista de espera"])


@r.post("/lista-espera", response_model=ListaEsperaResposta, status_code=status.HTTP_201_CREATED)
async def inscrever(corpo: ListaEsperaCorpo, request: Request, response: Response):
    arm = request.app.state.arm
    cfg: Config = request.app.state.config
    # o limite vem antes da isca: robô que martela a rota também esbarra nele
    if not await consumir_tentativa(arm, cfg, "espera-ip-" + _ip(request), cfg.lista_espera_por_ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Muitas tentativas a partir desta rede. Espere alguns minutos e tente de novo.")
    if corpo.site:
        # finge sucesso para o robô não aprender que foi pego
        return ListaEsperaResposta(ja_estava=False)
    inscrito = InscritoEspera(id=id_de_inscrito(corpo.email), email=normalizar_email(corpo.email), nome=corpo.nome,
                              empresa=corpo.empresa, papel=corpo.papel, obras_ativas=corpo.obras_ativas,
                              origem=corpo.origem)
    try:
        await arm.criar("lista_espera", doc(inscrito))
    except ConflitoDeId:
        # já inscrito: não sobrescreve (quem conhece o e-mail de alguém não troca os dados dele)
        response.status_code = status.HTTP_200_OK
        return ListaEsperaResposta(ja_estava=True)
    return ListaEsperaResposta(ja_estava=False)
