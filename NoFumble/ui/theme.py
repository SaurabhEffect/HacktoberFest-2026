"""Shared noFumble visual tokens for UI components and charts."""

TOKENS = {
    "background": "#F3F0E7",
    "surface": "#FFFDF6",
    "ink": "#163D38",
    "muted": "#50625D",
    "line": "#203B36",
    "accent": "#F17878",
    "accent_soft": "#FCE0D8",
    "lime": "#F5C95D",
    "success": "#187A57",
    "warning": "#946000",
    "danger": "#B43232",
}

RISK_COLORS = {
    "low": TOKENS["success"],
    "watch": TOKENS["warning"],
    "high": TOKENS["danger"],
}
