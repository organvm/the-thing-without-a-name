/** Pure state contract for the Danse progressive-disclosure controls. */

export const PLAYBACK = Object.freeze(["running", "held-user", "held-reduced"]);
export const PROGRAM = Object.freeze(["score-led", "free"]);
export const CUTOUT = Object.freeze(["off", "on"]);
export const MUSIC = Object.freeze(["unavailable", "stopped", "playing", "suspended-by-hold", "error"]);
export const AUDITION = Object.freeze(["follow-score", "override-active", "override-ready"]);
export const PRESENCE = Object.freeze(["off", "camera", "keyboard-touch", "replay"]);
export const CATEGORIES = Object.freeze(["hold", "river", "score", "presence", "map"]);

const PROJECT_FRAGMENT_SECTIONS = Object.freeze({
  content: "project-artwork",
  artwork: "project-artwork",
  film: "project-film",
  "ballet-score": "project-film",
  status: "project-status",
  readings: "project-readings",
  cubism: "project-readings",
  glitch: "project-readings",
  evidence: "project-evidence",
  access: "project-artwork",
  resources: "project-evidence",
  "installation-contract": "project-evidence",
});

/** Resolve only declared legacy Project fragments into the live dialog. */
export function projectSectionFor(fragment = "") {
  let key;
  try {
    key = decodeURIComponent(String(fragment).replace(/^#/, "")).toLowerCase();
  } catch {
    return null;
  }
  return Object.hasOwn(PROJECT_FRAGMENT_SECTIONS, key)
    ? PROJECT_FRAGMENT_SECTIONS[key]
    : null;
}

export const ACTIONS = Object.freeze({
  TOGGLE_HOLD: "toggle-hold",
  RESET_RIVER: "reset-river",
  SET_REDUCED_MOTION: "set-reduced-motion",
  SET_MOVEMENT: "set-movement",
  SET_PROGRAM: "set-program",
  TOGGLE_CUTOUT: "toggle-cutout",
  SET_MUSIC: "set-music",
  SET_CONDUCTOR: "set-conductor",
  SET_PRESENCE: "set-presence",
  OPEN_TRAY: "open-tray",
  OPEN_SHEET: "open-sheet",
  OPEN_MAP: "open-map",
  CLOSE_SURFACE: "close-surface",
  SET_STATUS: "set-status",
});

export function initialControlState({ reducedMotion = false, scoreAvailable = true } = {}) {
  return Object.freeze({
    playback: reducedMotion ? "held-reduced" : "running",
    program: "score-led",
    cutout: "off",
    music: scoreAvailable ? "stopped" : "unavailable",
    surface: "closed",
    audition: "follow-score",
    presence: "off",
    movement: 1,
    status: Object.freeze({ river: "", score: "", presence: "", map: "" }),
  });
}

export const INITIAL_CONTROL_STATE = initialControlState();

function withStatus(state, category, message) {
  if (!Object.hasOwn(state.status, category)) return state.status;
  return Object.freeze({ ...state.status, [category]: String(message ?? "") });
}

export function reduceControlState(state, action) {
  const next = { ...state };
  switch (action?.type) {
    case ACTIONS.TOGGLE_HOLD:
      if (state.playback === "held-reduced" && !action.allowMotion) return state;
      next.playback = state.playback === "running" ? "held-user" : "running";
      if (state.music === "playing" && next.playback !== "running") next.music = "suspended-by-hold";
      else if (state.music === "suspended-by-hold" && next.playback === "running") next.music = "playing";
      break;
    case ACTIONS.RESET_RIVER:
      next.playback = action.reducedMotion ? "held-reduced" : "running";
      next.movement = 1;
      if (next.playback === "running" && state.music === "suspended-by-hold") next.music = "playing";
      else if (next.playback !== "running" && state.music === "playing") next.music = "suspended-by-hold";
      break;
    case ACTIONS.SET_REDUCED_MOTION:
      if (action.value && state.playback === "running") next.playback = "held-reduced";
      else if (!action.value && state.playback === "held-reduced") next.playback = "running";
      if (state.music === "playing" && next.playback === "held-reduced") next.music = "suspended-by-hold";
      else if (state.playback === "held-reduced" && next.playback === "running" && state.music === "suspended-by-hold") next.music = "playing";
      break;
    case ACTIONS.SET_PROGRAM:
      next.program = action.value === "free" ? "free" : "score-led";
      break;
    case ACTIONS.TOGGLE_CUTOUT:
      next.cutout = state.cutout === "on" ? "off" : "on";
      break;
    case ACTIONS.SET_MOVEMENT:
      next.movement = Math.max(1, Math.min(7, Number(action.value) || 1));
      break;
    case ACTIONS.SET_MUSIC:
      if (MUSIC.includes(action.value)) next.music = action.value;
      break;
    case ACTIONS.SET_CONDUCTOR:
      next.audition = action.active ? "override-active" : action.ready ? "override-ready" : "follow-score";
      break;
    case ACTIONS.SET_PRESENCE:
      if (!PRESENCE.includes(action.value)) return state;
      next.presence = action.value;
      next.status = withStatus(state, "presence", "");
      break;
    case ACTIONS.OPEN_TRAY:
      if (!["river", "score", "presence"].includes(action.category)) return state;
      next.surface = state.surface === `tray:${action.category}` ? "closed" : `tray:${action.category}`;
      break;
    case ACTIONS.OPEN_SHEET:
      next.surface = `sheet:${String(action.section ?? "details")}`;
      break;
    case ACTIONS.OPEN_MAP:
      next.surface = "map";
      break;
    case ACTIONS.CLOSE_SURFACE:
      next.surface = "closed";
      break;
    case ACTIONS.SET_STATUS:
      next.status = withStatus(state, action.category, action.message);
      break;
    default:
      return state;
  }
  return Object.freeze(next);
}

export function isEditableTarget(target) {
  if (!target || typeof target !== "object") return false;
  const tag = String(target.tagName ?? "").toLowerCase();
  return ["input", "select", "textarea"].includes(tag) || Boolean(target.isContentEditable);
}

function isControlTarget(target) {
  if (isEditableTarget(target)) return true;
  if (typeof target?.closest === "function") {
    return Boolean(target.closest("button, input, select, textarea, a, [contenteditable]"));
  }
  const tag = String(target?.tagName ?? "").toLowerCase();
  return tag === "button" || tag === "a";
}

/** Translate compatibility keys to the same named actions used by buttons. */
export function shortcutAction(event) {
  if (!event || event.repeat || ((event.altKey || event.ctrlKey || event.metaKey) && event.key !== "Escape")) return null;
  const key = String(event.key ?? "");
  const lower = key.toLowerCase();
  if (key === "Escape") return { name: "close" };
  if (lower === "h") return { name: "toggleControls" };
  if (isControlTarget(event.target)) return null;
  if (key === " ") return { name: "hold" };
  if (lower === "n") return { name: "newRiver" };
  if (lower === "s") return { name: "share" };
  if (lower === "f") return { name: "toggleProgram" };
  if (lower === "m") return { name: "toggleCutout" };
  if (/^[1-7]$/.test(key)) return { name: "movement", value: Number(key) };
  return null;
}

export function sharePresentationState({ program, cutout }) {
  return Object.freeze({ mode: program === "free" ? "free" : null, cutout: cutout === "on" });
}

/** Admit only declared presentation state into a River link. */
export function sharePresentationUrl(href, { mode = null, cutout = false } = {}) {
  const url = new URL(String(href));
  url.search = "";
  if (cutout === true) url.searchParams.set("cutout", "1");
  const source = new URLSearchParams(url.hash.replace(/^#/, ""));
  const fragment = new URLSearchParams();
  for (const key of ["s", "e", "u"]) {
    if (source.has(key)) fragment.set(key, source.get(key));
  }
  if (mode === "free") fragment.set("p", "free");
  url.hash = fragment.toString();
  return url.href;
}
