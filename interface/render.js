import { CATEGORIES } from "./state.js";

export function renderControlSurface(root, state) {
  const doc = root.ownerDocument ?? document;
  for (const category of CATEGORIES) {
    const button = root.querySelector(`[data-category="${category}"]`);
    if (!button) continue;
    const selected = state.surface === `tray:${category}` || state.surface.startsWith(`sheet:${category}`) || state.surface === "map" && category === "map";
    button.setAttribute("aria-pressed", String(selected));
    button.classList.toggle("is-selected", selected);
  }
  root.dataset.surface = state.surface;
  root.dataset.playback = state.playback;
  root.dataset.program = state.program;
  root.dataset.cutout = state.cutout;
  const hold = root.querySelector('[data-category="hold"]');
  if (hold) {
    const held = state.playback !== "running";
    hold.setAttribute("aria-pressed", String(held));
    hold.textContent = held ? "Resume" : "Hold";
    hold.setAttribute("aria-label", state.playback === "held-reduced" ? "Resume motion despite reduced-motion preference" : held ? "Resume motion" : "Hold motion");
  }
  const score = doc.getElementById("program-score");
  const free = doc.getElementById("program-free");
  score?.setAttribute("aria-pressed", String(state.program === "score-led"));
  free?.setAttribute("aria-pressed", String(state.program === "free"));
  const cutout = doc.getElementById("cutout-toggle");
  if (cutout) {
    cutout.setAttribute("aria-pressed", String(state.cutout === "on"));
    cutout.textContent = `Figure cutout: ${state.cutout}`;
  }
  const music = doc.getElementById("music-tray");
  if (music) {
    music.disabled = state.music === "unavailable";
    music.textContent = `Music: ${state.music.replaceAll("-", " ")}`;
  }
  for (const movement of doc.querySelectorAll("[data-movement]")) {
    movement.setAttribute("aria-pressed", String(Number(movement.dataset.movement) === state.movement));
  }
  for (const [id, value] of [["presence-off", "off"], ["presence-camera", "camera"], ["presence-touch", "keyboard-touch"], ["presence-replay", "replay"]]) {
    doc.getElementById(id)?.setAttribute("aria-pressed", String(state.presence === value));
  }
}

export function renderProjectMap(list, status, map) {
  if (!map || map.schema !== "danse.map.v1" || !Array.isArray(map.nodes)) throw new TypeError("invalid danse.map.v1 record");
  list.replaceChildren();
  const doc = list.ownerDocument ?? document;
  for (const node of map.nodes) {
    const item = doc.createElement("li");
    const available = node.status === "admitted" && typeof node.href === "string";
    const label = available ? doc.createElement("a") : doc.createElement("span");
    label.textContent = node.label;
    if (available) label.href = node.href;
    else {
      label.setAttribute("aria-disabled", "true");
      const note = doc.createElement("small");
      note.textContent = ` (${node.availability ?? "unavailable"})`;
      label.append(note);
    }
    item.append(label);
    list.append(item);
  }
  status.textContent = map.nodes.some((node) => node.status !== "admitted")
    ? "Available routes are linked; gated routes remain visibly unavailable."
    : "All project routes in this map are available.";
}
