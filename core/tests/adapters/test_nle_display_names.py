from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from nle_test_fixtures import build_timeline

from roughcut.adapters.fcp7_xml import write_fcp7_xml
from roughcut.adapters.fcpxml import write_fcpxml
from roughcut.domain.nle_handoff import NleHandoffClip


@pytest.mark.parametrize("route", ["fcpxml", "fcp7_xml"])
@pytest.mark.parametrize("multicam", [False, True], ids=["single", "main-and-auxiliary"])
@pytest.mark.parametrize("friendly_names", [False, True], ids=["original", "friendly"])
@pytest.mark.parametrize("long_identity", [False, True], ids=["short-id", "hashed-fcp7-id"])
def test_timeline_names_match_sources_without_changing_media_or_edit(
    tmp_path: Path,
    route: str,
    multicam: bool,
    friendly_names: bool,
    long_identity: bool,
) -> None:
    base = build_timeline(tmp_path, multicam=multicam)
    if long_identity:
        first = replace(base.main_track.clips[0], logical_clip_id="clip_" + "a" * 123)
        main = replace(base.main_track, items=(first, *base.main_track.clips[1:]))
        base = replace(base, tracks=(main, *base.auxiliary_tracks))
    names = {
        "source_a": '主机位 访谈 <第一场> & "嘉宾" | 精选',
        "source_b": '副机位 反应 <近景> & "观众" | 精选',
    }
    timeline = (
        replace(
            base,
            tracks=tuple(
                replace(
                    track,
                    items=tuple(
                        replace(item, source_display_name=names[item.source_id])
                        if isinstance(item, NleHandoffClip)
                        else item
                        for item in track.items
                    ),
                )
                for track in base.tracks
            ),
        )
        if friendly_names
        else base
    )
    before = timeline.to_dict()
    writer = write_fcpxml if route == "fcpxml" else write_fcp7_xml
    output = writer(timeline)
    root = ET.fromstring(output)
    baseline = ET.fromstring(writer(base))
    clips = [clip for track in timeline.tracks for clip in track.clips]

    if route == "fcpxml":
        assets = {asset.attrib["id"]: asset for asset in root.findall("./resources/asset")}
        items = root.findall(".//asset-clip")
        assert len(items) == len(clips)
        for clip in clips:
            note = f"{clip.logical_clip_id} | {clip.camera_id} | {clip.source_id}"
            matches = [item for item in items if item.findtext("note") == note]
            assert len(matches) == 1
            item = matches[0]
            assert item[0].tag == "note"  # DTD: note precedes connected asset-clips.
            assert list(item[0]) == []
            assert item.attrib["name"] == clip.source_display_name
            assert assets[item.attrib["ref"]].attrib["name"] == clip.source_display_name
        for tree in (root, baseline):
            for element in (*tree.findall(".//asset"), *tree.findall(".//asset-clip")):
                element.attrib.pop("name")
    else:
        files = {
            file.attrib["id"]: file
            for file in root.findall(".//file")
            if file.find("name") is not None
        }
        items = root.findall(".//clipitem")
        assert len(items) == len(clips) * 2  # Original A/V clipitems remain linked.
        for clip in clips:
            note = f"{clip.logical_clip_id} | {clip.camera_id} | {clip.source_id}"
            matches = [item for item in items if item.findtext("logginginfo/lognote") == note]
            assert len(matches) == 2
            for item in matches:
                assert len(item.findall("logginginfo/lognote")) == 1
                assert item.findtext("name") == clip.source_display_name
                file = item.find("file")
                assert file is not None
                assert files[file.attrib["id"]].findtext("name") == clip.source_display_name
        for tree in (root, baseline):
            for element in (*tree.findall(".//clipitem/name"), *tree.findall(".//file/name")):
                element.text = None

    # Renaming only changes display fields: paths, references, IDs, links, rates,
    # timing, track/lane structure and the trace notes must remain identical.
    assert ET.tostring(root) == ET.tostring(baseline)
    assert timeline.to_dict() == before
    assert writer(timeline) == output
