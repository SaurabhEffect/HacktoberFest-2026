"""The front end is plain files with no build step, so nothing would catch a typo before a user does.

These checks do what a bundler would: references resolve, imports exist, every id the scripts look up is in the
page, every endpoint the UI calls is really served, and nothing reaches outside the app.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from math import atan2, hypot, pi, sin
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelcaptioner.app import create_app
from reelcaptioner.config import STATIC_DIR

from .conftest import make_settings

INDEX = STATIC_DIR / "index.html"
JS_DIR = STATIC_DIR / "js"
JS_FILES = sorted(JS_DIR.glob("*.js"))
CSS_FILES = [STATIC_DIR / "app.css", STATIC_DIR / "studio.css"]
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


class Page(HTMLParser):
    """Everything the checks need to know about index.html."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {k: v or "" for k, v in attrs}))

    def where(self, tag: str) -> list[dict[str, str]]:
        return [attrs for name, attrs in self.elements if name == tag]

    @property
    def ids(self) -> list[str]:
        return [attrs["id"] for _, attrs in self.elements if "id" in attrs]


@pytest.fixture(scope="module")
def page() -> Page:
    parser = Page()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    parser.close()
    return parser


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


# --------------------------------------------------------------------------- the page itself
def test_the_page_has_the_basics(page):
    html = read(INDEX)
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert page.where("html")[0].get("lang") == "en"
    assert any(m.get("name") == "viewport" and "width=device-width" in m.get("content", "") for m in page.where("meta"))
    assert re.search(r"<title>[^<]{3,}</title>", html)
    assert len(page.where("h1")) == 1


def test_ids_are_unique(page):
    dupes = sorted({i for i in page.ids if page.ids.count(i) > 1})
    assert not dupes, f"duplicate ids: {dupes}"


def test_links_labels_and_aria_references_point_at_real_ids(page):
    ids = set(page.ids)
    for attrs in page.where("a"):
        href = attrs.get("href", "")
        if href.startswith("#") and not href.startswith("#/"):          # "#/" is the app's own router
            assert href[1:] in ids, f"link {href} has no target"
    for attrs in page.where("label"):
        if "for" in attrs:
            assert attrs["for"] in ids, f"<label for={attrs['for']}> has no control"
    for _, attrs in page.elements:
        for attr in ("aria-labelledby", "aria-controls", "aria-describedby"):
            for ref in attrs.get(attr, "").split():
                assert ref in ids, f"{attr}={ref} has no target"


def test_buttons_never_submit_a_form_by_accident(page):
    untyped = [b.get("id") or b for b in page.where("button") if "type" not in b]
    assert not untyped, f"buttons without a type attribute: {untyped}"


def test_every_range_slider_has_its_readout(page):
    """The style panel writes each slider's value into <output id="<slider id>Out">."""
    ids = set(page.ids)
    sliders = [i["id"] for i in page.where("input") if i.get("type") == "range"]
    assert sliders
    assert [s for s in sliders if f"{s}Out" not in ids] == []


# --------------------------------------------------------------------------- files the page needs
def test_every_local_file_the_page_references_exists(page):
    refs = [(tag, attrs.get("href") or attrs.get("src")) for tag in ("link", "script")
            for attrs in page.where(tag) if attrs.get("href") or attrs.get("src")]
    assert any(url.endswith("app.js") for _, url in refs) and any(url.endswith("app.css") for _, url in refs)
    for tag, url in refs:
        if url.startswith("data:"):
            continue
        assert url.startswith("/static/"), f"<{tag}> reaches outside the app: {url}"
        assert (STATIC_DIR / url[len("/static/"):]).is_file(), f"<{tag}> points at a missing file: {url}"


@pytest.mark.parametrize("css", CSS_FILES, ids=lambda p: p.name)
def test_every_url_in_the_css_is_bundled(css):
    urls = re.findall(r"url\(\s*[\"']?([^)\"']+)[\"']?\s*\)", strip_css_comments(read(css)))
    for url in urls:
        if url.startswith(("data:", "#")):
            continue
        assert url.startswith("/static/"), f"{css.name} reaches outside the app: {url}"
        assert (STATIC_DIR / url[len("/static/"):]).is_file(), f"{css.name} points at a missing file: {url}"


