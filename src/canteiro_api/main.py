"""App FastAPI. `create_app` recebe o armazenamento para os testes rodarem contra memória e Mongo."""
from contextlib import asynccontextmanager
from functools import cache

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from canteiro_dominio.armazenamento import ConflitoDeId
from canteiro_dominio.regras import RegraViolada

from .armazenamento import criar_armazenamento
from .cofre import criar_cofre
from .config import Config
from .nucleo import ErroNegocio
from .rotas import auth, obras, propostas, sessoes


def create_app(config: Config | None = None, arm=None) -> FastAPI:
    config = config or Config()
    config.conferir()

    @asynccontextmanager
    async def ciclo(app: FastAPI):
        if app.state.arm is None:
            app.state.arm = criar_armazenamento(config.armazenamento, mongo_url=config.mongo_url,
                                                mongo_db=config.mongo_db)
            if hasattr(app.state.arm, "garantir_indices"):
                await app.state.arm.garantir_indices()
            yield
            await app.state.arm.fechar()
        else:
            yield

    app = FastAPI(title="Canteiro · API central", version="0.1.0", lifespan=ciclo,
                  description="Dona dos dados e das regras. Quantidade em milésimos, dinheiro em centavos.")
    app.state.config = config
    app.state.arm = arm
    app.state.cofre = criar_cofre(config.chave_cifra)

    @app.exception_handler(ErroNegocio)
    async def _erro_negocio(_: Request, e: ErroNegocio):
        return JSONResponse(status_code=e.codigo, content={"detail": e.mensagem})

    @app.exception_handler(RegraViolada)
    async def _regra(_: Request, e: RegraViolada):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    @app.exception_handler(ConflitoDeId)
    async def _conflito(_: Request, e: ConflitoDeId):
        return JSONResponse(status_code=409, content={"detail": "Já existe um registro com este identificador."})

    @app.get("/saude", tags=["infra"])
    async def saude():
        return {"ok": True}

    app.include_router(auth.r)
    app.include_router(obras.r)
    app.include_router(propostas.r)
    app.include_router(sessoes.r)
    return app


@cache
def _app_padrao() -> FastAPI:
    return create_app()


# `uvicorn canteiro_api.main:app` — criado na importação só quando rodado como servidor
def __getattr__(nome: str):
    if nome == "app":
        return _app_padrao()
    raise AttributeError(nome)
