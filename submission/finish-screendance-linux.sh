#!/bin/bash
set -euo pipefail

# Deadline-recovery finisher for the portable cloud render. This produces the
# competition screener, not the canonical exhibition master, and never replaces
# the required full-cut human review.

usage() {
  echo "usage: $0 /absolute/output/directory /absolute/picture.mp4 /absolute/audio.wav" >&2
  echo "optional: DANSE_PICTURE_RECEIPT=/absolute/receipt.json DANSE_AUDIO_RECEIPT=/absolute/receipt.json" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
for path in "$1" "$2" "$3"; do
  case "$path" in
    /*) ;;
    *) echo "all paths must be absolute: $path" >&2; exit 2 ;;
  esac
done

for command in awk basename cp dirname ffmpeg ffprobe find git mkdir mktemp python3 rm sha256sum sort stat; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "missing required command: $command" >&2
    exit 1
  }
done

DEST="$1"
PICTURE="$2"
AUDIO="$3"
FINAL_NAME="THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4"
FONT="${DANSE_CREDIT_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf}"
EXPECTED_DURATION="350.896343125"
EXPECTED_WIDTH="1920"
EXPECTED_HEIGHT="1080"
EXPECTED_FPS="30"
TARGET_LUFS="-16.0"
APPLICATION_TRUE_PEAK_DBTP="-1.5"
MAX_TRUE_PEAK_DBTP="-1.0"
LOUDNESS_TOLERANCE_LU="0.5"
TARGET_LRA_LU="11.0"

[[ -f "$PICTURE" ]] || { echo "missing picture: $PICTURE" >&2; exit 1; }
[[ -f "$AUDIO" ]] || { echo "missing audio: $AUDIO" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "missing credit-card font: $FONT" >&2; exit 1; }
[[ ! -L "$PICTURE" ]] || { echo "picture must be a regular non-symlink file: $PICTURE" >&2; exit 1; }
[[ ! -L "$AUDIO" ]] || { echo "audio must be a regular non-symlink file: $AUDIO" >&2; exit 1; }
[[ ! -L "$FONT" ]] || { echo "credit font must be a regular non-symlink file: $FONT" >&2; exit 1; }
[[ ! -e "$DEST" && ! -L "$DEST" ]] || {
  echo "refusing to overwrite output directory: $DEST" >&2
  exit 1
}
for unsafe_font_character in ':' ',' ';' '=' "'" '\' '"' '[' ']'; do
  if [[ "$FONT" == *"$unsafe_font_character"* ]]; then
    echo "credit-card font path contains characters unsafe for the ffmpeg filter: $FONT" >&2
    exit 1
  fi
done

SCRIPT_PATH="$(cd "$(dirname -- "$0")" && pwd -P)/$(basename -- "$0")"
ROOT="$(cd "$(dirname -- "$SCRIPT_PATH")/.." && pwd -P)"
SCRIPT_SHA256="$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')"
FONT_SHA256="$(sha256sum -- "$FONT" | awk '{print $1}')"
FONT_BYTES="$(stat -c '%s' -- "$FONT")"
FFMPEG_PATH="$(command -v ffmpeg)"
FFPROBE_PATH="$(command -v ffprobe)"
FFMPEG_SHA256="$(sha256sum -- "$FFMPEG_PATH" | awk '{print $1}')"
FFPROBE_SHA256="$(sha256sum -- "$FFPROBE_PATH" | awk '{print $1}')"
REPOSITORY_HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
[[ "$REPOSITORY_HEAD" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || {
  echo "cannot bind finishing run to an exact repository HEAD" >&2
  exit 1
}
REPOSITORY_TREE="$(git -C "$ROOT" rev-parse "${REPOSITORY_HEAD}^{tree}" 2>/dev/null || true)"
[[ "$REPOSITORY_TREE" =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ ]] || {
  echo "cannot bind finishing run to the exact repository tree" >&2
  exit 1
}
[[ -z "$(git -C "$ROOT" status --porcelain=v1 --untracked-files=no)" ]] || {
  echo "finishing requires a clean tracked Git worktree" >&2
  exit 1
}
EXPECTED_PICTURE_SOURCE_TREE="$(python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "render"))
import render

args = SimpleNamespace(
    tier="screen",
    score="music/score.json",
    timing_score=None,
    choreography="render/choreography.json",
)
print(render.source_tree_sha256(args))
PY
)"
[[ "$EXPECTED_PICTURE_SOURCE_TREE" =~ ^[0-9a-f]{64}$ ]] || {
  echo "cannot derive the current screener source-tree identity" >&2
  exit 1
}

PARENT="$(dirname -- "$DEST")"
BASE="$(basename -- "$DEST")"
[[ "$BASE" != "." && "$BASE" != ".." ]] || {
  echo "output directory must name a new child, not $BASE" >&2
  exit 1
}
mkdir -p "$PARENT"
[[ -d "$PARENT" && ! -L "$PARENT" ]] || {
  echo "output parent must be a real directory: $PARENT" >&2
  exit 1
}

umask 077
STAGE="$(mktemp -d "$PARENT/.${BASE}.finishing.XXXXXX")"
cleanup() {
  if [[ -n "${STAGE:-}" && -d "$STAGE" ]]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

FINAL="$STAGE/$FINAL_NAME"
PICTURE_SHA256="$(sha256sum -- "$PICTURE" | awk '{print $1}')"
AUDIO_SHA256="$(sha256sum -- "$AUDIO" | awk '{print $1}')"
PICTURE_BYTES="$(stat -c '%s' -- "$PICTURE")"
AUDIO_BYTES="$(stat -c '%s' -- "$AUDIO")"

find_source_receipt() {
  local kind="$1"
  local input="$2"
  local explicit=""
  local candidate
  local -a candidates=()

  if [[ "$kind" == "picture" ]]; then
    explicit="${DANSE_PICTURE_RECEIPT:-}"
    candidates=("$input.receipt.json")
  else
    explicit="${DANSE_AUDIO_RECEIPT:-}"
    candidates=(
      "$input.receipt.json"
      "$(dirname -- "$input")/audio-render.json"
      "$(dirname -- "$input")/portable-audio-receipt.json"
    )
  fi

  if [[ -n "$explicit" ]]; then
    case "$explicit" in
      /*) ;;
      *) echo "DANSE_${kind^^}_RECEIPT must be absolute: $explicit" >&2; return 2 ;;
    esac
    [[ -f "$explicit" ]] || {
      echo "missing explicit $kind receipt: $explicit" >&2
      return 1
    }
    printf '%s\n' "$explicit"
    return 0
  fi

  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
}

PICTURE_RECEIPT="$(find_source_receipt picture "$PICTURE" || true)"
AUDIO_RECEIPT="$(find_source_receipt audio "$AUDIO" || true)"
[[ -n "$PICTURE_RECEIPT" ]] || {
  echo "missing required picture producer receipt: $PICTURE.receipt.json" >&2
  exit 1
}
[[ -n "$AUDIO_RECEIPT" ]] || {
  echo "missing required audio producer receipt beside $AUDIO" >&2
  exit 1
}

PICTURE_RECEIPT_ARTIFACT="source-receipts/picture/concat.json"
PICTURE_GRAPH_ARTIFACT="source-receipts/picture/graph.json"
AUDIO_RECEIPT_ARTIFACT="source-receipts/audio.json"

picture_receipt_graph() {
  local mode="$1"
  python3 - "$mode" "$PICTURE_RECEIPT" "$PICTURE_SHA256" "$PICTURE_BYTES" \
    "$STAGE/source-receipts/picture" "$REPOSITORY_HEAD" "$REPOSITORY_TREE" \
    "$EXPECTED_PICTURE_SOURCE_TREE" <<'PY'
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

(
    mode,
    receipt_value,
    picture_sha256,
    picture_bytes_value,
    destination_value,
    repository_head,
    repository_tree,
    expected_source_tree,
) = sys.argv[1:]
receipt_path = Path(receipt_value)
destination = Path(destination_value)
picture_bytes = int(picture_bytes_value)
digest_pattern = re.compile(r"^[0-9a-f]{64}$")
name_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
if receipt_path.is_symlink() or not receipt_path.is_file():
    raise SystemExit(f"picture concat receipt is missing or unsafe: {receipt_path}")

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid render receipt {path}: {exc}")
    if not isinstance(value, dict):
        raise SystemExit(f"invalid render receipt {path}: root is not an object")
    return value

concat = load(receipt_path)
if concat.get("schema") != "danse.render.concat.v1":
    raise SystemExit(f"picture receipt is not a concat receipt: {receipt_path}")
if concat.get("codec") != "h264":
    raise SystemExit("picture concat receipt is not H.264")
if concat.get("file_sha256") != picture_sha256 or concat.get("file_bytes") != picture_bytes:
    raise SystemExit("picture concat receipt does not bind the selected picture bytes")
decoded = concat.get("decoded_video")
if not isinstance(decoded, dict) or not digest_pattern.fullmatch(str(decoded.get("sha256", ""))):
    raise SystemExit("picture concat receipt has no decoded-video identity")
if not all(type(decoded.get(key)) is int and decoded[key] > 0 for key in ("frames", "width", "height")):
    raise SystemExit("picture concat receipt has an invalid decoded-video shape")
if (decoded["width"], decoded["height"]) not in {(1280, 720), (1920, 1080)}:
    raise SystemExit("picture concat receipt is not a supported 16:9 emergency raster")
if decoded["frames"] != 10527 or float(decoded.get("fps", 0)) != 30.0:
    raise SystemExit("picture concat receipt is not the complete 350.896-second 30 fps screener")
segments = concat.get("segments")
if not isinstance(segments, list) or not segments:
    raise SystemExit("picture concat receipt has no ordered segment graph")

rows = []
seen_names = set()
seen_receipts = set()
expected_segment = None
shared_inputs = None
shared_capture = None
for index, row in enumerate(segments):
    if not isinstance(row, dict) or set(row) != {"name", "receipt_sha256"}:
        raise SystemExit(f"picture concat segment {index} has an invalid binding")
    name = row["name"]
    wanted = row["receipt_sha256"]
    if not isinstance(name, str) or not name_pattern.fullmatch(name) or Path(name).name != name:
        raise SystemExit(f"picture concat segment {index} has an unsafe name")
    if name in seen_names or not isinstance(wanted, str) or not digest_pattern.fullmatch(wanted):
        raise SystemExit(f"picture concat segment {index} has a duplicate or invalid receipt binding")
    seen_names.add(name)
    segment_receipt_path = receipt_path.parent / f"{name}.receipt.json"
    if segment_receipt_path.is_symlink() or not segment_receipt_path.is_file():
        raise SystemExit(f"missing regular segment receipt: {segment_receipt_path}")
    actual = sha256(segment_receipt_path)
    if actual != wanted:
        raise SystemExit(f"segment receipt digest differs for {name}")
    segment = load(segment_receipt_path)
    if segment.get("schema") != "danse.render.segment.v1":
        raise SystemExit(f"segment receipt has the wrong schema: {name}")
    segment_number = segment.get("segment")
    frames = segment.get("frames")
    if type(segment_number) is not int or segment_number < 0 or type(frames) is not int or frames < 1:
        raise SystemExit(f"segment receipt has invalid position/frame count: {name}")
    if expected_segment is None:
        expected_segment = 0
    if segment_number != expected_segment or segment_number != index:
        raise SystemExit("picture concat segment receipts are not contiguous and ordered")
    expected_segment += 1
    if not name.endswith(f"-seg-{index:03d}.mp4"):
        raise SystemExit(f"picture concat segment name does not match its index: {name}")
    inputs = segment.get("inputs")
    if not isinstance(inputs, dict):
        raise SystemExit(f"segment receipt has no input identity: {name}")
    if shared_inputs is None:
        shared_inputs = inputs
    elif inputs != shared_inputs:
        raise SystemExit("picture segment receipts do not share one exact input identity")
    if (
        inputs.get("window") != "screener"
        or inputs.get("start") != 0
        or inputs.get("tier") != "screen"
        or inputs.get("stream") != 0
        or inputs.get("codec") != "h264"
        or inputs.get("segment_frames") != 600
        or inputs.get("browser_render_context") != "emergency-software-capture"
        or inputs.get("repository_head") != repository_head
        or inputs.get("repository_tree") != repository_tree
        or inputs.get("effective_seed") != 20170620
        or inputs.get("source_tree_sha256") != expected_source_tree
        or not isinstance(inputs.get("music_score"), dict)
        or not isinstance(inputs.get("choreography"), dict)
    ):
        raise SystemExit(f"segment receipt is not from the fixed emergency screener rail: {name}")
    browser_toolchain = inputs.get("browser_toolchain")
    if (
        not isinstance(browser_toolchain, dict)
        or not isinstance(browser_toolchain.get("executable"), str)
        or not browser_toolchain["executable"]
        or not digest_pattern.fullmatch(str(browser_toolchain.get("executable_sha256", "")))
        or not isinstance(browser_toolchain.get("version"), str)
        or not browser_toolchain["version"]
    ):
        raise SystemExit(f"segment receipt has no exact Chromium toolchain: {name}")
    capture = segment.get("capture")
    if (
        not isinstance(capture, dict)
        or "swiftshader" not in str(capture.get("renderer", "")).lower()
        or capture.get("missing") != 0
        or not digest_pattern.fullmatch(str(capture.get("raw_rgba_sha256", "")))
        or not isinstance(capture.get("signature"), str)
        or not capture["signature"]
        or not isinstance(capture.get("passage"), dict)
    ):
        raise SystemExit(f"segment receipt lacks a complete SwiftShader capture: {name}")
    capture_identity = {
        key: capture[key] for key in ("renderer", "signature", "passage")
    }
    passage = capture["passage"]
    if (
        passage.get("index") != 0
        or passage.get("seed") != 2943173797
        or float(passage.get("t0", -1)) != 0.0
        or abs(float(passage.get("seconds", 0)) - 350.896343125) > 1e-9
    ):
        raise SystemExit(f"segment receipt is not river seed 20170620 passage 0: {name}")
    if shared_capture is None:
        shared_capture = capture_identity
    elif capture_identity != shared_capture:
        raise SystemExit("picture segment receipts do not share one capture identity")
    if not digest_pattern.fullmatch(str(segment.get("file_sha256", ""))):
        raise SystemExit(f"segment receipt has no encoded-file digest: {name}")
    if type(segment.get("file_bytes")) is not int or segment["file_bytes"] < 1:
        raise SystemExit(f"segment receipt has no encoded-file byte count: {name}")
    segment_decoded = segment.get("decoded_video")
    if (
        not isinstance(segment_decoded, dict)
        or not digest_pattern.fullmatch(str(segment_decoded.get("sha256", "")))
        or segment_decoded.get("frames") != frames
        or segment_decoded.get("width") != decoded["width"]
        or segment_decoded.get("height") != decoded["height"]
    ):
        raise SystemExit(f"segment receipt has no exact decoded-video identity: {name}")
    copied_relative = f"segments/{name}.receipt.json"
    if copied_relative in seen_receipts:
        raise SystemExit("picture concat graph maps two segments to one receipt artifact")
    seen_receipts.add(copied_relative)
    rows.append(
        {
            "index": index,
            "segment": segment_number,
            "name": name,
            "artifact": copied_relative,
            "receipt_sha256": actual,
            "file_sha256": segment["file_sha256"],
            "file_bytes": segment["file_bytes"],
            "frames": frames,
            "decoded_video": segment_decoded,
            "inputs": inputs,
            "capture": capture,
            "source": segment_receipt_path,
        }
    )
if sum(row["frames"] for row in rows) != decoded["frames"]:
    raise SystemExit("picture concat frame count differs from its ordered segment receipts")

graph_identity = hashlib.sha256()
graph_identity.update(bytes.fromhex(sha256(receipt_path)))
for row in rows:
    graph_identity.update(row["name"].encode())
    graph_identity.update(bytes.fromhex(row["receipt_sha256"]))
identity = graph_identity.hexdigest()
expected_graph = {
    "schema": "danse.submission.picture-receipt-graph.v1",
    "identity_sha256": identity,
    "concat": {
        "artifact": "concat.json",
        "sha256": sha256(receipt_path),
        "file_sha256": picture_sha256,
        "file_bytes": picture_bytes,
        "decoded_video": decoded,
    },
    "segments": [{key: value for key, value in row.items() if key != "source"} for row in rows],
}

if mode == "copy":
    if destination.exists():
        raise SystemExit("picture receipt destination already exists")
    (destination / "segments").mkdir(parents=True)
    shutil.copyfile(receipt_path, destination / "concat.json")
    for row in rows:
        shutil.copyfile(row["source"], destination / row["artifact"])
    pending = destination / "graph.json.tmp"
    with pending.open("x") as handle:
        json.dump(expected_graph, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    pending.replace(destination / "graph.json")
elif mode == "verify":
    graph = load(destination / "graph.json")
    if graph != expected_graph:
        raise SystemExit("copied picture receipt graph identity differs")
    if sha256(destination / "concat.json") != sha256(receipt_path):
        raise SystemExit("copied picture concat receipt differs")
    for row in rows:
        if sha256(destination / row["artifact"]) != row["receipt_sha256"]:
            raise SystemExit(f"copied segment receipt differs for {row['name']}")
else:
    raise SystemExit(f"unsupported picture graph mode: {mode}")
print(identity)
PY
}

validate_audio_receipt() {
  local receipt="$1"
  python3 - "$receipt" "$AUDIO_SHA256" "$AUDIO_BYTES" "$EXPECTED_DURATION" \
    "$STAGE/$PICTURE_GRAPH_ARTIFACT" "$REPOSITORY_HEAD" <<'PY'
import json
import math
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_sha256 = sys.argv[2]
expected_bytes = int(sys.argv[3])
expected_duration = float(sys.argv[4])
picture_graph_path = Path(sys.argv[5])
repository_head = sys.argv[6]
if path.is_symlink() or not path.is_file():
    raise SystemExit(f"audio receipt is missing or unsafe: {path}")
try:
    document = json.loads(path.read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid audio receipt {path}: {exc}")
if not isinstance(document, dict):
    raise SystemExit(f"invalid audio receipt {path}: root is not an object")
schema = document.get("schema")
required_gates = {
    "danse.submission.portable-audio.v1": {
        "duration_matches_score",
        "non_silent",
        "stems_non_silent",
        "polyphonic",
        "loudness_in_target",
        "true_peak_in_target",
    },
}.get(schema)
if required_gates is None:
    raise SystemExit(f"unsupported audio receipt schema in {path}: {schema!r}")
if document.get("profile") != "competition-classical" or document.get("repository_head") != repository_head:
    raise SystemExit("audio receipt is not the current competition profile/source HEAD")
try:
    picture_graph = json.loads(picture_graph_path.read_text())
    picture_inputs = picture_graph["segments"][0]["inputs"]
except (OSError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
    raise SystemExit("picture receipt graph has no cross-bindable source inputs") from exc
audio_inputs = document.get("inputs")
audio_score = audio_inputs.get("score") if isinstance(audio_inputs, dict) else None
audio_choreography = audio_inputs.get("choreography") if isinstance(audio_inputs, dict) else None
picture_score = picture_inputs.get("music_score") if isinstance(picture_inputs, dict) else None
picture_choreography = picture_inputs.get("choreography") if isinstance(picture_inputs, dict) else None
digest_pattern = re.compile(r"^[0-9a-f]{64}$")
cross_bindings = (
    audio_score.get("sha256") if isinstance(audio_score, dict) else None,
    audio_choreography.get("sha256") if isinstance(audio_choreography, dict) else None,
    picture_score.get("file_sha256") if isinstance(picture_score, dict) else None,
    picture_choreography.get("file_sha256") if isinstance(picture_choreography, dict) else None,
)
if (
    not isinstance(audio_score, dict)
    or not isinstance(audio_choreography, dict)
    or not isinstance(picture_score, dict)
    or not isinstance(picture_choreography, dict)
    or any(
        not isinstance(value, str) or not digest_pattern.fullmatch(value)
        for value in cross_bindings
    )
    or audio_score.get("sha256") != picture_score.get("file_sha256")
    or audio_choreography.get("sha256") != picture_choreography.get("file_sha256")
):
    raise SystemExit("audio receipt score/choreography differs from the picture receipt graph")
outputs = document.get("outputs")
master = outputs.get("master") if isinstance(outputs, dict) else None
if not isinstance(master, dict) or master.get("sha256") != expected_sha256:
    raise SystemExit("audio receipt does not bind the selected audio bytes")
sample_rate = master.get("sample_rate")
channels = master.get("channels")
frames = master.get("frames")
duration = master.get("duration_seconds")
if sample_rate != 48000 or channels != 2 or type(frames) is not int or frames < 1:
    raise SystemExit("audio receipt master is not exact 48 kHz stereo PCM")
try:
    duration = float(duration)
except (TypeError, ValueError) as exc:
    raise SystemExit("audio receipt master has no finite duration") from exc
if not math.isfinite(duration) or frames != round(expected_duration * sample_rate):
    raise SystemExit("audio receipt frame count differs from the fixed score duration")
if abs(duration - frames / sample_rate) > 1e-9 or abs(duration - expected_duration) > 1 / sample_rate:
    raise SystemExit("audio receipt duration differs from its exact frame count")
verification = document.get("verification")
if not isinstance(verification, dict) or any(verification.get(key) is not True for key in required_gates):
    raise SystemExit("audio receipt does not carry every required true verification gate")
if expected_bytes < 1:
    raise SystemExit("selected audio input has no bytes")
PY
}

PICTURE_GRAPH_SHA256="$(picture_receipt_graph copy)"
validate_audio_receipt "$AUDIO_RECEIPT"
AUDIO_RECEIPT_SHA256="$(sha256sum -- "$AUDIO_RECEIPT" | awk '{print $1}')"
cp -- "$AUDIO_RECEIPT" "$STAGE/$AUDIO_RECEIPT_ARTIFACT"
[[ "$(sha256sum -- "$STAGE/$AUDIO_RECEIPT_ARTIFACT" | awk '{print $1}')" == "$AUDIO_RECEIPT_SHA256" ]] || {
  echo "audio receipt changed while it was copied" >&2
  exit 1
}

# Analyze the selected PCM/master bytes, then apply the measured two-pass
# loudnorm transform while encoding. The encoded AAC is measured independently
# below; an input-side normalization receipt is not accepted as proof of the
# lossy delivery stream.
if ! ffmpeg -hide_banner -nostats -nostdin \
  -i "$AUDIO" -map 0:a:0 \
  -af "loudnorm=I=$TARGET_LUFS:TP=$APPLICATION_TRUE_PEAK_DBTP:LRA=$TARGET_LRA_LU:print_format=json" \
  -f null - 2> "$STAGE/audio-loudnorm-input.log"; then
  python3 - "$STAGE/audio-loudnorm-input.log" <<'PY'
import sys
from pathlib import Path

detail = Path(sys.argv[1]).read_text(errors="replace")[-4000:].strip()
raise SystemExit("FAIL: source-audio loudness analysis failed" + (f":\n{detail}" if detail else ""))
PY
fi

LOUDNORM_FILTER="$(python3 - "$STAGE/audio-loudnorm-input.log" \
  "$TARGET_LUFS" "$APPLICATION_TRUE_PEAK_DBTP" "$TARGET_LRA_LU" <<'PY'
import json
import math
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
blocks = re.findall(r'\{\s*"input_i".*?\}', path.read_text(), flags=re.DOTALL)
if not blocks:
    raise SystemExit("ffmpeg loudnorm input analysis returned no JSON measurement")
try:
    measured = json.loads(blocks[-1])
except json.JSONDecodeError as exc:
    raise SystemExit(f"ffmpeg loudnorm input analysis returned malformed JSON: {exc}")
required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
for key in required:
    try:
        value = float(measured[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"ffmpeg loudnorm input analysis lacks finite {key}") from exc
    if not math.isfinite(value):
        raise SystemExit(f"ffmpeg loudnorm input analysis has non-finite {key}")
print(
    f"loudnorm=I={sys.argv[2]}:TP={sys.argv[3]}:LRA={sys.argv[4]}"
    f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
    f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
    f":offset={measured['target_offset']}:linear=false:print_format=summary"
)
PY
)"

ffmpeg -hide_banner -loglevel error -nostdin -y \
  -i "$PICTURE" -i "$AUDIO" \
  -map 0:v:0 -map 1:a:0 -map_metadata -1 -map_chapters -1 -sn -dn \
  -vf "scale=$EXPECTED_WIDTH:$EXPECTED_HEIGHT:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,\
pad=$EXPECTED_WIDTH:$EXPECTED_HEIGHT:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=$EXPECTED_FPS,\
drawbox=color=black:t=fill:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='THE THING WITHOUT A NAME':fontcolor=white:fontsize=52:x=(w-text_w)/2:y=350:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='A FILM BY ANTHONY J. PADAVANO':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=470:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='MUSIC BY LÉO DELIBES':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=540:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='SOURCE ARRANGEMENTS · PAUL DE BRA · CC BY 4.0':fontcolor=white:fontsize=30:x=(w-text_w)/2:y=610:enable='gte(t,346.896343125)'" \
  -af "$LOUDNORM_FILTER" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p -fps_mode cfr \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -shortest "$FINAL"

# Metadata alone cannot prove decodability. Reject damaged/truncated output
# before extracting review stills or minting any receipt.
ffmpeg -hide_banner -nostdin -v error -xerror -err_detect explode \
  -i "$FINAL" -map 0:v:0 -map 0:a:0 -f null -

for item in "01:60" "02:180" "03:300"; do
  number="${item%%:*}"
  second="${item##*:}"
  ffmpeg -hide_banner -loglevel error -nostdin -y -ss "$second" -i "$FINAL" \
    -frames:v 1 -q:v 2 "$STAGE/still-$number.jpg"
done

ffmpeg -hide_banner -loglevel error -nostdin -y -ss 348 -i "$FINAL" \
  -frames:v 1 -q:v 2 "$STAGE/credit-card-check.jpg"
ffprobe -v error -count_frames -show_format -show_streams -of json "$FINAL" \
  > "$STAGE/ffprobe.json"

if ! ffmpeg -hide_banner -nostats -nostdin \
  -i "$FINAL" -map 0:a:0 \
  -af "loudnorm=I=$TARGET_LUFS:TP=$MAX_TRUE_PEAK_DBTP:LRA=$TARGET_LRA_LU:print_format=json" \
  -f null - 2> "$STAGE/final-aac-loudnorm.log"; then
  python3 - "$STAGE/final-aac-loudnorm.log" <<'PY'
import sys
from pathlib import Path

detail = Path(sys.argv[1]).read_text(errors="replace")[-4000:].strip()
raise SystemExit("FAIL: final-AAC loudness analysis failed" + (f":\n{detail}" if detail else ""))
PY
fi

python3 - "$STAGE/final-aac-loudnorm.log" "$STAGE/loudness.json" \
  "$TARGET_LUFS" "$LOUDNESS_TOLERANCE_LU" "$APPLICATION_TRUE_PEAK_DBTP" \
  "$MAX_TRUE_PEAK_DBTP" "$TARGET_LRA_LU" <<'PY'
import json
import math
import os
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
target_lufs = float(sys.argv[3])
tolerance_lu = float(sys.argv[4])
target_true_peak = float(sys.argv[5])
max_true_peak = float(sys.argv[6])
target_lra = float(sys.argv[7])
blocks = re.findall(r'\{\s*"input_i".*?\}', log_path.read_text(), flags=re.DOTALL)
if not blocks:
    raise SystemExit("ffmpeg final-AAC loudnorm analysis returned no JSON measurement")
try:
    block = json.loads(blocks[-1])
    measured = {
        "integrated_lufs": float(block["input_i"]),
        "true_peak_dbtp": float(block["input_tp"]),
        "lra_lu": float(block["input_lra"]),
        "threshold_lufs": float(block["input_thresh"]),
    }
except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
    raise SystemExit(f"ffmpeg final-AAC loudnorm analysis is malformed: {exc}")
if not all(math.isfinite(value) for value in measured.values()):
    raise SystemExit("ffmpeg final-AAC loudnorm analysis contains a non-finite value")
errors = []
if abs(measured["integrated_lufs"] - target_lufs) > tolerance_lu:
    errors.append(
        f"integrated loudness is {measured['integrated_lufs']:.2f} LUFS, "
        f"target {target_lufs:.2f} +/- {tolerance_lu:.2f}"
    )
if measured["true_peak_dbtp"] > max_true_peak:
    errors.append(
        f"true peak is {measured['true_peak_dbtp']:.2f} dBTP, "
        f"maximum {max_true_peak:.2f} dBTP"
    )
if errors:
    raise SystemExit("FAIL: final AAC " + "; ".join(errors))
document = {
    "schema": "danse.submission.final-aac-loudness.v1",
    "method": "ffmpeg-loudnorm-analysis-of-encoded-aac",
    "targets": {
        "integrated_lufs": target_lufs,
        "tolerance_lu": tolerance_lu,
        "target_true_peak_dbtp": target_true_peak,
        "max_true_peak_dbtp": max_true_peak,
        "lra_lu": target_lra,
    },
    "measured": measured,
    "verification": {"loudness_in_target": True, "true_peak_in_target": True},
}
pending = output_path.with_name(output_path.name + ".tmp")
with pending.open("w") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
pending.replace(output_path)
PY

rm -- "$STAGE/audio-loudnorm-input.log" "$STAGE/final-aac-loudnorm.log"

python3 - "$STAGE/ffprobe.json" "$EXPECTED_WIDTH" "$EXPECTED_HEIGHT" \
  "$EXPECTED_FPS" "$EXPECTED_DURATION" <<'PY'
import json
import math
import sys
from fractions import Fraction
from pathlib import Path

probe = json.loads(Path(sys.argv[1]).read_text())
expected_width = int(sys.argv[2])
expected_height = int(sys.argv[3])
expected_fps = Fraction(sys.argv[4])
expected_duration = float(sys.argv[5])
streams = probe.get("streams", [])
videos = [row for row in streams if row.get("codec_type") == "video"]
audios = [row for row in streams if row.get("codec_type") == "audio"]
errors = []
if len(streams) != 2:
    errors.append(f"expected exactly two streams, found {len(streams)}")
if len(videos) != 1:
    errors.append(f"expected exactly one video stream, found {len(videos)}")
if len(audios) != 1:
    errors.append(f"expected exactly one audio stream, found {len(audios)}")
video = videos[0] if len(videos) == 1 else {}
audio = audios[0] if len(audios) == 1 else {}
try:
    duration = float(probe.get("format", {}).get("duration", 0))
except (TypeError, ValueError):
    duration = 0.0
if not math.isfinite(duration):
    duration = 0.0
if video.get("codec_name") != "h264":
    errors.append("video codec is not H.264")
if video.get("disposition", {}).get("attached_pic") not in {0, "0"}:
    errors.append("video stream is marked as an attached picture")
if (video.get("width"), video.get("height")) != (expected_width, expected_height):
    errors.append(
        f"video raster is {video.get('width')}x{video.get('height')}, "
        f"expected {expected_width}x{expected_height}"
    )
if video.get("pix_fmt") != "yuv420p":
    errors.append(f"video pixel format is {video.get('pix_fmt')!r}, expected yuv420p")
if str(video.get("sample_aspect_ratio")) not in {"1:1", "1/1"}:
    errors.append(f"video sample aspect ratio is {video.get('sample_aspect_ratio')!r}, expected 1:1")
for field in ("r_frame_rate", "avg_frame_rate"):
    try:
        actual = Fraction(video.get(field, "0/1"))
    except (TypeError, ValueError, ZeroDivisionError):
        actual = Fraction(0)
    if actual != expected_fps:
        errors.append(f"video {field} is {video.get(field)!r}, expected {expected_fps}/1")
if audio.get("codec_name") != "aac":
    errors.append("audio codec is not AAC")
if int(audio.get("sample_rate", 0)) != 48000:
    errors.append("audio is not 48 kHz")
if int(audio.get("channels", 0)) != 2:
    errors.append("audio is not stereo")
for label, stream in (("video", video), ("audio", audio)):
    try:
        stream_duration = float(stream.get("duration", 0))
    except (TypeError, ValueError):
        stream_duration = 0.0
    if not math.isfinite(stream_duration) or abs(stream_duration - expected_duration) > 1 / float(expected_fps):
        errors.append(f"{label} stream duration is {stream_duration:.3f}s, expected {expected_duration:.3f}s")
expected_frames = round(expected_duration * expected_fps)
try:
    decoded_frames = int(video.get("nb_read_frames", 0))
except (TypeError, ValueError):
    decoded_frames = 0
if decoded_frames != expected_frames:
    errors.append(f"decoded video has {decoded_frames} frames, expected {expected_frames}")
if abs(duration - expected_duration) > 1 / float(expected_fps):
    errors.append(f"duration is {duration:.3f}s, expected {expected_duration:.3f}s")
if errors:
    raise SystemExit("FAIL: " + "; ".join(errors))
print(
    f"PROFILE READY: {video.get('width')}x{video.get('height')} "
    f"H.264 at {video.get('avg_frame_rate')} + 48 kHz stereo AAC, {duration:.3f}s"
)
PY

# Detect input or source-receipt mutation across the finishing run. The
# published receipt therefore binds the bytes that were actually consumed, not
# merely bytes that happened to occupy the same paths before or after it.
[[ "$(sha256sum -- "$PICTURE" | awk '{print $1}')" == "$PICTURE_SHA256" ]] || {
  echo "picture changed during finishing" >&2
  exit 1
}
[[ "$(stat -c '%s' -- "$PICTURE")" == "$PICTURE_BYTES" ]] || {
  echo "picture byte count changed during finishing" >&2
  exit 1
}
[[ "$(sha256sum -- "$AUDIO" | awk '{print $1}')" == "$AUDIO_SHA256" ]] || {
  echo "audio changed during finishing" >&2
  exit 1
}
[[ "$(stat -c '%s' -- "$AUDIO")" == "$AUDIO_BYTES" ]] || {
  echo "audio byte count changed during finishing" >&2
  exit 1
}
[[ "$(picture_receipt_graph verify)" == "$PICTURE_GRAPH_SHA256" ]] || {
  echo "picture receipt graph changed during finishing" >&2
  exit 1
}
validate_audio_receipt "$AUDIO_RECEIPT"
[[ "$(sha256sum -- "$AUDIO_RECEIPT" | awk '{print $1}')" == "$AUDIO_RECEIPT_SHA256" ]] || {
  echo "audio receipt changed during finishing" >&2
  exit 1
}
validate_audio_receipt "$STAGE/$AUDIO_RECEIPT_ARTIFACT"
[[ "$(sha256sum -- "$STAGE/$AUDIO_RECEIPT_ARTIFACT" | awk '{print $1}')" == "$AUDIO_RECEIPT_SHA256" ]] || {
  echo "copied audio receipt differs from its source" >&2
  exit 1
}
[[ "$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')" == "$SCRIPT_SHA256" ]] || {
  echo "finisher script changed during finishing" >&2
  exit 1
}
[[ "$(sha256sum -- "$FONT" | awk '{print $1}')" == "$FONT_SHA256" ]] || {
  echo "credit font changed during finishing" >&2
  exit 1
}
[[ "$(sha256sum -- "$FFMPEG_PATH" | awk '{print $1}')" == "$FFMPEG_SHA256" ]] || {
  echo "ffmpeg changed during finishing" >&2
  exit 1
}
[[ "$(sha256sum -- "$FFPROBE_PATH" | awk '{print $1}')" == "$FFPROBE_SHA256" ]] || {
  echo "ffprobe changed during finishing" >&2
  exit 1
}
[[ "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)" == "$REPOSITORY_HEAD" ]] || {
  echo "repository HEAD changed during finishing" >&2
  exit 1
}

python3 - "$STAGE" "$FINAL_NAME" \
  "$(basename -- "$PICTURE")" "$PICTURE_SHA256" "$PICTURE_BYTES" "$PICTURE_RECEIPT_ARTIFACT" \
  "$(basename -- "$AUDIO")" "$AUDIO_SHA256" "$AUDIO_BYTES" "$AUDIO_RECEIPT_ARTIFACT" \
  "$SCRIPT_SHA256" "$FFMPEG_PATH" "$FFMPEG_SHA256" "$FFPROBE_PATH" "$FFPROBE_SHA256" \
  "$(basename -- "$FONT")" "$FONT_SHA256" "$FONT_BYTES" "$REPOSITORY_HEAD" <<'PY'
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    stage_value,
    final_name,
    picture_name,
    picture_sha256,
    picture_bytes,
    picture_receipt_artifact,
    audio_name,
    audio_sha256,
    audio_bytes,
    audio_receipt_artifact,
    script_sha256,
    ffmpeg_path,
    ffmpeg_sha256,
    ffprobe_path,
    ffprobe_sha256,
    font_name,
    font_sha256,
    font_bytes,
    repository_head,
) = sys.argv[1:]
stage = Path(stage_value)

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def receipt_binding(artifact: str, expected: str, expected_bytes: int, kind: str):
    path = stage / artifact
    document = json.loads(path.read_text())
    schema = document.get("schema")
    if kind == "picture" and schema == "danse.render.concat.v1":
        bound = document.get("file_sha256")
        if document.get("file_bytes") != expected_bytes:
            raise SystemExit("copied picture receipt no longer binds its input byte count")
    elif kind == "audio" and schema in {"danse.audio.render.v1", "danse.submission.portable-audio.v1"}:
        outputs = document.get("outputs")
        master = outputs.get("master") if isinstance(outputs, dict) else None
        bound = master.get("sha256") if isinstance(master, dict) else None
    else:
        raise SystemExit(f"unsupported copied {kind} receipt schema: {schema!r}")
    if bound != expected:
        raise SystemExit(f"copied {kind} receipt no longer binds its input")
    binding = {
        "artifact": artifact,
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
        "schema": schema,
        "binds_input_sha256": True,
    }
    if kind == "picture":
        graph_path = path.with_name("graph.json")
        graph = json.loads(graph_path.read_text())
        if graph.get("schema") != "danse.submission.picture-receipt-graph.v1":
            raise SystemExit("copied picture graph has the wrong schema")
        segments = graph.get("segments")
        if not isinstance(segments, list) or not segments:
            raise SystemExit("copied picture graph has no segment receipts")
        durable_segments = []
        for row in segments:
            relative = row.get("artifact") if isinstance(row, dict) else None
            wanted = row.get("receipt_sha256") if isinstance(row, dict) else None
            segment_path = graph_path.parent / str(relative)
            if sha256(segment_path) != wanted:
                raise SystemExit("copied picture graph contains a stale segment receipt")
            durable_segments.append(
                {
                    "index": row["index"],
                    "segment": row["segment"],
                    "name": row["name"],
                    "artifact": segment_path.relative_to(stage).as_posix(),
                    "receipt_sha256": wanted,
                    "file_sha256": row["file_sha256"],
                    "file_bytes": row["file_bytes"],
                    "frames": row["frames"],
                    "decoded_video": row["decoded_video"],
                }
            )
        binding["graph"] = {
            "artifact": graph_path.relative_to(stage).as_posix(),
            "sha256": sha256(graph_path),
            "identity_sha256": graph["identity_sha256"],
            "segments": durable_segments,
        }
    return binding

probe = json.loads((stage / "ffprobe.json").read_text())
loudness = json.loads((stage / "loudness.json").read_text())
video = next(row for row in probe["streams"] if row.get("codec_type") == "video")
audio = next(row for row in probe["streams"] if row.get("codec_type") == "audio")
final = stage / final_name
document = {
    "schema": "danse.submission.emergency-finishing.v1",
    "purpose": "ScreenDance Miami 2027 deadline-recovery screener",
    "canonical_exhibition_master": False,
    "canonical_apple_metal_render": False,
    "machine_conformance_only": True,
    "human_full_cut_review_required": True,
    "human_final_cut_approved": False,
    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "inputs": {
        "picture": {
            "name": picture_name,
            "sha256": picture_sha256,
            "bytes": int(picture_bytes),
            "receipt": receipt_binding(
                picture_receipt_artifact, picture_sha256, int(picture_bytes), "picture"
            ),
        },
        "audio": {
            "name": audio_name,
            "sha256": audio_sha256,
            "bytes": int(audio_bytes),
            "receipt": receipt_binding(
                audio_receipt_artifact, audio_sha256, int(audio_bytes), "audio"
            ),
        },
    },
    "output": {
        "name": final_name,
        "sha256": sha256(final),
        "bytes": final.stat().st_size,
        "duration_seconds": float(probe["format"]["duration"]),
        "video": {
            "codec": video["codec_name"],
            "width": video["width"],
            "height": video["height"],
            "pixel_format": video["pix_fmt"],
            "sample_aspect_ratio": video["sample_aspect_ratio"],
            "r_frame_rate": video["r_frame_rate"],
            "avg_frame_rate": video["avg_frame_rate"],
            "decoded_frames": int(video["nb_read_frames"]),
        },
        "audio": {
            "codec": audio["codec_name"],
            "sample_rate": int(audio["sample_rate"]),
            "channels": audio["channels"],
            "final_aac_loudness": loudness,
        },
    },
    "toolchain": {
        "finisher_sha256": script_sha256,
        "ffmpeg_path": ffmpeg_path,
        "ffmpeg_sha256": ffmpeg_sha256,
        "ffmpeg_version": subprocess.run(
            [ffmpeg_path, "-version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0],
        "ffprobe_path": ffprobe_path,
        "ffprobe_sha256": ffprobe_sha256,
        "ffprobe_version": subprocess.run(
            [ffprobe_path, "-version"], check=True, capture_output=True, text=True
        ).stdout.splitlines()[0],
        "credit_font": {
            "name": font_name,
            "sha256": font_sha256,
            "bytes": int(font_bytes),
        },
        "repository_head": repository_head or None,
    },
    "artifacts": {
        name: {
            "sha256": sha256(stage / name),
            "bytes": (stage / name).stat().st_size,
        }
        for name in (
            "still-01.jpg",
            "still-02.jpg",
            "still-03.jpg",
            "credit-card-check.jpg",
            "ffprobe.json",
            "loudness.json",
        )
    },
    "verification": {
        "canonical_1920x1080_raster": True,
        "constant_30_fps": True,
        "full_media_decode_passed": True,
        "final_aac_loudness_in_target": True,
        "final_aac_true_peak_in_target": True,
        "input_hashes_rechecked_after_encode": True,
        "required_source_receipts_copied_and_bound": True,
    },
}
output_path = stage / "finishing-receipt.json"
pending = output_path.with_name(output_path.name + ".tmp")
with pending.open("w") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
pending.replace(output_path)
PY

manifest_files=(
  "$FINAL_NAME"
  "still-01.jpg"
  "still-02.jpg"
  "still-03.jpg"
  "credit-card-check.jpg"
  "ffprobe.json"
  "loudness.json"
  "finishing-receipt.json"
)
SOURCE_RECEIPT_LIST="$STAGE/.source-receipts.list"
(
  cd "$STAGE"
  find source-receipts -type f -print0 | sort -z > "$SOURCE_RECEIPT_LIST"
)
while IFS= read -r -d '' source_receipt; do
  manifest_files+=("$source_receipt")
done < "$SOURCE_RECEIPT_LIST"
rm -- "$SOURCE_RECEIPT_LIST"
(
  cd "$STAGE"
  sha256sum "${manifest_files[@]}" > SHA256SUMS
  sha256sum -c SHA256SUMS
)

# File-then-directory sync plus a same-filesystem rename makes the validated
# package the only state ever visible at DEST.
python3 - "$STAGE" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in sorted(root.rglob("*")):
    if path.is_file():
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
for path in sorted((row for row in root.rglob("*") if row.is_dir()), reverse=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

# Linux renameat2 with RENAME_NOREPLACE is the publication lock: if another
# finisher won the same destination while this job was rendering, this job
# fails without nesting into or replacing the winner.
python3 - "$STAGE" "$DEST" <<'PY'
import ctypes
import errno
import os
import sys

source, destination = (os.fsencode(value) for value in sys.argv[1:])
library = ctypes.CDLL(None, use_errno=True)
try:
    renameat2 = library.renameat2
except AttributeError as exc:
    raise SystemExit("renameat2 is required for atomic no-replace publication") from exc
renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
renameat2.restype = ctypes.c_int
if renameat2(-100, source, -100, destination, 1) != 0:  # AT_FDCWD, RENAME_NOREPLACE
    failure = ctypes.get_errno()
    if failure == errno.EEXIST:
        raise SystemExit(f"refusing to replace destination created during finishing: {sys.argv[2]}")
    raise OSError(failure, os.strerror(failure), sys.argv[2])
PY
STAGE=""
python3 - "$PARENT" <<'PY'
import os
import sys

descriptor = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY

echo "EMERGENCY SCREENER READY: $DEST/$FINAL_NAME"
echo "Human full-cut review and filing approval are still required."
