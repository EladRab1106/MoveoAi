"""Render a Claude Code session export (transcript.jsonl) as readable Markdown.

    python -m scripts.render_transcript session/transcript.jsonl session/transcript.md

Keeps user messages and assistant text in full; tool calls and results are shown
compactly (inputs/outputs truncated). The .jsonl file remains the complete record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_TOOL_INPUT = 600
MAX_TOOL_RESULT = 800


def _short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"


def _tool_input(name: str, inp: dict) -> str:
    if name in ("Write",) and "content" in inp:
        inp = {**inp, "content": f"<{len(inp['content'])} chars>"}
    if name == "Edit":
        inp = {k: (v if k == "file_path" else f"<{len(str(v))} chars>") for k, v in inp.items()}
    return _short(json.dumps(inp, ensure_ascii=False, indent=1), MAX_TOOL_INPUT)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if c.get("type") == "text":
            parts.append(c["text"])
        elif c.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def render(src: Path) -> str:
    out = ["# Session transcript: Weather Risk Intelligence Agent\n",
           "Readable rendering of `transcript.jsonl` (the complete, authoritative export). "
           "User and assistant messages are complete; tool calls and outputs are truncated.\n"]
    for line in src.open():
        d = json.loads(line)
        if d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
            continue
        msg = d.get("message", {})
        content = msg.get("content")
        ts = d.get("timestamp", "")[:19].replace("T", " ")
        if d["type"] == "user":
            if isinstance(content, str):
                if content.startswith("<") and "system-reminder" in content[:40]:
                    continue
                out.append(f"\n---\n\n## 🧑 User · {ts}\n\n{content.strip()}\n")
                continue
            for c in content or []:
                if c.get("type") == "text" and not c["text"].lstrip().startswith("<system-reminder"):
                    out.append(f"\n---\n\n## 🧑 User · {ts}\n\n{c['text'].strip()}\n")
                elif c.get("type") == "tool_result":
                    text = _result_text(c.get("content"))
                    flag = " (error)" if c.get("is_error") else ""
                    out.append(f"<details><summary>result{flag}</summary>\n\n```\n"
                               f"{_short(text, MAX_TOOL_RESULT)}\n```\n</details>\n")
        else:
            for c in content or []:
                if c.get("type") == "text" and c["text"].strip():
                    out.append(f"\n**🤖 Assistant · {ts}**\n\n{c['text'].strip()}\n")
                elif c.get("type") == "tool_use":
                    out.append(f"\n> 🔧 `{c['name']}`\n\n```json\n{_tool_input(c['name'], c.get('input', {}))}\n```\n")
    return "\n".join(out)


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.write_text(render(src))
    print(f"wrote {dst} ({dst.stat().st_size // 1024} KB)")
