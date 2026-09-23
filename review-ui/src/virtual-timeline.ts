export type VirtualTimelinePhase =
  | "paused"
  | "playing"
  | "waiting"
  | "seeking"
  | "ended"
  | "error";

export interface VirtualTimelineState {
  totalTicks: number;
  outputTicks: number;
  playIntent: boolean;
  phase: VirtualTimelinePhase;
  error: string | null;
}

export const PLAY_REQUESTED = "play-requested";
export const PAUSE_REQUESTED = "pause-requested";
export const SEEK_REQUESTED = "seek-requested";
export const SOURCE_WAITING = "source-waiting";
export const SOURCE_CANPLAY = "source-canplay";

export type VirtualTimelineEvent =
  | { type: typeof PLAY_REQUESTED }
  | { type: typeof PAUSE_REQUESTED }
  | { type: typeof SEEK_REQUESTED; outputTicks: number }
  | { type: "output-time"; outputTicks: number }
  | { type: typeof SOURCE_WAITING }
  | { type: typeof SOURCE_CANPLAY }
  | { type: "timeline-ended" }
  | { type: "media-error"; message: string };

export function createVirtualTimelineState(totalTicks: number): VirtualTimelineState {
  if (!Number.isSafeInteger(totalTicks) || totalTicks <= 0) {
    throw new Error("Virtual Timeline total must be a positive safe integer");
  }
  return {
    totalTicks,
    outputTicks: 0,
    playIntent: false,
    phase: "paused",
    error: null,
  };
}

export function reduceVirtualTimeline(
  state: VirtualTimelineState,
  event: VirtualTimelineEvent,
): VirtualTimelineState {
  if (state.phase === "error") return state;
  if (event.type === "media-error") {
    return {
      ...state,
      playIntent: false,
      phase: "error",
      error: event.message,
    };
  }
  if (
    state.phase === "ended"
    && (event.type === SOURCE_CANPLAY || event.type === SOURCE_WAITING)
  ) {
    return state;
  }
  if (event.type === PLAY_REQUESTED) {
    return {
      ...state,
      playIntent: true,
      phase: "waiting",
    };
  }
  if (event.type === PAUSE_REQUESTED) {
    return {
      ...state,
      playIntent: false,
      phase: state.outputTicks === state.totalTicks ? "ended" : "paused",
    };
  }
  if (event.type === SEEK_REQUESTED) {
    const outputTicks = clampTicks(event.outputTicks, state.totalTicks);
    return {
      ...state,
      outputTicks,
      playIntent: outputTicks === state.totalTicks ? false : state.playIntent,
      phase: outputTicks === state.totalTicks ? "ended" : "seeking",
    };
  }
  if (event.type === "output-time") {
    const outputTicks = clampTicks(event.outputTicks, state.totalTicks);
    return {
      ...state,
      outputTicks,
      playIntent: outputTicks === state.totalTicks ? false : state.playIntent,
      phase: outputTicks === state.totalTicks
        ? "ended"
        : state.playIntent
          ? "playing"
          : "paused",
    };
  }
  if (event.type === SOURCE_WAITING) {
    return {
      ...state,
      phase: state.playIntent ? "waiting" : state.phase,
    };
  }
  if (event.type === SOURCE_CANPLAY) {
    return {
      ...state,
      phase: state.playIntent ? "playing" : "paused",
    };
  }
  return {
    ...state,
    outputTicks: state.totalTicks,
    playIntent: false,
    phase: "ended",
  };
}

function clampTicks(value: number, totalTicks: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(totalTicks, Math.max(0, Math.round(value)));
}
