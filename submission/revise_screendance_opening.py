#!/usr/bin/env python3
"""Rebuild the ScreenDance opening around the score's authored phrase turns.

The artist-approved 00:00-00:30 registered-photo scan is preserved.  After that
fixed opening, the scan resolves into the hand-edited 2017 composite at the end
of ``sylvia-03``; the hand-edited composite owns ``sylvia-04``; and the
canonical moving picture enters across ``sylvia-05``, becoming complete at its
end.  Every score time is snapped once
to the 30 fps delivery grid and recorded in the receipt.  The picture from the
resolved handoff through 00:60 is therefore the matching-time canonical film,
so concatenating renderer segment 003 onward leaves the tail untouched.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXED_OPENING_SECOND = 30.0
ASSEMBLY_RESOLVE_PHRASE = "sylvia-03"
DIVISION_ENTRY_PHRASE = "sylvia-04"
CANONICAL_HANDOFF_PHRASE = "sylvia-05"
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


def frame_number(second: float) -> int:
    """Nearest delivery-frame boundary, expressed as a frame count from zero."""
    if not math.isfinite(second) or second < 0:
        raise ValueError(f"invalid score time: {second!r}")
    return int(math.floor(second * FPS + 0.5))


def frame_second(second: float) -> float:
    return frame_number(second) / FPS


def phrase(score: dict, phrase_id: str) -> dict:
    matches = [row for row in score.get("phrases", []) if row.get("id") == phrase_id]
    if len(matches) != 1:
        raise ValueError(f"score must contain exactly one phrase {phrase_id!r}, found {len(matches)}")
    return matches[0]


def opening_timing(score: dict, choreography: dict) -> dict:
    assignments = {
        row.get("phrase_id"): row.get("movement_id")
        for row in choreography.get("phrase_assignments", [])
    }
    expected_movements = {
        ASSEMBLY_RESOLVE_PHRASE: "ASSEMBLY",
        DIVISION_ENTRY_PHRASE: "ASSEMBLY",
        CANONICAL_HANDOFF_PHRASE: "DIVISION",
    }
    for phrase_id, movement_id in expected_movements.items():
        if assignments.get(phrase_id) != movement_id:
            raise ValueError(
                f"choreography assigns {phrase_id!r} to {assignments.get(phrase_id)!r}, "
                f"expected {movement_id!r}"
            )

    score_seconds = {
        "fixed_opening_end": FIXED_OPENING_SECOND,
        "assembly_resolve": float(phrase(score, ASSEMBLY_RESOLVE_PHRASE)["end_second"]),
        "division_entry": float(phrase(score, DIVISION_ENTRY_PHRASE)["end_second"]),
        "canonical_handoff": float(phrase(score, CANONICAL_HANDOFF_PHRASE)["end_second"]),
        "tail_unchanged": TAIL_UNCHANGED_SECOND,
    }
    if not (
        score_seconds["fixed_opening_end"]
        < score_seconds["assembly_resolve"]
        < score_seconds["division_entry"]
        < score_seconds["canonical_handoff"]
        < score_seconds["tail_unchanged"]
    ):
        raise ValueError(f"opening phrase boundaries are not strictly ordered: {score_seconds}")

    frame_seconds = {name: frame_second(value) for name, value in score_seconds.items()}
    frames = {name: frame_number(value) for name, value in score_seconds.items()}
    if frames["fixed_opening_end"] != round(FIXED_OPENING_SECOND * FPS):
        raise ValueError("fixed opening is not exactly representable on the delivery grid")
    if frames["tail_unchanged"] != round(TAIL_UNCHANGED_SECOND * FPS):
        raise ValueError("tail handoff is not exactly representable on the delivery grid")
    return {
        "score_seconds": score_seconds,
        "frame_seconds": frame_seconds,
        "frames": frames,
        "phrases": {
            "assembly_resolve": ASSEMBLY_RESOLVE_PHRASE,
            "division_entry": DIVISION_ENTRY_PHRASE,
            "canonical_handoff": CANONICAL_HANDOFF_PHRASE,
        },
    }


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


def pulse_times(score: dict, end_second: float) -> list[float]:
    ticks_per_quarter = int(score["time"]["ticks_per_quarter"])
    if ticks_per_quarter % PULSES_PER_BEAT:
        raise ValueError("score resolution cannot represent the opening pulse grid")
    step = ticks_per_quarter // PULSES_PER_BEAT
    result: list[float] = []
    tick = 0
    while True:
        second = second_at_tick(score, tick)
        if second >= end_second:
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


def encode_sequence(
    *,
    ffmpeg: str,
    sequence: Path,
    start_frame: int,
    frame_count: int,
    destination: Path,
    preset: str,
) -> None:
    if frame_count < 1:
        raise ValueError("sequence encode requires at least one frame")
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(FPS),
            "-start_number",
            str(start_frame),
            "-i",
            str(sequence / "opening-%04d.webp"),
            "-frames:v",
            str(frame_count),
            "-vf",
            "scale=1280:960:flags=lanczos,crop=1280:720:0:120,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(destination),
        ]
    )


def encode_video_segment(
    *,
    ffmpeg: str,
    source: Path,
    start_frame: int,
    frame_count: int,
    destination: Path,
    preset: str,
) -> None:
    if start_frame < 0 or frame_count < 1:
        raise ValueError("video segment requires a non-negative start and positive frame count")
    end_frame = start_frame + frame_count
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            str(frame_count),
            "-vf",
            (
                f"trim=start_frame={start_frame}:end_frame={end_frame},"
                "setpts=PTS-STARTPTS,fps=30,scale=1280:720:flags=lanczos,setsar=1"
            ),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "15360",
            "-an",
            str(destination),
        ]
    )


def crossfade_segment(
    *,
    ffmpeg: str,
    first: Path,
    second: Path,
    frame_count: int,
    destination: Path,
    preset: str,
) -> None:
    if frame_count < 2:
        raise ValueError("crossfade requires at least two frames")
    # The last sampled frame lands at (N - 1) / FPS.  Finishing the fade there
    # makes the following source-native segment continuous on the next frame.
    fade_duration = (frame_count - 1) / FPS
    filters = (
        f"[0:v][1:v]xfade=transition=fade:duration={fade_duration:.9f}:offset=0,"
        "setsar=1,format=yuv420p[out]"
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(first),
            "-i",
            str(second),
            "-filter_complex",
            filters,
            "-map",
            "[out]",
            "-frames:v",
            str(frame_count),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "15360",
            "-an",
            str(destination),
        ]
    )


def concat_segments(
    *,
    ffmpeg: str,
    segments: list[Path],
    output_frames: int,
    destination: Path,
    preset: str,
) -> None:
    if not segments:
        raise ValueError("final opening concat has no segments")
    filters = []
    for index in range(len(segments)):
        filters.append(f"[{index}:v]fps=30,setpts=PTS-STARTPTS[v{index}]")
    inputs = "".join(f"[v{index}]" for index in range(len(segments)))
    filters.append(
        f"{inputs}concat=n={len(segments)}:v=1:a=0,fps=30,setsar=1,"
        "format=yuv420p,setparams=range=unknown:color_primaries=unknown:"
        "color_trc=unknown:colorspace=unknown[out]"
    )
    arguments = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for segment in segments:
        arguments.extend(["-i", str(segment)])
    arguments.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-frames:v",
            str(output_frames),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "15360",
            "-movflags",
            "+faststart",
            "-an",
            str(destination),
        ]
    )
    run(arguments)


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
        help=(
            "reuse the verified 00:00-00:30 montage; the script renders only its "
            "score-phrase continuation before rebuilding the V1/canonical handoff"
        ),
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
        choreography_path = ROOT / "render" / "choreography.json"
        composite_path = ROOT / "reference" / "T-2017-full.png"
        manifest = document(manifest_path, "danse.corpus.v1")
        score = document(score_path, "danse.music.score.v1")
        choreography = document(choreography_path, "danse.choreography.v1")
        timing = opening_timing(score, choreography)
        if not composite_path.is_file():
            raise ValueError(f"missing hand-edited 2017 composite: {composite_path}")

        # The manifest also carries one later archival composite (IMG_1926).
        # Only the explicitly registered June 2017 exposures belong in the
        # artist-approved opening-photo sweep.
        frames = [row for row in manifest["frames"] if row.get("registered") is True]
        if len(frames) != 161:
            raise ValueError(f"expected 161 registered original photographs, found {len(frames)}")
        plates = [ROOT / "corpus" / "plates" / "screen" / f"{row['id']}.webp" for row in frames]
        missing = [str(path) for path in plates if not path.is_file()]
        if missing:
            raise ValueError(f"missing registered screen plates: {missing[:3]}")

        fixed_end_frame = timing["frames"]["fixed_opening_end"]
        montage_end_frame = timing["frames"]["assembly_resolve"]
        montage_end_second = timing["frame_seconds"]["assembly_resolve"]
        pulses = pulse_times(score, montage_end_second)

        destination.mkdir(parents=True)
        sequence = destination / "opening-frames"
        sequence.mkdir()
        schedule = []
        seen_before_fixed_end = set()
        sequence_start = fixed_end_frame if args.reuse_montage is not None else 0
        for output_frame in range(montage_end_frame):
            second = output_frame / FPS
            pulse = max(0, bisect.bisect_right(pulses, second) - 1)
            source_index = opening_source_index(pulse, len(plates))
            source = plates[source_index]
            if output_frame < fixed_end_frame:
                seen_before_fixed_end.add(frames[source_index]["id"])
            if output_frame >= sequence_start:
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
        if len(seen_before_fixed_end) != len(frames):
            raise ValueError(
                f"fixed 30-second opening omitted {len(frames) - len(seen_before_fixed_end)} "
                "registered photographs"
            )

        fixed_end = timing["frame_seconds"]["fixed_opening_end"]
        assembly_resolve = timing["frame_seconds"]["assembly_resolve"]
        division_entry = timing["frame_seconds"]["division_entry"]
        canonical_handoff = timing["frame_seconds"]["canonical_handoff"]
        division_entry_frame = timing["frames"]["division_entry"]
        canonical_handoff_frame = timing["frames"]["canonical_handoff"]
        output_frames = timing["frames"]["tail_unchanged"]

        fixed_montage = destination / "opening-montage-fixed-30s.mp4"
        reused_montage = None
        if args.reuse_montage is not None:
            reused_montage = args.reuse_montage.resolve()
            if not reused_montage.is_file():
                raise ValueError(f"missing reusable montage: {reused_montage}")
            fixed_media = probe(args.ffprobe, reused_montage)
            if (
                fixed_media.get("codec_name") != "h264"
                or int(fixed_media.get("width", 0)) != 1280
                or int(fixed_media.get("height", 0)) != 720
                or fixed_media.get("avg_frame_rate") != "30/1"
                or int(fixed_media.get("nb_read_frames", 0)) != fixed_end_frame
                or abs(float(fixed_media.get("duration", 0)) - FIXED_OPENING_SECOND) > 0.05
            ):
                raise ValueError(f"reusable montage is not the exact 30-second opening: {fixed_media}")
            fixed_montage = reused_montage
        else:
            encode_sequence(
                ffmpeg=args.ffmpeg,
                sequence=sequence,
                start_frame=0,
                frame_count=fixed_end_frame,
                destination=fixed_montage,
                preset=args.preset,
            )

        continuation_frames = montage_end_frame - fixed_end_frame
        continuation = destination / "opening-montage-assembly-transition.mp4"
        encode_sequence(
            ffmpeg=args.ffmpeg,
            sequence=sequence,
            start_frame=fixed_end_frame,
            frame_count=continuation_frames,
            destination=continuation,
            preset=args.preset,
        )

        # The hand-cut composite is no longer held as an inert 28-second plate.
        # It receives a continuous bounded camera path for the interval in which
        # it is the dominant image, then yields to matching-time canonical film.
        v1_frames = canonical_handoff_frame - fixed_end_frame
        v1_camera = destination / "v1-camera-through-division.mp4"
        run(
            [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(FPS),
                "-i",
                str(composite_path),
                "-frames:v",
                str(v1_frames),
                "-vf",
                (
                    "scale=1408:1056:flags=lanczos,"
                    "crop=1280:720:"
                    "x='(in_w-out_w)/2+40*sin(PI*t/20)':"
                    "y='(in_h-out_h)/2+18*(1-cos(PI*t/15))',"
                    "fps=30,setsar=1"
                ),
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
                "-an",
                str(v1_camera),
            ]
        )

        assembly_transition = destination / "transition-montage-to-v1.mp4"
        crossfade_segment(
            ffmpeg=args.ffmpeg,
            first=continuation,
            second=v1_camera,
            frame_count=continuation_frames,
            destination=assembly_transition,
            preset=args.preset,
        )

        v1_middle_start = continuation_frames
        v1_middle_frames = division_entry_frame - montage_end_frame
        v1_middle = destination / "v1-assembly-phrase.mp4"
        encode_video_segment(
            ffmpeg=args.ffmpeg,
            source=v1_camera,
            start_frame=v1_middle_start,
            frame_count=v1_middle_frames,
            destination=v1_middle,
            preset=args.preset,
        )

        division_transition_frames = canonical_handoff_frame - division_entry_frame
        v1_division = destination / "v1-division-source.mp4"
        encode_video_segment(
            ffmpeg=args.ffmpeg,
            source=v1_camera,
            start_frame=division_entry_frame - fixed_end_frame,
            frame_count=division_transition_frames,
            destination=v1_division,
            preset=args.preset,
        )
        canonical_division = destination / "canonical-division-source.mp4"
        encode_video_segment(
            ffmpeg=args.ffmpeg,
            source=picture,
            start_frame=division_entry_frame,
            frame_count=division_transition_frames,
            destination=canonical_division,
            preset=args.preset,
        )
        division_transition = destination / "transition-v1-to-canonical.mp4"
        crossfade_segment(
            ffmpeg=args.ffmpeg,
            first=v1_division,
            second=canonical_division,
            frame_count=division_transition_frames,
            destination=division_transition,
            preset=args.preset,
        )

        canonical_tail_frames = output_frames - canonical_handoff_frame
        canonical_tail = destination / "canonical-tail-to-60s.mp4"
        encode_video_segment(
            ffmpeg=args.ffmpeg,
            source=picture,
            start_frame=canonical_handoff_frame,
            frame_count=canonical_tail_frames,
            destination=canonical_tail,
            preset=args.preset,
        )

        segment_plan = [
            (fixed_montage, fixed_end_frame, "fixed opening"),
            (assembly_transition, continuation_frames, "montage-to-V1 transition"),
            (v1_middle, v1_middle_frames, "V1 assembly phrase"),
            (division_transition, division_transition_frames, "V1-to-canonical transition"),
            (canonical_tail, canonical_tail_frames, "canonical tail"),
        ]
        for segment_path, expected_frames, role in segment_plan:
            segment_media = probe(args.ffprobe, segment_path)
            if (
                segment_media.get("codec_name") != "h264"
                or int(segment_media.get("width", 0)) != 1280
                or int(segment_media.get("height", 0)) != 720
                or segment_media.get("avg_frame_rate") != "30/1"
                or int(segment_media.get("nb_read_frames", 0)) != expected_frames
            ):
                raise ValueError(f"{role} has the wrong media identity: {segment_media}")

        revised = destination / "first-minute-replacement.mp4"
        segment_paths = [row[0] for row in segment_plan]
        concat_segments(
            ffmpeg=args.ffmpeg,
            segments=segment_paths,
            output_frames=output_frames,
            destination=revised,
            preset=args.preset,
        )

        media = probe(args.ffprobe, revised)
        duration = float(media.get("duration", 0))
        frame_count = int(media.get("nb_read_frames", 0))
        if (
            media.get("codec_name") != "h264"
            or int(media.get("width", 0)) != 1280
            or int(media.get("height", 0)) != 720
        ):
            raise ValueError(f"revised picture has wrong media identity: {media}")
        if media.get("avg_frame_rate") != "30/1":
            raise ValueError(f"revised picture is not 30 fps: {media.get('avg_frame_rate')}")
        if abs(duration - TAIL_UNCHANGED_SECOND) > 0.05:
            raise ValueError(f"replacement duration is {duration:.6f}s")
        if frame_count != round(TAIL_UNCHANGED_SECOND * FPS):
            raise ValueError(f"revised picture has {frame_count} frames")

        receipt_segments = []
        cursor = 0
        for segment_path, segment_frames, role in segment_plan:
            receipt_segments.append(
                {
                    "role": role,
                    "path": str(segment_path),
                    "sha256": sha256(segment_path),
                    "start_frame": cursor,
                    "end_frame_exclusive": cursor + segment_frames,
                    "start_second": cursor / FPS,
                    "end_second": (cursor + segment_frames) / FPS,
                    "frames": segment_frames,
                }
            )
            cursor += segment_frames
        if cursor != output_frames:
            raise ValueError(f"opening segment plan totals {cursor} frames, expected {output_frames}")

        receipt = {
            "schema": "danse.submission.opening-revision.v2",
            "purpose": "ScreenDance artist-directed phrase-native opening revision",
            "inputs": {
                "picture": {"path": str(picture), "sha256": sha256(picture)},
                "manifest": {
                    "path": manifest_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(manifest_path),
                },
                "score": {
                    "path": score_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(score_path),
                },
                "choreography": {
                    "path": choreography_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(choreography_path),
                },
                "edited_v1": {
                    "path": composite_path.relative_to(ROOT).as_posix(),
                    "sha256": sha256(composite_path),
                },
                "renderer_script": {
                    "path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
                    "sha256": sha256(Path(__file__).resolve()),
                },
                "fixed_opening_source": {
                    "path": str(fixed_montage),
                    "sha256": sha256(fixed_montage),
                    "reused": reused_montage is not None,
                },
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
                "fixed_opening": {
                    "start_second": 0.0,
                    "end_second": FIXED_OPENING_SECOND,
                    "instruction": "preserve the accepted 161-image forward/backward scan",
                },
                "score_native_boundaries": timing,
                "assembly_transition": {
                    "start_second": fixed_end,
                    "end_second": assembly_resolve,
                    "source": "registered-photo scan",
                    "target": "moving hand-edited 2017 composite",
                },
                "v1_dominant_phrase": {
                    "start_second": assembly_resolve,
                    "end_second": division_entry,
                    "source": "moving hand-edited 2017 composite",
                },
                "canonical_picture_transition": {
                    "start_second": division_entry,
                    "end_second": canonical_handoff,
                    "source_time_aligned": True,
                    "target": "canonical moving film",
                },
                "canonical_picture_only": {
                    "start_second": canonical_handoff,
                    "end_second": TAIL_UNCHANGED_SECOND,
                },
                "v1_motion": (
                    "source-native composite; 1408x1056 cover with a visible bounded "
                    "40-pixel horizontal and 36-pixel vertical camera path"
                ),
                "transition_method": "source-native linear crossfade; no generated interpolation",
                "synthetic_visual_sources": False,
                "unchanged_tail_start_second": TAIL_UNCHANGED_SECOND,
                "segments": receipt_segments,
                "scan_schedule": schedule,
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
        (destination / "opening-revision-receipt.json").write_text(
            json.dumps(receipt, indent=2) + "\n"
        )
        shutil.rmtree(sequence)
        print(f"READY: {revised}")
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
