#!/usr/bin/env node

import assert from "node:assert/strict";
import { createControlActions } from "../../interface/actions.js";
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

process.stdout.write(`interface controls: ${passed} checks passed\n`);
