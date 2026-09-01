#!/usr/bin/env python3
"""Record the river — deterministically, in restartable segments.

This does not make the work; it makes a RECORDING of the work. The piece is the
engine running, unbounded, never the same passage twice. What comes out of here
is one stretch of it, named by the passage it caught.

The engine is a pure f(seed, t). This is the thing that exploits that: segment
*k* renders t ∈ [k·N/fps, (k+1)·N/fps) from the same function, so segments can be
rendered out of order, in parallel, on different days, and concatenated without a
seam. A failed segment costs one segment, not one film.

The capture path, every step of it chosen from measurement rather than intuition:

    draw                                        8–17 ms
      → readPixels into a PIXEL_PACK_BUFFER     direct readPixels is 889 ms. Never.
      → fenceSync + clientWaitSync(f, 0, 0)     POLLED — a large timeout throws
      → getBufferSubData                        11–28 ms
      → new Blob([buf])                         Blob 1470 MB/s vs Uint8Array 34 MB/s
      → POST to the local sink → ffmpeg stdin

Segmenting is not an optimisation. Sustained per-frame blob churn in one browser
process eventually raises net::ERR_BLOB_OUT_OF_MEMORY; a fresh process per segment
caps memory by construction.

    render.py --capture passage --tier film --segment 0
    render.py --determinism --segment 3          # render twice, require equal hashes
    render.py --concat out/passage               # stitch the segments into one recording
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
APP = HERE.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(APP / "pipeline"))
sys.path.insert(0, str(APP / "sound"))
from browser import (  # noqa: E402
    CANONICAL_RENDER_CONTEXT,
    EMERGENCY_SOFTWARE_CAPTURE_CONTEXT,
    browser,
    renderer_matches_context,
    serve,
)
from choreography import validate as validate_choreography  # noqa: E402
from corpus_contract import authorize_render_tier  # noqa: E402
from media_identity import (  # noqa: E402
    MediaIdentityError,
    decoded_video_identity,
    rgb24_stream_identity,
    video_stream_info,
)
from music_score import validate as validate_music_score  # noqa: E402

OUT = HERE / "out"

# GL reads bottom-up; every encode flips once, here, so no downstream consumer
# has to remember. ProRes 422 HQ is profile 3.
CODECS = {
    "prores": ["-c:v", "prores_ks", "-profile:v", "3", "-qscale:v", "9", "-pix_fmt", "yuv422p10le"],
    "h264": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
    "preview": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"],
}
SUFFIX = {"prores": ".mov", "h264": ".mp4", "preview": ".mp4"}
ENCODER_DIAGNOSTIC_MAX_BYTES = 64 << 10


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def hydrated_work_root() -> Path:
    """Honor the private-work mount used by corpus hydration and delivery."""
    configured = os.environ.get("DANSE_WORK")
    return Path(configured).expanduser() if configured else APP / "pipeline/.work"


def music_score_identity(args) -> dict | None:
    """Receipt-safe score identity with no local absolute path."""
    raw = getattr(args, "score", None)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise SystemExit("--score must name a non-empty score file")
    cached = getattr(args, "_music_score_identity", None)
    if cached:
        return cached
    candidate = Path(raw)
    candidate = candidate if candidate.is_absolute() else APP / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(APP.resolve())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--score must name a score file inside the repository: {raw} ({exc})") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise SystemExit(f"--score must name a regular score file: {raw}")
    try:
        score = json.loads(resolved.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid --score contract {raw}: {exc}") from exc
    try:
        score = validate_music_score(score)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid --score contract {raw}: {exc}") from exc
    args._music_score_contract = score
    identity = score.get("identity") or {}
    got = {
        "path": str(relative),
        "file_sha256": file_sha256(resolved),
        "contract_sha256": identity.get("contract_sha256"),
        "midi_sha256": identity.get("midi_sha256"),
        "provenance": score.get("provenance"),
        "stems": [
            {
                "id": stem.get("id"),
                "midi_source_sha256": stem.get("midi_source_sha256"),
                "audio_source_sha256": stem.get("audio_source_sha256"),
            }
            for stem in score.get("orchestration", [])
        ],
    }
    args._music_score_identity = got
    return got


def timing_score_identity(args) -> dict | None:
    """Bind a selected score while exposing only its passage clock to film.html."""
    raw = getattr(args, "timing_score", None)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise SystemExit("--timing-score must name a non-empty score file")
    cached = getattr(args, "_timing_score_identity", None)
    if cached:
        return cached
    candidate = Path(raw)
    candidate = candidate if candidate.is_absolute() else APP / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(APP.resolve())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"--timing-score must name a score file inside the repository: {raw} ({exc})"
        ) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise SystemExit(f"--timing-score must name a regular score file: {raw}")
    try:
        score = validate_music_score(json.loads(resolved.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid --timing-score contract {raw}: {exc}") from exc
    timing = score.get("time") if isinstance(score.get("time"), dict) else {}
    got = {
        "path": str(relative),
        "file_sha256": file_sha256(resolved),
        "contract_sha256": (score.get("identity") or {}).get("contract_sha256"),
        "passage_mapping": timing.get("passage_mapping"),
        "duration_seconds": timing.get("duration_seconds"),
    }
    args._timing_score_contract = score
    args._timing_score_identity = got
    return got


def choreography_identity(args) -> dict | None:
    """Validate and identify the choreography bound to picture and score."""
    raw = getattr(args, "choreography", None)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise SystemExit("--choreography must name a non-empty contract file")
    cached = getattr(args, "_choreography_identity", None)
    if cached:
        return cached
    score_identity = music_score_identity(args)
    if not score_identity:
        raise SystemExit("--choreography requires --score")
    candidate = Path(raw)
    candidate = candidate if candidate.is_absolute() else APP / candidate
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(APP.resolve())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--choreography must name a contract inside the repository: {raw} ({exc})") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise SystemExit(f"--choreography must name a regular contract file: {raw}")
    try:
        contract = json.loads(resolved.read_text())
        manifest_path = APP / "corpus/manifest.json"
        corpus_score_path = APP / "corpus/score-2017.json"
        manifest = json.loads(manifest_path.read_text())
        contract = validate_choreography(
            contract,
            score=args._music_score_contract,
            score_file_sha256=score_identity["file_sha256"],
            corpus_manifest=manifest,
            corpus_manifest_sha256=file_sha256(manifest_path),
            corpus_score_sha256=file_sha256(corpus_score_path),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid --choreography contract {raw}: {exc}") from exc
    got = {
        "path": str(relative),
        "file_sha256": file_sha256(resolved),
        "contract_sha256": contract["identity"]["contract_sha256"],
    }
    args._choreography_identity = got
    return got


def source_tree_sha256(args) -> str:
    """Identity of every source byte that can change an offline segment."""
    cached = getattr(args, "_source_tree_sha256", None)
    if cached:
        return cached
    roots = [
        APP / "film.html",
        APP / "render/program.json",
        APP / "render/render.py",
        APP / "render/browser.py",
        APP / "render/media_identity.py",
        APP / "pipeline/corpus_contract.py",
        APP / "corpus/manifest.json",
        APP / "corpus/room.webp",
        APP / "corpus/score-2017.json",
        APP / "corpus" / "tier-receipts" / f"{args.tier}.json",
    ]
    local = APP / "corpus/manifest.local.json"
    if local.is_file():
        roots.append(local)
    roots.extend(sorted((APP / "engine").glob("*.js")))
    score_identity = music_score_identity(args)
    if score_identity:
        roots.append(APP / score_identity["path"])
    timing_identity = timing_score_identity(args)
    if timing_identity:
        roots.append(APP / timing_identity["path"])
    choreography = choreography_identity(args)
    if choreography:
        roots.append(APP / choreography["path"])
    for kind in ("plates", "mattes"):
        roots.extend(sorted((APP / "corpus" / kind / args.tier).glob("*.webp")))
    h = hashlib.sha256()
    for path in roots:
        if not path.is_file():
            continue
        h.update(str(path.relative_to(APP)).encode())
        h.update(bytes.fromhex(file_sha256(path)))
    args._source_tree_sha256 = h.hexdigest()
    return args._source_tree_sha256


def film_url(base: str, args) -> str:
    """The one URL used by planning and rendering, including seed zero."""
    params = {"capture": args.window, "from": args.start, "tier": args.tier}
    score = music_score_identity(args)
    timing_score = timing_score_identity(args)
    choreography = choreography_identity(args)
    for key, value in (
        ("s", args.seed),
        ("u", args.stream),
        ("width", args.width),
        ("height", args.height),
        ("fps", args.fps),
        ("passage-seconds", timing_score["duration_seconds"] if timing_score else None),
        ("score", score["path"] if score else None),
        ("choreography", choreography["path"] if choreography else None),
    ):
        if value is not None:
            params[key] = value
    return f"{base}/film.html?{urlencode(params)}"


def browser_render_context(args) -> str:
    """Select software rendering only for an explicit emergency capture."""
    if getattr(args, "emergency_software_render", False):
        return EMERGENCY_SOFTWARE_CAPTURE_CONTEXT
    return CANONICAL_RENDER_CONTEXT


def emergency_source_identity(args) -> dict[str, object] | None:
    """Bind emergency receipts to clean Git sources and exact Chromium bytes."""
    if browser_render_context(args) == CANONICAL_RENDER_CONTEXT:
        return None
    cached = getattr(args, "_emergency_source_identity", None)
    if cached:
        return cached

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=APP,
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode or status.stdout.strip():
        raise SystemExit("emergency capture requires a clean tracked Git worktree")
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=APP,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{tree}"],
        cwd=APP,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if (
        len(head) not in {40, 64}
        or len(tree) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in head + tree)
    ):
        raise SystemExit("cannot bind emergency capture to an exact Git commit and tree")
    executable_value = os.environ.get("DANSE_CHROME_EXECUTABLE")
    if not executable_value:
        raise SystemExit("emergency capture requires DANSE_CHROME_EXECUTABLE")
    executable = Path(executable_value)
    if executable.is_symlink():
        raise SystemExit("emergency Chromium executable must not be a symlink")
    try:
        executable = executable.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("emergency Chromium executable is missing") from exc
    if not executable.is_file():
        raise SystemExit("emergency Chromium executable is not a regular file")
    version = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    version_line = (version.stdout or version.stderr).strip().splitlines()
    if version.returncode or not version_line:
        raise SystemExit("cannot identify emergency Chromium executable")
    program = json.loads((APP / "render/program.json").read_text())
    effective_seed = args.seed if args.seed is not None else program.get("seed")
    identity: dict[str, object] = {
        "repository_head": head,
        "repository_tree": tree,
        "effective_seed": effective_seed,
        "browser_toolchain": {
            "executable": str(executable),
            "executable_sha256": file_sha256(executable),
            "version": version_line[0],
        },
    }
    args._emergency_source_identity = identity
    return identity


def segment_identity(args, segment: int, frames: int) -> dict:
    payload = {
        "schema": "danse.render.segment.v1",
        "segment": segment,
        "frames": frames,
        "inputs": {
            "window": args.window,
            "start": args.start,
            "tier": args.tier,
            "seed": args.seed,
            "stream": args.stream,
            "codec": args.codec,
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "segment_frames": args.segment_frames,
            "source_tree_sha256": source_tree_sha256(args),
        },
    }
    score = music_score_identity(args)
    if score:
        payload["inputs"]["music_score"] = score
    choreography = choreography_identity(args)
    if choreography:
        payload["inputs"]["choreography"] = choreography
    timing_score = timing_score_identity(args)
    if timing_score:
        payload["inputs"]["timing_score"] = timing_score
        payload["inputs"]["passage_timing"] = {
            "mode": "fixed-passage",
            "seconds": timing_score["duration_seconds"],
        }
    render_context = browser_render_context(args)
    payload["inputs"]["browser_render_context"] = render_context
    if render_context != CANONICAL_RENDER_CONTEXT:
        payload["inputs"].update(emergency_source_identity(args) or {})
    return payload


def output_stem(args) -> Path:
    """Keep canonical A/B producers disjoint without renaming legacy delivery output."""
    stem_seed = args.seed if args.seed is not None else "default"
    stream_suffix = f"-stream-{args.stream}" if args.stream else ""
    renderer_suffix = (
        "-emergency-swiftshader"
        if browser_render_context(args) == EMERGENCY_SOFTWARE_CAPTURE_CONTEXT
        else ""
    )
    control_suffix = "-control" if timing_score_identity(args) else ""
    return args.out / f"{args.window}-{stem_seed}{stream_suffix}{renderer_suffix}{control_suffix}"


def segment_receipt_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".receipt.json")


def concat_listing_path(stem: Path) -> Path:
    return stem.parent / f"{stem.name}-segments.txt"


def determinism_path(stem: Path, codec: str, pass_: int) -> Path:
    return stem.parent / f".{stem.name}.determinism-{pass_}{SUFFIX[codec]}"


def passage_seed(seed: int, index: int, stream: int = 0) -> int:
    """Mirror engine/rng.js uint32 hashing for receipt-only passage identity."""
    mask = 0xFFFFFFFF
    words = (seed, 0x1A1, stream, index) if stream else (seed, 0x1A1, index)
    value = 0x9E3779B9
    for word in words:
        value = ((value ^ (word & mask)) * 0x5BD1E995) & mask
        value = ((value << 15) | (value >> 17)) & mask
    value ^= value >> 16
    value = (value * 0x85EBCA6B) & mask
    value ^= value >> 13
    value = (value * 0xC2B2AE35) & mask
    value ^= value >> 16
    return value & mask


def finite_receipt_number(value: object) -> bool:
    """Accept JSON numbers, never booleans, infinities, NaN, or oversized integers."""
    return (
        type(value) in (int, float)
        and -sys.float_info.max <= value <= sys.float_info.max
    )


def valid_passage_identity(value: object) -> bool:
    """Require the exact numeric passage shape emitted by the browser harness."""
    if not isinstance(value, dict) or set(value) != {"index", "seed", "t0", "seconds"}:
        return False
    index = value.get("index")
    seed = value.get("seed")
    t0 = value.get("t0")
    seconds = value.get("seconds")
    return (
        type(index) is int
        and index >= 0
        and type(seed) is int
        and 0 <= seed <= 0xFFFFFFFF
        and finite_receipt_number(t0)
        and t0 >= 0
        and finite_receipt_number(seconds)
        and seconds > 0
    )


def timing_capture_identity(expected: dict) -> dict | None:
    """Derive the one score-free passage a timing-control receipt must prove."""
    inputs = expected.get("inputs") if isinstance(expected.get("inputs"), dict) else {}
    timing = inputs.get("passage_timing")
    if not isinstance(timing, dict):
        return None
    seconds = timing.get("seconds")
    start = inputs.get("start")
    seed = inputs.get("seed")
    stream = inputs.get("stream")
    if seed is None:
        try:
            seed = json.loads((APP / "render/program.json").read_text())["seed"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None
    if (
        not finite_receipt_number(seconds)
        or seconds <= 0
        or not finite_receipt_number(start)
        or start < 0
        or type(seed) is not int
        or type(stream) is not int
    ):
        return None
    index = math.floor(start / seconds)
    return {
        "index": index,
        "seed": passage_seed(seed, index, stream),
        "t0": index * seconds,
        "seconds": seconds,
    }


def write_segment_receipt(
    dest: Path,
    args,
    segment: int,
    frames: int,
    *,
    capture: dict | None = None,
) -> None:
    """Persist planned inputs plus the complete renderer/output identities.

    ``capture`` is optional so callers can still read or mint the historical
    additive v1 shape.  The real renderer always supplies it; those receipts
    carry the GPU stream identity needed by downstream provenance checks.
    """

    payload = segment_identity(args, segment, frames)
    payload["file_sha256"] = file_sha256(dest)
    payload["file_bytes"] = dest.stat().st_size
    try:
        stream = video_stream_info(dest)
        if capture is not None:
            if capture.get("frames") != frames:
                raise MediaIdentityError("renderer capture frame count is stale")
            if stream["width"] != capture.get("width") or stream["height"] != capture.get("height"):
                raise MediaIdentityError("encoded segment dimensions differ from the renderer capture")
            if abs(float(stream["fps"]) - float(capture.get("fps", 0))) > 1e-9:
                raise MediaIdentityError("encoded segment rate differs from the renderer capture")
        payload["decoded_video"] = decoded_video_identity(
            dest,
            width=int(stream["width"]),
            height=int(stream["height"]),
            expected_frames=frames,
        )
    except (MediaIdentityError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot authenticate decoded segment video: {exc}") from exc
    if capture is not None:
        try:
            capture_receipt = {
                "renderer": capture["renderer"],
                "raw_rgba_sha256": capture["sha256"],
                "missing": capture["missing"],
                "signature": capture["signature"],
                "passage": capture["passage"],
            }
        except KeyError as exc:
            raise SystemExit(f"renderer capture result is incomplete: {exc.args[0]}") from exc
        render_context = payload["inputs"]["browser_render_context"]
        if (
            not isinstance(capture_receipt["renderer"], str)
            or not renderer_matches_context(capture_receipt["renderer"], render_context)
            or not isinstance(capture_receipt["raw_rgba_sha256"], str)
            or len(capture_receipt["raw_rgba_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in capture_receipt["raw_rgba_sha256"])
            or type(capture_receipt["missing"]) is not int
            or capture_receipt["missing"] < 0
            or not isinstance(capture_receipt["signature"], str)
            or not capture_receipt["signature"]
            or not valid_passage_identity(capture_receipt["passage"])
        ):
            raise SystemExit("renderer capture result has an invalid provenance identity")
        payload["capture"] = capture_receipt
    segment_receipt_path(dest).write_text(json.dumps(payload, indent=2) + "\n")

# Read the frame off the GPU without stalling the pipeline on it.
CAPTURE_JS = """
() => {
  const gl = document.getElementById("stage").getContext("webgl2");
  let pbo = null, size = 0;
  window.danseCapture = async function capture(url) {
    const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
    const need = w * h * 4;
    if (!pbo || size !== need) {
      if (pbo) gl.deleteBuffer(pbo);
      pbo = gl.createBuffer();
      size = need;
      gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pbo);
      gl.bufferData(gl.PIXEL_PACK_BUFFER, need, gl.STREAM_READ);
    }
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, pbo);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, 0);
    const fence = gl.fenceSync(gl.SYNC_GPU_COMMANDS_COMPLETE, 0);
    gl.flush();
    // Poll with a ZERO timeout. clientWaitSync with a large timeout raises
    // INVALID_OPERATION in WebGL2 — the spec forbids blocking the event loop.
    for (;;) {
      const s = gl.clientWaitSync(fence, 0, 0);
      if (s === gl.ALREADY_SIGNALED || s === gl.CONDITION_SATISFIED) break;
      if (s === gl.WAIT_FAILED) { gl.deleteSync(fence); throw new Error("fence wait failed"); }
      await new Promise((r) => setTimeout(r, 0));
    }
    gl.deleteSync(fence);
    const buf = new Uint8Array(need);
    gl.getBufferSubData(gl.PIXEL_PACK_BUFFER, 0, buf);
    gl.bindBuffer(gl.PIXEL_PACK_BUFFER, null);
    const res = await fetch(url, { method: "POST", body: new Blob([buf]) });
    if (!res.ok) throw new Error("sink " + res.status);
    return need;
  };
  return true;
}
"""


def ffmpeg_for(
    path: Path,
    width: int,
    height: int,
    fps: float,
    codec: str,
    *,
    stderr,
) -> subprocess.Popen:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-vf", "vflip",
        *CODECS[codec],
        str(path),
    ]  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=stderr)


def _encoder_diagnostic(stderr) -> str:
    """Read a bounded encoder diagnostic after the process has exited."""
    stderr.flush()
    stderr.seek(0)
    payload = stderr.read(ENCODER_DIAGNOSTIC_MAX_BYTES + 1)
    truncated = len(payload) > ENCODER_DIAGNOSTIC_MAX_BYTES
    payload = payload[:ENCODER_DIAGNOSTIC_MAX_BYTES]
    message = payload.decode(errors="replace")
    if truncated:
        message += "\n[ffmpeg stderr truncated]"
    return message


def _close_encoder_stdin(process: subprocess.Popen) -> OSError | None:
    stdin = process.stdin
    if stdin is None or stdin.closed:
        return None
    try:
        stdin.close()
    except OSError as exc:
        return exc
    return None


def _kill_and_wait_encoder(process: subprocess.Popen) -> BaseException | None:
    """Terminate a possibly-live encoder and confirm that it was reaped."""
    try:
        running = process.poll() is None
    except BaseException:
        running = True
    if running:
        try:
            process.kill()
        except BaseException:
            pass
    try:
        process.wait()
    except BaseException as exc:
        return exc
    return None


def _abort_encoder(process: subprocess.Popen) -> None:
    """Best-effort termination which never masks the capture-side exception."""
    try:
        running = process.poll() is None
    except BaseException:
        running = True
    if running:
        try:
            process.kill()
        except BaseException:
            pass
    try:
        _close_encoder_stdin(process)
    except BaseException:
        pass
    try:
        process.wait()
    except BaseException:
        pass


@contextmanager
def encoder_for_segment(
    path: Path,
    width: int,
    height: int,
    fps: float,
    codec: str,
    segment: int,
):
    """Own ffmpeg from launch through a bounded, deadlock-free exit."""
    with tempfile.TemporaryFile() as stderr:
        process = ffmpeg_for(path, width, height, fps, codec, stderr=stderr)
        try:
            yield process
        except BaseException:
            _abort_encoder(process)
            raise

        close_error = _close_encoder_stdin(process)
        if close_error is not None:
            termination_error = _kill_and_wait_encoder(process)
            detail = f"ffmpeg stdin close failed: {close_error}"
            if termination_error is not None:
                detail += f"\nffmpeg termination failed: {termination_error}"
            else:
                diagnostic = _encoder_diagnostic(stderr)
                if diagnostic:
                    detail += f"\n{diagnostic}"
            raise SystemExit(f"ffmpeg failed on segment {segment}:\n{detail}")
        try:
            returncode = process.wait()
        except BaseException:
            _abort_encoder(process)
            raise
        diagnostic = _encoder_diagnostic(stderr)
        if returncode != 0:
            detail = diagnostic or "no diagnostic"
            raise SystemExit(f"ffmpeg failed on segment {segment}:\n{detail}")


class _Slot:
    """The sink, armed late.

    The server has to exist before the page loads, but ffmpeg cannot start until
    the page has told us the frame size. One server with a swappable callback
    keeps the capture POST same-origin — two servers would make it cross-origin
    and the browser would refuse it.
    """

    fn = None

    def __call__(self, path: str, body: bytes) -> None:
        if self.fn is None:
            raise RuntimeError("frame posted before the encoder was armed")
        self.fn(path, body)


def render_segment(args, segment: int, dest: Path) -> dict:
    """One segment, start to finish, in its own browser process."""
    slot = _Slot()
    with serve(sink=slot) as base:
        # The window's format unless overridden; the page is asked for exactly
        # this size so the drawing buffer IS the delivery format.
        page_url = film_url(base, args)
        with browser(
            headless=not args.headed,
            width=320,
            height=240,
            render_context=browser_render_context(args),
        ) as page:
            page.goto(page_url, wait_until="load")
            page.wait_for_function("() => window.danseFilmReady === true", timeout=300_000)
            renderer = str(page.gl_renderer)
            film = page.evaluate(
                "() => ({ t0: window.danseFilm.window.t0, t1: window.danseFilm.window.t1,"
                " fps: window.danseFilm.window.fps, w: window.danseFilm.width, h: window.danseFilm.height,"
                " sig: window.danseFilm.signature, seed: window.danseFilm.seed,"
                " passage: window.danseFilm.passage, passageSeed: window.danseFilm.passageSeed,"
                " passageT0: window.danseFilm.passageT0,"
                " passageSeconds: window.danseFilm.passageSeconds,"
                " passageHex: window.danseFilm.passageHex })"
            )

            fps = args.fps or film["fps"]
            total = int(round((film["t1"] - film["t0"]) * fps))
            start = segment * args.segment_frames
            if start >= total:
                return {"frames": 0, "skipped": True}
            count = min(args.segment_frames, total - start)

            with encoder_for_segment(
                dest,
                film["w"],
                film["h"],
                fps,
                args.codec,
                segment,
            ) as enc:
                digest = hashlib.sha256()
                written = [0]

                def sink(_path: str, body: bytes) -> None:
                    digest.update(body)
                    enc.stdin.write(body)
                    written[0] += 1

                slot.fn = sink
                page.evaluate(CAPTURE_JS)
                began = time.time()
                missing = 0
                for i in range(count):
                    t = film["t0"] + (start + i) / fps
                    r = page.evaluate("(t) => window.danseFilm.renderAt(t)", t)
                    if timing_score_identity(args) and (
                        r.get("passage") != film["passage"]
                        or r.get("passageSeed") != film["passageSeed"]
                        or r.get("passageT0") != film["passageT0"]
                        or r.get("passageSeconds") != film["passageSeconds"]
                        or r.get("hasMusic") is not False
                        or r.get("hasChoreography") is not False
                    ):
                        raise SystemExit(
                            "timing-only control left its selected score-free passage or admitted score motion"
                        )
                    missing += r["missing"]
                    page.evaluate("(u) => window.danseCapture(u)", f"{base}/frame")
                    if args.progress and (i % 30 == 0 or i == count - 1):
                        done = i + 1
                        rate = done / max(1e-6, time.time() - began)
                        left = (count - done) / max(1e-6, rate)
                        print(
                            f"\r  seg {segment:>3} · {done}/{count} · {rate:.1f} fps · "
                            f"{r['movement']:<9} · {left / 60:4.1f} min left    ",
                            end="",
                            flush=True,
                        )
                if args.progress:
                    print()

            slot.fn = None
            if written[0] != count:
                raise SystemExit(f"segment {segment}: sank {written[0]} frames, rendered {count}")

            return {
                "frames": count,
                "missing": missing,
                "sha256": digest.hexdigest(),
                "seconds": time.time() - began,
                "signature": film["sig"],
                "renderer": renderer,
                "size": f"{film['w']}x{film['h']}",
                "width": film["w"],
                "height": film["h"],
                "fps": fps,
                "passage": {
                    "index": film["passage"],
                    "seed": film["passageSeed"],
                    "t0": film["passageT0"],
                    "seconds": film["passageSeconds"],
                },
            }


def expected_frames(segment: int, total: int, per_segment: int) -> int:
    return max(0, min(per_segment, total - segment * per_segment))


def complete(dest: Path, want: int, expected: dict) -> bool:
    """Does this segment already hold every frame it is supposed to?

    Segmenting was built so a FAILURE costs one segment rather than one film, but
    without this a RE-RUN costs the whole film anyway — and a 4K master is 39
    segments and half an hour. Frame count, not file existence: a segment killed
    mid-write leaves a perfectly plausible file with half the frames in it.
    """
    receipt_path = segment_receipt_path(dest)
    if want <= 0 or not dest.is_file() or dest.stat().st_size == 0 or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if receipt.get("schema") != expected["schema"] or receipt.get("segment") != expected["segment"]:
        return False
    if receipt.get("frames") != want or receipt.get("inputs") != expected["inputs"]:
        return False
    if (
        type(receipt.get("file_bytes")) is not int
        or receipt["file_bytes"] < 1
        or receipt["file_bytes"] != dest.stat().st_size
    ):
        return False
    if receipt.get("file_sha256") != file_sha256(dest):
        return False
    capture = receipt.get("capture")
    render_context = expected["inputs"].get("browser_render_context")
    if (
        not isinstance(capture, dict)
        or not isinstance(capture.get("renderer"), str)
        or not renderer_matches_context(capture.get("renderer", ""), render_context)
        or not isinstance(capture.get("raw_rgba_sha256"), str)
        or len(capture["raw_rgba_sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in capture["raw_rgba_sha256"])
        or type(capture.get("missing")) is not int
        or capture["missing"] < 0
        or not isinstance(capture.get("signature"), str)
        or not capture.get("signature")
        or not valid_passage_identity(capture.get("passage"))
    ):
        return False
    expected_timing_capture = timing_capture_identity(expected)
    if "timing_score" in expected["inputs"] and (
        capture is None
        or expected_timing_capture is None
        or capture.get("passage") != expected_timing_capture
    ):
        return False
    decoded = receipt.get("decoded_video")
    if decoded is not None:
        if not isinstance(decoded, dict):
            return False
        try:
            stream = video_stream_info(dest)
            if decoded.get("width") != stream["width"] or decoded.get("height") != stream["height"]:
                return False
            current = decoded_video_identity(
                dest,
                width=int(stream["width"]),
                height=int(stream["height"]),
                expected_frames=want,
            )
        except (MediaIdentityError, TypeError, ValueError):
            return False
        return decoded == current
    out = subprocess.run(
        # fmt: off
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            str(dest),
        ],
        # fmt: on
        capture_output=True,
        text=True,
    )
    return out.returncode == 0 and out.stdout.strip().isdigit() and int(out.stdout.strip()) == want


def segment_paths(stem: Path, codec: str, segments: list[int]) -> list[Path]:
    return [stem.parent / f"{stem.name}-seg-{segment:03d}{SUFFIX[codec]}" for segment in segments]


def concat_receipt_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".receipt.json")


def concat_identity(args, parts: list[Path]) -> dict:
    render_context = browser_render_context(args)
    payload = {
        "schema": "danse.render.concat.v1",
        "codec": args.codec,
        "browser_render_context": render_context,
        "segments": [
            {"name": part.name, "receipt_sha256": file_sha256(segment_receipt_path(part))} for part in parts
        ],
    }
    if render_context == EMERGENCY_SOFTWARE_CAPTURE_CONTEXT:
        payload["emergency_source_identity"] = emergency_source_identity(args)
    return payload


def planned_frame_count(parts: list[Path]) -> int:
    """Return the exact frame count owned by the ordered segment receipts."""

    total = 0
    for part in parts:
        receipt_path = segment_receipt_path(part)
        try:
            receipt = json.loads(receipt_path.read_text())
            frames = receipt["frames"]
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise MediaIdentityError(f"segment receipt has no exact frame count: {receipt_path.name}") from exc
        if type(frames) is not int or frames < 1:
            raise MediaIdentityError(f"segment receipt has an invalid frame count: {receipt_path.name}")
        total += frames
    if total < 1:
        raise MediaIdentityError("planned concat contains no frames")
    return total


def concat_decoded_video_identity(dest: Path, parts: list[Path]) -> dict[str, object]:
    """Recompute the normalized identity of one completed planned concat."""

    stream = video_stream_info(dest)
    decoded = decoded_video_identity(
        dest,
        width=int(stream["width"]),
        height=int(stream["height"]),
        expected_frames=planned_frame_count(parts),
    )
    decoded["fps"] = stream["fps"]
    return decoded


def concat_complete(stem: Path, args, parts: list[Path]) -> bool:
    dest = stem.with_suffix(SUFFIX[args.codec])
    receipt_path = concat_receipt_path(dest)
    if not dest.is_file() or not receipt_path.is_file():
        return False
    try:
        receipt = json.loads(receipt_path.read_text())
        expected = concat_identity(args, parts)
    except (OSError, json.JSONDecodeError):
        return False
    basic = (
        {key: receipt.get(key) for key in expected} == expected
        and type(receipt.get("file_bytes")) is int
        and receipt["file_bytes"] > 0
        and receipt["file_bytes"] == dest.stat().st_size
        and receipt.get("file_sha256") == file_sha256(dest)
    )
    if not basic:
        return False
    decoded = receipt.get("decoded_video")
    if decoded is None:
        return True
    if not isinstance(decoded, dict):
        return False
    try:
        return decoded == concat_decoded_video_identity(dest, parts)
    except (MediaIdentityError, TypeError, ValueError):
        return False


def concat(stem: Path, args, parts: list[Path]) -> Path:
    """Stitch exactly the planned segments without re-encoding."""
    if not parts:
        raise SystemExit(f"no planned segments at {stem}")
    missing = [part.name for part in parts if not part.is_file()]
    if missing:
        raise SystemExit("missing planned segment(s): " + ", ".join(missing))
    extras = sorted(set(stem.parent.glob(f"{stem.name}-seg-*{SUFFIX[args.codec]}")) - set(parts))
    if extras:
        print(f"  ignoring {len(extras)} surplus segment(s) outside the current plan")
    listing = concat_listing_path(stem)
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
    dest = stem.with_suffix(SUFFIX[args.codec])
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(dest)],
        check=True,
    )  # fmt: skip
    receipt = concat_identity(args, parts)
    receipt["file_sha256"] = file_sha256(dest)
    receipt["file_bytes"] = dest.stat().st_size
    try:
        receipt["decoded_video"] = concat_decoded_video_identity(dest, parts)
    except (MediaIdentityError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot authenticate decoded concat video: {exc}") from exc
    concat_receipt_path(dest).write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"  {dest.name} ← {len(parts)} segments")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", "--capture", dest="window", default="passage",
                    help="a named capture preset from render/program.json")
    ap.add_argument("--start", type=float, default=0.0,
                    help="where in the river to begin recording, in seconds. A `passages` capture snaps "
                         "forward to the next passage boundary; a `seconds` capture starts exactly here.")
    ap.add_argument("--tier", default="screen", help="corpus tier (`film` for the 4K master)")
    ap.add_argument("--seed", type=int, help="override the program's seed")
    ap.add_argument("--stream", type=int, default=0, help="optional passage-stream discriminator")
    ap.add_argument("--codec", default="prores", choices=sorted(CODECS))
    ap.add_argument("--width", type=int, help="override the window's width")
    ap.add_argument("--height", type=int, help="override the window's height")
    ap.add_argument("--fps", type=float, help="override the window's frame rate")
    ap.add_argument(
        "--score",
        help="opt into a compiled score contract (for example music/score.json); omitted keeps the current artwork",
    )
    ap.add_argument(
        "--timing-score",
        help="bind a score's fixed passage clock without admitting score motion or choreography",
    )
    ap.add_argument(
        "--choreography",
        help="score-led choreography contract (required for a production score)",
    )
    ap.add_argument("--segment", type=int, help="render one segment (default: all of them)")
    ap.add_argument("--segment-frames", type=int, default=600)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument(
        "--emergency-software-render",
        action="store_true",
        help="deadline screener capture only: explicitly use Chromium SwiftShader instead of canonical Apple Metal",
    )
    ap.add_argument("--quiet", dest="progress", action="store_false")
    ap.add_argument("--concat", action="store_true", help="stitch existing segments and exit")
    ap.add_argument("--check-concat", action="store_true", help="validate the planned segments and concatenated receipt")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="keep segments that already hold their full frame count. Safe because the engine is a "
        "pure f(seed, t): a segment rendered yesterday is the same file it would be rendered now.",
    )
    ap.add_argument(
        "--determinism",
        action="store_true",
        help="render the segment TWICE and require identical sha256 — the gate that catches "
        "any leak of impurity into the engine",
    )
    args = ap.parse_args()

    score = music_score_identity(args)
    timing_score = timing_score_identity(args)
    choreography = choreography_identity(args)
    if score and timing_score:
        ap.error("--score and --timing-score are mutually exclusive")
    if timing_score and choreography:
        ap.error("--timing-score cannot admit choreography")
    if timing_score and (
        timing_score["passage_mapping"] != "native-tempo"
        or not isinstance(timing_score["duration_seconds"], (int, float))
        or isinstance(timing_score["duration_seconds"], bool)
        or not math.isfinite(float(timing_score["duration_seconds"]))
        or timing_score["duration_seconds"] <= 0
    ):
        ap.error("--timing-score requires a finite positive native-tempo passage")
    if score and args._music_score_contract["release_status"] != "fixture-only" and not choreography:
        ap.error("a production --score requires --choreography")

    stem = output_stem(args)
    if (args.concat or args.check_concat) and args.segment is not None:
        ap.error("--concat/--check-concat cannot be combined with --segment")

    tier_ok, tier_detail = authorize_render_tier(APP / "corpus", hydrated_work_root(), args.tier)
    if not tier_ok:
        print(f"corpus tier {args.tier} is not authorized for rendering: {tier_detail}", file=sys.stderr)
        return 1

    if args.determinism:
        seg = args.segment if args.segment is not None else 3
        args.progress = False
        hashes = []
        for pass_ in (1, 2):
            deterministic_path = determinism_path(stem, args.codec, pass_)
            r = render_segment(args, seg, deterministic_path)
            hashes.append(r["sha256"])
            print(f"  pass {pass_}: {r['frames']} frames · {r['sha256'][:16]}… · {r['seconds']:.1f}s")
        for p in (1, 2):
            determinism_path(stem, args.codec, p).unlink(missing_ok=True)
        if hashes[0] != hashes[1]:
            print("\nDETERMINISM BROKEN — the same segment rendered two different films.")
            print("Something in engine/ is reading a clock, an rAF timestamp, or Math.random.")
            return 1
        print(f"\nDETERMINISM HOLDS — segment {seg} is bit-identical across two renders")
        return 0

    segments = [args.segment] if args.segment is not None else None
    total = None
    if segments is None:
        # Ask the page for the window length rather than assuming it here.
        with serve() as base, browser(
            headless=True,
            width=320,
            height=240,
            render_context=browser_render_context(args),
        ) as page:
            page.goto(film_url(base, args), wait_until="load")
            page.wait_for_function("() => window.danseFilmReady === true", timeout=300_000)
            w = page.evaluate("() => ({ ...window.danseFilm.window, passage: window.danseFilm.passage })")
        fps = args.fps or w["fps"]
        total = int(round((w["t1"] - w["t0"]) * fps))
        segments = list(range(math.ceil(total / args.segment_frames)))
        print(f"{args.window}: {total} frames at {fps} fps → {len(segments)} segments\n")

    parts = segment_paths(stem, args.codec, segments)
    if args.concat or args.check_concat:
        assert total is not None
        invalid = []
        for segment, part in zip(segments, parts, strict=True):
            want = expected_frames(segment, total, args.segment_frames)
            if not complete(part, want, segment_identity(args, segment, want)):
                invalid.append(part.name)
        if invalid:
            print("invalid planned segment(s): " + ", ".join(invalid), file=sys.stderr)
            return 1
        if args.check_concat:
            return 0 if concat_complete(stem, args, parts) else 1
        concat(stem, args, parts)
        return 0

    for seg, dest in zip(segments, parts, strict=True):
        want = expected_frames(seg, total, args.segment_frames) if total is not None else 0
        expected = segment_identity(args, seg, want) if total is not None else {}
        if args.resume and total is not None and complete(dest, want, expected):
            print(f"  {dest.name} · already complete, kept")
            continue
        r = render_segment(args, seg, dest)
        if r.get("skipped"):
            continue
        write_segment_receipt(dest, args, seg, r["frames"], capture=r)
        note = f" · {r['missing']} MISSING PLATES" if r["missing"] else ""
        print(
            f"  {dest.name} · {r['frames']} frames · {r['size']} @{r['fps']} · "
            f"{r['frames'] / max(1e-6, r['seconds']):.1f} fps · {r['sha256'][:12]}…{note}"
        )

    if len(segments) > 1:
        concat(stem, args, parts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
