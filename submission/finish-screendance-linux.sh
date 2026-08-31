#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 /absolute/output/directory /absolute/picture.mp4 /absolute/audio.wav /absolute/picture-receipt.json /absolute/audio-receipt.json" >&2
  exit 2
}

[[ $# -eq 5 ]] || usage
for path in "$@"; do
  case "$path" in
    /*) ;;
    *) echo "all paths must be absolute: $path" >&2; exit 2 ;;
  esac
done

DEST="$1"
PICTURE="$2"
AUDIO="$3"
PICTURE_RECEIPT="$4"
AUDIO_RECEIPT="$5"
FINAL="$DEST/THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4"
FONT="${DANSE_CREDIT_FONT:-/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf}"

for path in "$PICTURE" "$AUDIO" "$PICTURE_RECEIPT" "$AUDIO_RECEIPT" "$FONT"; do
  [[ -f "$path" ]] || { echo "missing input: $path" >&2; exit 1; }
done
[[ ! -e "$DEST" ]] || { echo "refusing to overwrite output directory: $DEST" >&2; exit 1; }

mkdir -p "$DEST"
cp "$PICTURE_RECEIPT" "$DEST/picture-source-receipt.json"
cp "$AUDIO_RECEIPT" "$DEST/audio-source-receipt.json"

ffmpeg -hide_banner -loglevel error -y \
  -i "$PICTURE" -i "$AUDIO" \
  -map 0:v:0 -map 1:a:0 \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,\
pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=30,\
drawbox=color=black:t=fill:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='THE THING WITHOUT A NAME':fontcolor=white:fontsize=52:x=(w-text_w)/2:y=350:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='A FILM BY ANTHONY J. PADAVANO':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=470:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='MUSIC BY LÉO DELIBES':fontcolor=white:fontsize=34:x=(w-text_w)/2:y=540:enable='gte(t,346.896343125)',\
drawtext=fontfile=$FONT:text='SOURCE ARRANGEMENTS · PAUL DE BRA · CC BY 4.0':fontcolor=white:fontsize=30:x=(w-text_w)/2:y=610:enable='gte(t,346.896343125)'" \
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
ffmpeg -hide_banner -nostats -i "$FINAL" \
  -af "loudnorm=I=-16:LRA=11:TP=-1:print_format=json" \
  -f null - 2> "$DEST/loudness.log"

python3 - \
  "$DEST/ffprobe.json" "$DEST/loudness.log" "$FINAL" "$PICTURE" "$AUDIO" \
  "$DEST/picture-source-receipt.json" "$DEST/audio-source-receipt.json" \
  "$DEST/finish-receipt.json" <<'PY'
import hashlib
import json
import re
import sys
from fractions import Fraction
from pathlib import Path

(
    probe_path,
    loudness_path,
    final_path,
    picture_path,
    audio_path,
    picture_receipt_path,
    audio_receipt_path,
    finish_receipt_path,
) = map(Path, sys.argv[1:])

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

probe = json.loads(probe_path.read_text())
streams = probe.get("streams", [])
video = next((row for row in streams if row.get("codec_type") == "video"), {})
audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
duration = float(probe.get("format", {}).get("duration", 0))
fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
fps = float(Fraction(fps_text))

matches = re.findall(r'\{\s*"input_i".*?\}', loudness_path.read_text(), flags=re.DOTALL)
if not matches:
    raise SystemExit("FAIL: final AAC loudness measurement is missing")
loudness = json.loads(matches[-1])
integrated_lufs = float(loudness["input_i"])
true_peak_dbtp = float(loudness["input_tp"])

picture_receipt = json.loads(picture_receipt_path.read_text())
audio_receipt = json.loads(audio_receipt_path.read_text())
picture_digest = sha256(picture_path)
audio_digest = sha256(audio_path)
if picture_receipt.get("file_sha256") != picture_digest:
    raise SystemExit("FAIL: picture receipt does not bind the selected picture")
if audio_receipt.get("schema") != "danse.submission.portable-audio.v1":
    raise SystemExit("FAIL: audio receipt schema is not the portable submission contract")
if audio_receipt.get("outputs", {}).get("master", {}).get("sha256") != audio_digest:
    raise SystemExit("FAIL: audio receipt does not bind the selected master")

errors = []
if video.get("codec_name") != "h264":
    errors.append("video codec is not H.264")
if int(video.get("width", 0)) < 1920 or int(video.get("height", 0)) < 1080:
    errors.append("video is below the 1920x1080 screener contract")
if abs(fps - 30.0) > 0.001:
    errors.append(f"video frame rate is {fps:.6f}, expected 30 fps")
if audio.get("codec_name") != "aac":
    errors.append("audio codec is not AAC")
if int(audio.get("sample_rate", 0)) != 48000:
    errors.append("audio is not 48 kHz")
if int(audio.get("channels", 0)) != 2:
    errors.append("audio is not stereo")
if abs(duration - 350.896343125) > 0.15:
    errors.append(f"duration is {duration:.3f}s, expected 350.896s")
if abs(integrated_lufs - (-16.0)) > 0.5:
    errors.append(f"final AAC loudness is {integrated_lufs:.2f} LUFS")
if true_peak_dbtp > -1.0:
    errors.append(f"final AAC true peak is {true_peak_dbtp:.2f} dBTP")
if errors:
    raise SystemExit("FAIL: " + "; ".join(errors))

receipt = {
    "schema": "danse.submission.portable-finish.v1",
    "purpose": "ScreenDance Miami 2027 deadline screener",
    "canonical_exhibition_master": False,
    "inputs": {
        "picture": {
            "sha256": picture_digest,
            "receipt_sha256": sha256(picture_receipt_path),
            "receipt_schema": picture_receipt.get("schema"),
        },
        "audio": {
            "sha256": audio_digest,
            "receipt_sha256": sha256(audio_receipt_path),
            "receipt_schema": audio_receipt.get("schema"),
        },
    },
    "output": {
        "filename": final_path.name,
        "sha256": sha256(final_path),
        "bytes": final_path.stat().st_size,
        "width": int(video["width"]),
        "height": int(video["height"]),
        "fps": fps,
        "duration_seconds": duration,
        "video_codec": video["codec_name"],
        "audio_codec": audio["codec_name"],
        "audio_sample_rate": int(audio["sample_rate"]),
        "audio_channels": int(audio["channels"]),
        "integrated_lufs": integrated_lufs,
        "true_peak_dbtp": true_peak_dbtp,
    },
}
finish_receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
print(
    f"READY: {video.get('width')}x{video.get('height')} H.264 at {fps:.3f} fps + "
    f"48 kHz AAC, {duration:.3f}s, {integrated_lufs:.2f} LUFS, {true_peak_dbtp:.2f} dBTP"
)
PY

(
  cd "$DEST"
  sha256sum \
    THE_THING_WITHOUT_A_NAME_SCREENDANCE_MIAMI_2027.mp4 \
    still-01.jpg still-02.jpg still-03.jpg credit-card-check.jpg \
    picture-source-receipt.json audio-source-receipt.json \
    ffprobe.json loudness.log finish-receipt.json > SHA256SUMS
)

echo "Output: $FINAL"
