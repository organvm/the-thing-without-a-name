import { ACTIONS, initialControlState, reduceControlState } from "./state.js";

/** Buttons, shortcuts, and browser probes all call this one named action API. */
export function createControlActions(adapter, options = {}) {
  let state = initialControlState(options);
  const listeners = new Set();
  const emit = () => { for (const listener of listeners) listener(state); };
  const dispatch = (action) => { state = reduceControlState(state, action); emit(); return state; };
  const run = async (name, operation, stateAction = null) => {
    try {
      const result = await operation();
      if (stateAction) {
        const action = typeof stateAction === "function" ? stateAction(result) : stateAction;
        if (action) dispatch(action);
      }
      return result;
    } catch (error) {
      adapter.reportError?.(name, error);
      throw error;
    }
  };
  const actions = {
    hold: () => run("hold", adapter.hold, { type: ACTIONS.TOGGLE_HOLD, allowMotion: true }),
    setReducedMotion: (value) => { adapter.setReducedMotion?.(value); return dispatch({ type: ACTIONS.SET_REDUCED_MOTION, value }); },
    newRiver: () => run("new-river", adapter.newRiver),
    undoRiver: () => run("undo-river", adapter.undoRiver),
    share: () => run("share", adapter.share),
    movement: (value) => run("movement", () => adapter.movement(value), (changed) => changed ? { type: ACTIONS.SET_MOVEMENT, value } : null),
    setProgram: (value) => run("program", () => adapter.setProgram(value), { type: ACTIONS.SET_PROGRAM, value }),
    toggleProgram: () => actions.setProgram(state.program === "score-led" ? "free" : "score-led"),
    toggleCutout: () => run("figure-cutout", adapter.toggleCutout, { type: ACTIONS.TOGGLE_CUTOUT }),
    music: () => run("music", adapter.music, (value) => ({ type: ACTIONS.SET_MUSIC, value })),
    conductor: (value) => run("conductor", () => adapter.conductor(value), (result) => ({ type: ACTIONS.SET_CONDUCTOR, ...result })),
    presence: (value) => run("presence", () => adapter.presence(value), (result) => ({ type: ACTIONS.SET_PRESENCE, value: result ?? state.presence })),
    openTray: (category) => dispatch({ type: ACTIONS.OPEN_TRAY, category }),
    openSheet: (section) => dispatch({ type: ACTIONS.OPEN_SHEET, section }),
    openMap: () => dispatch({ type: ACTIONS.OPEN_MAP }),
    close: () => { adapter.close?.(); return dispatch({ type: ACTIONS.CLOSE_SURFACE }); },
    status: (category, message) => dispatch({ type: ACTIONS.SET_STATUS, category, message }),
    sync: (action) => dispatch(action),
  };
  return Object.freeze({
    actions: Object.freeze(actions),
    getState: () => state,
    subscribe(listener) { listeners.add(listener); listener(state); return () => listeners.delete(listener); },
  });
}