def test_the_bundled_fonts_ship_with_their_licence():
    fonts = {p.name for p in (STATIC_DIR / "fonts").glob("*.ttf")}
    assert {"Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-Bold.ttf", "Lora-Italic-Variable.ttf"} <= fonts
    assert "OPEN FONT LICENSE" in read(STATIC_DIR / "fonts" / "OFL.txt").upper()


def test_nothing_in_the_front_end_reaches_outside_the_app():
    """The privacy promise: no CDN, no analytics, no web fonts. (www.w3.org is only an SVG namespace string.)"""
    for path in [*JS_FILES, *CSS_FILES, INDEX]:
        text = read(path)
        absolute = [u for u in re.findall(r"""(?:src|href)=["'](https?://[^"']+)|url\(\s*["']?(https?://[^)"']+)""", text)
                    if any(u)]
        assert not absolute, f"{path.name} loads something from the internet: {absolute}"
    for path in JS_FILES:
        assert not re.findall(r"https?://(?!www\.w3\.org)", read(path)), f"{path.name} contains an absolute URL"


def test_the_outline_is_not_drawn_with_text_stroke():
    """-webkit-text-stroke has mitred corners (spikes on A, M, N) that the burned-in render does not have."""
    for css in CSS_FILES:
        assert "text-stroke" not in strip_css_comments(read(css)), css.name
    for path in JS_FILES:
        assert not re.search(r"webkitTextStroke|setProperty\(\s*[\"']-webkit-text-stroke", read(path)), path.name


# --------------------------------------------------------------------------- the scripts
IMPORT = re.compile(r"""import\s*\{([^}]*)\}\s*from\s*["'](\.[^"']+)["']""")
EXPORT = re.compile(r"export\s+(?:async\s+)?(?:function|class|const|let)\s+([A-Za-z_$][\w$]*)")


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_every_import_resolves_to_something_exported(path):
    for names, target in IMPORT.findall(read(path)):
        module = (path.parent / target).resolve()
        assert module.is_file(), f"{path.name} imports {target}, which does not exist"
        wanted = {n.strip().split(" as ")[0].strip() for n in names.split(",") if n.strip()}
        missing = wanted - set(EXPORT.findall(read(module)))
        assert not missing, f"{path.name} imports {sorted(missing)} from {target}, which does not export them"


def test_the_modules_do_not_import_each_other_in_circles():
    graph = {p.name: {Path(t).name for _, t in IMPORT.findall(read(p))} for p in JS_FILES}

    def visit(node: str, trail: tuple[str, ...]) -> None:
        assert node not in trail, f"import cycle: {' -> '.join((*trail, node))}"
        for nxt in graph.get(node, ()):
            visit(nxt, (*trail, node))

    for name in graph:
        visit(name, ())


STATIC_ID = re.compile(r"""(?:\$|this\.el|getElementById)\(\s*["']([A-Za-z][\w-]*)["']\s*\)""")


def test_every_id_the_scripts_look_up_is_in_the_page(page):
    ids = set(page.ids)
    missing = {}
    for path in JS_FILES:
        for found in STATIC_ID.findall(read(path)):
            if found not in ids:
                missing.setdefault(found, []).append(path.name)
    assert not missing, f"ids used by scripts but absent from index.html: {missing}"


def test_the_tab_ids_the_script_builds_exist(page):
    """app.js finds tabs as `t-<name>` and their panels as `tab-<name>`, from one list of names."""
    ids = set(page.ids)
    listed = re.search(r"const tabs = \[([^\]]*)\]", read(JS_DIR / "app.js"))
    assert listed, "the tab list moved: update this test"
    names = re.findall(r"[\"']([a-z]+)[\"']", listed.group(1))
    assert names
    for name in names:
        assert {f"t-{name}", f"tab-{name}"} <= ids, name


