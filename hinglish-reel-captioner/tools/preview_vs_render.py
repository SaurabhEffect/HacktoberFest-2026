#!/usr/bin/env python3
"""Does the studio's live preview match the burned-in video? This measures it.

For nine combinations of look and position it captions the same flat-colour clip twice: once as the studio shows
it (a screenshot of the live preview in headless Chromium) and once burned in by FFmpeg + libass (a frame of the
rendered MP4). Then it compares where the caption's bounding box lands, as a share of the frame.

    pip install playwright && playwright install chromium
    python tools/preview_vs_render.py                    # prints a table and the worst difference
    python tools/preview_vs_render.py --sheet sheet.png  # also saves a side-by-side picture

Needs FFmpeg with libass on the PATH. Chromium builds without H.264 (Playwright's own is one) cannot play the
test clip, so a WebM copy of the same flat clip is served to the preview's <video> instead. That only affects the
video element, never the captions. Exit status: 0 when every difference is within --max-diff, 1 when not,
2 when the check could not be run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from reelcaptioner.captions import preset_catalog  # noqa: E402
from reelcaptioner.config import FONTS_DIR  # noqa: E402

COMBOS = [("pumpkin", "lower"), ("pumpkin", "top"), ("pumpkin", "middle"), ("pumpkin", "bottom"),
          ("pop", "lower"), ("classic", "bottom"), ("box", "lower"), ("box", "top"), ("box", "middle")]
BACKGROUND = (0x6B, 0x7F, 0x79)
INSTANT = 1.1                      # seconds: inside the first caption card of the demo script
H264 = 'video/mp4; codecs="avc1.42E01E, mp4a.40.2"'


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def ffmpeg(*args: object) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)


def bbox(png: Path, inset: int = 0) -> np.ndarray | None:
    """Where everything that is not the flat background sits, as (x0, y0, x1, y1) shares of the picture."""
    pixels = np.asarray(Image.open(png).convert("RGB")).astype(int)
    h, w, _ = pixels.shape
    background = np.median(pixels.reshape(-1, 3), axis=0)           # the caption covers far less than half
    changed = np.abs(pixels - background).max(axis=2) > 40
    if inset:                                                       # a fractional-pixel edge on the screenshot
        changed[:inset], changed[h - inset:] = False, False
        changed[:, :inset], changed[:, w - inset:] = False, False
    ys, xs = np.where(changed)
    if not len(xs):
        return None
    return np.array([xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h])


def wait_for_server(port: int, proc: subprocess.Popen, seconds: float = 40.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def contact_sheet(rows: list[tuple[str, Path, Path, float]], out: Path) -> None:
    """Browser preview and FFmpeg render side by side, one pair per look/position, three pairs per row."""
    tile_w, tile_h, gap, label_h = 180, 320, 14, 30
    cols = 3
    pair_w = tile_w * 2 + 4
    sheet_rows = (len(rows) + cols - 1) // cols
    size = (cols * (pair_w + gap) + gap, sheet_rows * (tile_h + label_h + gap) + gap)
    sheet = Image.new("RGB", size, (247, 242, 230))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(FONTS_DIR / "Poppins-Medium.ttf"), 13)
    for i, (label, browser_png, render_png, diff) in enumerate(rows):
        x = gap + (i % cols) * (pair_w + gap)
        y = gap + (i // cols) * (tile_h + label_h + gap)
        draw.text((x, y + 4), f"{label}   browser | render   (off by {diff * 100:.2f}%)", fill=(20, 40, 36), font=font)
        for j, png in enumerate((browser_png, render_png)):
            sheet.paste(Image.open(png).convert("RGB").resize((tile_w, tile_h), Image.Resampling.LANCZOS),
                        (x + j * (tile_w + 4), y + label_h))
    sheet.save(out, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure the studio preview against the burned-in render.")
    parser.add_argument("--max-diff", type=float, default=0.01,
                        help="largest accepted difference, as a share of the frame (default 0.01 = 1%%)")
    parser.add_argument("--sheet", type=Path, help="save a side-by-side picture of every pair to this PNG file")
    parser.add_argument("--server-python", default=sys.executable,
                        help="python that has the app's requirements, if not the one running this script")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("This check drives a browser. Install it first:  pip install playwright && playwright install chromium",
              file=sys.stderr)
        return 2
    if not shutil.which("ffmpeg"):
        print("FFmpeg was not found on the PATH.", file=sys.stderr)
        return 2

    styles = {p["preset"]: p for p in preset_catalog()}
    port = free_port()
    base = f"http://127.0.0.1:{port}/"
    with tempfile.TemporaryDirectory(prefix="hrc-parity-") as tmp_name:
        tmp = Path(tmp_name)
        hexcolour = "0x%02x%02x%02x" % BACKGROUND
        ffmpeg("-f", "lavfi", "-i", f"color=c={hexcolour}:s=360x640:r=10:d=3", "-f", "lavfi", "-i",
               "anullsrc=r=16000:cl=mono", "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", tmp / "flat.mp4")
        log = (tmp / "server.log").open("w")
        env = dict(os.environ, HRC_MOCK_DELAY="0", PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        command = [args.server_python, "-m", "reelcaptioner", "--demo", "--no-browser", "--port", str(port),
                   "--data-dir", str(tmp / "data")]
        server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            if not wait_for_server(port, server):
                log.flush()
                print("The app did not start:\n" + (tmp / "server.log").read_text(), file=sys.stderr)
                return 2
            return measure(sync_playwright, base, tmp, styles, args)
        finally:
            server.terminate()
            try:
                server.wait(10)
            except subprocess.TimeoutExpired:
                server.kill()
            log.close()


def measure(sync_playwright, base: str, tmp: Path, styles: dict, args: argparse.Namespace) -> int:
    results: list[tuple[str, Path, Path, float]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1360, "height": 900}).new_page()
        if not page.evaluate(f"document.createElement('video').canPlayType('{H264}')"):
            ffmpeg("-i", tmp / "flat.mp4", "-an", "-c:v", "libvpx", "-b:v", "300k", tmp / "flat.webm")
            webm = (tmp / "flat.webm").read_bytes()

            def stand_in(route):
                header = {"Content-Type": "video/webm", "Accept-Ranges": "bytes"}
                wanted = route.request.headers.get("range")
                if not wanted:
                    return route.fulfill(status=200, body=webm, headers=header)
                m = re.match(r"bytes=(\d+)-(\d*)", wanted)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(webm) - 1, len(webm) - 1)
                return route.fulfill(status=206, body=webm[a:b + 1],
                                     headers={**header, "Content-Range": f"bytes {a}-{b}/{len(webm)}"})

            page.route("**/api/jobs/*/media", stand_in)

        reply = page.request.post(base + "api/jobs", multipart={
            "file": {"name": "flat.mp4", "mimeType": "video/mp4", "buffer": (tmp / "flat.mp4").read_bytes()},
            "language": "hinglish"})
        job_id = reply.json()["id"]
        for _ in range(200):
            if page.request.get(base + f"api/jobs/{job_id}").json()["status"] == "ready":
                break
            time.sleep(0.1)
        json_headers = {"Content-Type": "application/json"}

        print(f"{'look':8} {'position':8}  {'browser x0 y0 x1 y1':26} {'render x0 y0 x1 y1':26} off by")
        for look, position in COMBOS:
            style = {**styles[look], "position": position, "max_words": 4, "lines": 2}
            page.request.post(base + f"api/jobs/{job_id}/regroup", data=json.dumps({"style": style}),
                              headers=json_headers)
            page.request.post(base + f"api/jobs/{job_id}/render", data=json.dumps({"max_height": 0}),
                              headers=json_headers)
            for _ in range(600):
                task = page.request.get(base + f"api/jobs/{job_id}").json()["task"]
                if task["status"] != "working":
                    break
                time.sleep(0.1)
            if task["status"] != "done":
                print(f"The render failed: {task}", file=sys.stderr)
                return 2
            video = tmp / f"{look}_{position}.mp4"
            video.write_bytes(page.request.get(base + f"api/jobs/{job_id}/download/mp4").body())
            render_png = tmp / f"{look}_{position}_render.png"
            ffmpeg("-ss", INSTANT, "-i", video, "-frames:v", "1", render_png)

            page.goto(base + f"#/job/{job_id}")
            page.reload()
            page.wait_for_selector("#panel:not([hidden])", timeout=20000)
            page.wait_for_function("document.getElementById('player').readyState >= 2", timeout=15000)
            page.add_style_tag(content="#frame { border: 0 !important; border-radius: 0 !important; "
                                       "box-shadow: none !important; background: #6b7f79 !important }")
            page.evaluate("document.getElementById('player').controls = false; "
                          "window.dispatchEvent(new Event('resize'))")
            time.sleep(0.4)
            page.evaluate("""new Promise(done => { const v = document.getElementById('player'); v.pause();
                v.addEventListener('seeked', () => done(), {once: true}); v.currentTime = %s; })""" % INSTANT)
            time.sleep(0.4)
            browser_png = tmp / f"{look}_{position}_browser.png"
            page.locator("#frame").screenshot(path=str(browser_png))

            seen, burned = bbox(browser_png, inset=3), bbox(render_png)
            if seen is None or burned is None:
                print(f"{look} {position}: no caption was found in the {'browser' if seen is None else 'render'} frame",
                      file=sys.stderr)
                return 2
            diff = float(np.abs(seen - burned).max())
            results.append((f"{look} / {position}", browser_png, render_png, diff))
            print(f"{look:8} {position:8}  {np.array2string(np.round(seen, 3), precision=3):26} "
                  f"{np.array2string(np.round(burned, 3), precision=3):26} {diff * 100:.2f}%")
        browser.close()

        if args.sheet:
            contact_sheet(results, args.sheet)
            print(f"\nSide-by-side picture saved to {args.sheet}")

    worst = max(diff for *_, diff in results)
    verdict = "within" if worst <= args.max_diff else "OVER"
    print(f"\nWorst difference: {worst * 100:.2f}% of the frame, {verdict} the {args.max_diff * 100:.2f}% limit.")
    return 0 if worst <= args.max_diff else 1


if __name__ == "__main__":
    raise SystemExit(main())
