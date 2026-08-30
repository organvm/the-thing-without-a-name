#!/usr/bin/env node

import assert from "node:assert/strict";
import { createControlActions } from "../../interface/actions.js";
import { renderControlSurface } from "../../interface/render.js";
import { ScoreAudio } from "../../sound/browser-midi.js";
import {
  ACTIONS,
  initialControlState,
  reduceControlState,
  sharePresentationState,
  sharePresentationUrl,
  shortcutAction,
} from "../../interface/state.js";

let passed = 0;
async function test(name, run) {
  await run();
  passed += 1;
  process.stdout.write(`  ok ${passed} - ${name}\n`);
}

await test("state contracts cover hold, program, cutout, music, surfaces, audition, and presence", () => {
  let state = initialControlState({ reducedMotion: true, scoreAvailable: false });
  assert.equal(state.playback, "held-reduced");
  assert.equal(state.music, "unavailable");
  assert.strictEqual(reduceControlState(state, { type: ACTIONS.TOGGLE_HOLD }), state);
  state = reduceControlState(state, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true });
  assert.equal(state.playback, "running");
  state = reduceControlState(state, { type: ACTIONS.OPEN_TRAY, category: "score" });
  assert.equal(state.surface, "tray:score");
  state = reduceControlState(state, { type: ACTIONS.OPEN_SHEET, section: "score-audition" });
  assert.equal(state.surface, "sheet:score-audition");
  state = reduceControlState(state, { type: ACTIONS.SET_PROGRAM, value: "free" });
  state = reduceControlState(state, { type: ACTIONS.TOGGLE_CUTOUT });
  state = reduceControlState(state, { type: ACTIONS.SET_PRESENCE, value: "keyboard-touch" });
  state = reduceControlState(state, { type: ACTIONS.SET_CONDUCTOR, active: true });
  assert.deepEqual([state.program, state.cutout, state.presence, state.audition], ["free", "on", "keyboard-touch", "override-active"]);
});

await test("river and reduced-motion transitions keep playback and music synchronized", () => {
  let state = initialControlState();
  state = reduceControlState(state, { type: ACTIONS.SET_MUSIC, value: "playing" });
  state = reduceControlState(state, { type: ACTIONS.SET_MOVEMENT, value: 4 });
  state = reduceControlState(state, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true });
  assert.deepEqual([state.playback, state.music, state.movement], ["held-user", "suspended-by-hold", 4]);
  state = reduceControlState(state, { type: ACTIONS.RESET_RIVER, reducedMotion: false });
  assert.deepEqual([state.playback, state.music, state.movement], ["running", "playing", 1]);
  state = reduceControlState(state, { type: ACTIONS.SET_REDUCED_MOTION, value: true });
  assert.deepEqual([state.playback, state.music], ["held-reduced", "suspended-by-hold"]);
  state = reduceControlState(state, { type: ACTIONS.SET_REDUCED_MOTION, value: false });
  assert.deepEqual([state.playback, state.music], ["running", "playing"]);
});

await test("reduced-motion changes preserve a manual hold and its suspended music", () => {
  let state = initialControlState();
  state = reduceControlState(state, { type: ACTIONS.SET_MUSIC, value: "playing" });
  state = reduceControlState(state, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true });
  assert.deepEqual([state.playback, state.music], ["held-user", "suspended-by-hold"]);
  state = reduceControlState(state, { type: ACTIONS.SET_REDUCED_MOTION, value: true });
  assert.deepEqual([state.playback, state.music], ["held-user", "suspended-by-hold"]);
  state = reduceControlState(state, { type: ACTIONS.SET_REDUCED_MOTION, value: false });
  assert.deepEqual([state.playback, state.music], ["held-user", "suspended-by-hold"]);
});

await test("a new Presence transition clears an obsolete receipt error", () => {
  let state = initialControlState();
  state = reduceControlState(state, { type: ACTIONS.SET_STATUS, category: "presence", message: "Receipt rejected" });
  state = reduceControlState(state, { type: ACTIONS.SET_PRESENCE, value: "camera" });
  assert.equal(state.presence, "camera");
  assert.equal(state.status.presence, "");
});

await test("an invalid Presence transition is an identity-preserving no-op", () => {
  let state = initialControlState();
  state = reduceControlState(state, { type: ACTIONS.SET_STATUS, category: "presence", message: "Receipt rejected" });
  const before = state;
  state = reduceControlState(state, { type: ACTIONS.SET_PRESENCE, value: "invalid" });
  assert.strictEqual(state, before);
  assert.deepEqual([state.presence, state.status.presence], ["off", "Receipt rejected"]);
});

await test("an invalid Presence action does not clear status or reach the adapter", async () => {
  let calls = 0;
  const bus = createControlActions({ presence: () => { calls += 1; } });
  bus.actions.status("presence", "Receipt rejected");
  const before = bus.getState();
  assert.equal(await bus.actions.presence("invalid"), "off");
  assert.strictEqual(bus.getState(), before);
  assert.equal(calls, 0);
});

