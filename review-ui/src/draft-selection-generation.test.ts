import { describe, expect, it } from "vitest";

import { DraftSelectionGeneration } from "./draft-selection-generation";

describe("draft selection request generation", () => {
  it("allows only the latest request for the displayed candidate to land", () => {
    const generation = new DraftSelectionGeneration();
    const first = generation.begin("candidate_a");
    const second = generation.begin("candidate_a");

    expect(generation.isCurrent(first, "candidate_a")).toBe(false);
    expect(generation.isCurrent(second, "candidate_a")).toBe(true);
    expect(generation.isCurrent(second, "candidate_b")).toBe(false);
  });

  it("invalidates a pending request even when the replacement has the same start", () => {
    const generation = new DraftSelectionGeneration();
    const first = generation.begin("candidate_a");
    generation.invalidate();
    const reselection = generation.begin("candidate_a");

    expect(generation.isCurrent(first, "candidate_a")).toBe(false);
    expect(generation.isCurrent(reselection, "candidate_a")).toBe(true);
  });

  it("keeps an ordinary caret request from landing after target mode starts", () => {
    const selectionGeneration = new DraftSelectionGeneration();
    const caretGeneration = new DraftSelectionGeneration();
    const ordinaryCaret = caretGeneration.begin("candidate_a");

    selectionGeneration.begin("candidate_a");
    caretGeneration.invalidate();

    expect(caretGeneration.isCurrent(ordinaryCaret, "candidate_a")).toBe(false);
  });

  it("accepts only the latest target and rejects the cancelled round", () => {
    const generation = new DraftSelectionGeneration();
    const targetA = generation.begin("candidate_a");
    const targetB = generation.begin("candidate_a");

    expect(generation.isCurrent(targetA, "candidate_a")).toBe(false);
    expect(generation.isCurrent(targetB, "candidate_a")).toBe(true);

    generation.invalidate();
    const nextRound = generation.begin("candidate_a");
    expect(generation.isCurrent(targetB, "candidate_a")).toBe(false);
    expect(generation.isCurrent(nextRound, "candidate_a")).toBe(true);
  });
});
