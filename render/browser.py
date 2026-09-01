#!/usr/bin/env python3
"""A browser with a real GPU in it — and the assertion that it is one.

The film is 23,400 frames. Rendering all of them on a software rasteriser takes
most of a day and produces a file that looks subtly, unfixably wrong, and the
only way to find out is to watch the whole thing at the end. So every path that
opens a browser for danse comes through here, and here refuses to proceed unless
the GL renderer string names Apple's Metal backend.

Two facts this encodes, both measured rather than assumed:

  - Playwright's BUNDLED chromium is not installed on this machine, and
    `chrome-headless-shell` has no GPU at all. `channel="chrome"` — the system
    Google Chrome in /Applications — is the one that gets ANGLE Metal.
  - `--headless=new` keeps the GPU. The old headless mode does not.

    render/browser.py --check          # print the GL renderer and exit
    render/browser.py --verify         # run verify.html, print the verdict
    render/browser.py --arrival        # two visitors, two rivers, in a real browser
    render/browser.py --probe          # projection continuity, numerically
    render/browser.py --interaction    # local pose privacy, fallback, replay, model
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

# The machine-bound receipt contract accepts only system Chrome's structured
# ANGLE identity.  An Apple OpenGL label or non-Apple Metal renderer must never
# satisfy the canonical production gate.
APPLE_ANGLE_METAL_RENDERER = re.compile(
    r"\AANGLE \(Apple, ANGLE Metal Renderer: "
    r"Apple M[1-9][0-9]*(?: (?:Pro|Max|Ultra))?, Unspecified Version\)\Z"
)
CANONICAL_RENDER_CONTEXT = "canonical"
EMERGENCY_SOFTWARE_CAPTURE_CONTEXT = "emergency-software-capture"

READ_RENDERER = """
() => {
  const c = document.createElement("canvas");
  const gl = c.getContext("webgl2");
  if (!gl) return { ok: false, renderer: "no webgl2 context" };
  const ext = gl.getExtension("WEBGL_debug_renderer_info");
  return {
    ok: true,
    renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
    maxTexture: gl.getParameter(gl.MAX_TEXTURE_SIZE),
  };
}
"""

# Without these, headless Chrome quietly falls back to SwiftShader on a machine
# with no attached display — which is exactly the situation a background render
# runs in.
GPU_ARGS = [
    "--use-angle=metal",
    "--enable-gpu",
    "--ignore-gpu-blocklist",
    "--enable-unsafe-webgpu",
    "--disable-gpu-driver-bug-workarounds",
    # The film's plates are large and numerous; the default cache evicts them
    # mid-segment and the reads stall behind refetches.
    "--disable-dev-shm-usage",
]

# A deadline screener can be rendered on a standard cloud runner when the
# project Mac is unavailable.  This is deliberately opt-in: production and
# exhibition captures still require the measured Metal path above.  Chromium's
# supported SwiftShader WebGL backend keeps the render deterministic and lets a
# portal-valid 1080p screener be recovered entirely from committed source.
SOFTWARE_GPU_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--disable-gpu-driver-bug-workarounds",
    "--disable-dev-shm-usage",
]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    """Serves the app, and — when a sink is attached — swallows frames.

    The capture path posts raw RGBA back to the same origin it loaded from. A
    3840×2160 frame is 33 MB; routing it over CDP as base64 instead would cost
    more than rendering it. Keeping the sink here means the whole render is one
    Python process with an ffmpeg on the end, and no second runtime to supervise.
    """

    sink = None

    def log_message(self, *_args):  # a render log is not an access log
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.sink is None:
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            self.sink(self.path, body)
        except Exception as exc:  # a broken ffmpeg must fail the render, loudly
            self.send_error(500, str(exc))
            return
        self.send_response(204)
        self.end_headers()


@contextlib.contextmanager
def serve(root: Path = APP, port: int = 0, sink=None):
    """A static server over the app directory. Port 0 picks a free one, so two
    renders running side by side never collide. `sink(path, body)` receives POSTs.

    Threaded: a 33 MB frame upload must not block the page's next module fetch.
    """
    handler = functools.partial(_Quiet, directory=str(root))
    if sink is not None:
        handler = functools.partial(type("_Sinking", (_Quiet,), {"sink": staticmethod(sink)}), directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        actual = httpd.socket.getsockname()[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{actual}"
        finally:
            httpd.shutdown()


def reachable(url: str, timeout: float = 0.4) -> bool:
    host, _, port = url.removeprefix("http://").partition(":")
    with contextlib.suppress(OSError):
        with socket.create_connection((host, int(port or 80)), timeout=timeout):
            return True
    return False


def renderer_matches_context(name: str, render_context: str) -> bool:
    """Classify one measured renderer under the exact requested contract."""
    if render_context == CANONICAL_RENDER_CONTEXT:
        return APPLE_ANGLE_METAL_RENDERER.fullmatch(name) is not None
    if render_context == EMERGENCY_SOFTWARE_CAPTURE_CONTEXT:
        return "swiftshader" in name.lower()
    return False


def emergency_executable_identity(raw: str | None) -> dict[str, str]:
    """Resolve and bind the exact Chromium binary used for emergency pixels."""
    if not raw:
        raise SystemExit("emergency software capture requires DANSE_CHROME_EXECUTABLE")
    selected = Path(raw)
    if not selected.is_absolute():
        raise SystemExit("DANSE_CHROME_EXECUTABLE must be an absolute path")
    try:
        resolved = selected.resolve(strict=True)
    except OSError as exc:
        raise SystemExit(f"cannot resolve DANSE_CHROME_EXECUTABLE: {exc}") from exc
    if not resolved.is_file():
        raise SystemExit(f"DANSE_CHROME_EXECUTABLE is not a regular file: {resolved}")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    version = subprocess.run(
        [str(resolved), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    line = (version.stdout + version.stderr).strip().splitlines()
    if version.returncode != 0 or not line:
        raise SystemExit(f"cannot identify DANSE_CHROME_EXECUTABLE: {resolved}")
    return {"path": str(resolved), "sha256": digest.hexdigest(), "version": line[0]}


@contextlib.contextmanager
def browser(
    headless: bool = True,
    width: int = 1024,
    height: int = 768,
    *,
    render_context: str = CANONICAL_RENDER_CONTEXT,
):
    """A page under one explicit renderer contract, asserted before drawing.

    Canonical callers always use system Chrome and Apple Metal. The emergency
    SwiftShader path is reachable only through an explicit capture context; an
    ambient ``DANSE_ALLOW_SOFTWARE_RENDER`` cannot weaken verification or a
    production render. The executable override is likewise emergency-only.
    """
    from playwright.sync_api import sync_playwright

    if render_context == CANONICAL_RENDER_CONTEXT:
        gpu_args = GPU_ARGS
        launch_target = {"channel": "chrome"}
        executable_identity = None
    elif render_context == EMERGENCY_SOFTWARE_CAPTURE_CONTEXT:
        gpu_args = SOFTWARE_GPU_ARGS
        executable_identity = emergency_executable_identity(os.environ.get("DANSE_CHROME_EXECUTABLE"))
        launch_target = {"executable_path": executable_identity["path"]}
    else:
        raise ValueError(f"unknown browser render context: {render_context!r}")
    with sync_playwright() as p:
        launched = p.chromium.launch(headless=headless, args=gpu_args, **launch_target)
        try:
            page = launched.new_page(viewport={"width": width, "height": height})
            gpu = page.evaluate(READ_RENDERER)
            if not gpu["ok"]:
                raise SystemExit(f"no WebGL2: {gpu['renderer']}")
            name = str(gpu["renderer"])
            if not renderer_matches_context(name, render_context):
                if render_context == EMERGENCY_SOFTWARE_CAPTURE_CONTEXT:
                    raise SystemExit(
                        f"refusing deadline software render on {name!r}; "
                        "the portable path requires Chromium SwiftShader"
                    )
                raise SystemExit(
                    f"refusing to render on {name!r}.\n"
                    "The renderer is not the canonical system-Chrome ANGLE Apple-Metal backend.\n"
                    "Check that Google Chrome is installed, channel='chrome' resolves to it, "
                    "and ANGLE reports the Apple Metal device identity."
                )
            page.gl_renderer = name
            page.browser_executable_identity = executable_identity
            yield page
        finally:
            launched.close()


def run_verify(page, base: str) -> int:
    """The regression net: verify.html renders the flat state and measures it
    against the 25 July 2017 composite. Any engine change that stops the piece
    being a reproduction shows up here as a number, not as an opinion."""
    page.goto(f"{base}/verify.html", wait_until="load")
    page.wait_for_function("() => window.danseVerify !== undefined", timeout=180_000)
    r = page.evaluate("() => window.danseVerify")

    print(f"\n  renderer   {page.gl_renderer}")
    print(f"  live path  {r['live']:.2f} dB")
    print(f"  ceiling    {r['plateCeiling']:.2f} dB  (same score, same plates, numpy, no GPU)")
    print(f"  gap        {abs(r['live'] - r['plateCeiling']):.2f} dB\n")
    for run in r["runs"]:
        print(f"    {run['psnr']:>6.2f} dB   {run['label']}")
    print()
    if r["pass"]:
        print("REPRODUCTION HOLDS — the flat state is still the 2017 piece")
        return 0
    print("REPRODUCTION BROKEN — an engine change moved the flat state off the original")
    return 1


SNAP = """() => ({
  seed: danse.river.seed,
  t: danse.t,
  hash: location.hash,
  stored: localStorage.getItem('danse.river'),
  program: !!danse.program,
  passage: document.getElementById('passage').textContent.trim(),
})"""


def run_arrival(page, base: str) -> int:
    """Each visitor gets their own river — checked in a real browser.

    `check-danse.py` holds arrival to arithmetic with the clock and the entropy
    replaced. What it cannot see is the part that actually faces a visitor: real
    `crypto`, real `localStorage`, a real address bar, and the fact that changing
    only a fragment is a SAME-DOCUMENT navigation. That last one is why this
    exists — a pasted river link that silently does nothing is the one way a
    shared river fails while looking like it worked.
    """
    failures: list[str] = []

    def want(ok: bool, msg: str) -> None:
        if not ok:
            failures.append(msg)

    def visit(frag: str = "", fresh: bool = True) -> dict:
        # A bare fragment change would not reload, and would skip arrive().
        if fresh:
            page.goto("about:blank", wait_until="load")
        page.goto(f"{base}/index.html{frag}", wait_until="load")
        page.wait_for_function("() => !!(window.danse && window.danse.river)", timeout=180_000)
        page.wait_for_timeout(1600)  # let the address bar tick at least once
        return page.evaluate(SNAP)

    forget = lambda: page.evaluate("() => localStorage.clear()")  # noqa: E731

    page.goto(f"{base}/index.html", wait_until="load")
    forget()
    a = visit()
    forget()
    b = visit()
    print(f"\n  visitor A   river {a['seed']:#010x}   {a['passage']}")
    print(f"  visitor B   river {b['seed']:#010x}   {b['passage']}")
    print(f"  address bar {a['hash']}")
    want(a["seed"] != b["seed"], "two cold arrivals were given the same river")
    want(a["program"] and b["program"], "the river is not what a bare URL runs")
    want("s=" in a["hash"] and "e=" in a["hash"], "the address bar does not name the river")
    want("t=" not in a["hash"], "the address bar writes t — a reload would resume, not flow on")
    want(bool(a["stored"]), "the river was not kept for the next visit")

    before = visit()
    time.sleep(3.0)
    after = visit()
    print(f"  return      river {after['seed']:#010x}   t {before['t']:.1f}s → {after['t']:.1f}s")
    want(before["seed"] == after["seed"], "a returning visitor was given a different river")
    want(after["t"] > before["t"] + 2.0, "a returning visitor rejoined upstream")

    forget()
    guest = visit(after["hash"])
    print(f"  guest       river {guest['seed']:#010x}   t {guest['t']:.1f}s against host {after['t']:.1f}s")
    want(guest["seed"] == after["seed"], "a shared link did not carry the river")
    want(abs(guest["t"] - after["t"]) < 30, "a shared link landed in different water")

    cited = visit(f"#s={after['seed']}&t=1234.5")
    print(f"  citation    river {cited['seed']:#010x}   t {cited['t']:.1f}s for 1234.5   held {cited['hash']}")
    want(abs(cited["t"] - 1234.5) < 5, "a cited moment did not resolve")
    want("t=1234.5" in cited["hash"], "a debugging position overwrote the address bar")

    arch = visit("#s=20170620")
    print(f"  archival    river {arch['seed']:#010x}   t {arch['t']:.1f}s   {arch['passage']}")
    want(arch["seed"] == 20170620 and arch["t"] < 5, "the 2017 seed no longer starts at its source")

    page.evaluate("() => { location.hash = '#s=20170620&t=777'; }")
    page.wait_for_timeout(900)
    pasted = page.evaluate(SNAP)
    print(f"  pasted      river {pasted['seed']:#010x}   t {pasted['t']:.1f}s for 777   same-document")
    want(pasted["seed"] == 20170620 and abs(pasted["t"] - 777) < 5, "a river pasted into an open page was ignored")

    print()
    if failures:
        for f in failures:
            print(f"  BROKEN — {f}")
        print("\nARRIVAL BROKEN — a visitor is not being given their own river")
        return 1
    print("ARRIVAL HOLDS — each visitor gets their own river, and it only flows forward")
    return 0


def run_probe(page, base: str) -> int:
    """Turn probe.html's projective-continuity self-test into an exit code."""
    page.goto(f"{base}/probe.html", wait_until="load")
    page.wait_for_function("() => !!(window.danse && window.danse.selfTest)", timeout=180_000)
    passed = bool(page.evaluate("() => window.danse.selfTest()"))
    verdict = page.locator("#verdict").inner_text().strip()
    print(f"\n  renderer   {page.gl_renderer}")
    for line in verdict.splitlines():
        print(f"  {line}")
    print()
    if passed:
        print("PROJECTION CONTINUITY HOLDS — window and carried-picture paths agree")
        return 0
    print("PROJECTION CONTINUITY BROKEN — placement and projector disagree")
    return 1


