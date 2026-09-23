from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest
from windows.w6_support import (
    ExportReviewFixture,
    build_persistent_runtime,
    make_av_source,
    seed_export_review_project,
)

from roughcut.application.nle_handoff import approve_nle_export
from roughcut.domain.project import ImportMode, SourceAsset

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="W7 requires native Windows drive and file-URI semantics",
)


def _resolved_source_path(project_path: Path, source: SourceAsset) -> Path:
    if source.import_mode is ImportMode.COPIED:
        relative = source.locator.get("project_relative_path")
        assert relative is not None
        return (project_path / Path(relative)).resolve()
    absolute = source.locator.get("absolute_path")
    assert absolute is not None
    return Path(absolute).resolve()


def _fcpxml_ticks(value: str) -> int:
    assert value.endswith("s")
    raw = value[:-1]
    fraction = Fraction(raw) if "/" in raw else Fraction(int(raw), 1)
    ticks = fraction * 120_000
    assert ticks.denominator == 1
    return ticks.numerator


def _assert_published_receipt(
    project_path: Path,
    action_id: str,
    destination: Path,
    output: bytes,
) -> Path:
    assert destination.is_file()
    receipt_path = project_path / "exports" / "handoffs" / "receipts" / f"{action_id}.json"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["output_bytes"] == len(output) == destination.stat().st_size
    assert receipt["output_sha256"] == hashlib.sha256(output).hexdigest()
    assert receipt["alignment_artifact_id"] is None
    return receipt_path


def _assert_xml_safety(
    output: bytes,
    *,
    destination: Path,
    receipt_path: Path,
) -> str:
    assert output.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    text = output.decode("utf-8")
    ET.fromstring(output)
    assert str(destination.resolve()) not in text
    assert destination.as_uri() not in text
    assert str(receipt_path.resolve()) not in text
    assert "\\" not in text
    assert "SECRET_W7_SENTINEL" not in text
    for marker in (".candidate", ".tmp", "/renders/", "/proxies/", "parallel"):
        assert marker.lower() not in text.lower()
    return text


def _seed_w7_fixture(tmp_path: Path) -> tuple[ExportReviewFixture, dict[Path, bytes]]:
    fixture_root = tmp_path / "W7 fixture 中文 with spaces"
    fixture_root.mkdir()
    media_root = fixture_root / "原始素材 Unicode with spaces"
    media_root.mkdir()
    source_a = media_root / "A 主机位 source & spaces.mp4"
    source_b = media_root / "B 副机位 source & spaces.mp4"
    make_av_source(source_a, color="red", frequency=440, duration=1.4)
    make_av_source(source_b, color="blue", frequency=660, duration=1.4)
    source_bytes = {source_a: source_a.read_bytes(), source_b: source_b.read_bytes()}
    fixture = seed_export_review_project(
        fixture_root / "project root 中文 with spaces",
        (source_a, source_b),
        import_modes=(ImportMode.COPIED, ImportMode.LINKED),
    )
    return fixture, source_bytes


@pytest.fixture
def persistent_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    runtime = build_persistent_runtime(tmp_path / "W7 persistent runtime")
    monkeypatch.setenv("ROUGHCUT_RUNTIME_BINDING", str(runtime))
    monkeypatch.delenv("ROUGHCUT_FFMPEG_COMMAND", raising=False)
    monkeypatch.delenv("ROUGHCUT_FFPROBE_COMMAND", raising=False)
    return runtime


