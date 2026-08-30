#!/usr/bin/env python3
"""Compile an owned/generated MIDI source into Danse's immutable score contract.

Only MIDI bytes and the layered repertoire register are authored inputs. The
compiler resolves tempo, meter, beats/downbeats, phrases, cues/accents,
dynamics, orchestration, notes, and movement bindings onto absolute source time.
No wall clock or entropy enters the result.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from validate_repertoire import ROOT, load_register, sha256, validate_document

DEFAULT_REGISTER = ROOT / "music" / "repertoire.yaml"
DEFAULT_PROGRAM = ROOT / "render" / "program.json"
DEFAULT_OUTPUT = ROOT / "music" / "score.json"
SCHEMA = "danse.music.score.v1"
COMPILER = "danse.music.compiler.v1"


@dataclass(frozen=True)
class Event:
    tick: int
    track: int
    order: int
    kind: str
    data: tuple[Any, ...]


def _canonical_tree(value: Any) -> list[Any]:
    """Type-tag a JSON value so Python and JavaScript hash identical bytes."""
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["boolean", value]
    if type(value) in (int, float):
        number = float(value)
        if not math.isfinite(number) or (type(value) is int and int(number) != value):
            raise ValueError(f"number cannot be represented as finite IEEE-754: {value!r}")
        return ["number", struct.pack(">d", number).hex()]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_canonical_tree(item) for item in value]]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        return ["object", [[key, _canonical_tree(value[key])] for key in sorted(value)]]
    raise ValueError(f"unsupported canonical JSON value {type(value).__name__}")


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(_canonical_tree(value), separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def variable_length(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise ValueError("truncated MIDI variable-length value")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, position
    raise ValueError("MIDI variable-length value exceeds four bytes")


def parse_track(data: bytes, track: int) -> tuple[list[Event], int]:
    events: list[Event] = []
    position = 0
    tick = 0
    running: int | None = None
    order = 0
    end_tick = 0
    while position < len(data):
        delta, position = variable_length(data, position)
        tick += delta
        end_tick = max(end_tick, tick)
        if position >= len(data):
            raise ValueError(f"track {track}: missing event after delta")
        status = data[position]
        if status & 0x80:
            position += 1
            if status < 0xF0:
                running = status
        elif running is not None:
            status = running
        else:
            raise ValueError(f"track {track}: data byte without running status")

        if status == 0xFF:
            if position >= len(data):
                raise ValueError(f"track {track}: truncated meta event")
            kind = data[position]
            position += 1
            length, position = variable_length(data, position)
            payload = data[position : position + length]
            if len(payload) != length:
                raise ValueError(f"track {track}: truncated meta payload")
            position += length
            if kind == 0x2F:
                events.append(Event(tick, track, order, "end", ()))
            elif kind == 0x51:
                if length != 3:
                    raise ValueError(f"track {track}: tempo payload is not three bytes")
                events.append(Event(tick, track, order, "tempo", (int.from_bytes(payload, "big"),)))
            elif kind == 0x58:
                if length != 4:
                    raise ValueError(f"track {track}: meter payload is not four bytes")
                events.append(Event(tick, track, order, "meter", tuple(payload)))
            elif kind == 0x21:
                raise ValueError("MIDI port meta events are unsupported; one output port is required")
            elif kind in {0x03, 0x06}:
                try:
                    value = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"track {track}: metadata is not UTF-8") from exc
                events.append(Event(tick, track, order, "track_name" if kind == 0x03 else "marker", (value,)))
            # Standard MIDI File meta events cancel any channel-message
            # running status. Subsequent data bytes must introduce a new
            # channel status byte rather than inheriting across metadata.
            running = None
            order += 1
            continue
        if status in {0xF0, 0xF7}:
            length, position = variable_length(data, position)
            position += length
            if position > len(data):
                raise ValueError(f"track {track}: truncated system-exclusive event")
            running = None
            order += 1
            continue
        if status >= 0xF0:
            raise ValueError(f"track {track}: unsupported system event 0x{status:02x}")

        family = status >> 4
        channel = status & 0x0F
        width = 1 if family in {0xC, 0xD} else 2
        payload = data[position : position + width]
        if len(payload) != width:
            raise ValueError(f"track {track}: truncated channel event")
        position += width
        if any(byte & 0x80 for byte in payload):
            raise ValueError(f"track {track}: invalid channel data byte")
        if family == 0x8 or (family == 0x9 and payload[1] == 0):
            events.append(Event(tick, track, order, "note_off", (channel, payload[0], payload[1])))
        elif family == 0x9:
            events.append(Event(tick, track, order, "note_on", (channel, payload[0], payload[1])))
        elif family == 0xB:
            events.append(Event(tick, track, order, "control", (channel, payload[0], payload[1])))
        elif family == 0xC:
            events.append(Event(tick, track, order, "program", (channel, payload[0])))
        order += 1
    return events, end_tick


def parse_midi(path: Path) -> tuple[int, list[Event], int]:
    data = path.read_bytes()
    if data[:4] != b"MThd" or len(data) < 14:
        raise ValueError("not a Standard MIDI File")
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length != 6:
        raise ValueError(f"unsupported MIDI header length {header_length}")
    midi_format, track_count, division = struct.unpack(">HHH", data[8:14])
    if midi_format not in {0, 1}:
        raise ValueError(f"unsupported MIDI format {midi_format}")
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported")
    position = 14
    events: list[Event] = []
    duration_tick = 0
    for track in range(track_count):
        if data[position : position + 4] != b"MTrk" or position + 8 > len(data):
            raise ValueError(f"missing MIDI track {track}")
        length = struct.unpack(">I", data[position + 4 : position + 8])[0]
        position += 8
        payload = data[position : position + length]
        if len(payload) != length:
            raise ValueError(f"truncated MIDI track {track}")
        position += length
        parsed, end_tick = parse_track(payload, track)
        events.extend(parsed)
        duration_tick = max(duration_tick, end_tick)
    if position != len(data):
        raise ValueError("bytes follow the declared MIDI tracks")
    return division, sorted(events, key=lambda event: (event.tick, event.track, event.order)), duration_tick


def rounded(value: Fraction | float, places: int = 9) -> float:
    return round(float(value), places)


class Timeline:
    def __init__(self, division: int, events: list[Event]):
        raw = [(event.tick, int(event.data[0])) for event in events if event.kind == "tempo"]
        if not raw or raw[0][0] != 0:
            raw.insert(0, (0, 500_000))
        by_tick: dict[int, int] = {}
        for tick, value in raw:
            if value <= 0:
                raise ValueError("tempo must be positive")
            by_tick[tick] = value
        self.division = division
        self.ticks = sorted(by_tick)
        self.tempos = [by_tick[tick] for tick in self.ticks]
        self.starts: list[Fraction] = [Fraction(0)]
        for index in range(1, len(self.ticks)):
            delta = self.ticks[index] - self.ticks[index - 1]
            self.starts.append(self.starts[-1] + Fraction(delta * self.tempos[index - 1], division * 1_000_000))

    def seconds(self, tick: int | Fraction) -> Fraction:
        index = bisect.bisect_right(self.ticks, tick) - 1
        delta = Fraction(tick) - self.ticks[index]
        return self.starts[index] + delta * Fraction(self.tempos[index], self.division * 1_000_000)

    def rows(self) -> list[dict[str, Any]]:
        return [
            {
                "tick": tick,
                "quarter": rounded(Fraction(tick, self.division)),
                "second": rounded(self.starts[index]),
                "microseconds_per_quarter": self.tempos[index],
                "bpm": rounded(Fraction(60_000_000, self.tempos[index]), 3),
            }
            for index, tick in enumerate(self.ticks)
        ]


def track_names(events: list[Event]) -> dict[int, str]:
    names: dict[int, str] = {}
    for event in events:
        if event.kind == "track_name" and event.track not in names:
            names[event.track] = str(event.data[0])
    return names


def marker_rows(
    events: list[Event],
    prefix: str,
    duration_tick: int,
    division: int,
    timeline: Timeline,
) -> list[dict[str, Any]]:
    found = [(event.tick, str(event.data[0])[len(prefix) :]) for event in events if event.kind == "marker" and str(event.data[0]).startswith(prefix)]
    if not found or found[0][0] != 0:
        raise ValueError(f"MIDI must declare {prefix[:-1]} at tick zero")
    if len({tick for tick, _ in found}) != len(found):
        raise ValueError(f"MIDI declares multiple {prefix[:-1]} markers at one tick")
    rows = []
    for index, (tick, name) in enumerate(found):
        end_tick = found[index + 1][0] if index + 1 < len(found) else duration_tick
        if not name or end_tick <= tick:
            raise ValueError(f"invalid {prefix[:-1]} marker {name!r} at tick {tick}")
        rows.append(
            {
                "index": index,
                "id": name,
                "start_tick": tick,
                "end_tick": end_tick,
                "start_quarter": rounded(Fraction(tick, division)),
                "end_quarter": rounded(Fraction(end_tick, division)),
                "start_second": rounded(timeline.seconds(tick)),
                "end_second": rounded(timeline.seconds(end_tick)),
            }
        )
    return rows


def meter_rows(events: list[Event], duration_tick: int, division: int, timeline: Timeline) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = [(event.tick, *map(int, event.data)) for event in events if event.kind == "meter"]
    if not raw or raw[0][0] != 0:
        raw.insert(0, (0, 4, 2, 24, 8))
    by_tick: dict[int, tuple[int, int, int, int]] = {}
    for tick, numerator, exponent, clocks, notes in raw:
        denominator = 2**exponent
        if numerator <= 0 or denominator > 64:
            raise ValueError("invalid MIDI time signature")
        by_tick[tick] = (numerator, denominator, clocks, notes)
    ticks = sorted(by_tick)
    meters = []
    beats = []
    beat_index = 0
    bar = 1
    for index, tick in enumerate(ticks):
        numerator, denominator, clocks, notes = by_tick[tick]
        end_tick = ticks[index + 1] if index + 1 < len(ticks) else duration_tick
        beat_ticks = Fraction(division * 4, denominator)
        span = Fraction(end_tick - tick, 1)
        count = math.ceil(span / beat_ticks)
        meters.append(
            {
                "tick": tick,
                "quarter": rounded(Fraction(tick, division)),
                "second": rounded(timeline.seconds(tick)),
                "numerator": numerator,
                "denominator": denominator,
                "clocks_per_click": clocks,
                "thirty_seconds_per_quarter": notes,
            }
        )
        for local in range(count):
            at = Fraction(tick, 1) + local * beat_ticks
            if at >= end_tick:
                break
            beat_in_bar = local % numerator
            if local and beat_in_bar == 0:
                bar += 1
            beats.append(
                {
                    "index": beat_index,
                    "tick": int(at) if at.denominator == 1 else rounded(at, 3),
                    "quarter": rounded(at / division),
                    "second": rounded(timeline.seconds(at)),
                    "bar": bar,
                    "beat_in_bar": beat_in_bar + 1,
                    "downbeat": beat_in_bar == 0,
                }
            )
            beat_index += 1
        # `bar` names the last bar emitted in this meter section. A following
        # meter change always begins a new bar, whether this section ended on a
        # complete bar or interrupted one part-way through.
        if count:
            bar += 1
    return meters, beats


def cue_rows(
    events: list[Event],
    bindings: dict[str, Any],
    duration_tick: int,
    division: int,
    timeline: Timeline,
) -> list[dict[str, Any]]:
    cues = []
    pattern = re.compile(r"^cue:([^:]+):(cue|accent):(\d{1,3})$")
    found_ids: set[str] = set()
    recast_count = 0
    for event in events:
        if event.kind != "marker":
            continue
        match = pattern.fullmatch(str(event.data[0]))
        if not match:
            continue
        cue_id, kind, raw_strength = match.groups()
        if cue_id in found_ids:
            raise ValueError(f"duplicate cue id {cue_id}")
        found_ids.add(cue_id)
        strength = int(raw_strength)
        if strength > 127:
            raise ValueError(f"cue {cue_id} strength exceeds MIDI range")
        binding = bindings.get(cue_id, {})
        window_beats = binding.get("window_beats", 0.5)
        window_ticks = Fraction(str(window_beats)) * division
        if window_ticks.denominator != 1 or window_ticks <= 0:
            raise ValueError(f"cue {cue_id} window must resolve to a positive whole MIDI tick")
        visual = binding.get("visual", {})
        if visual.get("recast"):
            recast_count += 1
        end_tick = event.tick + int(window_ticks)
        if end_tick > duration_tick:
            raise ValueError(
                f"cue {cue_id} window ends at tick {end_tick}, beyond MIDI duration {duration_tick}"
            )
        cues.append(
            {
                "index": len(cues),
                "id": cue_id,
                "kind": kind,
                "tick": event.tick,
                "quarter": rounded(Fraction(event.tick, division)),
                "second": rounded(timeline.seconds(event.tick)),
                "end_tick": end_tick,
                "end_second": rounded(timeline.seconds(end_tick)),
                "strength": rounded(Fraction(strength, 127)),
                "visual": {
                    "hold": bool(visual.get("hold", False)),
                    "recast": bool(visual.get("recast", False)),
                    "recast_index": recast_count,
                    "channel_offsets": {
                        key: float(value) for key, value in sorted((visual.get("channel_offsets") or {}).items())
                    },
                },
            }
        )
    unknown = sorted(set(bindings) - found_ids)
    if unknown:
        raise ValueError(f"repertoire binds cue(s) absent from MIDI: {', '.join(unknown)}")
    return cues


def dynamics_rows(
    events: list[Event],
    division: int,
    timeline: Timeline,
    declared_source: dict[str, Any],
) -> list[dict[str, Any]]:
    track = declared_source.get("track")
    channel = declared_source.get("channel")
    if type(track) is not int or track < 0 or type(channel) is not int or not 0 <= channel <= 15:
        raise ValueError("score dynamics_source must declare a non-negative track and MIDI channel 0..15")
    expression = [
        event
        for event in events
        if event.kind == "control"
        and event.track == track
        and int(event.data[0]) == channel
        and int(event.data[1]) == 11
    ]
    if not expression:
        raise ValueError(f"MIDI has no CC11 expression on declared dynamics source track {track}, channel {channel}")
    by_tick: dict[int, int] = {}
    for event in expression:
        # Multiple messages at one tick on the one declared source retain MIDI
        # event order: the last authored message is the state at that tick.
        by_tick[event.tick] = int(event.data[2])
    if 0 not in by_tick:
        by_tick[0] = 127
    return [
        {
            "index": index,
            "tick": tick,
            "quarter": rounded(Fraction(tick, division)),
            "second": rounded(timeline.seconds(tick)),
            "track": track,
            "channel": channel,
            "midi_expression": value,
            "level": rounded(Fraction(value, 127)),
        }
        for index, (tick, value) in enumerate(sorted(by_tick.items()))
    ]


def note_and_orchestration_rows(
    events: list[Event],
    names: dict[int, str],
    duration_tick: int,
    division: int,
    timeline: Timeline,
    midi_sha256: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    programs: dict[int, int] = {}
    pedal: dict[int, bool] = {}
    active: dict[tuple[int, int, int], list[tuple[Event, int]]] = {}
    sustained: dict[int, list[tuple[Event, int]]] = {}
    notes: list[dict[str, Any]] = []
    stems: dict[tuple[int, int, int], str] = {}

    def close_note(start: Event, program: int, end_tick: int) -> None:
        channel, pitch, _velocity = map(int, start.data)
        if end_tick <= start.tick:
            raise ValueError("MIDI note has non-positive duration")
        stem_key = (start.track, channel, program)
        base = re.sub(r"[^a-z0-9]+", "-", names.get(start.track, f"track-{start.track}").lower()).strip("-")
        stem = stems.setdefault(stem_key, f"{base or 'track'}-ch{channel + 1}-p{program}")
        notes.append(
            {
                "index": len(notes),
                "start_tick": start.tick,
                "end_tick": end_tick,
                "start_quarter": rounded(Fraction(start.tick, division)),
                "end_quarter": rounded(Fraction(end_tick, division)),
                "start_second": rounded(timeline.seconds(start.tick)),
                "end_second": rounded(timeline.seconds(end_tick)),
                "track": start.track,
                "source_order": start.order,
                "pitch": pitch,
                "velocity": int(start.data[2]),
                "channel": channel,
                "program": program,
                "stem": stem,
            }
        )

    for event in events:
        if event.kind == "program":
            programs[int(event.data[0])] = int(event.data[1])
        elif event.kind == "control" and int(event.data[1]) == 64:
            channel = int(event.data[0])
            was_down = pedal.get(channel, False)
            is_down = int(event.data[2]) >= 64
            pedal[channel] = is_down
            if was_down and not is_down:
                for start, program in sustained.pop(channel, []):
                    close_note(start, program, event.tick)
        elif event.kind == "note_on":
            channel, pitch, velocity = map(int, event.data)
            program = programs.get(channel, 0)
            active.setdefault((event.track, channel, pitch), []).append((event, program))
        elif event.kind == "note_off":
            channel, pitch, _velocity = map(int, event.data)
            key = (event.track, channel, pitch)
            if not active.get(key):
                raise ValueError(f"note-off without note-on at tick {event.tick}, track {event.track}, pitch {pitch}")
            start, program = active[key].pop(0)
            if pedal.get(channel, False):
                sustained.setdefault(channel, []).append((start, program))
            else:
                close_note(start, program, event.tick)
    unfinished = [key for key, stack in active.items() if stack]
    if unfinished:
        raise ValueError(f"unterminated MIDI note(s): {unfinished[:3]}")
    for pending in sustained.values():
        for start, program in pending:
            close_note(start, program, duration_tick)
    # At an identical tick the authored note-on order is semantic. Track order
    # follows the Standard MIDI File, then each track's original event order.
    notes.sort(key=lambda row: (row["start_tick"], row["track"], row["source_order"]))
    for index, note in enumerate(notes):
        note["index"] = index
    orchestration = [
        {
            "index": index,
            "id": stem,
            "track": track,
            "track_name": names.get(track, f"track-{track}"),
            "channel": channel,
            "program": program,
            "midi_source_sha256": midi_sha256,
            "audio_source_sha256": None,
            "sample_status": "none",
        }
        for index, ((track, channel, program), stem) in enumerate(sorted(stems.items(), key=lambda item: item[1]))
    ]
    return notes, orchestration


def latest_index(rows: list[dict[str, Any]], second: float, key: str = "second") -> int:
    values = [float(row[key]) for row in rows]
    return max(0, bisect.bisect_right(values, second) - 1)


def lookup_rows(
    duration_seconds: float,
    tempo: list[dict[str, Any]],
    meter: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    phrases: list[dict[str, Any]],
    cues: list[dict[str, Any]],
    dynamics: list[dict[str, Any]],
    movements: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    buckets = []
    note_seconds = [float(note["start_second"]) for note in notes]
    for bucket in range(math.ceil(duration_seconds)):
        start = float(bucket)
        end = min(duration_seconds, start + 1.0)
        note_start = bisect.bisect_left(note_seconds, start)
        note_end = bisect.bisect_left(note_seconds, end)
        active_cues = [
            cue["index"]
            for cue in cues
            if float(cue["second"]) < end and float(cue["end_second"]) > start
        ]
        recast = max(
            (
                int(cue["visual"]["recast_index"])
                for cue in cues
                if cue["visual"]["recast"] and float(cue["second"]) <= start
            ),
            default=0,
        )
        buckets.append(
            {
                "tempo": latest_index(tempo, start),
                "meter": latest_index(meter, start),
                "beat": latest_index(beats, start),
                "phrase": latest_index(phrases, start, "start_second"),
                "dynamic": latest_index(dynamics, start),
                "movement": latest_index(movements, start, "start_second"),
                "active_cues": active_cues,
                "recast": recast,
                "note_start": [note_start, note_end],
            }
        )
    maxima = {
        "active_cues_per_bucket": max((len(bucket["active_cues"]) for bucket in buckets), default=0),
        "note_starts_per_bucket": max((bucket["note_start"][1] - bucket["note_start"][0] for bucket in buckets), default=0),
    }
    return {"quantum_seconds": 1, "buckets": buckets, "maxima": maxima}


def score_bound_recording(recording: dict[str, Any]) -> dict[str, Any]:
    """Remove the output-custody edge from the upstream score identity.

    A project-authored recording is derived from this score. Once its tracked
    custody receipt is added to the repertoire register, hashing that receipt
    back into the score would create an impossible score -> render -> receipt ->
    score cycle. Preserve the pre-render declaration for score compilation;
    repertoire validation still authenticates the final recording separately.
    """
    source = recording.get("source")
    if (
        recording.get("status") == "project-authored"
        and isinstance(source, dict)
        and source.get("custody") == "hydrated-derived"
    ):
        return {**recording, "status": "pending-render", "source": None}
    return recording


def score_bound_work(work: dict[str, Any]) -> dict[str, Any]:
    entry = {key: value for key, value in work.items() if key != "derived_artifacts"}
    entry["recording"] = score_bound_recording(work["recording"])
    return entry


def provenance_layers(work: dict[str, Any]) -> dict[str, Any]:
    layers = {}
    for name in ("composition", "edition", "arrangement_midi", "performance", "recording"):
        row = score_bound_recording(work[name]) if name == "recording" else work[name]
        layers[name] = {"status": row["status"], "source": row.get("source")}
    layers["sample"] = {
        "status": work["samples"]["status"],
        "sources": [item["source"] for item in work["samples"]["items"]],
    }
    return layers


def compile_contract(register: dict[str, Any], program: dict[str, Any], work_id: str) -> dict[str, Any]:
    errors = validate_document(register, check_derived=False)
    if errors:
        raise ValueError("invalid repertoire register:\n" + "\n".join(errors))
    matches = [work for work in register["works"] if work["id"] == work_id]
    if len(matches) != 1:
        raise ValueError(f"register must contain exactly one work {work_id!r}")
    work = matches[0]
    midi = ROOT / work["score"]["source_midi"]["path"]
    midi_digest = sha256(midi)
    declared_midi_digest = work["score"]["source_midi"]["sha256"]
    if midi_digest != declared_midi_digest:
        raise ValueError(
            f"score.source_midi.sha256 {declared_midi_digest} does not match actual {midi_digest}"
        )
    division, events, duration_tick = parse_midi(midi)
    timeline = Timeline(division, events)
    duration_seconds = rounded(timeline.seconds(duration_tick))
    names = track_names(events)
    tempo = timeline.rows()
    meter, beats = meter_rows(events, duration_tick, division, timeline)
    phrases = marker_rows(events, "phrase:", duration_tick, division, timeline)
    movements = marker_rows(events, "movement:", duration_tick, division, timeline)
    cues = cue_rows(events, work["score"]["cue_bindings"], duration_tick, division, timeline)
    dynamics_source = work["score"]["dynamics_source"]
    dynamics = dynamics_rows(events, division, timeline, dynamics_source)
    notes, orchestration = note_and_orchestration_rows(
        events,
        names,
        duration_tick,
        division,
        timeline,
        midi_digest,
    )

    # The generated fixture deliberately mirrors the seven dramatic program
    # movements and keeps their old affine mapping for regression tests. A real
    # selected score owns its musical movement boundaries and native duration;
    # choreography binds score phrases to dramatic movements separately.
    if work["role"] == "fixture":
        program_ids = [movement["id"] for movement in program.get("movements", [])]
        score_ids = [movement["id"] for movement in movements]
        if score_ids != program_ids:
            raise ValueError(f"MIDI movement markers {score_ids} do not match program order {program_ids}")
        shares = [float(movement["share"]) for movement in program["movements"]]
        total_share = sum(shares)
        cursor = 0.0
        for index, movement in enumerate(movements):
            expected_start = duration_seconds * cursor / total_share
            cursor += shares[index]
            expected_end = duration_seconds * cursor / total_share
            if abs(movement["start_second"] - expected_start) > 1e-6 or abs(movement["end_second"] - expected_end) > 1e-6:
                raise ValueError(
                    f"movement {movement['id']} MIDI boundary {movement['start_second']}..{movement['end_second']} "
                    f"does not match program share {expected_start}..{expected_end}"
                )

    entry_for_identity = score_bound_work(work)
    release_status = "fixture-only" if work["role"] == "fixture" else (
        "production-selected"
        if register["artistic_gate"]["status"] == "accepted" and work["selection"]["status"] == "selected"
        else "artistic-gate-required"
    )
    score: dict[str, Any] = {
        "schema": SCHEMA,
        "compiler": COMPILER,
        "release_status": release_status,
        "artistic_gate": {
            "status": register["artistic_gate"]["status"],
            "authority": register["artistic_gate"]["authority"],
            "evidence": register["artistic_gate"].get("evidence"),
        },
        "identity": {
            "work_id": work_id,
            "repertoire_entry_sha256": canonical_sha256(entry_for_identity),
            "midi_sha256": midi_digest,
        },
        "time": {
            "basis": "absolute-seconds",
            "ticks_per_quarter": division,
            "duration_ticks": duration_tick,
            "duration_quarters": rounded(Fraction(duration_tick, division)),
            "duration_seconds": duration_seconds,
            "passage_mapping": "restart-and-affine-stretch" if work["role"] == "fixture" else "native-tempo",
        },
        "provenance": {"layers": provenance_layers(work)},
        "tempo": tempo,
        "meter": meter,
        "beats": beats,
        "phrases": phrases,
        "cues": cues,
        "dynamics_source": {
            "track": int(dynamics_source["track"]),
            "channel": int(dynamics_source["channel"]),
        },
        "dynamics": dynamics,
        "orchestration": orchestration,
        "notes": notes,
        "movements": movements,
    }
    score["lookup"] = lookup_rows(
        duration_seconds,
        tempo,
        meter,
        beats,
        phrases,
        cues,
        dynamics,
        movements,
        notes,
    )
    score["identity"]["contract_sha256"] = canonical_sha256(score)
    return score


def output_bytes(score: dict[str, Any]) -> bytes:
    return (json.dumps(score, indent=2, ensure_ascii=False) + "\n").encode()


def display_path(path: Path) -> str:
    """Prefer a repository-relative diagnostic without rejecting external paths."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(resolved)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", type=Path, default=DEFAULT_REGISTER)
    parser.add_argument("--program", type=Path, default=DEFAULT_PROGRAM)
    parser.add_argument("--work", default="delibes-screendance-suite")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="require tracked score.json to be byte-identical")
    args = parser.parse_args()
    try:
        score = compile_contract(load_register(args.register), json.loads(args.program.read_text()), args.work)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    expected = output_bytes(score)
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != expected:
            parser.error(f"{args.out} is absent or stale; compile it without --check")
        print(f"ok: {display_path(args.out)} {score['identity']['contract_sha256']}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(expected)
    print(f"{args.out} {score['identity']['contract_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
