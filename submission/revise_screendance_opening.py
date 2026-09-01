#!/usr/bin/env python3
"""Replace the ScreenDance screener's first minute without rerendering its tail.

0:00-0:30 scans every registered source photograph forward and backward on a
sixth-of-a-beat grid derived from the immutable score tempo map.  0:30-0:58
holds the exact hand-edited 2017 composite with a restrained camera drift.
0:58-1:00 dissolves into the existing picture.  The output is an exact
60-second picture-only replacement that can be concatenated with renderer
segment 003 onward, leaving every source frame after 1:00 untouched.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWITCH_SECOND = 30.0
TAIL_JOIN_START = 58.0
TAIL_UNCHANGED_SECOND = 60.0
FPS = 30
PULSES_PER_BEAT = 6


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(arguments: list[str]) -> None:
    subprocess.run(arguments, check=True)


def document(path: Path, schema: str) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError(f"{path}: expected {schema}")
    return value


def second_at_tick(score: dict, tick: int) -> float:
    tempo = score["tempo"]
    ticks = [int(row["tick"]) for row in tempo]
    index = max(0, bisect.bisect_right(ticks, tick) - 1)
    row = tempo[index]
    delta = tick - int(row["tick"])
    return float(row["second"]) + (
        delta * int(row["microseconds_per_quarter"])
        / int(score["time"]["ticks_per_quarter"])
        / 1_000_000
    )


def pulse_times(score: dict) -> list[float]:
    ticks_per_quarter = int(score["time"]["ticks_per_quarter"])
    if ticks_per_quarter % PULSES_PER_BEAT:
        raise ValueError("score resolution cannot represent the opening pulse grid")
    step = ticks_per_quarter // PULSES_PER_BEAT
    result: list[float] = []
    tick = 0
    while True:
        second = second_at_tick(score, tick)
        if second >= SWITCH_SECOND:
            break
        result.append(second)
        tick += step
    if not result or abs(result[0]) > 1e-9:
        raise ValueError("opening pulse grid does not begin at zero")
    return result


def opening_source_index(index: int, count: int) -> int:
    """Forward/reverse scan whose rests land on authored score cues.

    The source set is the 161 registered 2017 exposures.  Two one-pulse holds
    place the forward pivot on bar 10 and the completed return on bar 19; the
    mid-run holds land on the two phrase cues at quarters 24 and 51.
    """
    if count != 161:
        raise ValueError(f"opening cue map requires 161 registered originals, found {count}")
    phase = index % 324
    if phase <= 161:
        return phase if phase <= 143 else phase - 1
    reverse = phase - 162
    if reverse <= 143:
        return 160 - reverse
    if reverse == 144:
        return 17
    return 161 - reverse


def probe(ffprobe: str, picture: Path) -> dict:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,avg_frame_rate,nb_read_frames,duration",
            "-of",
            "json",
            str(picture),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)["streams"][0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--picture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    parser.add_argument("--preset", default="slow")
    parser.add_argument(
        "--reuse-montage",
        type=Path,
        help="reuse a previously verified 30-second montage and render only the V1/handoff pass",
    )
    args = parser.parse_args()

    try:
        if args.ffmpeg is None or args.ffprobe is None:
            raise ValueError("ffmpeg and ffprobe are required")
        picture = args.picture.resolve()
        destination = args.out.resolve()
        if not picture.is_file():
            raise ValueError(f"missing picture: {picture}")
        if destination.exists():
            raise ValueError(f"refusing to overwrite destination: {destination}")

        manifest_path = ROOT / "corpus" / "manifest.json"
        score_path = ROOT / "music" / "score.json"
        composite_path = ROOT / "reference" / "T-2017-full.png"
        manifest = document(manifest_path, "danse.corpus.v1")
        score = document(score_path, "danse.music.score.v1")
        if not composite_path.is_file():
            raise ValueError(f"missing hand-edited 2017 composite: {composite_path}")
        # The manifest also carries one later archival composite (IMG_1926).
        # Only the explicitly registered June 2017 exposures belong in the
        # artist-requested original-photo sweep.
        frames = [row for row in manifest["frames"] if row.get("registered") is True]
        if len(frames) != 161:
            raise ValueError(f"expected 161 registered original photographs, found {len(frames)}")
        plates = [ROOT / "corpus" / "plates" / "screen" / f"{row['id']}.webp" for row in frames]
        missing = [str(path) for path in plates if not path.is_file()]
        if missing:
            raise ValueError(f"missing registered screen plates: {missing[:3]}")

        pulses = pulse_times(score)
        destination.mkdir(parents=True)
        sequence = destination / "opening-frames"
        if args.reuse_montage is None:
            sequence.mkdir()
        schedule = []
        seen = set()
        for output_frame in range(round(SWITCH_SECOND * FPS)):
            second = output_frame / FPS
            pulse = max(0, bisect.bisect_right(pulses, second) - 1)
            source_index = opening_source_index(pulse, len(plates))
            source = plates[source_index]
            seen.add(frames[source_index]["id"])
            if args.reuse_montage is None:
                target = sequence / f"opening-{output_frame:04d}.webp"
                target.symlink_to(source)
            schedule.append(
                {
                    "output_frame": output_frame,
                    "second": round(second, 9),
                    "pulse": pulse,
                    "source_frame_id": frames[source_index]["id"],
                }
            )
        if len(seen) != len(frames):
            raise ValueError(f"opening scan omitted {len(frames) - len(seen)} registered photographs")

        if args.reuse_montage is not None:
            montage = args.reuse_montage.resolve()
            if not montage.is_file():
                raise ValueError(f"missing reusable montage: {montage}")
            montage_media = probe(args.ffprobe, montage)
            if (
                int(montage_media.get("nb_read_frames", 0)) != round(SWITCH_SECOND * FPS)
                or abs(float(montage_media.get("duration", 0)) - SWITCH_SECOND) > 0.05
            ):
                raise ValueError(f"reusable montage is not exactly 30 seconds: {montage_media}")
        else:
            montage = destination / "opening-montage.mp4"
            run(
                [
                    args.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-framerate",
                    str(FPS),
                    "-start_number",
                    "0",
                    "-i",
                    str(sequence / "opening-%04d.webp"),
                    "-frames:v",
                    str(round(SWITCH_SECOND * FPS)),
                    "-vf",
                    "scale=1280:960:flags=lanczos,crop=1280:720:0:120,setsar=1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    args.preset,
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(montage),
                ]
            )

        revised = destination / "first-minute-replacement.mp4"
        filters = (
            "[0:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS[montage];"
            "[1:v]scale=1306:980:flags=lanczos,"
            "crop=1280:720:"
            "x='(in_w-out_w)/2+8*sin(t*PI/14)':"
            "y='(in_h-out_h)/2+4*cos(t*PI/9)',"
            "fps=30,trim=duration=30,settb=AVTB,setpts=PTS-STARTPTS[v1];"
            "[2:v]trim=start=58:end=60,fps=30,settb=AVTB,setpts=PTS-STARTPTS[tail];"
            "[v1][tail]xfade=transition=fade:duration=2:offset=28[after];"
            "[montage][after]concat=n=2:v=1:a=0,"
            "setsar=0,setparams=range=unknown:color_primaries=unknown:"
            "color_trc=unknown:colorspace=unknown[out]"
        )
        run(
            [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(montage),
                "-loop",
                "1",
                "-framerate",
                str(FPS),
                "-i",
                str(composite_path),
                "-i",
                str(picture),
                "-filter_complex",
                filters,
                "-map",
                "[out]",
                "-c:v",
                "libx264",
                "-preset",
                args.preset,
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-video_track_timescale",
                "15360",
                "-movflags",
                "+faststart",
                "-an",
                str(revised),
            ]
        )

        media = probe(args.ffprobe, revised)
        duration = float(media.get("duration", 0))
        frame_count = int(media.get("nb_read_frames", 0))
        if media.get("codec_name") != "h264" or int(media.get("width", 0)) != 1280 or int(media.get("height", 0)) != 720:
            raise ValueError(f"revised picture has wrong media identity: {media}")
        if media.get("avg_frame_rate") != "30/1":
            raise ValueError(f"revised picture is not 30 fps: {media.get('avg_frame_rate')}")
        if abs(duration - TAIL_UNCHANGED_SECOND) > 0.05:
            raise ValueError(f"replacement duration is {duration:.6f}s")
        if frame_count != round(TAIL_UNCHANGED_SECOND * FPS):
            raise ValueError(f"revised picture has {frame_count} frames")

        receipt = {
            "schema": "danse.submission.opening-revision.v1",
            "purpose": "ScreenDance Miami 2027 artist-directed opening revision",
            "inputs": {
                "picture": {"path": str(picture), "sha256": sha256(picture)},
                "manifest": {"path": manifest_path.relative_to(ROOT).as_posix(), "sha256": sha256(manifest_path)},
                "score": {"path": score_path.relative_to(ROOT).as_posix(), "sha256": sha256(score_path)},
                "edited_v1": {
                    "path": composite_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(composite_path),
                },
                "renderer_script": {
                    "path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
                    "sha256": sha256(Path(__file__).resolve()),
                },
                "montage": {"path": str(montage), "sha256": sha256(montage)},
                "registered_screen_plates": [
                    {
                        "frame_id": row["id"],
                        "path": path.relative_to(ROOT).as_posix(),
                        "sha256": sha256(path),
                    }
                    for row, path in zip(frames, plates)
                ],
            },
            "edit": {
                "registered_originals": len(frames),
                "source_order": [row["id"] for row in frames],
                "pulse_subdivision_per_beat": PULSES_PER_BEAT,
                "montage_start_second": 0.0,
                "montage_end_second": SWITCH_SECOND,
                "v1_composite": composite_path.relative_to(ROOT).as_posix(),
                "v1_motion": "2% center enlargement with eight-pixel horizontal and four-pixel vertical score-span drift",
                "tail_dissolve_start_second": TAIL_JOIN_START,
                "unchanged_tail_start_second": TAIL_UNCHANGED_SECOND,
                "schedule": schedule,
            },
            "output": {
                "path": str(revised),
                "sha256": sha256(revised),
                "duration_seconds": duration,
                "frames": frame_count,
                "width": int(media["width"]),
                "height": int(media["height"]),
                "fps": media["avg_frame_rate"],
            },
        }
        (destination / "opening-revision-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if sequence.exists():
            shutil.rmtree(sequence)
        print(f"READY: {revised}")
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