@needs_node
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_every_script_is_valid_module_syntax(path, tmp_path):
    copy = tmp_path / f"{path.stem}.mjs"                    # node only treats .mjs as ES modules without a package.json
    copy.write_text(read(path), encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(copy)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


SHADOW = re.compile(r"(-?\d+(?:\.\d+)?)px (-?\d+(?:\.\d+)?)px 0 (\S+)")


@needs_node
def test_the_outline_is_two_unbroken_rings_of_hard_shadows(tmp_path):
    module = tmp_path / "preview.mjs"
    module.write_text(read(JS_DIR / "preview.js"), encoding="utf-8")
    radii = [0, 0.29, 0.3, 0.5, 1, 3, 6]
    runner = tmp_path / "run.mjs"
    runner.write_text(
        f"import {{ outlineShadow }} from {json.dumps(module.as_uri())};\n"
        f"const out = {{}};\nfor (const r of {json.dumps(radii)}) out[r] = outlineShadow(r, '#17302B');\n"
        "console.log(JSON.stringify(out));\n", encoding="utf-8")
    done = subprocess.run([NODE, str(runner)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    shadows = json.loads(done.stdout)

    assert shadows["0"] == shadows["0.29"] == "none"                       # too thin to see: draw nothing
    for radius in (0.3, 0.5, 1, 3, 6):
        parts = shadows[str(radius)].split(",")
        assert len(parts) <= 64                                              # keeps the CSS small and fast
        points = []
        for part in parts:
            x, y, colour = SHADOW.fullmatch(part.strip()).groups()           # offset, offset, zero blur, colour
            assert colour == "#17302B"
            points.append((float(x), float(y)))
        reach = [hypot(x, y) for x, y in points]
        assert {round(r / radius, 1) for r in reach} == {0.5, 1.0}, "an outer ring and a half-size inner ring"
        outer = sorted((atan2(y, x) for (x, y), r in zip(points, reach) if r > radius * 0.75))
        gaps = [b - a for a, b in zip(outer, outer[1:])] + [outer[0] + 2 * pi - outer[-1]]
        assert max(2 * radius * sin(g / 2) for g in gaps) <= 1.2, "neighbouring copies must overlap: no scalloped edge"


# --------------------------------------------------------------------------- the contract with the server
UI_CALLS = [
    ("GET", "/api/status"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs/{id}"),
    ("DELETE", "/api/jobs/{id}"),
    ("GET", "/api/jobs/{id}/cards"),
    ("POST", "/api/jobs/{id}/sync"),
    ("POST", "/api/jobs/{id}/regroup"),
    ("POST", "/api/jobs/{id}/cancel"),
    ("POST", "/api/jobs/{id}/render"),
    ("GET", "/api/jobs/{id}/download/{kind}"),
]


def expand(path: str):
    """`${cond ? "a" : "b"}` inside a template literal stands for two different endpoints."""
    ternary = re.search(r"""\$\{[^}]*\?\s*"([^"]+)"\s*:\s*"([^"]+)"\s*\}""", path)
    if ternary:
        for word in ternary.groups():
            yield from expand(path[:ternary.start()] + word + path[ternary.end():])
    else:
        yield re.sub(r"\$\{[^}]*\}", "{x}", path)


def api_paths_used_by_scripts() -> set[str]:
    used: set[str] = set()
    for path in JS_FILES:
        text = read(path)
        for raw in re.findall(r"`(/api/[^`]*)`", text) + re.findall(r'"(/api/[^"]*)"', text):
            used.update(expand(raw))
    return used


def test_the_table_of_endpoints_matches_what_the_scripts_call():
    listed = {re.sub(r"\{[^}]*\}", "{x}", path) for _, path in UI_CALLS}
    assert api_paths_used_by_scripts() == listed, "the UI calls a different set of endpoints than UI_CALLS lists"


def test_the_server_serves_every_endpoint_the_ui_calls(tmp_path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        routes = [(route.path_regex, route.methods) for route in client.app.routes
                  if getattr(route, "methods", None) and hasattr(route, "path_regex")]
    for method, path in UI_CALLS:
        concrete = re.sub(r"\{[^}]*\}", "abc123", path)
        assert any(method in methods and rx.match(concrete) for rx, methods in routes), f"{method} {path} is not served"


def test_the_upload_form_sends_only_fields_the_server_understands(tmp_path):
    schema = create_app(make_settings(tmp_path)).openapi()
    body = schema["paths"]["/api/jobs"]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    if "$ref" in body:
        body = schema["components"]["schemas"][body["$ref"].split("/")[-1]]
    accepted, required = set(body["properties"]), set(body.get("required", []))
    sent = set(re.findall(r"""form\.append\(\s*["']([a-z_]+)["']""", read(JS_DIR / "app.js")))
    assert sent, "the upload code moved: update this test"
    assert sent <= accepted, f"the UI sends fields the server ignores: {sorted(sent - accepted)}"
    assert required <= sent, f"the server requires fields the UI never sends: {sorted(required - sent)}"