await test("the primary Music action mirrors unavailable and available state", () => {
  const music = { disabled: false, textContent: "" };
  const doc = {
    getElementById: (id) => id === "music-tray" ? music : null,
    querySelectorAll: () => [],
  };
  const root = { ownerDocument: doc, dataset: {}, querySelector: () => null };
  renderControlSurface(root, initialControlState({ scoreAvailable: false }));
  assert.deepEqual([music.disabled, music.textContent], [true, "Music: unavailable"]);
  renderControlSurface(root, initialControlState({ scoreAvailable: true }));
  assert.deepEqual([music.disabled, music.textContent], [false, "Music: stopped"]);
});

await test("keyboard compatibility yields named actions and respects editable/native controls", () => {
  const event = (key, tagName = "DIV", extra = {}) => ({ key, target: { tagName, isContentEditable: false }, ...extra });
  assert.deepEqual(shortcutAction(event(" ")), { name: "hold" });
  assert.deepEqual(shortcutAction(event("7")), { name: "movement", value: 7 });
  assert.deepEqual(shortcutAction(event("m")), { name: "toggleCutout" });
  assert.deepEqual(shortcutAction(event("Escape", "INPUT")), { name: "close" });
  for (const tag of ["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA"]) {
    for (const key of [" ", "n", "s", "f", "m", "1", "7"]) assert.equal(shortcutAction(event(key, tag)), null);
    assert.deepEqual(shortcutAction(event("h", tag)), { name: "toggleControls" });
    assert.deepEqual(shortcutAction(event("Escape", tag)), { name: "close" });
  }
  assert.equal(shortcutAction({ ...event("n"), target: { tagName: "DIV", isContentEditable: true } }), null);
  assert.deepEqual(shortcutAction({ ...event("h"), target: { tagName: "DIV", isContentEditable: true } }), { name: "toggleControls" });
  assert.equal(shortcutAction(event("8")), null);
});

await test("rapid program toggles derive from pending intent and settle on the latest choice", async () => {
  const calls = [];
  let finishScore;
  const bus = createControlActions({
    setProgram: (value) => {
      calls.push(value);
      if (value === "free") return true;
      return new Promise((resolve) => { finishScore = resolve; });
    },
  });
  bus.actions.sync({ type: ACTIONS.SET_PROGRAM, value: "free" });
  const score = bus.actions.toggleProgram();
  const free = bus.actions.toggleProgram();
  await free;
  finishScore(true);
  await score;
  assert.deepEqual(calls, ["score-led", "free"]);
  assert.equal(bus.getState().program, "free");
});

await test("stale program failures cannot report over a newer choice", async () => {
  const reports = [];
  let rejectScore;
  const bus = createControlActions({
    setProgram: (value) => value === "score-led"
      ? new Promise((resolve, reject) => { rejectScore = reject; })
      : true,
    reportError: (name, error) => reports.push([name, error.message]),
  });
  bus.actions.sync({ type: ACTIONS.SET_PROGRAM, value: "free" });
  const score = bus.actions.setProgram("score-led");
  await bus.actions.setProgram("free");
  rejectScore(new Error("stale score failure"));
  await score;
  assert.equal(bus.getState().program, "free");
  assert.deepEqual(reports, []);
});

await test("rapid music toggles serialize pending intent and settle stopped", async () => {
  const calls = [];
  let finishStart;
  const bus = createControlActions({
    music: (intent) => {
      calls.push(intent);
      if (intent === "stopped") return "stopped";
      return new Promise((resolve) => { finishStart = resolve; });
    },
  });
  const start = bus.actions.music();
  const stop = bus.actions.music();
  await Promise.resolve();
  assert.deepEqual(calls, ["playing"]);
  finishStart("playing");
  await Promise.all([start, stop]);
  assert.deepEqual(calls, ["playing", "stopped"]);
  assert.equal(bus.getState().music, "stopped");
});

await test("music startup fails closed when Web Audio scheduling throws", async () => {
  const originalWindow = globalThis.window;
  class FailingAudioContext {
    constructor() {
      this.currentTime = 0;
      this.destination = {};
    }
    createGain() {
      return { gain: { value: 0 }, connect() {} };
    }
    async resume() {}
    createOscillator() {
      throw new Error("fixture scheduling failure");
    }
  }
  globalThis.window = { AudioContext: FailingAudioContext };
  try {
    const audio = new ScoreAudio({
      time: { duration_seconds: 4 },
      notes: [{ start_second: 0, end_second: 1, stem: "violin-1", pitch: 60, velocity: 64 }],
    });
    await assert.rejects(audio.start(0), /fixture scheduling failure/);
    assert.equal(audio.playing, false);
    assert.equal(audio.nodes.size, 0);
  } finally {
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  }
});

