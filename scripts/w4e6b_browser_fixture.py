"""Run the disposable W4-E6B Chrome acceptance project."""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from roughcut.adapters.project_store import ProjectStore
from roughcut.application.content_drafts import (
    read_content_draft,
    revise_content_draft_scoped,
)
from roughcut.application.people import create_person
from roughcut.review.server import start_review_server
from w4c_browser_fixture import _prepare_fixture


def main() -> None:
    temporary_root = Path(tempfile.mkdtemp(prefix="roughcut-w4e6b-chrome-"))
    review = None
    try:
        project_path, bindings, draft_id = _prepare_fixture(
            temporary_root,
            confirm=False,
            display_title="校园探访精华",
            with_sections=True,
            tail_count=520,
            compound=True,
            project_name="W4-E6B 合成浏览器验收",
        )
        review = start_review_server(
            project_path,
            source_bindings=bindings,
            content_draft_id=draft_id,
        )
        project = ProjectStore(project_path).load()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "url": review.url,
                    "project_path": str(project_path),
                    "project_revision": project.revision,
                    "content_draft_id": draft_id,
                    "source_bindings": bindings,
                    "compound_exact_refs": 12,
                    "long_transcript_segments_per_source": 520,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        for command in iter(input, "stop"):
            if command == "status":
                current = ProjectStore(project_path).load()
                print(
                    json.dumps(
                        {
                            "status": "running",
                            "project_revision": current.revision,
                            "active_content_draft_id": current.active_content_draft_id,
                        }
                    ),
                    flush=True,
                )
            elif command == "mutate":
                current = ProjectStore(project_path).load()
                mutation = create_person(
                    project_path,
                    name="外部变更人物",
                    role="验收",
                    note="用于 stale 页面锁定测试",
                    expected_revision=current.revision,
                )
                print(json.dumps(mutation.to_dict(), ensure_ascii=False), flush=True)
            elif command == "revise":
                revision = _revise_and_handoff(
                    project_path,
                    review.url,
                    review.token,
                )
                print(json.dumps(revision, ensure_ascii=False), flush=True)
            elif command:
                print(json.dumps({"status": "unknown_command"}), flush=True)
    finally:
        if review is not None:
            review.close()
        shutil.rmtree(temporary_root, ignore_errors=True)
        print(json.dumps({"status": "cleaned"}), flush=True)


def _revise_and_handoff(
    project_path: Path,
    review_url: str,
    token: str,
) -> dict[str, object]:
    parsed = urlsplit(review_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    read_request = urllib.request.Request(
        f"{origin}/api/workflow/draft-editor",
        headers={"X-Roughcut-Token": token},
    )
    with urllib.request.urlopen(read_request, timeout=5) as response:  # noqa: S310
        editor = json.load(response)
    parent_id = editor["candidate"]["candidate_id"]
    parent = read_content_draft(project_path, parent_id).content_draft
    mutable = parent.blocks[-1]
    blocks = [block.to_dict() for block in parent.blocks]
    current_title = blocks[-1].get("section_title")
    blocks[-1] = {
        **blocks[-1],
        "section_title": (
            "Agent 局部改稿 2"
            if current_title == "Agent 局部改稿 1"
            else "Agent 局部改稿 1"
        ),
    }
    child = revise_content_draft_scoped(
        project_path,
        parent_draft_id=parent_id,
        mutable_block_ids=[mutable.block_id],
        blocks=blocks,
        expected_revision=parent.base_project_revision,
    )
    child_id = child.content_draft.content_draft_id
    request = urllib.request.Request(
        f"{origin}/api/workflow/draft-candidate-select",
        data=json.dumps(
            {
                "parent_candidate_id": parent_id,
                "child_candidate_id": child_id,
            }
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "X-Roughcut-Token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
        handoff = json.load(response)
    return {
        "status": "revised",
        "parent_candidate_id": parent_id,
        "child_candidate_id": child_id,
        "revision_summary": child.to_dict()["revision_summary"],
        "displayed_candidate_id": handoff["draft_editor"]["candidate"]["candidate_id"],
    }


if __name__ == "__main__":
    main()
