"""PreToolUse hook for rag_ingestion_service - blocks a Write/Edit under
src/rag_ingestion/** unless at least one phase in ROADMAP.md has a written,
not-yet-done spec. Same convention as hrb_chatbot_service's own spec_gate.py.

Deliberately scoped to src/rag_ingestion/** only - never .claude/, tests/, or
docs/ - so this hook can never lock out fixing itself or writing a spec.

Run manually with: python .claude/scripts/spec_gate.py < payload.json
"""

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ROADMAP = PROJECT_ROOT / "ROADMAP.md"


def should_gate(file_path: str) -> bool:
    if not file_path.endswith(".py"):
        return False
    try:
        relative = Path(file_path).resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        relative = file_path.replace("\\", "/")
    return relative.startswith("src/rag_ingestion")


def has_open_spec() -> bool:
    text = ROADMAP.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=- \[[ x]\] \*\*Phase)", text)
    for block in blocks:
        if block.startswith("- [ ]") and re.search(r"^\s+- \*\*Spec:\*\*", block, re.MULTILINE):
            return True
    return False


def deny(reason: str) -> None:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return

    file_path = payload.get("tool_input", {}).get("file_path", "")
    if not should_gate(file_path):
        return

    if not has_open_spec():
        deny(
            "No open spec found in ROADMAP.md's ## Phases section "
            "(a '- [ ]' phase with a nested '- **Spec:**' block). Write one "
            "with /spec-new before editing src/rag_ingestion/**."
        )


if __name__ == "__main__":
    main()