await test("stale Presence completions cannot replace newer modes or status", async () => {
  const cameraSettlements = [];
  const reports = [];
  const bus = createControlActions({
    presence: (value) => value === "camera"
      ? new Promise((resolve, reject) => { cameraSettlements.push({ resolve, reject }); })
      : value,
    reportError: (name, error) => reports.push([name, error.message]),
  });
  const camera = bus.actions.presence("camera");
  await bus.actions.presence("keyboard-touch");
  bus.actions.status("presence", "Keyboard-touch remains selected.");
  cameraSettlements.shift().resolve("off");
  await camera;
  assert.deepEqual([bus.getState().presence, bus.getState().status.presence], ["keyboard-touch", "Keyboard-touch remains selected."]);

  const retriedCamera = bus.actions.presence("camera");
  bus.actions.sync({ type: ACTIONS.SET_PRESENCE, value: "replay" });
  bus.actions.status("presence", "Replay receipt loaded.");
  cameraSettlements.shift().reject(new Error("stale camera failure"));
  await retriedCamera;
  assert.deepEqual([bus.getState().presence, bus.getState().status.presence], ["replay", "Replay receipt loaded."]);
  assert.deepEqual(reports, []);

  const latestCamera = bus.actions.presence("camera");
  cameraSettlements.shift().reject(new Error("latest camera failure"));
  await latestCamera;
  assert.deepEqual(reports, [["presence", "latest camera failure"]]);
});

await test("Presence status and authoritative sync participate in intent ordering", async () => {
  const cameraResolvers = [];
  const bus = createControlActions({
    presence: (value) => value === "camera"
      ? new Promise((resolve) => { cameraResolvers.push(resolve); })
      : value,
  });
  const echoedCamera = bus.actions.presence("camera");
  bus.actions.sync({ type: ACTIONS.SET_PRESENCE, value: "camera" });
  cameraResolvers.shift()("off");
  await echoedCamera;
  assert.equal(bus.getState().presence, "off");

  const statusGuardedCamera = bus.actions.presence("camera");
  bus.actions.status("presence", "A newer Presence receipt is authoritative.");
  cameraResolvers.shift()("camera");
  await statusGuardedCamera;
  assert.deepEqual([bus.getState().presence, bus.getState().status.presence], ["off", "A newer Presence receipt is authoritative."]);
});

await test("button and shortcut callers share one action bus", async () => {
  const calls = [];
  const adapter = {
    hold: () => calls.push("hold"),
    newRiver: () => calls.push("new-river"),
    undoRiver: () => calls.push("undo-river"),
    share: () => calls.push("share"),
    movement: (value) => { calls.push(`movement:${value}`); return true; },
    setProgram: (value) => calls.push(`program:${value}`),
    toggleCutout: () => calls.push("cutout"),
    music: () => "playing",
    conductor: (value) => value,
    presence: (value) => value,
  };
  const bus = createControlActions(adapter);
  await bus.actions.hold();
  await bus.actions.movement(4);
  await bus.actions.setProgram("free");
  await bus.actions.toggleCutout();
  await bus.actions.music();
  await bus.actions.presence("replay");
  assert.deepEqual(calls, ["hold", "movement:4", "program:free", "cutout"]);
  assert.deepEqual(
    [bus.getState().playback, bus.getState().movement, bus.getState().program, bus.getState().cutout, bus.getState().music, bus.getState().presence],
    ["held-user", 4, "free", "on", "playing", "replay"],
  );
});

await test("only program and Figure cutout enter shareable presentation state", () => {
  const presentation = sharePresentationState({ program: "free", cutout: "on", playback: "held-user", music: "playing" });
  assert.deepEqual(presentation, { mode: "free", cutout: true });
  const shared = new URL(sharePresentationUrl(
    "https://example.test/danse/?score=fixture.json&choreography=debug.json&conductor=waltz&meter=4%2F4&bpm=96&token=SECRET#s=42&e=99&u=7&debug=SECRET",
    presentation,
  ));
  assert.equal(shared.pathname, "/danse/");
  assert.deepEqual([...shared.searchParams], [["cutout", "1"]]);
  assert.equal(shared.hash, "#s=42&e=99&u=7&p=free");

  const scoreLed = new URL(sharePresentationUrl(
    "https://example.test/danse/?cutout=1&score=fixture.json#s=42&e=99&u=7&p=free",
    sharePresentationState({ program: "score-led", cutout: "off" }),
  ));
  assert.equal(scoreLed.search, "");
  assert.equal(scoreLed.hash, "#s=42&e=99&u=7");
});

await test("reported action failures are consumed at the UI action boundary", async () => {
  const reports = [];
  const bus = createControlActions({
    setProgram: async () => { throw new Error("score unavailable"); },
    reportError: (name, error) => reports.push([name, error.message]),
  });
  const result = await bus.actions.setProgram("free");
  assert.equal(result, undefined);
  assert.deepEqual(reports, [["program", "score unavailable"]]);
  assert.equal(bus.getState().program, "score-led");
});

process.stdout.write(`interface controls: ${passed} checks passed\n`);
