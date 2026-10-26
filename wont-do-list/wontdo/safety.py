"""The safety net: the model proposes, plain rules veto.

A small language model will sometimes tell you to ignore your tax filing.
Nothing here is clever, and that is the point: a short list of tripwires that
a model cannot talk its way past.  Only the person can overrule them, by moving
a task by hand.

False alarms cost one extra item on the list.  A missed alarm costs a late fee.
The rules lean toward false alarms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .constants import LABELS, LATER, NOW, SKIP

_FLAGS = re.I | re.X


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    note: str
    pattern: "re.Pattern[str]"


def _rule(key: str, label: str, note: str, pattern: str) -> Rule:
    return Rule(key, label, note, re.compile(pattern, _FLAGS))


RULES: List[Rule] = [
    _rule(
        "money",
        "money",
        "Money owed or due comes with late fees.",
        r"""
        \b(?: tax(?:es)? | itr | gst | vat | irs | hmrc | tds | emi | rent | mortgage | loans? | invoices?
            | premiums?\s+(?:payment|due|renewal) | insurance | (?:traffic|parking|speeding|late|library)\s+fines?
            | fines | challan | penalt(?:y|ies) | overdraft | credit\s+card | utilit(?:y|ies) | electricity
            | bijli | kiraya | bima | salary | payroll | refunds? | reimburse\w* | repay\w* | debt | dues
            | owe | owed | owes | settle\s+up | cheques?
            | (?:send|wire|transfer)\s+(?:the\s+)?(?:money|funds|payment|cash) )\b
        | (?-i:\bbills?\b)
        | \bpay(?:ing|ment|ments)?\b(?!\s+(?:attention|respect|a\s+visit|heed))
        | [\u20b9$\u20ac\u00a3]\s?\d
        | \b(?:rs\.?|inr|usd|eur|gbp)\s?\d
        """,
    ),
    _rule(
        "legal",
        "official paperwork",
        "Official paperwork has hard deadlines and slow fixes.",
        r"""
        \b(?: passport | visa | licen[cs]e | permit | court | lawyer | attorney | solicitor | legal | notar\w+
            | affidavit | contracts? | agreement | deed | custody | jury | summons | subpoena | police
            | aadhaar | aadhar | pan\s+card | voter\s+id | rc\s+book | puc | customs | immigration
            | citizenship | stamp\s+duty | power\s+of\s+attorney | renew(?:al|als|ing|ed)? | id\s+card
            | driving\s+test | e?-?kyc
            | (?:vehicle|car|company|business|marriage|birth|trademark)\s+registration
            | register\s+(?:the\s+|my\s+|our\s+|a\s+)?(?:vehicle|car|company|business|marriage|birth|trademark) )\b
        | (?-i:\bFIR\b)
        """,
    ),
    _rule(
        "health",
        "health",
        "Health tasks cost the most when they slip.",
        r"""
        \b(?: doctor | dentist | dental | orthodont\w+ | physio\w* | therap(?:y|ist|ists) | psycholog\w+
            | psychiatr\w+ | counsel+or | clinic | hospital | pharmacy | chemist | prescriptions?
            | medicines? | medications? | meds | pills? | refills? | vaccin\w+ | immuni[sz]\w+ | check-?ups?
            | blood\s+(?:test|work|pressure|sugar) | lab\s+(?:test|results?) | test\s+results? | biops\w+
            | surger\w+ | surgeon | mri | x-?rays? | ultrasound | scan\s+results? | eye\s+(?:exam|test)
            | optician | dawai | dawa | dava | injur\w+ | symptoms? | allerg\w+ | insulin | inhaler
            | physical\s+exam )\b
        | \bdr\.\s*[a-z]
        """,
    ),
    _rule(
        "safety",
        "safety",
        "Safety problems can hurt someone, so they never get skipped.",
        r"""
        \b(?: gas\s+leak | (?:water|roof|pipe|tap|faucet|oil|gas)\s+leak\w* | leaking
            | smoke\s+(?:alarm|detector) | co\s+detector | carbon\s+monoxide
            | fire\s+(?:alarm|extinguisher|safety|exit) | brakes? | brake\s+pads? | tyres? | tires?
            | exposed\s+wires? | sparking | short\s+circuit | recall\s+(?:notice|alert)
            | baby[-\s]?proof\w* | child[-\s]?lock | expired\s+(?:medicine|food) )\b
        """,
    ),
    _rule(
        "commitment",
        "a booked commitment",
        "Appointments, bookings and applications involve other people or fees.",
        r"""
        \b(?: appointments? | interviews? | exams? | viva | flights? | boarding\s+pass | web\s+check-?in
            | train\s+tickets? | tatkal | reservations? | bookings? | deadlines? | submissions? | submit
            | applications? | apply\s+(?:for|to) | filing
            | file\s+(?:my\s+|the\s+|your\s+)?(?:returns?|taxes|itr|claim) | admission | enrol+ment )\b
        """,
    ),
    _rule(
        "people",
        "a person who matters",
        "Occasions and family duties are expensive to miss and easy to forget.",
        r"""
        \b(?: birthdays? | b'?days? | anniversar(?:y|ies) | weddings? | funeral | condolences?
            | baby\s+shower | school\s+fees? | tuition | permission\s+slip | parent[-\s]teacher | ptm
            | daycare | childcare | pick\s*-?up\s+(?:the\s+)?kids? | graduation )\b
        """,
    ),
    _rule(
        "authority",
        "someone you answer to",
        "A boss, client or teacher is waiting on this.",
        r"""
        \b(?: boss | clients? | investors? | supervisor | professor | prof | advisor | adviser | ceo
            | (?:my|our)\s+manager )\b
        """,
    ),
]

RULE_LABELS: Dict[str, str] = {rule.key: rule.label for rule in RULES}
RULE_LABELS["yours"] = "your never-skip list"


def compile_never_skip(words: Sequence[str]) -> List["re.Pattern[str]"]:
    """Turn the person's own never-skip words into whole-word, case-blind patterns."""
    patterns = []
    for entry in words:
        word = str(entry).strip()
        if len(word) < 2:
            continue
        patterns.append(re.compile(r"(?<!\w)%s(?!\w)" % re.escape(word), re.I | re.U))
    return patterns


