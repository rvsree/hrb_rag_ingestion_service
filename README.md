# rag_ingestion_service

Ingestion half of the HRB chatbot RAG pipeline: extract -> chunk -> embed -> index, plus document metadata/dedup/supersedes. Extracted out of `hrb_chatbot_service`'s monolith.

Full design doc: `hrb_chatbot_service`'s `docs/dev-reference/rag-ingestion/` and `docs/dev-reference/microservices-rollout-plan.html`.

Status: scaffolding only - no implementation yet. See `ROADMAP.md`.