@pytest.mark.parametrize(
    ("route", "suffix"),
    [("fcpxml", ".fcpxml"), ("fcp7_xml", ".xml")],
)
def test_w7_windows_publishes_shared_nle_artifact_for_each_route(
    tmp_path: Path,
    route: str,
    suffix: str,
    persistent_runtime: Path,
) -> None:
    del persistent_runtime
    fixture, source_bytes = _seed_w7_fixture(tmp_path)
    assert fixture.sources[0].import_mode is ImportMode.COPIED
    assert fixture.sources[1].import_mode is ImportMode.LINKED
    assert set(fixture.sources[0].locator) == {"project_relative_path"}
    copied_locator = fixture.sources[0].locator["project_relative_path"]
    assert not Path(copied_locator).is_absolute()
    assert not Path(copied_locator).drive
    assert set(fixture.sources[1].locator) == {"absolute_path"}
    linked_locator = Path(fixture.sources[1].locator["absolute_path"])
    assert linked_locator.is_absolute() and linked_locator.drive

    resolved_sources = tuple(
        _resolved_source_path(fixture.project_path, source) for source in fixture.sources
    )
    assert all(path.is_file() for path in resolved_sources)
    expected_source_urls = {path.as_uri() for path in resolved_sources}
    assert all(path.drive for path in resolved_sources)
    assert all(
        path.as_uri().startswith(f"file:///{path.drive}") for path in resolved_sources
    )
    assert "%20" in " ".join(expected_source_urls)
    assert any("%E" in url.upper() for url in expected_source_urls)

    output_root = tmp_path / "NLE 交付 output SECRET_W7_SENTINEL with spaces"
    output_root.mkdir()
    destination = output_root / f"Windows native {route} 导出{suffix}"
    action_id = f"act_w7_{route}"

    outcome = approve_nle_export(
        fixture.project_path,
        run_id=fixture.run_id,
        action_id=action_id,
        edit_version_id=_decision_id(fixture),
        expected_revision=fixture.revision,
        route=route,
        destination=destination,
        alignment_artifact_id=None,
    )
    output = destination.read_bytes()
    receipt_path = _assert_published_receipt(
        fixture.project_path,
        action_id,
        destination,
        output,
    )
    assert outcome.readback is False
    assert outcome.receipt.output_sha256 == hashlib.sha256(output).hexdigest()
    assert not list(output_root.glob(".*.tmp"))
    assert not list(fixture.project_path.rglob("*.tmp"))
    assert _source_bytes_unchanged(source_bytes)

    text = _assert_xml_safety(
        output,
        destination=destination,
        receipt_path=receipt_path,
    )
    root = ET.fromstring(output)
    if route == "fcpxml":
        _assert_fcpxml(root, expected_source_urls, fixture, output)
    else:
        _assert_fcp7_xml(root, expected_source_urls, fixture, output, text)


def _decision_id(fixture: ExportReviewFixture) -> str:
    decision_ref = json.loads(
        (fixture.project_path / "workflow" / "runs" / f"{fixture.run_id}.json").read_text(
            encoding="utf-8"
        )
    )["artifact_refs"]["decision"]
    return str(decision_ref["artifact_id"])


def _source_bytes_unchanged(source_bytes: dict[Path, bytes]) -> bool:
    return all(path.read_bytes() == before for path, before in source_bytes.items())


def _assert_fcpxml(
    root: ET.Element,
    expected_source_urls: set[str],
    fixture: ExportReviewFixture,
    output: bytes,
) -> None:
    assert root.tag == "fcpxml"
    assert root.attrib == {"version": "1.14"}
    sequence = root.find("./library/event/project/sequence")
    spine = root.find("./library/event/project/sequence/spine")
    assert sequence is not None and spine is not None
    main = spine.findall("asset-clip")
    assert len(main) == 3
    assert spine.findall("spine") == []
    assert root.findall(".//generator") == []
    assert root.findall(".//gap") == []

    expected_source_ids = [source.source_id for source in fixture.sources]
    expected_clip_source_ids = [
        expected_source_ids[0],
        expected_source_ids[1],
        expected_source_ids[0],
    ]
    assert [item.attrib["ref"].removeprefix("asset-") for item in main] == (
        expected_clip_source_ids
    )
    assert [item.attrib["offset"] for item in main] == ["0s", "3/5s", "6/5s"]
    assert [item.attrib["duration"] for item in main] == ["3/5s", "3/5s", "3/5s"]

    media_rep_urls = {
        item.attrib["src"] for item in root.findall("./resources/asset/media-rep")
    }
    assert media_rep_urls == expected_source_urls
    assert all(url.startswith("file:///") for url in media_rep_urls)
    assert all(item.attrib.get("format") for item in main)

    assets = {
        asset.attrib["id"]: asset for asset in root.findall("./resources/asset")
    }
    assert len(assets) == len(fixture.sources) == 2
    for source in fixture.sources:
        asset = assets[f"asset-{source.source_id}"]
        assert asset.attrib["uid"] == source.source_id
        assert asset.attrib["name"] == source.display_name
        assert asset.attrib["format"]
        assert asset.attrib["hasVideo"] == "1"
        assert asset.attrib["hasAudio"] == "1"
        assert asset.attrib["audioRate"] == "48000"
        media_rep = asset.find("media-rep")
        assert media_rep is not None
        assert media_rep.attrib["kind"] == "original-media"
        assert media_rep.attrib["src"] in expected_source_urls

    for index, item in enumerate(main):
        asset = assets[item.attrib["ref"]]
        source_start = _fcpxml_ticks(item.attrib["start"])
        duration = _fcpxml_ticks(item.attrib["duration"])
        source_duration = _fcpxml_ticks(asset.attrib["duration"])
        assert source_start >= 0
        assert source_start + duration <= source_duration
        if index == 2:
            assert source_start > 0
            assert source_start + duration < source_duration

    assert b"original-media" in output


