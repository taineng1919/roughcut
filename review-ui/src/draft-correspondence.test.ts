import { describe, expect, it } from "vitest";

import { draftCorrespondences } from "./draft-correspondence";
import type { DraftEditorSelectionResponse } from "./workflow-types";

describe("draft source correspondence", () => {
  it("maps one visual selection to ordered exact ranges across sources and people", () => {
    const selection = {
      correspondence_groups: [
        {
          source_id: "src_a",
          source_display_name: "素材 A",
          paragraph_id: "source_a",
          start_offset: 12,
          end_offset: 14,
        },
        {
          source_id: "src_b",
          source_display_name: "素材 B",
          paragraph_id: "source_b",
          start_offset: 2,
          end_offset: 6,
        },
        {
          source_id: "src_c",
          source_display_name: "素材 C",
          paragraph_id: "source_c",
          start_offset: 7,
          end_offset: 8,
        },
      ],
      display_range: {
        anchor: { paragraph_id: "draft_2", character_offset: 1, utf16_offset: 1 },
        focus: { paragraph_id: "draft_1", character_offset: 2, utf16_offset: 2 },
      },
    } as DraftEditorSelectionResponse;

    expect(draftCorrespondences(selection)).toEqual([
      {
        sourceId: "src_a",
        sourceDisplayName: "素材 A",
        paragraphId: "source_a",
        start: 12,
        end: 14,
      },
      {
        sourceId: "src_b",
        sourceDisplayName: "素材 B",
        paragraphId: "source_b",
        start: 2,
        end: 6,
      },
      {
        sourceId: "src_c",
        sourceDisplayName: "素材 C",
        paragraphId: "source_c",
        start: 7,
        end: 8,
      },
    ]);
  });
});
