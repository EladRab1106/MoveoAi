# AI-assistant session

This is the full working session with the AI coding agent (Claude Code in the Claude desktop
app) that planned and built this project. It's included as the assignment requires.

| File | Contents |
|---|---|
| `transcript.jsonl` | Complete raw export of the session: every message, tool call and tool result. This is the authoritative record. |
| `transcript.md` | Readable rendering of the same session. All user and assistant messages are included in full; tool inputs and outputs are truncated. Regenerate with `python -m scripts.render_transcript session/transcript.jsonl session/transcript.md`. |

**What to look for:**
- the initial plan (also in [`PLAN.md`](../PLAN.md))
- the data-driven scoring decisions (NRI loss-rate percentile, hurricane proxy, cold wave)
- the eval-driven fixes (disaster declarations, ties, rule wording, the adversarial 2012 case)
- the voice-input debugging
- the deployment and error-handling fixes

The export was taken during the final submission check, so the last few messages of that
check may not be included.
