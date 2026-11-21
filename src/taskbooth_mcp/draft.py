"""Drafting a job from a short idea, with a small LLM.

A person types what they need in their own words; this returns a clean draft of
the job. The draft becomes terms two parties sign, so the one rule that matters is
that nothing the person did not say slips in as if they had said it:

- ``said`` is the idea rewritten clearly: a title, a brief, the deliverables, a
  needed-by date only when the idea gave one, and for a chore how often it repeats.
  A page fills its form from these.
- ``proposed`` is everything the idea left open (grades and what each earns, a
  deposit, milestones, deemed acceptance, IP). A page shows each as a proposal the
  person applies by choice, never silently.
- ``questions`` and ``assumptions`` are what the person must settle before posting.

The model's answer is adversarial input. Every field is checked, and one that fails
is dropped. Two checks catch the commonest invention:
- a needed-by date when the idea named no time is dropped and asked about;
- a number in ``said`` that the idea does not contain is flagged as an assumption.

Money is proposed in the person's currency, which small models handle well, and
converted to sats here at mempool.space's price.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from tollbooth.llm_call import LlmAnswerError, number, one_of, tagged, text, whole

KINDS = ("commission", "chore")
IP_CHOICES = ("assign", "license", "worker_keeps")

MAX_IDEA = 2000
MAX_TOKENS = 3000
TIMEOUT_S = 60.0
TAG = "draft"

_SATS_PER_BTC = 100_000_000
_MAX_GRADES = 6
_MAX_MILESTONES = 6
_MAX_LISTED = 8
_MAX_AMOUNT = 10_000_000  # in the person's currency; past this, no small job's grade

# Words that mean the idea did name a time, so a needed-by date may be its.
_TIME_WORDS = re.compile(
    r"\d|\b(today|tonight|tomorrow|week|weekend|month|year|day|days|by|before|until|due|deadline|asap|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
    r"september|october|november|december|christmas|easter|spring|summer|autumn|fall|winter|birthday)\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


class DraftError(LlmAnswerError):
    """The draft could not be made. A ValueError, so the fare is refunded."""


def checked_idea(value: Any) -> str:
    """The idea as typed, or ``ValueError``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Say what you need, in a sentence or two.")
    if len(value) > MAX_IDEA:
        raise ValueError(f"Keep the idea under {MAX_IDEA} characters; the details come after.")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise ValueError("The idea has characters that are not text.")
    return value.strip()


def checked_kind(value: Any) -> str:
    if value not in KINDS:
        raise ValueError(f"kind is one of: {', '.join(KINDS)}.")
    return str(value)


# ── The fixed prompts ─────────────────────────────────────────────────────

_RULES = """\
You turn a person's short, messy idea for a job into a clean draft. You never invent.

Answer with ONE JSON object inside <draft></draft> tags, and nothing else after it.

The object has four parts:

"said": ONLY what the person actually said, rewritten clearly. Never add a detail they did not give:
no sizes, materials, colours, quantities, dates, places, styles or names of your own.
If they did not say it, it does not go here; ask about it in "questions" instead.

"proposed": suggestions for what the idea left open, which the person may accept or not.

"questions": short, plain questions about what the person must decide before anyone can do the work.
Ask about the things a worker would need: sizes, materials, quantities, style, where, when, what done looks like.
At most 8. Do not ask about anything the person already said.

"assumptions": anything you had to assume to write the draft, said plainly, so the person can correct it.
At most 8.

Write to the person as "you", in the same language as the idea, in short, plain sentences.
"""

