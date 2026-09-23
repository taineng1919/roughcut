import type {
  DraftEditorSelectionResponse,
} from "./workflow-types";

export interface DraftCorrespondence {
  sourceId: string;
  sourceDisplayName: string;
  paragraphId: string;
  start: number;
  end: number;
}

export function draftCorrespondences(
  selection: DraftEditorSelectionResponse,
): DraftCorrespondence[] {
  return selection.correspondence_groups.map((group) => ({
    sourceId: group.source_id,
    sourceDisplayName: group.source_display_name,
    paragraphId: group.paragraph_id,
    start: group.start_offset,
    end: group.end_offset,
  }));
}
