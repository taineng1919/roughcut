from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest
from nle_test_fixtures import (
    FCPXML_FORENSIC_CASES,
    build_fcpxml_forensic_timeline,
    build_partial_connected_child_timeline,
    build_timeline,
)

from roughcut.adapters.fcp7_xml import write_fcp7_xml
from roughcut.adapters.fcpxml import write_fcpxml
from roughcut.adapters.nle_time import fcpxml_time, project_fcpxml_timing
from roughcut.domain.nle_handoff import NleHandoffClip, NleHandoffError
from roughcut.domain.time import TICKS_PER_SECOND

ORIGIN = 5_513_332_800  # 12:45:44:22 at 50fps, expressed on the Core tick clock.


@pytest.mark.parametrize(
    "parent_origin,child_origin", [(ORIGIN, 0), (ORIGIN, ORIGIN + 2400), (0, ORIGIN)]
)
@pytest.mark.parametrize("case", ["repeated", "partial", "half-frame"])
def test_source_origins_only_translate_source_and_parent_coordinates(
    tmp_path: Path,
    parent_origin: int,
    child_origin: int,
    case: str,
) -> None:
    if case == "repeated":
        base = build_timeline(tmp_path)
    elif case == "partial":
        base = build_partial_connected_child_timeline(tmp_path)
    else:
        base = build_fcpxml_forensic_timeline(tmp_path, FCPXML_FORENSIC_CASES[0])
        child = base.auxiliary_tracks[0].clips[-1]
        child = replace(child, source_in_ticks=19_825_200, source_out_ticks=20_031_600)
        base = replace(
            base, tracks=(base.main_track, replace(base.auxiliary_tracks[0], items=(child,)))
        )
    origins = {
        base.main_track.clips[0].source_id: parent_origin,
        base.auxiliary_tracks[0].clips[0].source_id: child_origin,
    }
    timeline = replace(
        base,
        tracks=tuple(
            replace(
                track,
                items=tuple(
                    replace(item, source_timecode_origin_ticks=origins[item.source_id])
                    if isinstance(item, NleHandoffClip)
                    else item
                    for item in track.items
                ),
            )
            for track in base.tracks
        ),
    )
    assert project_fcpxml_timing(timeline) == project_fcpxml_timing(base)
    assert timeline.to_dict() == base.to_dict()  # Transient facts do not alter canonical truth.
    root = ET.fromstring(write_fcpxml(timeline))
    assets = {asset.attrib["id"]: asset for asset in root.findall("./resources/asset")}
    for asset in assets.values():
        origin = origins[asset.attrib["uid"]]
        assert Fraction(asset.attrib["start"][:-1]) == Fraction(origin, TICKS_PER_SECOND)
        asset.attrib["start"] = "0s"

    def normalize(item: ET.Element, parent_origin_ticks: int = 0) -> None:
        origin = origins[assets[item.attrib["ref"]].attrib["uid"]]
        start = Fraction(item.attrib["start"][:-1]) * TICKS_PER_SECOND
        offset = Fraction(item.attrib["offset"][:-1]) * TICKS_PER_SECOND
        item.attrib["start"] = fcpxml_time(int(start) - origin)
        item.attrib["offset"] = fcpxml_time(int(offset) - parent_origin_ticks)
        for child in item.findall("asset-clip"):
            normalize(child, origin)

    for item in root.findall("./library/event/project/sequence/spine/asset-clip"):
        normalize(item)
    # After removing only origins, the complete document matches: all cut ranges,
    # duration, formats, paths, names, IDs, lane placement and ordering are intact.
    assert ET.tostring(root) == ET.tostring(ET.fromstring(write_fcpxml(base)))
    if case == "repeated":
        assert write_fcp7_xml(timeline) == write_fcp7_xml(base)


@pytest.mark.parametrize("origin", [-1, True, 1.5, "100"])
def test_malformed_transient_origin_is_rejected(tmp_path: Path, origin: object) -> None:
    clip = build_timeline(tmp_path).main_track.clips[0]
    with pytest.raises(NleHandoffError, match="source_timecode_origin_ticks"):
        replace(clip, source_timecode_origin_ticks=origin)


def test_repeated_source_origin_must_be_consistent(tmp_path: Path) -> None:
    base = build_timeline(tmp_path, multicam=False)
    first, *rest = base.main_track.clips
    with pytest.raises(NleHandoffError, match="inconsistent metadata"):
        replace(
            base,
            tracks=(
                replace(
                    base.main_track,
                    items=(
                        replace(first, source_timecode_origin_ticks=ORIGIN),
                        *rest,
                    ),
                ),
            ),
        )