_COMMISSION = _RULES + """
This is a commission: a client pays a professional in bitcoin for a piece of work.
Both sign one offer under the Task Booth Standard Terms. The client may pay a deposit when the work is claimed
and dated milestones before it is done; the professional delivers; the client grades the work against grades
set in advance, and the grade decides what the rest pays. Work the client does not grade in time is deemed
accepted at a named grade. The cover page says who owns the work afterwards (IP).

The JSON shape:
{"said": {"title": "...", "brief": "...", "deliverables": ["..."], "needed_by": "YYYY-MM-DD" or null},
 "proposed": {"grades": [{"name": "...", "amount": 0, "criteria": "..."}], "deposit_pct": 0,
              "milestones": [{"name": "...", "pct": 0, "after_days": 0}], "deemed_days": 14,
              "ip_option": "assign" | "license" | "worker_keeps"},
 "questions": ["..."], "assumptions": ["..."]}

- title: a short name for the work, at most 80 characters.
- brief: the idea restated in a few clear sentences, in markdown, with what the person said and nothing more.
- deliverables: each thing the person said they would receive, one per line. Empty if they named none.
- needed_by: a calendar date ONLY if the idea gave a time ("by March 3", "in two weeks", "before Christmas").
  Today's date is given with the idea, for working out relative times. Otherwise null.
- grades: 2 to 4 grades from best to worst, the last usually worth 0. "amount" is in the person's currency
  (given with the idea), a fair price for the work as described; it is only a suggestion.
- deposit_pct: 0 to 50. milestones: 0 to 3, each a share (pct) of the top grade paid after_days from claiming.
  deposit_pct plus every milestone's pct is at most 90.
- deemed_days: days the client has to grade before the work is deemed accepted, 7 to 30.
- ip_option: assign (the client owns it once paid), license (the client may use it; the maker keeps ownership),
  or worker_keeps (the maker keeps every right). Propose assign unless the idea suggests otherwise.

Example. Idea: "need a portrait of my grandfather from an old photo, oil maybe, for his 90th in june"
<draft>{"said": {"title": "Portrait of my grandfather from an old photo",
"brief": "A painted portrait of your grandfather, worked from an old photograph you will supply, for his 90th birthday.\\n\\nYou are thinking of oil paint.",
"deliverables": ["A painted portrait of your grandfather"], "needed_by": "2027-06-01"},
"proposed": {"grades": [{"name": "Accepted", "amount": 900, "criteria": "A good likeness, finished in time for the birthday"},
{"name": "Accepted with changes", "amount": 700, "criteria": "A good likeness after one round of changes"},
{"name": "Not accepted", "amount": 0, "criteria": "Not a likeness, or not delivered"}],
"deposit_pct": 20, "milestones": [{"name": "Sketch approved", "pct": 20, "after_days": 14}], "deemed_days": 14, "ip_option": "assign"},
"questions": ["What size should the portrait be?", "Is it oil, or is the painter free to choose?",
"Should it be framed?", "What day in June is the birthday?", "Will you send the photo as a scan or the original?"],
"assumptions": ["June 1 is used as the needed-by date until you give the birthday."]}</draft>

Example. Idea: "walnut dining table seats 8"
<draft>{"said": {"title": "Walnut dining table for eight",
"brief": "A dining table made of walnut that seats eight people.",
"deliverables": ["A walnut dining table that seats eight"], "needed_by": null},
"proposed": {"grades": [{"name": "Accepted", "amount": 4000, "criteria": "Built as agreed, solid and well finished"},
{"name": "Accepted with repairs", "amount": 3200, "criteria": "Needed repairs before it was right"},
{"name": "Not accepted", "amount": 0, "criteria": "Not as agreed"}],
"deposit_pct": 30, "milestones": [{"name": "Top glued up", "pct": 30, "after_days": 30}], "deemed_days": 14, "ip_option": "worker_keeps"},
"questions": ["What size should the table be?", "What shape: rectangular, oval or round?", "What finish do you want?",
"Do you need it delivered, and where?", "When do you need it?"],
"assumptions": ["The maker keeps the design, since furniture makers usually do."]}</draft>
"""

