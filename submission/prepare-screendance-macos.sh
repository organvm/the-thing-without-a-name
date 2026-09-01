#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 /absolute/output/directory [/absolute/approved-first-30s.mp4]" >&2
  exit 2
}

[[ $# -ge 1 && $# -le 2 ]] || usage
case "$1" in
  /*) ;;
  *) echo "output directory must be an absolute path" >&2; exit 2 ;;
esac
if [[ $# -eq 2 ]]; then
  case "$2" in
    /*) ;;
    *) echo "approved first-30-second montage must be an absolute path" >&2; exit 2 ;;
  esac
  [[ -f "$2" ]] || { echo "missing approved first-30-second montage: $2" >&2; exit 1; }
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
DEST="$1"
WORK="$DEST/render-work"
PICTURE="$WORK/screener-default.mp4"
OPENING_WORK="$WORK/opening-revision"
REVISED_PICTURE="$WORK/screener-default-opening-revised.mp4"
AUDIO="$ROOT/.work/music/competition/delibes-master.wav"
FINAL="$DEST/THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4"
FONT="/System/Library/Fonts/Menlo.ttc"

[[ "$(uname -s)" == "Darwin" ]] || { echo "this final render must run on the project Mac" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "missing macOS credit-card font: $FONT" >&2; exit 1; }
[[ ! -e "$DEST" ]] || { echo "refusing to overwrite existing output directory: $DEST" >&2; exit 1; }
git -C "$ROOT" diff --quiet && git -C "$ROOT" diff --cached --quiet || {
  echo "commit or stash tracked changes before rendering" >&2
  exit 1
}

mkdir -p "$DEST" "$WORK"

python3 "$ROOT/sound/render_music.py" \
  --choreography "$ROOT/render/choreography.json"

python3 "$ROOT/render/render.py" \
  --capture screener \
  --start 0 \
  --tier screen \
  --codec h264 \
  --score music/score.json \
  --choreography render/choreography.json \
  --out "$WORK" \
  --resume

[[ -f "$PICTURE" ]] || { echo "missing rendered picture: $PICTURE" >&2; exit 1; }
[[ -f "$AUDIO" ]] || { echo "missing rendered score: $AUDIO" >&2; exit 1; }

OPENING_ARGS=(
  --picture "$PICTURE"
  --out "$OPENING_WORK"
)
if [[ $# -eq 2 ]]; then
  OPENING_ARGS+=(--reuse-montage "$2")
fi
python3 "$ROOT/submission/revise_screendance_opening.py" "${OPENING_ARGS[@]}"

[[ -f "$OPENING_WORK/first-minute-replacement.mp4" ]] || {
  echo "missing phrase-native first-minute replacement" >&2
  exit 1
}

# The replacement owns frames 0-1799. The canonical renderer resumes at frame
# 1800, preserving every frame from 01:00 onward while avoiding timestamp or
# keyframe drift at the splice.
ffmpeg -hide_banner -loglevel error -y \
  -i "$OPENING_WORK/first-minute-replacement.mp4" \
  -i "$PICTURE" \
  -filter_complex "[1:v]trim=start_frame=1800,setpts=PTS-STARTPTS,fps=30,scale=1280:720:flags=lanczos,setsar=1[tail];[0:v][tail]concat=n=2:v=1:a=0,setsar=1[v]" \
  -map "[v]" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  -video_track_timescale 15360 -an "$REVISED_PICTURE"

ffmpeg -hide_banner -loglevel error -y \
  -i "$REVISED_PICTURE" -i "$AUDIO" \
  -map 0:v:0 -map 1:a:0 \
  -vf "drawbox=color=black:t=fill:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='THE THING WITHOUT A NAME':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=250:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='PERFORMANCE · MADISON GARBER':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=350:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='PRIMARY CHOREOGRAPHY · MADISON GARBER':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=395:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='ANTHONY J. PADAVANO':fontcolor=white:fontsize=30:x=(w-text_w)/2:y=460:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='CONCEPT · DIRECTION · ADDITIONAL CHOREOGRAPHY · PHOTOGRAPHY · EDITING':fontcolor=white:fontsize=18:x=(w-text_w)/2:y=505:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='SOUND · SOFTWARE · ARCHIVE · PRODUCTION':fontcolor=white:fontsize=20:x=(w-text_w)/2:y=535:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='MUSIC · LÉO DELIBES':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=590:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='SOURCE ARRANGEMENTS · PAUL DE BRA · CC BY 4.0':fontcolor=white:fontsize=21:x=(w-text_w)/2:y=630:enable='gte(t,346.896343125)'" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -shortest "$FINAL"

for item in "01:60" "02:180" "03:300"; do
  number="${item%%:*}"
  second="${item##*:}"
  ffmpeg -hide_banner -loglevel error -y -ss "$second" -i "$FINAL" \
    -frames:v 1 -q:v 2 "$DEST/still-$number.jpg"
done

ffprobe -v error -show_format -show_streams -of json "$FINAL" > "$DEST/ffprobe.json"
(
  cd "$DEST"
  shasum -a 256 \
    THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4 \
    still-01.jpg still-02.jpg still-03.jpg > SHA256SUMS
)

python3 - "$DEST/ffprobe.json" <<'PY'
import json
import sys
from pathlib import Path

probe = json.loads(Path(sys.argv[1]).read_text())
streams = probe.get("streams", [])
video = next((row for row in streams if row.get("codec_type") == "video"), {})
audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
duration = float(probe.get("format", {}).get("duration", 0))
errors = []
if video.get("codec_name") != "h264": errors.append("video codec is not H.264")
if int(video.get("width", 0)) < 1280 or int(video.get("height", 0)) < 720: errors.append("video is below 720 HD")
if audio.get("codec_name") != "aac": errors.append("audio codec is not AAC")
if int(audio.get("sample_rate", 0)) != 48000: errors.append("audio is not 48 kHz")
if abs(duration - 350.896343125) > 0.15: errors.append(f"duration is {duration:.3f}s, expected 350.896s")
if errors:
    raise SystemExit("FAIL: " + "; ".join(errors))
print(f"READY: {video.get('width')}x{video.get('height')} H.264 + 48 kHz AAC, {duration:.3f}s")
PY

echo "Opening receipt: $OPENING_WORK/opening-revision-receipt.json"
echo "Output: $FINAL"
