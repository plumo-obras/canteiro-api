"""Arquivos privados (comprovantes de gasto). Hoje: memória (testes) ou disco local (desenvolvimento).

Fica atrás de `Arquivos`, como o `Cofre`, para trocar por um bucket privado (GCS) sem mexer nas rotas. Ninguém acessa
o arquivo direto: ele só sai pela rota da API, que confere login e tenant. Comprovante tem dado pessoal (nome, CPF,
conta): nunca vai para o git, e o diretório local fica fora do repositório versionado (`.arquivos/` no .gitignore).
"""
from pathlib import Path
from typing import Protocol


class Arquivos(Protocol):
    async def salvar(self, caminho: str, conteudo: bytes) -> None: ...
    async def ler(self, caminho: str) -> bytes | None: ...


class ArquivosMemoria:
    def __init__(self):
        self._dados: dict[str, bytes] = {}

    async def salvar(self, caminho: str, conteudo: bytes) -> None:
        self._dados[caminho] = conteudo

    async def ler(self, caminho: str) -> bytes | None:
        return self._dados.get(caminho)


class ArquivosDisco:
    """Só para desenvolvimento: o disco do contêiner some a cada deploy."""

    def __init__(self, raiz: str):
        self._raiz = Path(raiz).resolve()

    def _alvo(self, caminho: str) -> Path:
        alvo = (self._raiz / caminho).resolve()
        # o caminho é montado pela API com IDs nossos; mesmo assim, nada sai da raiz
        if not alvo.is_relative_to(self._raiz):
            raise ValueError("Caminho de arquivo fora da raiz.")
        return alvo

    async def salvar(self, caminho: str, conteudo: bytes) -> None:
        alvo = self._alvo(caminho)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(conteudo)

    async def ler(self, caminho: str) -> bytes | None:
        alvo = self._alvo(caminho)
        return alvo.read_bytes() if alvo.is_file() else None


def criar_arquivos(tipo: str, raiz: str) -> Arquivos:
    if tipo == "memoria":
        return ArquivosMemoria()
    if tipo == "disco":
        return ArquivosDisco(raiz)
    raise RuntimeError(f"ARQUIVOS desconhecido: {tipo!r} (use memoria ou disco).")
