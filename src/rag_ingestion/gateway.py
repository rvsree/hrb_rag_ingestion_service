"""Resolves which concrete backend to use for this repo's own pipeline/store code.

hrb_chatbot_service reaches these through its own combined db_gateway/client_gateway
singletons. This repo has no such combined gateway (hrb-common-libs deliberately
keeps db/llm/vectorstore as separate extras, see that repo's own ROADMAP.md Phase 2
scope note) - this module is the one place that picks a provider by name, mirroring
hrb_common.vectorstore.langchain_vector_store._build_vector_db_client()'s own pattern."""

from hrb_common.db.connection import PostgresConnectionConfig, get_connection
from hrb_common.llm.gateway import get_llm_gateway
from hrb_common.observability.secrets import read_secret
from hrb_common.vectorstore.chroma_client import ChromaDBClient
from hrb_common.vectorstore.pinecone_client import PineconeClient

COLLECTION_NAME = "hrb_chatbot_kb"

_vector_store_clients: dict[str, object] = {}


def get_postgres_connection():
    return get_connection(PostgresConnectionConfig.from_env())


def get_vector_store(provider: str | None = None):
    """Returns the ChromaDBClient/PineconeClient for `provider` (or ACTIVE_VECTOR_DB
    if not given), cached per-provider so repeated calls reuse one client, same as
    hrb_chatbot_service's own db_gateway singleton behavior."""
    resolved_provider = read_secret("ACTIVE_VECTOR_DB", "chromadb", passed_in_value=provider)

    if resolved_provider not in _vector_store_clients:
        if resolved_provider == "pinecone":
            _vector_store_clients[resolved_provider] = PineconeClient()
        else:
            _vector_store_clients[resolved_provider] = ChromaDBClient()

    return _vector_store_clients[resolved_provider]


def get_openai_chat_client():
    return get_llm_gateway().openai_chat()


def get_openai_embedding_client():
    return get_llm_gateway().openai_embedding()