def run_interaction(page, base: str) -> int:
    """Exercise the browser-side adapter and instantiate only vendored pose bytes."""
    external: list[str] = []

    def observe(request) -> None:
        if not request.url.startswith((base, "data:", "blob:")):
            external.append(request.url)

    page.on("request", observe)
    page.goto(f"{base}/interaction-test.html", wait_until="load")
    page.wait_for_function(
        "() => window.danseInteractionVerify !== undefined", timeout=180_000
    )
    result = page.evaluate("() => window.danseInteractionVerify")
    page.remove_listener("request", observe)
    print(f"\n  renderer   {page.gl_renderer}")
    for run in result["runs"]:
        mark = "PASS" if run["pass"] else "FAIL"
        print(f"  {mark:4}       {run['label']}")
        if run.get("detail"):
            print(f"             {run['detail']}")
    if external:
        print("  FAIL       external request(s):")
        for url in external:
            print(f"             {url}")
    print()
    if result["pass"] and not external:
        print("INTERACTION HOLDS — local pose, fallback, dropout, and replay share one bounded adapter")
        return 0
    print("INTERACTION BROKEN — the local interaction contract did not hold")
    return 1


def run_controls(page, base: str, screenshot_dir: Path | None = None) -> int:
    """Exercise the progressive controls against the live engine adapter."""
    failures: list[str] = []
    console_errors: list[str] = []
    http_errors: list[str] = []
    page.on("console", lambda message: console_errors.append(f"{message.text} @ {message.location}") if message.type == "error" else None)
    page.on("response", lambda response: http_errors.append(f"{response.status} {response.url}") if response.status >= 400 else None)

    def want(ok: bool, message: str) -> None:
        if not ok:
            failures.append(message)

    def shot(name: str) -> None:
        if screenshot_dir is None:
            return
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        # Receipts remain in their category trays; hide only the temporary toast
        # so deterministic review captures describe the selected surface itself.
        page.evaluate("""() => {
          const toast = document.getElementById('toast');
          if (toast) { toast.hidden = true; toast.textContent = ''; }
        }""")
        page.screenshot(path=str(screenshot_dir / f"{name}.png"), full_page=False)

    def touch_targets(scope: str, label: str) -> None:
        targets = page.evaluate(
            """(scope) => {
              const root = document.querySelector(scope);
              if (!root) return [];
              const selector = [
                'button:not([disabled])',
                'a[href]:not([aria-disabled="true"])',
                'input:not([disabled]):not([type="hidden"])',
                'select:not([disabled])',
                'summary',
                'label.file-control',
              ].join(',');
              return [...root.querySelectorAll(selector)].flatMap((target) => {
                const style = getComputedStyle(target);
                const box = target.getBoundingClientRect();
                if (
                  target.hidden || target.closest('[hidden]') ||
                  style.display === 'none' || style.visibility === 'hidden' ||
                  style.visibility === 'collapse' || Number(style.opacity) === 0 ||
                  box.width <= 0 || box.height <= 0
                ) return [];
                return [{
                  name: target.id || target.dataset.category ||
                    target.getAttribute('aria-label') || target.textContent.trim().slice(0, 48),
                  width: box.width,
                  height: box.height,
                }];
              });
            }""",
            scope,
        )
        want(bool(targets), f"{label} exposed no visible interactive targets")
        undersized = [
            target
            for target in targets
            if target["width"] < 44 or target["height"] < 44
        ]
        want(not undersized, f"{label} has targets below 44px: {undersized}")

    page.set_viewport_size({"width": 1024, "height": 768})
    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{base}/index.html?score=", wait_until="load")
    page.wait_for_function("() => !!window.danse?.actions", timeout=180_000)
    page.wait_for_function("() => document.getElementById('veil').hidden", timeout=180_000)
    state = page.evaluate("() => danse.controlState")
    want(state["surface"] == "closed", "control surface did not start closed")
    want(state["music"] == "unavailable", "missing score was not stated as unavailable")
    want(page.locator("#music-tray").is_disabled(), "unavailable primary Music action stayed enabled")
    want(page.locator("#music-tray").inner_text() == "Music: unavailable", "unavailable primary Music status was incoherent")
    want(page.locator("#danse-dock button").count() == 5, "five-category dock is incomplete")
    want(page.locator("#danse-dock").is_visible(), "initialized dock stayed hidden")
    want(page.locator("#hud-toggle").is_visible(), "initialized Details control stayed hidden")
    touch_targets("#danse-dock", "desktop dock")
    shot("desktop-closed")

    page.click('[data-category="river"]')
    want(page.locator('#surface-tray[data-open="river"]').is_visible(), "River tray did not open")
    touch_targets("#surface-tray", "River tray")
    first_seed = page.evaluate("() => danse.seed")
    page.click("#river-new")
    second_seed = page.evaluate("() => danse.seed")
    want(first_seed != second_seed, "New river did not change the river")
    want(not page.locator("#river-undo").is_disabled(), "New river did not enable Undo")
    page.click("#river-undo")
    want(page.evaluate("() => danse.seed") == first_seed, "Undo did not restore the prior river")
    page.click("#river-new")
    joined_seed = page.evaluate("""() => {
      const seed = (danse.seed + 1) >>> 0;
      location.hash = `s=${seed}&e=${danse.river.epoch}&u=${danse.river.stream}`;
      return seed;
    }""")
    page.wait_for_function(f"() => danse.seed === {joined_seed}")
    want(page.locator("#river-undo").is_disabled(), "hash navigation retained stale river Undo")
    want("undo is unavailable" in page.locator("#river-receipt").inner_text().lower(), "hash navigation retained stale Undo status")
    want(page.evaluate("async () => await danse.actions.undoRiver()") is None, "hash navigation retained internal river Undo state")
    want(page.evaluate("() => danse.seed") == joined_seed, "stale Undo changed the hash-arrived river")
    page.evaluate("""() => {
      window.__sharedRiver = null;
      Object.defineProperty(navigator, 'share', { configurable:true, value:undefined });
      Object.defineProperty(navigator, 'clipboard', { configurable:true, value:{ writeText: async (value) => { window.__sharedRiver = value; } } });
    }""")
    page.click("#river-share")
    page.wait_for_function("() => !!window.__sharedRiver")
    shared = page.evaluate("""() => {
      const url = new URL(window.__sharedRiver);
      return { search:url.search, hash:url.hash };
    }""")
    want("#s=" in shared["hash"], "clipboard fallback did not receive a river link")
    want(shared["search"] == "", "shared River retained the debug score query")
    stale_share = page.evaluate("""async () => {
      let finishShare;
      Object.defineProperty(navigator, 'share', {
        configurable:true,
        value:() => new Promise((resolve) => { finishShare = resolve; }),
      });
      try {
        const pending = danse.actions.share();
        await danse.actions.newRiver();
        const before = document.getElementById('river-receipt').textContent;
        finishShare();
        await pending;
        return { before, after:document.getElementById('river-receipt').textContent };
      } finally {
        Object.defineProperty(navigator, 'share', { configurable:true, value:undefined });
      }
    }""")
    want(
        stale_share["after"] == stale_share["before"]
        and "new river" in stale_share["after"].lower(),
        "a stale Share completion overwrote the newer River receipt",
    )
    shot("desktop-river-tray")

    page.click('[data-category="score"]')
    touch_targets("#surface-tray", "Score tray")
    page.click("#program-free")
    want(page.evaluate("() => danse.controlState.program") == "free", "Free did not change the shared program state")
    page.click("#program-score")
    page.wait_for_function("() => danse.controlState.program === 'score-led'")
    page.click('[data-movement="3"]')
    want(page.evaluate("() => danse.controlState.movement") == 3, "movement selection did not use the shared action")
    page.click("#cutout-toggle")
    want(page.evaluate("() => danse.controlState.cutout") == "on", "Figure cutout state did not turn on")
    want("cutout=1" in page.url, "Figure cutout did not enter shareable presentation state")
    page.click("#score-details")
    want(page.locator("#hud").is_visible(), "visual audition sheet did not open")
    touch_targets("#hud", "Score Details")
    page.select_option("#conductor-model", "waltz")
    want(page.evaluate("() => danse.controlState.audition") == "override-active", "conductor override did not become active")
    shared = page.evaluate("""async () => {
      window.__sharedRiver = null;
      await danse.actions.share();
      const url = new URL(window.__sharedRiver);
      return { query:[...url.searchParams], mode:new URLSearchParams(url.hash.slice(1)).get('p') };
    }""")
    want(shared["query"] == [["cutout", "1"]], "shared River retained non-presentation query state")
    want(shared["mode"] is None, "Score-led shared River carried the wrong program")
    page.click("#conductor-reset")
    want(page.evaluate("() => danse.controlState.audition") == "override-ready", "conductor reset did not report override-ready")
    shot("desktop-score-sheet")
    page.click('[data-category="river"]')
    want(page.locator("#hud").is_hidden(), "opening River left Details visible")
    want(page.locator('#surface-tray[data-open="river"]').is_visible(), "River did not replace Details")
    want(page.locator("#hud-toggle").get_attribute("aria-expanded") == "false", "Details stayed expanded after opening River")
    want(page.evaluate("() => danse.controlState.surface") == "tray:river", "River replacement left surface state inconsistent")
    page.press('[data-category="river"]', "Escape")
    want(page.evaluate("() => document.activeElement?.dataset.category") == "river", "closing the replacement tray lost its trigger")
    page.click('[data-category="river"]')
    page.click('[data-category="river"]')
    page.focus('[data-category="hold"]')
    page.press('[data-category="hold"]', "Escape")
    want(
        page.evaluate("() => document.activeElement?.dataset.category") == "hold",
        "a toggle-closed tray retained a stale focus-return target",
    )
    page.click('[data-category="score"]')
    page.click("#score-details")
    page.press("#conductor-model", "Escape")
    want(page.locator("#hud").is_hidden(), "Escape did not close the advanced sheet")
    want(page.evaluate("() => document.activeElement?.dataset.category") == "score", "advanced-sheet focus did not return to the Score control")

    page.click('[data-category="presence"]')
    touch_targets("#surface-tray", "Presence tray")
    page.click("#presence-details")
    touch_targets("#hud", "Presence Details")
    page.focus("#fallback-x")
    page.press("#fallback-x", "ArrowRight")
    want(page.evaluate("() => danse.interaction.snapshot().mode") == "off", "a slider activated Presence implicitly")
    presence_race = page.evaluate("""async () => {
      const startCamera = danse.interaction.startCamera;
      let finishCamera;
      danse.interaction.startCamera = () => new Promise((resolve) => { finishCamera = resolve; });
      try {
        const camera = danse.actions.presence('camera');
        await danse.actions.presence('keyboard-touch');
        danse.actions.status('presence', 'Keyboard-touch remains selected.');
        finishCamera(false);
        await camera;
        return { presence:danse.controlState.presence, status:danse.controlState.status.presence };
      } finally {
        danse.interaction.startCamera = startCamera;
      }
    }""")
    want(presence_race == {"presence": "keyboard-touch", "status": "Keyboard-touch remains selected."}, "stale Camera completion replaced newer Presence state")
    page.click("#fallback-start")
    want(page.evaluate("() => danse.controlState.presence") == "keyboard-touch", "keyboard-touch Presence did not activate")
    want(not page.locator("#receipt-save").is_disabled(), "receipt Save stayed disabled after samples existed")
    page.click("#interaction-stop")
    page.evaluate("""() => {
      window.__originalFileText = File.prototype.text;
      const receipt = JSON.stringify(danse.interaction.receipt());
      File.prototype.text = () => new Promise((resolve) => {
        window.__finishReceiptText = () => resolve(receipt);
      });
    }""")
    page.set_input_files("#receipt-load", files=[{"name": "slow.json", "mimeType": "application/json", "buffer": b"{}"}])
    page.wait_for_function("() => typeof window.__finishReceiptText === 'function'")
    page.evaluate("""async () => {
      await danse.actions.presence('keyboard-touch');
      window.__finishReceiptText();
      await new Promise((resolve) => setTimeout(resolve));
    }""")
    want(page.evaluate("() => danse.controlState.presence") == "keyboard-touch", "slow replay receipt replaced a later Presence choice")
    page.evaluate("""() => {
      File.prototype.text = window.__originalFileText;
      delete window.__originalFileText;
      delete window.__finishReceiptText;
    }""")
    page.set_input_files("#receipt-load", files=[{"name": "invalid.json", "mimeType": "application/json", "buffer": b"{}"}])
    page.wait_for_function(
        "() => (document.getElementById('presence-receipt')?.textContent || '').toLowerCase().includes('rejected')",
        timeout=10_000,
    )
    page.wait_for_timeout(400)
    want("rejected" in page.locator("#presence-receipt").inner_text().lower(), "invalid receipt did not receive explicit rejection status")
    want(page.evaluate("() => danse.controlState.presence") == "keyboard-touch", "invalid receipt replaced the active Presence mode")
    page.press("#fallback-x", "m")
    want(page.evaluate("() => danse.controlState.cutout") == "on", "letter shortcut fired while a slider held focus")
    page.press("#fallback-x", "Escape")

    page.click('[data-category="map"]')
    page.wait_for_function("() => document.getElementById('project-map').open")
    want(page.locator("#project-map").is_visible(), "Project did not open")
    touch_targets("#project-map", "Project")
    want(page.locator("#project-artwork").is_visible(), "Project artwork record is missing")
    want("05:50.896" in page.locator("#project-film").inner_text(), "Project film target is stale")
    want("not yet encoded or rendered" in page.locator("#project-film").inner_text(), "Project opening status is missing")
    want(page.locator("#project-status [data-status]").count() == 4, "Project delivery ledger is incomplete")
    native_before = page.evaluate("() => ({ seed:danse.seed, playback:danse.controlState.playback, program:danse.controlState.program, cutout:danse.controlState.cutout, movement:danse.controlState.movement })")
    for key in ("n", "s", "f", "m", "7", "Space"):
        page.press('[data-project-section="project-artwork"]', key)
    native_after = page.evaluate("() => ({ seed:danse.seed, playback:danse.controlState.playback, program:danse.controlState.program, cutout:danse.controlState.cutout, movement:danse.controlState.movement })")
    want(native_after == native_before, "a focused Project control triggered a background shortcut")
    shot("desktop-project")
    page.press("#map-close", "h")
    want(page.locator("#project-map").is_hidden(), "H left Project open over Details")
    want(page.locator("#hud").is_visible(), "H did not open Details after closing the Map")
    want(page.evaluate("() => danse.controlState.surface") == "sheet:details", "H left surface state inconsistent after Project")
    page.press("#sheet-close", "Escape")
    page.click('[data-category="map"]')
    page.wait_for_function("() => document.getElementById('project-map').open")
    page.press("#map-close", "Escape")
    want(page.evaluate("() => document.activeElement?.dataset.category") == "map", "Project focus did not return to its trigger")

    page.focus('[data-category="hold"]')
    page.press('[data-category="hold"]', "Space")
    want(page.evaluate("() => danse.controlState.playback") == "held-user", "native button Space did not perform exactly one hold action")
    page.press('[data-category="hold"]', "Space")
    want(page.evaluate("() => danse.controlState.playback") == "running", "native button Space did not resume")

    for width in (320, 390):
        page.set_viewport_size({"width": width, "height": 780})
        page.click('[data-category="score"]')
        touch_targets("#surface-tray", f"{width}px Score tray")
        metrics = page.evaluate("""() => ({
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          targets: [...document.querySelectorAll('#danse-dock button')].map((button) => ({ width:button.getBoundingClientRect().width, height:button.getBoundingClientRect().height })),
          dock: document.getElementById('danse-dock').getBoundingClientRect().height,
        })""")
        want(not metrics["overflow"], f"{width}px layout overflowed horizontally")
        want(metrics["dock"] >= 64, f"{width}px dock was shorter than 64px")
        want(all(item["width"] >= 44 and item["height"] >= 44 for item in metrics["targets"]), f"{width}px dock has a target below 44px")
        shot(f"mobile-{width}-score-tray")
        page.click('[data-category="score"]')

    page.set_viewport_size({"width": 1024, "height": 768})
    page.evaluate("() => { document.documentElement.style.zoom = '200%'; }")
    zoom = page.evaluate("() => ({ overflow:document.documentElement.scrollWidth > document.documentElement.clientWidth, dock:document.getElementById('danse-dock').getBoundingClientRect().width })")
    want(not zoom["overflow"], "200% zoom introduced horizontal page overflow")
    want(zoom["dock"] <= page.viewport_size["width"], "200% zoom pushed the dock outside the viewport")
    page.evaluate("() => { document.documentElement.style.zoom = ''; }")

    page.emulate_media(reduced_motion="reduce")
    page.reload(wait_until="load")
    page.wait_for_function("() => !!window.danse?.actions", timeout=180_000)
    page.wait_for_function("() => document.getElementById('veil').hidden", timeout=180_000)
    want(page.evaluate("() => danse.controlState.playback") == "held-reduced", "reduced motion did not arrive held")
    page.click('[data-category="hold"]')
    want(page.evaluate("() => danse.controlState.playback") == "running", "explicit reduced-motion opt-in did not resume")
    page.click('[data-category="hold"]')
    manual_hold = page.evaluate("() => danse.t")
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_function("() => !matchMedia('(prefers-reduced-motion: reduce)').matches && danse.controlState.playback === 'held-user'")
    want(page.evaluate("() => danse.t") == manual_hold, "preference change replaced the manual held frame")
    page.click('[data-category="hold"]')
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("() => danse.controlState.playback === 'held-reduced'")
    page.emulate_media(reduced_motion="no-preference")
    page.wait_for_function("() => danse.controlState.playback === 'running'")
    shot("reduced-motion-opt-in")

    # The unavailable-score visit above proves fail-readable controls. A second
    # phase must load the shipped score and choreography and exercise Web Audio;
    # otherwise missing production files or a broken audio integration could
    # still produce a green progressive-controls receipt.
    page.goto(f"{base}/index.html", wait_until="load")
    page.wait_for_function("() => !!window.danse?.actions", timeout=180_000)
    page.wait_for_function("() => document.getElementById('veil').hidden", timeout=180_000)
    want(page.evaluate("() => danse.controlState.music") == "stopped", "shipped score did not initialize Music")
    want(not page.locator("#music-tray").is_disabled(), "shipped score left Music unavailable")
    page.click('[data-category="score"]')
    page.click("#music-tray")
    page.wait_for_function("() => danse.controlState.music === 'playing'")
    want("follows" in page.locator("#music-status").inner_text().lower(), "shipped score did not start Web Audio")
    page.click('[data-category="hold"]')
    page.wait_for_function("() => danse.controlState.music === 'suspended-by-hold'")
    want("suspended" in page.locator("#music-status").inner_text().lower(), "Hold did not suspend shipped score audio")
    page.click('[data-category="hold"]')
    page.wait_for_function("() => danse.controlState.music === 'playing'")
    page.click("#music-tray")
    page.wait_for_function("() => danse.controlState.music === 'stopped'")
    want("stopped" in page.locator("#music-status").inner_text().lower(), "shipped score audio did not stop")
    shot("shipped-score-audio")

    page.add_init_script("""(() => {
      const getContext = HTMLCanvasElement.prototype.getContext;
      HTMLCanvasElement.prototype.getContext = function(kind, ...args) {
        if (kind === 'webgl2') return null;
        return getContext.call(this, kind, ...args);
      };
    })();""")
    page.emulate_media(reduced_motion="no-preference")
    page.goto(f"{base}/index.html?score=", wait_until="load")
    page.wait_for_function("() => !!window.danse?.actions", timeout=180_000)
    page.wait_for_function("() => document.getElementById('veil').hidden", timeout=180_000)
    want(page.evaluate("() => danse.rendererAvailable") is False, "forced no-WebGL visit reported a renderer")
    want(page.locator("#renderer-fallback").is_visible(), "no-WebGL visit did not show a readable fallback")
    want(page.locator("#danse-dock").is_visible(), "no-WebGL visit did not initialize the primary controls")
    page.press("body", "h")
    want(page.locator("#hud").is_visible(), "H did not open Details without WebGL")
    touch_targets("#hud", "no-WebGL Details")
    page.wait_for_function("() => document.getElementById('river').textContent !== '—'")
    want(page.locator("#planes").inner_text() == "unavailable", "renderer fallback reported a fake plane count")
    want(page.locator("#cut").inner_text() != "—", "renderer fallback left the deterministic cut unknown")
    # Arrival supplies a random passage stream even when the displayed seed is
    # pinned. Derive the expected control index from the movement actually
    # rendered for that complete river identity instead of asserting a flaky
    # seed-only movement number.
    fallback_movement_variants = (
        (0x12345678, 60),
        (0x12345678, 90),
        (0x87654321, 60),
        (0x87654321, 90),
    )
    for fallback_seed, fallback_time in fallback_movement_variants:
        movement = page.evaluate(
            """async ({ seed, time }) => {
              danse.seed = seed;
              danse.t = time;
              await new Promise((resolve) => requestAnimationFrame(
                () => requestAnimationFrame(resolve)
              ));
              const movementId = document.getElementById('cut').textContent.split(' · ')[0];
              const expected = danse.program.movements.findIndex(
                (candidate) => candidate.id === movementId
              ) + 1;
              const selected = [...document.querySelectorAll('[data-movement]')]
                .filter((button) => button.getAttribute('aria-pressed') === 'true')
                .map((button) => Number(button.dataset.movement));
              return {
                movementId,
                expected,
                control: danse.controlState.movement,
                selected,
              };
            }""",
            {"seed": fallback_seed, "time": fallback_time},
        )
        want(
            movement["expected"] > 0
            and movement["control"] == movement["expected"]
            and movement["selected"] == [movement["expected"]],
            "no-WebGL movement did not synchronize its live Details state and "
            f"pressed Score control at seed {fallback_seed:#010x}, t={fallback_time}: "
            f"{movement}",
        )
    want(page.locator("#conductor-controls").is_hidden(), "no-WebGL visit exposed invisible visual-conductor controls")
    want(page.locator("#interaction-controls").is_hidden(), "no-WebGL visit exposed interaction controls that cannot affect a visible room")
    want(page.locator('[data-category="hold"]').is_hidden(), "no-WebGL visit exposed the unavailable Pause control")
    want(page.locator('[data-category="score"]').is_hidden(), "no-WebGL visit exposed invisible Score controls")
    want(page.locator('[data-category="presence"]').is_hidden(), "no-WebGL visit exposed interaction controls in the dock")
    want(page.locator("#interaction-summary").inner_text() == "off", "renderer fallback reported phantom interaction")
    page.press("#sheet-close", "Escape")
    want(page.locator("#hud").is_hidden(), "Escape did not close Details without WebGL")
    page.click('[data-category="map"]')
    page.wait_for_function("() => document.getElementById('project-map').open")
    want(page.locator("#project-map").is_visible(), "Project did not open without WebGL")
    touch_targets("#project-map", "no-WebGL Project")
    page.press("#map-close", "Escape")

    if console_errors:
        failures.extend(f"console error: {message}" for message in console_errors)
    if http_errors:
        failures.extend(f"HTTP error: {message}" for message in http_errors)
    print(f"\n  renderer   {page.gl_renderer}")
    print("  viewports  320x780, 390x780, 1024x768, 200% zoom")
    print("  states     closed, River, Score, sheet, Presence, Project, reduced motion")
    if failures:
        for failure in failures:
            print(f"  BROKEN — {failure}")
        print("\nPROGRESSIVE CONTROLS BROKEN")
        return 1
    print("\nPROGRESSIVE CONTROLS HOLD — shared actions, focus, status, and layouts passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="print the GL renderer and exit")
    ap.add_argument("--verify", action="store_true", help="run verify.html and report the verdict")
    ap.add_argument("--arrival", action="store_true", help="run the live page and check every visitor's river")
    ap.add_argument("--probe", action="store_true", help="run probe.html's projection-continuity self-test")
    ap.add_argument("--interaction", action="store_true", help="run local pose/fallback/privacy verification")
    ap.add_argument("--controls", action="store_true", help="run progressive-control and responsive-layout verification")
    ap.add_argument("--screenshots", type=Path, help="write control verification screenshots")
    ap.add_argument("--headed", action="store_true", help="show the window (debugging)")
    ap.add_argument("--base", help="use an already-running server instead of starting one")
    args = ap.parse_args()

    if not args.check and not args.verify and not args.arrival and not args.probe and not args.interaction and not args.controls:
        ap.error("nothing to do — pass --check, --verify, --arrival, --probe, --interaction or --controls")

    with contextlib.ExitStack() as stack:
        if args.base:
            if not reachable(args.base):
                ap.error(f"explicit --base is unreachable: {args.base}")
            base = args.base
        else:
            base = stack.enter_context(serve())
        page = stack.enter_context(browser(headless=not args.headed))

        if args.check:
            gpu = page.evaluate(READ_RENDERER)
            print(json.dumps({**gpu, "serving": base}, indent=1))
            if not args.verify and not args.arrival and not args.probe and not args.interaction and not args.controls:
                return 0
        rc = run_verify(page, base) if args.verify else 0
        if args.arrival:
            rc = run_arrival(page, base) or rc
        if args.probe:
            rc = run_probe(page, base) or rc
        if args.interaction:
            rc = run_interaction(page, base) or rc
        if args.controls:
            rc = run_controls(page, base, args.screenshots) or rc
        return rc


if __name__ == "__main__":
    sys.exit(main())
