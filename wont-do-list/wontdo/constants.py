"""Shared vocabulary: the four buckets, effort sizes and risk levels."""

from __future__ import annotations

NOW = "now"
LATER = "later"
EXPIRE = "expire"
NOTMINE = "notmine"

BUCKETS = (NOW, LATER, EXPIRE, NOTMINE)
KEEP = (NOW, LATER)  # tasks the person will still carry
SKIP = (EXPIRE, NOTMINE)  # tasks the person is putting down

LABELS = {
    NOW: "Do now",
    LATER: "Do later",
    EXPIRE: "Let it expire",
    NOTMINE: "Not my problem",
}

# What the model writes in its JSON, and what we call it internally.
MODEL_NAMES = {NOW: "do_now", LATER: "do_later", EXPIRE: "let_expire", NOTMINE: "not_mine"}
FROM_MODEL = {name: bucket for bucket, name in MODEL_NAMES.items()}

# Loose spellings that small models produce instead of the exact enum value.
BUCKET_ALIASES = {
    "do_now": NOW,
    "now": NOW,
    "today": NOW,
    "urgent": NOW,
    "do_it_now": NOW,
    "do_later": LATER,
    "later": LATER,
    "defer": LATER,
    "schedule": LATER,
    "soon": LATER,
    "this_week": LATER,
    "let_expire": EXPIRE,
    "let_it_expire": EXPIRE,
    "expire": EXPIRE,
    "skip": EXPIRE,
    "ignore": EXPIRE,
    "drop": EXPIRE,
    "delete": EXPIRE,
    "discard": EXPIRE,
    "forget": EXPIRE,
    "not_mine": NOTMINE,
    "not_my_problem": NOTMINE,
    "notmine": NOTMINE,
    "delegate": NOTMINE,
    "someone_else": NOTMINE,
    "hand_off": NOTMINE,
    "handoff": NOTMINE,
    "waiting": NOTMINE,
}

MINUTES = (5, 10, 15, 30, 60, 120, 240)
DEFAULT_MINUTES = 15

RISKS = ("none", "low", "medium", "high")
RISK_RANK = {name: rank for rank, name in enumerate(RISKS)}

OUTCOMES = ("fine", "regret")

MAX_TASKS = 200
MAX_INPUT_CHARS = 120_000
MAX_TASK_CHARS = 300
