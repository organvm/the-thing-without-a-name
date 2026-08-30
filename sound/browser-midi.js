/**
 * Small, dependency-free score player for the live Danse site.
 *
 * It deliberately reads the compiled, hash-bound score notes rather than an
 * undocumented browser instrument or a second timing source. Web Audio needs
 * a visitor gesture, so this module is inert until start() is called.
 */

const HORIZON_SECONDS = 4;
const RESYNC_SECONDS = 0.15;

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const midiHz = (pitch) => 440 * 2 ** ((pitch - 69) / 12);

function voiceFor(stem) {
  if (stem === "contrabass" || stem === "cello") return "sine";
  if (stem === "viola") return "triangle";
  return "sawtooth";
}

/** A seekable, score-native chamber reduction for the live visual. */
export class ScoreAudio {
  constructor(score) {
    if (!Array.isArray(score?.notes) || !(score.time?.duration_seconds > 0)) {
      throw new TypeError("score audio requires compiled score notes and duration");
    }
    this.score = score;
    this.context = null;
    this.gain = null;
    this.playing = false;
    this.startedAt = 0;
    this.sourceAt = 0;
    this.scheduledThrough = 0;
    this.nodes = new Set();
  }

  async start(sourceSecond = 0) {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) throw new Error("Web Audio is not available in this browser");
    if (!this.context) {
      this.context = new Audio();
      this.gain = this.context.createGain();
      this.gain.gain.value = 0.16;
      this.gain.connect(this.context.destination);
    }
    await this.context.resume();
    this.playing = true;
    try {
      this.seek(sourceSecond, true);
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  stop() {
    this.playing = false;
    this.cancel();
  }

  cancel() {
    for (const node of this.nodes) {
      try { node.stop(); } catch { /* already finished */ }
    }
    this.nodes.clear();
  }

  seek(sourceSecond = 0, force = false) {
    if (!this.playing || !this.context) return;
    const duration = this.score.time.duration_seconds;
    const source = ((sourceSecond % duration) + duration) % duration;
    if (!force && Math.abs(source - this.sourceAt - (this.context.currentTime - this.startedAt)) < RESYNC_SECONDS) return;
    this.cancel();
    this.sourceAt = source;
    this.startedAt = this.context.currentTime;
    this.scheduledThrough = source;
    this.schedule();
  }

  sync(sourceSecond, paused) {
    if (!this.playing || !this.context) return;
    if (paused) {
      this.cancel();
      return;
    }
    this.seek(sourceSecond);
    this.schedule();
  }

  schedule() {
    if (!this.playing || !this.context || !this.gain) return;
    const nowSource = this.sourceAt + (this.context.currentTime - this.startedAt);
    const end = nowSource + HORIZON_SECONDS;
    if (end <= this.scheduledThrough + 0.05) return;
    const duration = this.score.time.duration_seconds;
    for (const note of this.score.notes) {
      const start = note.start_second;
      const stop = note.end_second;
      for (let cycle = Math.floor(nowSource / duration); cycle <= Math.floor(end / duration); cycle++) {
        const noteStart = cycle * duration + start;
        const noteEnd = cycle * duration + stop;
        if (noteEnd <= this.scheduledThrough || noteStart >= end) continue;
        this.scheduleNote(note, noteStart, noteEnd);
      }
    }
    this.scheduledThrough = end;
  }

  scheduleNote(note, sourceStart, sourceEnd) {
    const audioStart = this.startedAt + sourceStart - this.sourceAt;
    const audioEnd = this.startedAt + sourceEnd - this.sourceAt;
    if (audioEnd <= this.context.currentTime) return;
    const oscillator = this.context.createOscillator();
    const envelope = this.context.createGain();
    const level = clamp((note.velocity ?? 64) / 127, 0.08, 0.72) * 0.045;
    const start = Math.max(audioStart, this.context.currentTime);
    const end = Math.max(start + 0.012, audioEnd);
    oscillator.type = voiceFor(note.stem);
    oscillator.frequency.value = midiHz(note.pitch);
    envelope.gain.setValueAtTime(0.0001, start);
    envelope.gain.exponentialRampToValueAtTime(level, Math.min(end, start + 0.025));
    envelope.gain.setValueAtTime(level, Math.max(start + 0.025, end - 0.04));
    envelope.gain.exponentialRampToValueAtTime(0.0001, end);
    oscillator.connect(envelope).connect(this.gain);
    oscillator.addEventListener("ended", () => this.nodes.delete(oscillator), { once: true });
    this.nodes.add(oscillator);
    oscillator.start(start);
    oscillator.stop(end + 0.01);
  }
}
