# Implement a spec'd phase

Execute one phase from `ROADMAP.md` that already has a written `**Spec:**`
block. Stay inside that spec's declared scope.

## Steps

1. Read the phase's `**Spec:**` block in full.
2. Copy the source file(s) from `hrb_chatbot_service` (local clone or pasted
   content), adapt imports to `src/rag_ingestion/<module>/`, port the matching
   test file(s), run them, confirm they pass unmodified in behavior.
3. Run `pytest -v` for the whole repo, not just the new files.
4. Flip the phase's checkbox to `- [x]` only after tests pass.
5. Produce a delivery note (`/delivery-note`).