def scan(text: str, never_skip: Sequence[str] = ()) -> List[dict]:
    """Return one hit per rule that matches the task text."""
    hits: List[dict] = []
    for rule in RULES:
        match = rule.pattern.search(text)
        if match:
            hits.append({"rule": rule.key, "label": rule.label, "match": match.group(0).strip(), "note": rule.note})
    for pattern in compile_never_skip(never_skip):
        match = pattern.search(text)
        if match:
            hits.append(
                {
                    "rule": "yours",
                    "label": RULE_LABELS["yours"],
                    "match": match.group(0).strip(),
                    "note": "You asked never to skip tasks that mention this.",
                }
            )
            break
    return hits


@dataclass
class Reconciled:
    bucket: str
    flags: List[dict] = field(default_factory=list)
    must_do: bool = False


def _labels(hits: Sequence[dict]) -> str:
    seen: List[str] = []
    for hit in hits:
        if hit["label"] not in seen:
            seen.append(hit["label"])
    return ", ".join(seen)


def reconcile(
    bucket: str,
    risk: str,
    hits: Sequence[dict],
    due_days: Optional[int] = None,
    urgent: bool = False,
) -> Reconciled:
    """Apply the veto rules to what the model said.

    1. A tripwire task can never be let go or handed off.
    2. A model that rates ignoring a task 'medium' or 'high' risk cannot also skip it.
    3. A tripwire (or high-risk) task due within two days, or overdue, goes to Do now,
       and is exempt from the energy budget.
    """
    result = Reconciled(bucket=bucket)
    protected = bool(hits)
    overdue_or_soon = due_days is not None and due_days <= 2
    soon = overdue_or_soon or urgent

    if result.bucket in SKIP and protected:
        was = result.bucket
        result.bucket = LATER
        result.flags.append(
            {
                "kind": "safety",
                "title": "Safety net: %s" % _labels(hits),
                "detail": "The model said \u201c%s\u201d. %s Moved to %s. You can still overrule this."
                % (LABELS[was], hits[0]["note"], LABELS[LATER]),
                "rules": [hit["rule"] for hit in hits],
                "was": was,
                "now": LATER,
            }
        )

    if result.bucket in SKIP and risk in ("medium", "high"):
        was = result.bucket
        result.bucket = LATER
        result.flags.append(
            {
                "kind": "consistency",
                "title": "Too risky to skip",
                "detail": "The model rated the cost of ignoring this as %s, so it cannot also say \u201c%s\u201d."
                % (risk, LABELS[was]),
                "rules": [],
                "was": was,
                "now": LATER,
            }
        )

    matters = protected or risk == "high"
    if matters and soon and result.bucket != NOW:
        was = result.bucket
        result.bucket = NOW
        if due_days is not None and due_days < 0:
            when = "overdue"
        elif overdue_or_soon:
            when = "due within two days"
        else:
            when = "marked urgent"
        result.flags.append(
            {
                "kind": "due",
                "title": "Due soon",
                "detail": "This is %s and it matters, so it goes to %s." % (when, LABELS[NOW]),
                "rules": [hit["rule"] for hit in hits],
                "was": was,
                "now": NOW,
            }
        )

    result.must_do = matters and soon and result.bucket == NOW
    return result
