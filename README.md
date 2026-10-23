# rag_ingestion_service

Ingestion half of the HRB chatbot RAG pipeline: extract -> chunk -> embed -> index, plus document metadata/dedup/supersedes. Extracted out of `hrb_chatbot_service`'s monolith.

Full design doc: `hrb_chatbot_service`'s `docs/dev-reference/rag-ingestion/` and `docs/dev-reference/microservices-rollout-plan.html`.

**Status, 2026-10-23: both paths live in production, real cutover done.** Two independent deployments of the same pipeline:
- **Async (Lambda)** - `rag-ingestion-index-document`, triggered by the real production SQS queue (`hrb-chatbot-ingest-queue`) on an S3 upload. The old monolith's own Lambda is disabled, not deleted - reversible. Verified via a real upload through the live trigger.
- **Sync (container)** - deployed to AWS App Runner (`https://wkfgxanevz.us-east-1.awsapprunner.com`) for the admin-triggered upload/reindex/list/delete routes. Verified live via `GET /documents`, which returned real production documents from the shared Postgres metadata store.

See `ROADMAP.md` for phase history, including 2 real bugs found and fixed during live testing (a missing `git` package for the pinned dependency install, and an implicit-model-resolution bug that bypassed Secrets Manager for `OPENAI_API_KEY`).
