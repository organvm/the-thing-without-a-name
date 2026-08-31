#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 /absolute/output/directory /absolute/picture.mp4 /absolute/audio.wav" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
for path in "$1" "$2" "$3"; do
  case "$path" in
    /*) ;;
    *) echo "all paths must be absolute: $path" >&2; exit 2 ;;
  esac
done

DEST="$1"
PICTURE="$2"
AUDIO="$3"
FINAL="$DEST/THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4"
FONT="${DANSE_CREDIT_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf}"

[[ -f "$PICTURE" ]] || { echo "missing picture: $PICTURE" >&2; exit 1; }
[[ -f "$AUDIO" ]] || { echo "missing audio: $AUDIO" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "missing credit-card font: $FONT" >&2; exit 1; }
[[ ! -e "$DEST" ]] || { echo "refusing to overwrite output directory: $DEST" >&2; exit 1; }

mkdir -p "$DEST"

ffmpeg -hide_banner -loglevel error -y \
  -i "$PICTURE" -i "$AUDIO" \
  -map 0:v:0 -map 1:a:0 \
  -vf "drawbox=color=black:t=fill:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='THE THING WITHOUT A NAME':fontcolor=white:fontsize=42:x=(w-text_w)/2:y=230:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='A FILM BY ANTHONY J. PADAVANO':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=310:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='MUSIC BY LÉO DELIBES':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=365:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='SOURCE ARRANGEMENTS · PAUL DE BRA · CC BY 4.0':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=420:enable='gte(t,346.896343125)'" \
  -c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p \
  -c:a aac -b:a 320k -ar 48000 -ac 2 \
  -movflags +faststart -shortest "$FINAL"

for item in "01:60" "02:180" "03:300"; do
  number="${item%%:*}"
  second="${item##*:}"
  ffmpeg -hide_banner -loglevel error -y -ss "$second" -i "$FINAL" \
    -frames:v 1 -q:v 2 "$DEST/still-$number.jpg"
done

ffmpeg -hide_banner -loglevel error -y -ss 348 -i "$FINAL" \
  -frames:v 1 -q:v 2 "$DEST/credit-card-check.jpg"
ffprobe -v error -show_format -show_streams -of json "$FINAL" > "$DEST/ffprobe.json"

(
  cd "$DEST"
  sha256sum \
    THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4 \
    still-01.jpg still-02.jpg still-03.jpg credit-card-check.jpg > SHA256SUMS
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
if int(video.get("width", 0)) < 1280 or int(video.get("height", 0)) < 720:
    errors.append("video is below 720 HD")
if audio.get("codec_name") != "aac": errors.append("audio codec is not AAC")
if int(audio.get("sample_rate", 0)) != 48000: errors.append("audio is not 48 kHz")
if abs(duration - 350.896343125) > 0.15:
    errors.append(f"duration is {duration:.3f}s, expected 350.896s")
if errors:
    raise SystemExit("FAIL: " + "; ".join(errors))
print(
    f"READY: {video.get('width')}x{video.get('height')} H.264 + "
    f"48 kHz AAC, {duration:.3f}s"
)
PY

echo "Output: $FINAL"
