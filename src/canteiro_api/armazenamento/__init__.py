"""Adaptadores da porta `canteiro_dominio.armazenamento`. Só `mongo.py` conhece o pymongo."""
from .base import DocumentoAusente, EscritaAntesDaLeitura
from .memoria import ArmazenamentoMemoria

__all__ = ["ArmazenamentoMemoria", "DocumentoAusente", "EscritaAntesDaLeitura", "criar_armazenamento"]


def criar_armazenamento(tipo: str, *, mongo_url: str = "", mongo_db: str = ""):
    if tipo == "memoria":
        return ArmazenamentoMemoria()
    if tipo == "mongo":
        from .mongo import ArmazenamentoMongo
        return ArmazenamentoMongo(mongo_url, mongo_db)
    raise ValueError(f"Armazenamento desconhecido: {tipo}")
