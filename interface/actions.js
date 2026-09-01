import { ACTIONS, MUSIC, PRESENCE, initialControlState, reduceControlState } from "./state.js";

/** Buttons, shortcuts, and browser probes all call this one named action API. */
export function createControlActions(adapter, options = {}) {
  let state = initialControlState(options);
  let programIntent = state.program;
  let programIntentRevision = 0;
  let musicIntent = state.music;
  let musicIntentRevision = 0;
  let musicOperation = Promise.resolve();
  let presenceIntent = state.presence;
  let presenceRevision = 0;
  const listeners = new Set();
  const emit = () => { for (const listener of listeners) listener(state); };
  const dispatch = (action) => { state = reduceControlState(state, action); emit(); return state; };
  const run = async (name, operation, stateAction = null, isCurrent = () => true) => {
    try {
      const result = await operation();
      if (stateAction && isCurrent()) {
        const action = typeof stateAction === "function" ? stateAction(result) : stateAction;
        if (action) dispatch(action);
      }
      return result;
    } catch (error) {
      if (isCurrent()) adapter.reportError?.(name, error);
      return undefined;
    }
  };
  const setProgram = (value) => {
    const intent = value === "free" ? "free" : "score-led";
    const revision = ++programIntentRevision;
    programIntent = intent;
    return run(
      "program",
      () => adapter.setProgram(intent),
      (changed) => changed === false || revision !== programIntentRevision
        ? null
        : { type: ACTIONS.SET_PROGRAM, value: intent },
      () => revision === programIntentRevision,
    ).finally(() => {
      if (revision === programIntentRevision) programIntent = state.program;
    });
  };
  const setPresence = (value) => {
    if (!PRESENCE.includes(value)) return Promise.resolve(state.presence);
    const intent = value;
    const revision = ++presenceRevision;
    presenceIntent = intent;
    dispatch({ type: ACTIONS.SET_STATUS, category: "presence", message: "" });
    return run(
      "presence",
      () => adapter.presence(value),
      (result) => ({ type: ACTIONS.SET_PRESENCE, value: result ?? state.presence }),
      () => revision === presenceRevision,
    ).finally(() => {
      if (revision === presenceRevision) presenceIntent = state.presence;
    });
  };
  const toggleMusic = () => {
    if (musicIntent === "unavailable") return Promise.resolve("unavailable");
    const intent = ["playing", "suspended-by-hold"].includes(musicIntent) ? "stopped" : "playing";
    const revision = ++musicIntentRevision;
    musicIntent = intent;
    const operation = musicOperation.then(() => adapter.music(intent));
    musicOperation = operation.catch(() => undefined);
    return run(
      "music",
      () => operation,
      (value) => revision === musicIntentRevision ? { type: ACTIONS.SET_MUSIC, value } : null,
    ).finally(() => {
      if (revision === musicIntentRevision) musicIntent = state.music;
    });
  };
  const sync = (action) => {
    if (action?.type === ACTIONS.SET_PROGRAM) {
      programIntentRevision += 1;
      programIntent = action.value === "free" ? "free" : "score-led";
    }
    if (action?.type === ACTIONS.SET_PRESENCE && PRESENCE.includes(action.value)) {
      if (action.value !== presenceIntent) presenceRevision += 1;
      presenceIntent = action.value;
    }
    if (action?.type === ACTIONS.SET_MUSIC && MUSIC.includes(action.value)) {
      musicIntentRevision += 1;
      musicIntent = action.value;
    }
    return dispatch(action);
  };
  const actions = {
    hold: () => run("hold", adapter.hold, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true }),
    setReducedMotion: (value) => { adapter.setReducedMotion?.(value); return dispatch({ type: ACTIONS.SET_REDUCED_MOTION, value }); },
    newRiver: () => run(
      "new-river",
      adapter.newRiver,
      (result) => result ? { type: ACTIONS.RESET_RIVER, reducedMotion: Boolean(result.reducedMotion) } : null,
    ),
    undoRiver: () => run(
      "undo-river",
      adapter.undoRiver,
      (result) => result ? { type: ACTIONS.RESET_RIVER, reducedMotion: Boolean(result.reducedMotion) } : null,
    ),
    share: () => run("share", adapter.share),
    movement: (value) => run("movement", () => adapter.movement(value), (changed) => changed ? { type: ACTIONS.SET_MOVEMENT, value } : null),
    setProgram,
    toggleProgram: () => setProgram(programIntent === "score-led" ? "free" : "score-led"),
    toggleCutout: () => run("figure-cutout", adapter.toggleCutout, { type: ACTIONS.TOGGLE_CUTOUT }),
    music: toggleMusic,
    conductor: (value) => run("conductor", () => adapter.conductor(value), (result) => ({ type: ACTIONS.SET_CONDUCTOR, ...result })),
    presence: setPresence,
    openTray: (category) => dispatch({ type: ACTIONS.OPEN_TRAY, category }),
    openSheet: (section) => dispatch({ type: ACTIONS.OPEN_SHEET, section }),
    openMap: () => dispatch({ type: ACTIONS.OPEN_MAP }),
    close: () => { adapter.close?.(); return dispatch({ type: ACTIONS.CLOSE_SURFACE }); },
    status: (category, message) => {
      if (category === "presence") presenceRevision += 1;
      return dispatch({ type: ACTIONS.SET_STATUS, category, message });
    },
    sync,
  };
  return Object.freeze({
    actions: Object.freeze(actions),
    getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); },
  });
}