_CHORE = _RULES + """
This is a job around the home for a child. A parent posts it, a child takes it on and says when it is done,
and the parent checks the work and rewards it with a little bitcoin, according to grades set in advance.
Keep every word simple enough for a child to read.

The JSON shape:
{"said": {"title": "...", "brief": "...", "needed_by": "YYYY-MM-DD" or null, "repeat_days": 0 | 1 | 7},
 "proposed": {"grades": [{"name": "...", "amount": 0, "criteria": "..."}]},
 "questions": ["..."], "assumptions": ["..."]}

- title: a short name for the job, at most 60 characters.
- brief: what to do, in one to three short sentences a child understands, from what the parent said.
- needed_by: a date ONLY if the idea gave a time. Today's date is given with the idea. Otherwise null.
- repeat_days: 1 if the idea says daily, 7 if weekly, otherwise 0.
- grades: 2 or 3 grades from best to worst, the last worth 0. "amount" is in the parent's currency (given with
  the idea): a small reward a parent might give, usually well under 10.

Example. Idea: "tidy room every saturday"
<draft>{"said": {"title": "Tidy your room", "brief": "Tidy your room.", "needed_by": null, "repeat_days": 7},
"proposed": {"grades": [{"name": "Spotless", "amount": 2, "criteria": "Nothing on the floor, bed made"},
{"name": "Tidy", "amount": 1, "criteria": "Mostly put away"}, {"name": "Not done", "amount": 0, "criteria": ""}]},
"questions": ["Should the bed be made too?", "Does the floor need vacuuming?"],
"assumptions": []}</draft>

Example. Idea: "wash the car before grandma visits sunday"
<draft>{"said": {"title": "Wash the car", "brief": "Wash the car before Grandma visits on Sunday.",
"needed_by": "2026-11-22", "repeat_days": 0},
"proposed": {"grades": [{"name": "Shiny", "amount": 5, "criteria": "Clean outside, no soap left"},
{"name": "Washed", "amount": 3, "criteria": "Clean, a few spots missed"}, {"name": "Not done", "amount": 0, "criteria": ""}]},
"questions": ["Inside too, or just the outside?", "Who helps with the hose and the soap?"],
"assumptions": ["Sunday means this coming Sunday."]}</draft>
"""

SYSTEM = {"commission": _COMMISSION, "chore": _CHORE}


def user_turn(idea: str, *, today: dt.date, currency: str) -> str:
    """The one part of the request that changes: the idea, today's date, the currency."""
    return f"Today is {today.isoformat()}. The person's currency is {currency}.\n\nThe idea:\n{idea}"


# ── Reading the answer ────────────────────────────────────────────────────


@dataclass
class Draft:
    said: dict[str, Any]
    proposed: dict[str, Any]
    questions: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"said": self.said, "proposed": self.proposed,
                "questions": self.questions, "assumptions": self.assumptions}


def _lines(v: Any, limit: int, most: int = _MAX_LISTED) -> list[str]:
    items = v if isinstance(v, list) else []
    out = [t for t in (text(i, limit) for i in items) if t]
    return list(dict.fromkeys(out))[:most]


def _date(v: Any, today: dt.date) -> str | None:
    """A date from today to two years on, as YYYY-MM-DD, or nothing."""
    try:
        d = dt.date.fromisoformat(text(v, 10))
    except ValueError:
        return None
    return d.isoformat() if today <= d <= today + dt.timedelta(days=730) else None


def _grades(v: Any) -> list[dict[str, Any]]:
    rows = []
    for g in (v if isinstance(v, list) else [])[:_MAX_GRADES]:
        if not isinstance(g, dict):
            continue
        name, amount = text(g.get("name"), 40), number(g.get("amount"), 0, _MAX_AMOUNT)
        if name and amount is not None:
            rows.append({"name": name, "amount": amount, "criteria": text(g.get("criteria"), 200)})
    names = [r["name"].lower() for r in rows]
    amounts = [r["amount"] for r in rows]
    # Best to worst, distinct names, and worth something at the top; anything else is not a rubric.
    if len(rows) < 2 or len(set(names)) != len(names) or amounts != sorted(amounts, reverse=True) or amounts[0] <= 0:
        return []
    return rows


