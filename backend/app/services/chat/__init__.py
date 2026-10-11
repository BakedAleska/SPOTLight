"""AI Assistance chat (docs/chat-contract.md): Gemini-written, database-grounded answers streamed over SSE.

catalog (models, thinking config) · gemini (REST client, error mapping) · retrieval (documents, graph, SQL digests)
· prompt (system prompt, request body, titles) · grounding (citation and number check) · service (turns, locks).
"""
from .catalog import MODES, catalog, thinking_config
from .gemini import GeminiClient, GeminiError
from .grounding import check as grounding_check
from .retrieval import Retrieval, RetrievalFailed, Retriever
from .service import ChatError, ChatService, Turn

__all__ = ["MODES", "catalog", "thinking_config", "GeminiClient", "GeminiError", "grounding_check", "Retrieval",
           "RetrievalFailed", "Retriever", "ChatError", "ChatService", "Turn"]
