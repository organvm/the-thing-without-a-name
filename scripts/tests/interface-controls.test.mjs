#!/usr/bin/env node

import assert from "node:assert/strict";
import { createControlActions } from "../../interface/actions.js";
import { renderControlSurface } from "../../interface/render.js";
import {
  ACTIONS,
  initialControlState,
  reduceControlState,
  sharePresentationState,
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
  state = reduceControlState(state, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true });
  assert.deepEqual([state.playback, state.music], ["held-user", "suspended-by-hold"]);
  state = reduceControlState(state, { type: ACTIONS.RESET_RIVER, reducedMotion: false });
  assert.deepEqual([state.playback, state.music], ["running", "playing"]);
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
  assert.equal(shortcutAction(event("h", "INPUT")), null);
  assert.equal(shortcutAction(event(" ", "BUTTON")), null);
  assert.equal(shortcutAction(event("8")), null);
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
  assert.deepEqual(sharePresentationState({ program: "free", cutout: "on", playback: "held-user", music: "playing" }), { mode: "free", cutout: true });
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
