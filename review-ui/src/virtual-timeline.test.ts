import { describe, expect, it } from "vitest";

import {
  createVirtualTimelineState,
  reduceVirtualTimeline,
} from "./virtual-timeline";

describe("Virtual Timeline transport state", () => {
  it("keeps output time and total independent from Source duration", () => {
    let state = createVirtualTimelineState(360_000);
    state = reduceVirtualTimeline(state, { type: "play-requested" });
    state = reduceVirtualTimeline(state, { type: "source-canplay" });
    state = reduceVirtualTimeline(state, {
      type: "output-time",
      outputTicks: 120_000,
    });

    expect(state.phase).toBe("playing");
    expect(state.outputTicks).toBe(120_000);
    expect(state.totalTicks).toBe(360_000);
    expect(state.playIntent).toBe(true);
  });

  it("preserves play intent through waiting, seeking, and A→B→A source switches", () => {
    let state = createVirtualTimelineState(360_000);
    state = reduceVirtualTimeline(state, { type: "play-requested" });
    state = reduceVirtualTimeline(state, { type: "source-waiting" });
    expect(state.phase).toBe("waiting");

    state = reduceVirtualTimeline(state, {
      type: "seek-requested",
      outputTicks: 120_000,
    });
    expect(state).toMatchObject({
      phase: "seeking",
      outputTicks: 120_000,
      playIntent: true,
    });
    state = reduceVirtualTimeline(state, { type: "source-canplay" });
    expect(state.phase).toBe("playing");

    state = reduceVirtualTimeline(state, {
      type: "seek-requested",
      outputTicks: 240_000,
    });
    state = reduceVirtualTimeline(state, { type: "source-canplay" });
    expect(state).toMatchObject({
      phase: "playing",
      outputTicks: 240_000,
      totalTicks: 360_000,
    });
  });

  it("ends at the virtual total and does not let later media events overwrite errors", () => {
    let state = createVirtualTimelineState(360_000);
    state = reduceVirtualTimeline(state, { type: "play-requested" });
    state = reduceVirtualTimeline(state, { type: "timeline-ended" });
    state = reduceVirtualTimeline(state, { type: "source-canplay" });
    state = reduceVirtualTimeline(state, { type: "source-waiting" });
    expect(state).toMatchObject({
      phase: "ended",
      outputTicks: 360_000,
      playIntent: false,
    });

    state = reduceVirtualTimeline(state, {
      type: "media-error",
      message: "素材无法播放",
    });
    state = reduceVirtualTimeline(state, { type: "source-canplay" });
    state = reduceVirtualTimeline(state, { type: "source-waiting" });
    expect(state).toMatchObject({
      phase: "error",
      outputTicks: 360_000,
      playIntent: false,
      error: "素材无法播放",
    });
  });

  it("clamps seek and pause without resetting the visible output position", () => {
    let state = createVirtualTimelineState(360_000);
    state = reduceVirtualTimeline(state, {
      type: "seek-requested",
      outputTicks: 999_999,
    });
    expect(state.outputTicks).toBe(360_000);
    expect(state.phase).toBe("ended");

    state = reduceVirtualTimeline(state, {
      type: "seek-requested",
      outputTicks: 180_000,
    });
    state = reduceVirtualTimeline(state, { type: "pause-requested" });
    expect(state).toMatchObject({
      phase: "paused",
      outputTicks: 180_000,
      playIntent: false,
    });
  });
});
