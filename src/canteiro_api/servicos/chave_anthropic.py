"""Confere com a Anthropic se a chave colada funciona antes de guardar.

Listar modelos não gasta token e já diz se a chave existe e está ativa. Não diz se a organização tem saldo: isso só
aparece na primeira conversa, e o agente responde com o erro da Anthropic.
"""
import anthropic

from ..nucleo import ErroNegocio


class ChaveRecusada(ErroNegocio):
    codigo = 422


class AnthropicForaDoAr(ErroNegocio):
    codigo = 503


async def conferir_chave(chave: str, http_client: anthropic.DefaultAsyncHttpxClient | None = None) -> None:
    async with anthropic.AsyncAnthropic(api_key=chave, max_retries=1, timeout=15, http_client=http_client) as cliente:
        try:
            await cliente.models.list(limit=1)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise ChaveRecusada(
                "A Anthropic recusou essa chave. Confira se copiou inteira e se ela está ativa no Console.") from None
        except anthropic.RateLimitError:
            return  # limite de uso só acontece com chave válida
        except anthropic.APIError:
            # sem o texto do erro: ele pode repetir parte do que foi enviado
            raise AnthropicForaDoAr(
                "Não consegui confirmar a chave com a Anthropic agora. Tente de novo em instantes.") from None