def _assert_fcp7_xml(
    root: ET.Element,
    expected_source_urls: set[str],
    fixture: ExportReviewFixture,
    output: bytes,
    text: str,
) -> None:
    assert root.tag == "xmeml"
    assert root.attrib == {"version": "5"}
    sequence = root.find("sequence")
    assert sequence is not None
    video_tracks = sequence.findall("./media/video/track")
    audio_tracks = sequence.findall("./media/audio/track")
    assert len(video_tracks) == 1
    assert len(audio_tracks) == 1
    video_items = video_tracks[0].findall("clipitem")
    audio_items = audio_tracks[0].findall("clipitem")
    assert len(video_items) == len(audio_items) == 3

    expected_source_ids = [source.source_id for source in fixture.sources]
    expected_clip_source_ids = [
        expected_source_ids[0],
        expected_source_ids[1],
        expected_source_ids[0],
    ]
    assert [
        item.find("file").attrib["id"].removeprefix("file-") for item in video_items
    ] == expected_clip_source_ids
    assert [item.findtext("start") for item in video_items] == ["0", "15", "30"]
    assert [item.findtext("end") for item in video_items] == ["15", "30", "45"]
    assert [item.findtext("in") for item in video_items] == ["0", "0", "15"]
    assert [item.findtext("out") for item in video_items] == ["15", "15", "30"]

    for video_item, audio_item in zip(video_items, audio_items, strict=True):
        video_id = video_item.attrib["id"]
        audio_id = audio_item.attrib["id"]
        assert audio_id in {link.text for link in video_item.findall("link/linkclipref")}
        assert video_id in {link.text for link in audio_item.findall("link/linkclipref")}

    path_urls = {
        path.text
        for path in root.findall("./sequence/media/video/track/clipitem/file/pathurl")
    }
    assert path_urls == expected_source_urls
    assert all(url is not None and url.startswith("file:///") for url in path_urls)

    file_definitions = {
        file.attrib["id"].removeprefix("file-"): file
        for file in root.findall("./sequence/media/video/track/clipitem/file")
        if file.find("pathurl") is not None
    }
    assert set(file_definitions) == set(expected_source_ids)
    for source in fixture.sources:
        expected_url = _resolved_source_path(fixture.project_path, source).as_uri()
        file = file_definitions[source.source_id]
        assert file.findtext("name") == source.display_name
        assert file.findtext("pathurl") == expected_url
        assert file.findtext("rate/timebase") == "25"
        assert file.findtext("rate/ntsc") == "FALSE"
        assert int(file.findtext("duration", "0")) > 30
        characteristics = file.find("media/video/samplecharacteristics")
        assert characteristics is not None
        assert characteristics.findtext("width") == "160"
        assert characteristics.findtext("height") == "90"
        assert characteristics.findtext("rate/timebase") == "25"
        assert characteristics.findtext("rate/ntsc") == "FALSE"
        audio_characteristics = file.find("media/audio/samplecharacteristics")
        assert audio_characteristics is not None
        assert audio_characteristics.findtext("samplerate") == "48000"

    file_durations = {
        source_id: int(file.findtext("duration", "0"))
        for source_id, file in file_definitions.items()
    }
    last_file_id = video_items[-1].find("file").attrib["id"]
    source_duration = file_durations.get(last_file_id.removeprefix("file-"))
    assert source_duration is not None
    assert int(video_items[-1].findtext("in", "0")) > 0
    assert int(video_items[-1].findtext("out", "0")) < source_duration
    assert root.findall(".//generatoritem") == []
    assert b"parallel" not in output.lower()
    assert "sequence" in text
