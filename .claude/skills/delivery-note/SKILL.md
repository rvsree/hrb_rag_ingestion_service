---
name: delivery-note
description: >
  Generate a concise delivery note after finishing a task on
  rag_ingestion_service. Invoke automatically at the end of a task, or
  explicitly as /delivery-note.
allowed-tools: Bash(git diff --stat *) Bash(git status --short *)
---

# Delivery note

After completing a task, produce this note - under 20 lines total.

```
## Delivery note - <task name>

**Done:**
- <one line per file created/changed, with path>

**Files changed:** <git diff --stat output>

**Tests:** <passed/failed, with counts if run - or "not run">

**Flagged, not fixed:** <any out-of-scope issue noticed, or "none">

**Next step (suggested):** <one sentence>
```

Stop after the note. Wait for the next instruction.
