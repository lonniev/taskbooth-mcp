"""Which small model drafts a job most faithfully? Run each candidate on the same messy ideas.

    OPENROUTER_API_KEY=… uv run python scripts/draft_eval.py [out.md]

Every model gets the same fixed prompt (taskbooth_mcp.draft.SYSTEM) at low effort. For each
idea the table shows what each model wrote as *said*, so a person can see at a glance what
was made up, plus what the checks caught, its questions, the time it took and its tokens.
The key is read from the environment and never printed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import time
from pathlib import Path

from tollbooth.llm_call import ask, usage_of

from taskbooth_mcp import draft

MODELS = ("anthropic/claude-haiku-5.5", "google/gemini-3.1-flash-lite", "openai/gpt-oss-20b")
EFFORT = "low"

IDEAS: tuple[tuple[str, str], ...] = (
    ("commission", "need someone to paint our dog, hes a lab, for my wifes birthday"),
    ("commission", "translate my grandmothers letters from italian, about 40 pages handwritten"),
    ("commission", "logo for my bakery called Rise & Shine, something warm"),
    ("commission", "fix the squeaky stairs in a 1920s house"),
    ("commission", "wedding dress alterations, take in the waist and shorten. wedding is june 14"),
    ("commission", "want a website for my piano lessons, nothing fancy"),
    ("commission", "3d print replacement knob for old stove, i have the broken one"),
    ("chore", "feed the cat"),
    ("chore", "rake leaves in the front yard before saturday"),
    ("chore", "practice piano 20 min every day"),
    ("chore", "help unload groceries"),
    ("chore", "clean out the garage with your brother, the big job"),
)


async def one(key: str, model: str, kind: str, idea: str, today: dt.date) -> dict:
    start = time.monotonic()
    try:
        data = await ask(key, system=draft.SYSTEM[kind], user=draft.user_turn(idea, today=today, currency="USD"),
                         max_tokens=draft.MAX_TOKENS, model=model, effort=EFFORT, cache_system=True,
                         timeout_s=draft.TIMEOUT_S, purpose="draft_eval")
    except Exception as exc:  # noqa: BLE001 — an eval reports every failure, whatever it is
        return {"model": model, "error": f"{type(exc).__name__}: {exc}", "s": time.monotonic() - start}
    took = time.monotonic() - start
    try:
        d = draft.from_answer(data, kind=kind, idea=idea, today=today)
    except ValueError as exc:
        return {"model": model, "error": str(exc), "s": took, "usage": usage_of(data)}
    return {"model": model, "draft": d, "s": took, "usage": usage_of(data)}


def _row(r: dict) -> str:
    u = r.get("usage", {})
    head = f"**{r['model']}** · {r['s']:.1f}s · in {u.get('input_tokens', '?')} (cached {u.get('cached_input_tokens', 0)}) · out {u.get('output_tokens', '?')}"
    if "error" in r:
        return f"{head}\n\n> refused: {r['error']}\n"
    d = r["draft"]
    s = d.said
    lines = [head, "", f"- **said** title: {s.get('title')!r}", f"- **said** brief: {s.get('brief')!r}"]
    if "deliverables" in s:
        lines.append(f"- **said** deliverables: {s['deliverables']}")
    lines.append(f"- **said** needed_by: {s.get('needed_by')}" + (f", repeat {s.get('repeat_days')}" if "repeat_days" in s else ""))
    g = "; ".join(f"{x['name']} ${x['amount']:g}" for x in d.proposed.get("grades", []))
    lines.append(f"- proposed grades: {g or '—'}")
    lines.append(f"- questions ({len(d.questions)}): " + " · ".join(d.questions))
    lines.append(f"- assumptions ({len(d.assumptions)}): " + " · ".join(d.assumptions))
    return "\n".join(lines) + "\n"


async def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("Set OPENROUTER_API_KEY in the environment.")
    out = sys.argv[1] if len(sys.argv) > 1 else "draft_eval.md"
    today = dt.date.today()
    parts = [f"# Draft eval — {today.isoformat()}, effort {EFFORT}\n"]
    for kind, idea in IDEAS:
        results = await asyncio.gather(*(one(key, m, kind, idea, today) for m in MODELS))
        parts.append(f"## {kind}: “{idea}”\n")
        parts.extend(_row(r) for r in results)
        print(f"done: {idea[:50]}", file=sys.stderr)
    Path(out).write_text("\n".join(parts))
    print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
