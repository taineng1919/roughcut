from __future__ import annotations

import pytest

from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.content_draft import (
    ContentDraft,
    ContentDraftRef,
    NarrationBlock,
    SourceExcerptBlock,
)
from roughcut.domain.project import ProjectError


def _brief() -> EditBrief:
    return EditBrief("brief_fixture", "主题", 1_200_000, ("重点",), True)


def _ref() -> ContentDraftRef:
    return ContentDraftRef("src_a", "tr_a", "seg_a", 0, 120_000)


def test_content_draft_schema_one_roundtrip() -> None:
    draft = ContentDraft(
        content_draft_id="draft_fixture",
        parent_draft_id=None,
        base_project_revision=3,
        confirmed_by_user=False,
        brief_snapshot=_brief(),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="a" * 64,
        blocks=(
            SourceExcerptBlock(
                "block_quote",
                (_ref(),),
                "来源原话",
                section_title="开场",
            ),
            NarrationBlock(
                "block_narration",
                "编辑旁白",
                "draft",
                (),
                section_title="主体",
            ),
        ),
        display_title="校园探访初稿",
    )

    assert ContentDraft.from_dict(draft.to_dict()) == draft
    assert draft.to_dict()["schema_version"] == 1
    assert draft.to_dict()["display_title"] == "校园探访初稿"
    assert draft.to_dict()["blocks"][0]["section_title"] == "开场"  # type: ignore[index]


def test_content_draft_schema_one_reads_old_payload_without_guessing_titles() -> None:
    draft = ContentDraft(
        content_draft_id="draft_fixture",
        parent_draft_id=None,
        base_project_revision=3,
        confirmed_by_user=False,
        brief_snapshot=_brief(),
        source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
        context_hash="a" * 64,
        blocks=(SourceExcerptBlock("block_quote", (_ref(),), "来源原话"),),
    )
    legacy = draft.to_dict()
    legacy.pop("display_title")
    for block in legacy["blocks"]:  # type: ignore[union-attr]
        block.pop("section_title")

    restored = ContentDraft.from_dict(legacy)

    assert restored.display_title is None
    assert restored.blocks[0].section_title is None
    assert restored.brief_snapshot.theme == "主题"


@pytest.mark.parametrize("value", ["", "   ", "x" * 81])
def test_content_draft_rejects_invalid_display_metadata(value: str) -> None:
    with pytest.raises(ProjectError, match="title"):
        ContentDraft(
            content_draft_id="draft_fixture",
            parent_draft_id=None,
            base_project_revision=3,
            confirmed_by_user=False,
            brief_snapshot=_brief(),
            source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
            context_hash="a" * 64,
            blocks=(SourceExcerptBlock("block_quote", (_ref(),), "来源原话"),),
            display_title=value,
        )
    with pytest.raises(ProjectError, match="title"):
        SourceExcerptBlock(
            "block_quote",
            (_ref(),),
            "来源原话",
            section_title=value,
        )


@pytest.mark.parametrize("status", ["", "pending", "done"])
def test_narration_rejects_unknown_status(status: str) -> None:
    with pytest.raises(ProjectError, match="status"):
        NarrationBlock("block_narration", "旁白", status, ())


def test_content_draft_rejects_duplicate_block_ids_and_invalid_parent() -> None:
    block = SourceExcerptBlock("block_same", (_ref(),), "来源原话")
    with pytest.raises(ProjectError, match="duplicate"):
        ContentDraft(
            content_draft_id="draft_fixture",
            parent_draft_id=None,
            base_project_revision=3,
            confirmed_by_user=False,
            brief_snapshot=_brief(),
            source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
            context_hash="a" * 64,
            blocks=(block, block),
        )
    with pytest.raises(ProjectError, match="parent"):
        ContentDraft(
            content_draft_id="draft_fixture",
            parent_draft_id="../escape",
            base_project_revision=3,
            confirmed_by_user=False,
            brief_snapshot=_brief(),
            source_bindings=(SourceTranscriptBinding("src_a", "tr_a"),),
            context_hash="a" * 64,
            blocks=(block,),
        )
