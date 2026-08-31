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
import http.server
import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

# ANGLE on macOS reports e.g. "ANGLE (Apple, ANGLE Metal Renderer: Apple M5, …)".
# SwiftShader reports "SwiftShader" and llvmpipe reports "llvmpipe" — either means
# the frame is being drawn on the CPU.
WANTED = ("metal", "apple")

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
# portal-valid 720p screener be recovered entirely from committed source.
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


@contextlib.contextmanager
def browser(headless: bool = True, width: int = 1024, height: int = 768):
    """A page on the system Chrome, GPU asserted before anything is drawn."""
    from playwright.sync_api import sync_playwright

    allow_software = os.environ.get("DANSE_ALLOW_SOFTWARE_RENDER") == "1"
    gpu_args = SOFTWARE_GPU_ARGS if allow_software else GPU_ARGS
    executable = os.environ.get("DANSE_CHROME_EXECUTABLE")
    launch_target = {"executable_path": executable} if executable else {"channel": "chrome"}
    with sync_playwright() as p:
        launched = p.chromium.launch(headless=headless, args=gpu_args, **launch_target)
        try:
            page = launched.new_page(viewport={"width": width, "height": height})
            gpu = page.evaluate(READ_RENDERER)
            if not gpu["ok"]:
                raise SystemExit(f"no WebGL2: {gpu['renderer']}")
            name = str(gpu["renderer"])
            if allow_software and "swiftshader" not in name.lower():
                raise SystemExit(
                    f"refusing deadline software render on {name!r}; "
                    "the portable path requires Chromium SwiftShader"
                )
            if not allow_software and not any(w in name.lower() for w in WANTED):
                raise SystemExit(
                    f"refusing to render on {name!r}.\n"
                    "This is a software rasteriser. The film would take a day and come out wrong.\n"
                    "Check that Google Chrome is installed and that channel='chrome' resolved to it."
                )
            page.gl_renderer = name
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="print the GL renderer and exit")
    ap.add_argument("--verify", action="store_true", help="run verify.html and report the verdict")
    ap.add_argument("--arrival", action="store_true", help="run the live page and check every visitor's river")
    ap.add_argument("--probe", action="store_true", help="run probe.html's projection-continuity self-test")
    ap.add_argument("--interaction", action="store_true", help="run local pose/fallback/privacy verification")
    ap.add_argument("--headed", action="store_true", help="show the window (debugging)")
    ap.add_argument("--base", help="use an already-running server instead of starting one")
    args = ap.parse_args()

    if not args.check and not args.verify and not args.arrival and not args.probe and not args.interaction:
        ap.error("nothing to do — pass --check, --verify, --arrival, --probe or --interaction")

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
            if not args.verify and not args.arrival and not args.probe and not args.interaction:
                return 0
        rc = run_verify(page, base) if args.verify else 0
        if args.arrival:
            rc = run_arrival(page, base) or rc
        if args.probe:
            rc = run_probe(page, base) or rc
        if args.interaction:
            rc = run_interaction(page, base) or rc
        return rc


if __name__ == "__main__":
    sys.exit(main())
