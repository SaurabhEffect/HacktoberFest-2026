"""Where the caption geometry comes from.

The preview (CSS) and the burn-in (ASS for libass) agree because both are derived from Poppins' vertical metrics.
Change the font file, or one constant on its own, and they drift apart. These tests tie every number back to the font.

Browsers lay text out with the hhea/typo ascent and descent (1.05 and 0.35 em for Poppins); libass sizes and places
text with the Windows ascent and descent (1.135 and 0.627 em). Poppins sets USE_TYPO_METRICS and its typo and hhea
values are equal, so every browser on every platform reads the same numbers.
"""
import re
from types import SimpleNamespace

import pytest

from reelcaptioner import exporters
from reelcaptioner.config import FONTS_DIR, STATIC_DIR

ttlib = pytest.importorskip("fontTools.ttLib", reason="fontTools reads the font metrics (pip install fonttools)")

FONT_FILES = ["Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-Bold.ttf"]


@pytest.fixture(params=FONT_FILES)
def font(request):
    face = ttlib.TTFont(FONTS_DIR / request.param)
    upm, os2, hhea = face["head"].unitsPerEm, face["OS/2"], face["hhea"]
    return SimpleNamespace(
        name=request.param, os2=os2,
        ascent=hhea.ascent / upm, descent=-hhea.descent / upm,                     # what browsers use
        win_ascent=os2.usWinAscent / upm, win_descent=os2.usWinDescent / upm,       # what libass uses
        typo_ascent=os2.sTypoAscender / upm, typo_descent=-os2.sTypoDescender / upm,
    )


@pytest.fixture(scope="module")
def css() -> str:
    return re.sub(r"/\*.*?\*/", "", (STATIC_DIR / "studio.css").read_text(encoding="utf-8"), flags=re.S)


def css_value(css: str, selector: str, prop: str) -> str:
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert block, f"{selector} is not in studio.css"
    found = re.search(r"(?<![\w-])" + re.escape(prop) + r"\s*:\s*([^;]+);?", block.group(1))
    assert found, f"{selector} has no {prop}"
    return found.group(1).strip()


# --------------------------------------------------------------------------- the premise
def test_every_browser_reads_the_same_line_metrics(font):
    assert font.os2.fsSelection & (1 << 7), "USE_TYPO_METRICS is off: Windows browsers would use other numbers"
    assert (font.typo_ascent, font.typo_descent) == (font.ascent, font.descent)


# --------------------------------------------------------------------------- the ASS side
def test_libass_font_size_is_the_windows_line_box(font):
    assert exporters.ASS_EM_TO_FONTSIZE == pytest.approx(font.win_ascent + font.win_descent, abs=0.0005)


def test_libass_puts_the_baseline_a_descent_above_the_bottom_anchor(font):
    assert exporters.ASS_BELOW_BASELINE == pytest.approx(font.win_descent, abs=0.005)


def test_outline_styles_stack_lines_at_the_css_line_height(font, css):
    height = float(css_value(css, ".cap", "line-height"))
    assert exporters.LINE_PITCH == height
    half_leading = (height - (font.ascent + font.descent)) / 2
    assert exporters.BASELINE_FROM_TOP == pytest.approx(half_leading + font.ascent, abs=0.005)


def test_box_style_uses_libass_native_stacking(font, css):
    assert exporters.BOX_LINE_PITCH == exporters.ASS_EM_TO_FONTSIZE
    assert float(css_value(css, ".cap.box", "line-height")) == exporters.BOX_LINE_PITCH


# --------------------------------------------------------------------------- the CSS side of the box
def test_the_preview_box_has_the_extent_of_the_libass_box(font, css):
    """Browser: content area (hhea) plus padding. libass: the Windows ascent and descent plus the outline."""
    padding = css_value(css, ".cap.box .cap-line > span.bg", "padding")
    extra = [float("0." + digits) for digits in re.findall(r"var\(--fs\) \* \.(\d+)", padding)]
    assert len(extra) == 2, f"expected a top and a bottom padding in em, got: {padding}"
    top, bottom = extra
    assert top == pytest.approx(font.win_ascent - font.ascent, abs=0.01)
    assert bottom == pytest.approx(font.win_descent - font.descent, abs=0.01)


def test_the_preview_box_is_lifted_to_where_libass_puts_its_baseline(font, css):
    lift = css_value(css, ".cap.box .cap-line", "top")
    match = re.fullmatch(r"calc\(var\(--fs\) \* -(\d*\.?\d+)\)", lift)
    assert match, f"unexpected form: {lift}"
    half_leading = (exporters.BOX_LINE_PITCH - (font.ascent + font.descent)) / 2
    css_baseline_from_line_top = half_leading + font.ascent
    assert float(match.group(1)) == pytest.approx(css_baseline_from_line_top - font.win_ascent, abs=0.001)
