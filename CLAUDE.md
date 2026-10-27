# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The ingestion half of the HRB chatbot's RAG pipeline, extracted as its own repo out of `hrb_chatbot_service`'s current monolith: extract PDF text -> chunk -> embed -> index, plus the document-metadata/dedup/supersedes logic around it. Retrieval (decompose -> retrieve -> generate, agentic modes, HITL, memory) is a separate sibling service, `hrb_rag_retrieval_service` - not this repo's concern.

Full design lives in `hrb_chatbot_service`'s own `docs/dev-reference/rag-ingestion/` doc set (`overview.html`, `spec.html`, `architecture.html`, `design.html`, `decisions.html`) and `docs/dev-reference/service-boundary-decision.html`/`microservices-rollout-plan.html` - read those before changing anything here.

Python 3.12, FastAPI (sync path) + AWS Lambda (async path) - see "Deployment model" below.

## Deployment model - resolved 2026-10-22

**Both shapes, by design - not a choice between them.** `hrb_chatbot_service` today already runs both, live, inside its own monolith:
1. **Sync, in-request** (any vector store) - a caller uploads a document, the whole extract->chunk->embed->index pipeline runs inline, the response carries the result. This repo's own FastAPI container (`uvicorn src.rag_ingestion.main:app`).
2. **Async, event-driven** (Pinecone-only - Chroma's local-disk nature doesn't survive Lambda's ephemeral filesystem) - S3 upload -> SQS -> Lambda runs the same pipeline out of band. Real, deployed, verified live on AWS (`hrb_chatbot_service`'s own Phase 88/89), not a design doc.

Built across 2 phases, not one shot: **Phase 1** ports the sync/container path; **Phase 2** ports the Lambda/async path once Phase 1's shared pipeline code exists to call into.

**Sequencing note:** `microservices-rollout-plan.html`'s own dependency-ordered plan sequences this split *after* the API Gateway's production cutover (ingestion is flagged there as the highest-risk piece of the split). User-directed override, 2026-10-22: build this first anyway, Gateway cutover stays parked. Not a silent deviation - recorded here so a future session doesn't assume the original order was followed.

## Commands

```bash
# install
pip install -r requirements.txt

# run
uvicorn src.rag_ingestion.main:app --reload --port 8300

# test
pytest -v
```

## Architecture

Ports `hrb_chatbot_service`'s `ai/doc_processing/**` pipeline (chunking via LangChain splitters, embedding, indexing via LlamaIndex's `VectorStoreIndex` - see that repo's own CLAUDE.md for why each stage uses a different library), `services/documents_service.py`'s dedup/supersedes/file-validation logic, and the upload/list/get/delete routes from `api/rag/ingest_document.py`. Document metadata lives in the `hrb_rag_ingestion` Postgres schema (split out in `hrb_chatbot_service`'s own Phase 251 schema migration) - **not** `doc_metadata_store.py`, which tracks an unrelated admin docs-sync-to-S3 feature and stays in `hrb_chatbot_service`.

Depends on `hrb-common-libs`' `db`, `vectorstore`, `llm`, and `storage` extras - all four already built there (Phases 2/4/6/11-13).

## Spec-Driven Development

Same convention as `hrb_chatbot_service`: a spec is a `**Spec:**` block in `ROADMAP.md`'s `## Phases` section, written before that phase's code starts. `.claude/scripts/spec_gate.py` blocks a `Write`/`Edit` under `src/rag_ingestion/**` unless `ROADMAP.md` has an open, spec'd phase.

**Four working norms:**
- **One task at a time.**
- **Flag, don't fix.**
- **Nothing gets pushed/deployed without the user's own review and explicit go-ahead first - every time, not just the first time.**
- **Behavior parity for every extracted phase** - the point of an extraction is that it doesn't change behavior; a ported test must pass the same way it did in `hrb_chatbot_service`, not a looser approximation.

**Available skills**: `session-start`, `delivery-note`, `spec-new`, `spec-implement`.
**Subagents**: `.claude/agents/implementer.md`, `.claude/agents/code-reviewer.md`.
