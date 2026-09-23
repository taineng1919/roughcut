from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from roughcut.adapters.nle_export_store import NleExportStore
from roughcut.domain.nle_handoff import NleExportReceipt, NleHandoffError, NleSourceSnapshot
from roughcut.domain.project import SourceFingerprint


def _receipt(destination: Path) -> NleExportReceipt:
    payload = b'<fcpxml version="1.14" />\n'
    return NleExportReceipt(
        schema_version=1,
        action_id="act_nle",
        request_hash="a" * 64,
        project_id="project_nle",
        run_id="run_nle",
        project_revision=3,
        edit_version_id="edit_nle",
        decision_schema_version=1,
        decision_content_hash="b" * 64,
        route="fcpxml",
        exporter_profile="roughcut_fcpxml_1_14",
        destination=str(destination),
        source_snapshots=(
            NleSourceSnapshot(
                source_id="source_a",
                snapshot_hash="c" * 64,
                locator_hash="d" * 64,
                fingerprint=SourceFingerprint(1, 1, "e" * 64),
                duration_ticks=120_000,
            ),
        ),
        alignment_artifact_id=None,
        alignment_content_hash=None,
        output_sha256=hashlib.sha256(payload).hexdigest(),
        output_bytes=len(payload),
        created_at="fixture",
    )


def test_nle_receipt_store_is_immutable_and_canonical(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    destination = tmp_path / "handoff.fcpxml"
    payload = b'<fcpxml version="1.14" />\n'
    destination.write_bytes(payload)
    receipt = _receipt(destination)
    store = NleExportStore(root)

    stored = store.write(receipt)
    assert store.read("act_nle") == receipt
    store.validate_output(stored)
    assert (root / "exports/handoffs/receipts/act_nle.json").is_file()

    different = replace(receipt, request_hash="f" * 64)
    with pytest.raises(NleHandoffError) as error:
        store.write(different)
    assert error.value.code == "nle_export_action_conflict"


def test_nle_receipt_store_rejects_changed_output(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    destination = tmp_path / "handoff.fcpxml"
    destination.write_bytes(b'<fcpxml version="1.14" />\n')
    receipt = _receipt(destination)
    store = NleExportStore(root)
    store.write(receipt)
    destination.write_bytes(b"changed")

    with pytest.raises(NleHandoffError) as error:
        store.validate_output(receipt)
    assert error.value.code == "nle_export_integrity_error"