def _milestones(v: Any, deposit: int) -> list[dict[str, Any]]:
    rows = []
    for m in (v if isinstance(v, list) else [])[:_MAX_MILESTONES]:
        if not isinstance(m, dict):
            continue
        name, pct, after = text(m.get("name"), 60), whole(m.get("pct"), 1, 90), whole(m.get("after_days"), 1, 365)
        if name and pct is not None and after is not None:
            rows.append({"name": name, "pct": pct, "after_days": after})
    return rows if deposit + sum(r["pct"] for r in rows) <= 90 else []


def _invented_numbers(said: dict[str, Any], idea: str) -> list[str]:
    """Numbers the draft states as said that the idea does not contain."""
    have = {n.replace(",", ".") for n in _NUMBER.findall(idea)}
    words = " ".join([said.get("title", ""), said.get("brief", ""), *said.get("deliverables", [])])
    return [n for n in dict.fromkeys(_NUMBER.findall(words)) if n.replace(",", ".") not in have]


def validate(raw: str, *, kind: str, idea: str, today: dt.date) -> Draft:
    """The model's JSON as a :class:`Draft`, every field checked; ``DraftError`` if nothing usable."""
    try:
        obj = json.loads(raw)
    except ValueError as exc:
        raise DraftError("The AI's draft could not be read. No fare was charged.") from exc
    if not isinstance(obj, dict):
        raise DraftError("The AI's draft could not be read. No fare was charged.")
    s = obj.get("said") if isinstance(obj.get("said"), dict) else {}
    p = obj.get("proposed") if isinstance(obj.get("proposed"), dict) else {}
    questions = _lines(obj.get("questions"), 200)
    assumptions = _lines(obj.get("assumptions"), 200)

    title = text(s.get("title"), 80 if kind == "commission" else 60)
    brief = s.get("brief") if isinstance(s.get("brief"), str) else ""
    brief = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", brief).strip()[:4000]
    if not title and not brief:
        raise DraftError("The AI could not make a draft from that idea. No fare was charged.")
    said: dict[str, Any] = {"title": title, "brief": brief}

    needed_by = _date(s.get("needed_by"), today)
    if needed_by and not _TIME_WORDS.search(idea):
        needed_by = None
    if needed_by is None and not any("when" in q.lower() for q in questions):
        questions.append("When do you need it done?" if kind == "commission" else "When should it be done?")
    said["needed_by"] = needed_by

    proposed: dict[str, Any] = {"grades": _grades(p.get("grades"))}
    if kind == "commission":
        said["deliverables"] = _lines(s.get("deliverables"), 200, 12)
        deposit = whole(p.get("deposit_pct"), 0, 90)
        proposed["deposit_pct"] = deposit
        proposed["milestones"] = _milestones(p.get("milestones"), deposit or 0)
        proposed["deemed_days"] = whole(p.get("deemed_days"), 1, 90)
        proposed["ip_option"] = one_of(p.get("ip_option"), IP_CHOICES)
    else:
        repeat = whole(s.get("repeat_days"), 0, 7)
        said["repeat_days"] = repeat if repeat in (0, 1, 7) else 0

    for n in _invented_numbers(said, idea):
        assumptions.append(f"“{n}” is not in what you wrote; check it.")
    return Draft(said=said, proposed=proposed, questions=questions[:_MAX_LISTED + 1],
                 assumptions=assumptions[:_MAX_LISTED + 4])


def tidy_sats(sats: float) -> int:
    """Sats rounded to a figure a person would choose: two significant digits."""
    n = round(sats)
    if n < 100:
        return n
    step = 10 ** (len(str(n)) - 2)
    return round(n / step) * step


def with_sats(draft: Draft, price: float) -> Draft:
    """Each proposed grade's amount, at ``price`` per bitcoin, as sats beside it."""
    for g in draft.proposed.get("grades", []):
        g["sats"] = tidy_sats(g["amount"] / price * _SATS_PER_BTC)
    return draft


def from_answer(data: dict[str, Any], *, kind: str, idea: str, today: dt.date) -> Draft:
    """The whole answer read: its tagged block, then every field."""
    return validate(tagged(data, TAG), kind=kind, idea=idea, today=today)
