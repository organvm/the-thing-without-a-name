#!/usr/bin/env node

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { renderControlSurface } from "../../interface/render.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const expected =
  "Performance and primary choreography — Madison Garber. " +
  "Concept, direction, additional choreography, photography, editing, sound, software, archive, and production — Anthony J. Padavano. " +
  "Music — Léo Delibes; source arrangements — Paul De Bra (CC BY 4.0).";

const byId = new Map();
const parent = {
  inserted: [],
  add(element) {
    this.inserted.push(element);
    if (element.id) byId.set(element.id, element);
  },
  insertBefore(element) { this.add(element); },
};
function anchor(id) {
  const value = {
    id,
    parentNode: parent,
    nextSibling: null,
    insertAdjacentElement(position, element) {
      assert.equal(position, "afterend");
      parent.add(element);
    },
  };
  byId.set(id, value);
  return value;
}
anchor("intro-behavior");
anchor("project-map-status");

const doc = {
  getElementById(id) { return byId.get(id) ?? null; },
  createElement(tagName) {
    return {
      tagName: tagName.toUpperCase(),
      id: "",
      className: "",
      textContent: "",
    };
  },
  querySelectorAll() { return []; },
};
const root = {
  ownerDocument: doc,
  dataset: {},
  querySelector() { return null; },
};
const state = {
  surface: "closed",
  playback: "running",
  program: "score-led",
  cutout: "off",
  music: "unavailable",
  movement: 1,
  presence: "off",
};

renderControlSurface(root, state);
assert.equal(parent.inserted.length, 2);
assert.deepEqual(
  parent.inserted.map((element) => [element.id, element.className, element.textContent]),
  [
    ["intro-project-credit", "project-credit", expected],
    ["project-map-credit", "project-credit", expected],
  ],
);
renderControlSurface(root, state);
assert.equal(parent.inserted.length, 2, "public credits must not duplicate on rerender");

const directive = await readFile(resolve(ROOT, "press/public_credits.md"), "utf8");
assert.match(directive, /Performance and primary choreography/);
assert.match(directive, /Madison Garber/);
assert.match(directive, /additional choreography, photography, editing, sound, software, archive, and production/);
assert.match(directive, /Anthony J\. Padavano/);
assert.match(directive, /Do not collapse third-party music credits/);

const exporter = await readFile(resolve(ROOT, "submission/prepare-screendance-macos.sh"), "utf8");
for (const text of [
  "PERFORMANCE · MADISON GARBER",
  "PRIMARY CHOREOGRAPHY · MADISON GARBER",
  "ANTHONY J. PADAVANO",
  "ADDITIONAL CHOREOGRAPHY",
  "MUSIC · LÉO DELIBES",
  "SOURCE ARRANGEMENTS · PAUL DE BRA · CC BY 4.0",
]) assert.ok(exporter.includes(text), `missing export credit: ${text}`);

process.stdout.write("  ok 1 - corrected public and film-export credits stay aligned\n");
