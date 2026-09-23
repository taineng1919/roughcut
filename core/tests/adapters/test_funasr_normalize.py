from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from roughcut.adapters.funasr.normalize import (
    FUNASR_TIMESTAMP_QUANTUM_TICKS,
    TranscriptNormalizationError,
    normalize_funasr,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "fixtures" / "asr"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def normalize_synthetic(
    raw: object,
    *,
    source_duration_ticks: int = 240_000,
    parameters: dict[str, object] | None = None,
):
    return normalize_funasr(
        raw,
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=source_duration_ticks,
        package_version="1.3.14",
        models={"asr": "fixture"},
        parameters=parameters or {},
    )


def test_normalizes_sentence_timestamps_speakers_and_real_fine_units() -> None:
    transcript = normalize_funasr(
        load_fixture("funasr_sentence_info.json"),
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=360_000,
        package_version="1.3.8",
        models={"asr": "fixture-asr"},
        parameters={"sentence_timestamp": True},
    )

    assert [segment.segment_id for segment in transcript.segments] == ["seg_000001", "seg_000002"]
    assert [(segment.start_ticks, segment.end_ticks) for segment in transcript.segments] == [
        (12_000, 96_000),
        (144_000, 264_000),
    ]
    assert [segment.local_speaker_id for segment in transcript.segments] == ["spk_0", "spk_1"]
    assert [unit.text for unit in transcript.segments[0].fine_units] == ["第一", "句", "。"]
    assert all(unit.kind == "token" for unit in transcript.segments[0].fine_units)


def test_clamps_exact_final_ten_millisecond_overrun_without_mutating_raw() -> None:
    raw = [
        {
            "text": "甲乙",
            "sentence_info": [
                {"text": "甲", "start": 0, "end": 1000, "spk": 0},
                {"text": "乙", "start": 1000, "end": 2010, "spk": 1},
            ],
        }
    ]
    before = deepcopy(raw)

    transcript = normalize_synthetic(raw)

    assert FUNASR_TIMESTAMP_QUANTUM_TICKS == 1200
    assert transcript.segments[-1].end_ticks == 240_000
    assert transcript.segments[-1].start_ticks == 120_000
    assert transcript.segments[-1].original_text == "乙"
    assert transcript.segments[-1].local_speaker_id == "spk_1"
    assert raw == before


def test_final_ten_millisecond_overrun_preserves_enabled_speaker() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲",
                "sentence_info": [
                    {"text": "甲", "start": 0, "end": 2010, "spk": 2}
                ],
            }
        ],
        parameters={"speaker_diarization": {"enabled": True}},
    )

    assert transcript.segments[0].local_speaker_id == "spk_2"
    assert transcript.segments[0].end_ticks == 240_000


def test_clamps_only_the_last_fine_unit_that_shares_final_overrun() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙",
                "sentence_info": [
                    {
                        "text": "甲乙",
                        "raw_text": "甲 乙",
                        "start": 1000,
                        "end": 2010,
                        "timestamp": [[1000, 1500], [1500, 2010]],
                    }
                ],
            }
        ]
    )

    units = transcript.segments[0].fine_units
    assert [(unit.start_ticks, unit.end_ticks) for unit in units] == [
        (120_000, 180_000),
        (180_000, 240_000),
    ]
    assert all(
        segment_start <= unit.start_ticks < unit.end_ticks <= segment_end
        for segment_start, segment_end in [
            (transcript.segments[0].start_ticks, transcript.segments[0].end_ticks)
        ]
        for unit in units
    )


def test_unsafe_final_fine_unit_clamp_falls_back_to_empty_units() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙",
                "sentence_info": [
                    {
                        "text": "甲乙",
                        "raw_text": "甲 乙",
                        "start": 1000,
                        "end": 2010,
                        "timestamp": [[1000, 1500], [2000, 2010]],
                    }
                ],
            }
        ]
    )

    assert transcript.segments[0].fine_units == ()


def test_overrun_above_fun_asr_timestamp_quantum_still_fails_closed() -> None:
    with pytest.raises(
        TranscriptNormalizationError,
        match="FunASR segment exceeds source duration",
    ):
        normalize_synthetic(
            [
                {
                    "text": "甲",
                    "sentence_info": [
                        {"text": "甲", "start": 0, "end": 2010.01}
                    ],
                }
            ]
        )


def test_non_final_segment_overrun_still_fails_closed() -> None:
    with pytest.raises(
        TranscriptNormalizationError,
        match="FunASR segment exceeds source duration",
    ):
        normalize_synthetic(
            [
                {
                    "text": "甲乙",
                    "sentence_info": [
                        {"text": "甲", "start": 0, "end": 2010},
                        {"text": "乙", "start": 2010, "end": 2200},
                    ],
                }
            ]
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [(2000, 2010), (2000.01, 2000.5)],
)
def test_final_segment_start_at_or_after_source_duration_still_fails_closed(
    start: float,
    end: float,
) -> None:
    with pytest.raises(
        TranscriptNormalizationError,
        match="FunASR segment exceeds source duration",
    ):
        normalize_synthetic(
            [
                {
                    "text": "甲",
                    "sentence_info": [{"text": "甲", "start": start, "end": end}],
                }
            ]
        )


def test_normal_fixture_output_is_unchanged() -> None:
    transcript = normalize_funasr(
        load_fixture("funasr_sentence_info.json"),
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=360_000,
        package_version="1.3.8",
        models={"asr": "fixture-asr"},
        parameters={"sentence_timestamp": True},
    )

    assert transcript.to_dict() == {
        "language": "zh-CN",
        "parent_version_id": None,
        "provenance": {
            "backend": "funasr",
            "completed_at": "unknown",
            "exit_status": 0,
            "models": {"asr": "fixture-asr"},
            "package_version": "1.3.8",
            "parameters": {"sentence_timestamp": True},
            "raw_result_path": "raw-asr/src_fixture/run.json",
            "started_at": "unknown",
        },
        "schema_version": 1,
        "segments": [
            {
                "confidence": None,
                "corrected_text": None,
                "editorial_mark": "unmarked",
                "end_ticks": 96_000,
                "fine_units": [
                    {
                        "confidence": None,
                        "end_ticks": 36_000,
                        "kind": "token",
                        "start_ticks": 12_000,
                        "text": "第一",
                    },
                    {
                        "confidence": None,
                        "end_ticks": 60_000,
                        "kind": "token",
                        "start_ticks": 36_000,
                        "text": "句",
                    },
                    {
                        "confidence": None,
                        "end_ticks": 96_000,
                        "kind": "token",
                        "start_ticks": 60_000,
                        "text": "。",
                    },
                ],
                "local_speaker_id": "spk_0",
                "original_text": "第一句。",
                "person_id": None,
                "segment_id": "seg_000001",
                "start_ticks": 12_000,
            },
            {
                "confidence": None,
                "corrected_text": None,
                "editorial_mark": "unmarked",
                "end_ticks": 264_000,
                "fine_units": [
                    {
                        "confidence": None,
                        "end_ticks": 180_000,
                        "kind": "token",
                        "start_ticks": 144_000,
                        "text": "第二",
                    },
                    {
                        "confidence": None,
                        "end_ticks": 216_000,
                        "kind": "token",
                        "start_ticks": 180_000,
                        "text": "句",
                    },
                    {
                        "confidence": None,
                        "end_ticks": 264_000,
                        "kind": "token",
                        "start_ticks": 216_000,
                        "text": "！",
                    },
                ],
                "local_speaker_id": "spk_1",
                "original_text": "第二句！",
                "person_id": None,
                "segment_id": "seg_000002",
                "start_ticks": 144_000,
            },
        ],
        "source_id": "src_fixture",
        "transcript_version_id": "tr_fixture",
    }


@pytest.mark.parametrize("speaker", [True, -1, "", "../../person", [], {}])
def test_rejects_invalid_sentence_speaker_values(speaker: object) -> None:
    raw = [
        {
            "text": "原始结果。",
            "sentence_info": [
                {
                    "text": "原始结果。",
                    "start": 0,
                    "end": 800,
                    "spk": speaker,
                }
            ],
        }
    ]

    with pytest.raises(TranscriptNormalizationError, match="speaker") as error:
        normalize_funasr(
            raw,
            source_id="src_fixture",
            transcript_version_id="tr_fixture",
            raw_result_path="raw-asr/src_fixture/run.json",
            source_duration_ticks=120_000,
            package_version="1.3.8",
            models={"asr": "fixture"},
            parameters={"speaker_diarization": {"enabled": True}},
        )

    assert error.value.raw_result_path == "raw-asr/src_fixture/run.json"


def test_disabled_speaker_mode_preserves_legacy_null_speaker_behavior() -> None:
    transcript = normalize_funasr(
        [
            {
                "text": "未启用说话人。",
                "sentence_info": [
                    {
                        "text": "未启用说话人。",
                        "start": 0,
                        "end": 800,
                        "spk": None,
                    }
                ],
            }
        ],
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=120_000,
        package_version="1.3.8",
        models={"asr": "fixture"},
        parameters={"speaker_diarization": {"enabled": False}},
    )

    assert transcript.segments[0].local_speaker_id is None


def test_normalizes_only_top_level_entries_with_real_boundaries() -> None:
    transcript = normalize_funasr(
        load_fixture("funasr_top_level.json"),
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=240_000,
        package_version="1.3.8",
        models={},
        parameters={},
    )

    assert [segment.original_text for segment in transcript.segments] == ["顶层片段一。", "顶层片段二。"]
    assert transcript.segments[0].fine_units == ()
    assert transcript.segments[0].local_speaker_id is None


def test_text_tn_is_used_only_when_the_entries_have_real_segment_boundaries() -> None:
    transcript = normalize_funasr(
        load_fixture("funasr_text_tn.json"),
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=240_000,
        package_version="1.3.8",
        models={},
        parameters={},
    )

    assert [segment.original_text for segment in transcript.segments] == [
        "二零二六年。",
        "标准化文本。",
    ]
    assert [unit.text for unit in transcript.segments[0].fine_units] == ["二零", "二六", "年", "。"]


def test_top_level_timestamps_are_split_into_real_short_boundaries_when_tokens_align() -> None:
    text = "一二三四五六七八九十甲乙"
    transcript = normalize_funasr(
        [
            {
                "text": text,
                "raw_text": " ".join(text),
                "timestamp": [[index * 1000, (index + 1) * 1000] for index in range(len(text))],
                "sentence_info": [],
            }
        ],
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=1_440_000,
        package_version="1.3.8",
        models={},
        parameters={},
    )

    assert [(segment.start_ticks, segment.end_ticks) for segment in transcript.segments] == [
        (0, 960_000),
        (960_000, 1_440_000),
    ]
    assert [segment.original_text for segment in transcript.segments] == ["一二三四五六七八", "九十甲乙"]


def test_long_sentence_info_preserves_its_text_and_real_boundaries() -> None:
    tokens = [f"token{index}" for index in range(35)]
    text = "这是完整句子，标点必须保留！"
    transcript = normalize_funasr(
        [
            {
                "text": text,
                "sentence_info": [
                    {
                        "text": text,
                        "raw_text": " ".join(tokens),
                        "start": 100,
                        "end": 3600,
                        "timestamp": [
                            [100 + index * 100, 200 + index * 100]
                            for index in range(len(tokens))
                        ],
                    }
                ],
            }
        ],
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=500_000,
        package_version="1.3.8",
        models={},
        parameters={},
    )

    assert len(transcript.segments) == 1
    assert transcript.segments[0].original_text == text
    assert (transcript.segments[0].start_ticks, transcript.segments[0].end_ticks) == (
        12_000,
        432_000,
    )
    assert [unit.text for unit in transcript.segments[0].fine_units] == tokens


def test_unspaced_chinese_raw_text_preserves_real_character_timestamps_as_tokens() -> None:
    transcript = normalize_funasr(
        [
            {
                "text": "测试文本，",
                "sentence_info": [
                    {
                        "text": "测试文本，",
                        "raw_text": "测试文本",
                        "start": 100,
                        "end": 900,
                        "timestamp": [[100, 250], [250, 450], [450, 650], [650, 900]],
                    }
                ],
            }
        ],
        source_id="src_fixture",
        transcript_version_id="tr_fixture",
        raw_result_path="raw-asr/src_fixture/run.json",
        source_duration_ticks=120_000,
        package_version="1.3.8",
        models={},
        parameters={},
    )

    assert [unit.text for unit in transcript.segments[0].fine_units] == ["测", "试", "文", "本"]
    assert [(unit.start_ticks, unit.end_ticks) for unit in transcript.segments[0].fine_units] == [
        (12_000, 30_000),
        (30_000, 54_000),
        (54_000, 78_000),
        (78_000, 108_000),
    ]


@pytest.mark.parametrize(
    "bad_item",
    [
        None,
        {"text": "缺少边界"},
        {"start": 900, "end": 1200},
    ],
)
def test_nonempty_sentence_info_fails_if_any_item_is_malformed(bad_item: object) -> None:
    raw_path = "raw-asr/src_fixture/preserved.json"
    with pytest.raises(TranscriptNormalizationError, match="sentence_info") as error:
        normalize_funasr(
            [
                {
                    "text": "不得回退到这段顶层文本",
                    "raw_text": "不 得 回 退",
                    "timestamp": [[0, 100], [100, 200], [200, 300], [300, 400]],
                    "sentence_info": [
                        {"text": "有效句。", "start": 0, "end": 800},
                        bad_item,
                    ],
                }
            ],
            source_id="src_fixture",
            transcript_version_id="tr_fixture",
            raw_result_path=raw_path,
            source_duration_ticks=240_000,
            package_version="1.3.8",
            models={},
            parameters={},
        )

    assert error.value.raw_result_path == raw_path


def test_non_list_sentence_info_does_not_silently_use_top_level_fallback() -> None:
    raw_path = "raw-asr/src_fixture/preserved.json"
    with pytest.raises(TranscriptNormalizationError, match="sentence_info") as error:
        normalize_funasr(
            [
                {
                    "text": "不得回退",
                    "raw_text": "不 得 回 退",
                    "timestamp": [[0, 100], [100, 200], [200, 300], [300, 400]],
                    "sentence_info": {"text": "错误结构"},
                }
            ],
            source_id="src_fixture",
            transcript_version_id="tr_fixture",
            raw_result_path=raw_path,
            source_duration_ticks=240_000,
            package_version="1.3.8",
            models={},
            parameters={},
        )

    assert error.value.raw_result_path == raw_path


@pytest.mark.parametrize(
    "raw, message",
    [
        ([], "no timed segments"),
        (load_fixture("funasr_unknown.json"), "no timed segments"),
        (
            [{"text": "整段文本不得伪装成句段", "timestamp": [[0, 300], [300, 900]]}],
            "no timed segments",
        ),
        (
            [
                {"text": "后段", "start": 1000, "end": 2000},
                {"text": "倒退", "start": 500, "end": 900},
            ],
            "not monotonic",
        ),
        ([{"text": "越界", "start": 0, "end": 3000}], "exceeds source duration"),
    ],
)
def test_unknown_nonmonotonic_and_out_of_bounds_results_fail_explicitly(
    raw: object, message: str
) -> None:
    with pytest.raises(TranscriptNormalizationError, match=message) as error:
        normalize_funasr(
            raw,
            source_id="src_fixture",
            transcript_version_id="tr_fixture",
            raw_result_path="raw-asr/src_fixture/preserved.json",
            source_duration_ticks=240_000,
            package_version="1.3.8",
            models={},
            parameters={},
        )

    assert error.value.raw_result_path == "raw-asr/src_fixture/preserved.json"


def test_report_shape_370_entries_final_ten_ms_synthetic_equivalent() -> None:
    """脱敏回归：上传报告 370 entries、source_duration 100051200、末句溢出 1200 ticks 的等价形状。

    用户提供的脱敏故障报告仅含时码摘要、未提供 raw 文件或媒体路径，
    因此不猜测真实项目路径、不读取真实媒体，改用合成 370 条
    sentence_info 复现相同边界：前 369 条在界内，最后一条 start < source_duration 且
    end = source_duration + 10ms，期望 clamp 成功且保留 speaker，raw 输入不被修改。
    """
    source_duration = 100051200  # 来自报告的 source_duration_ticks
    quantum = FUNASR_TIMESTAMP_QUANTUM_TICKS
    assert quantum == 1200
    entries: list[dict[str, object]] = []
    tick_duration = 240_000  # 2s per segment
    for i in range(369):
        start_ms = (i * 2000)
        end_ms = start_ms + 2000
        entries.append({"text": f"句{i}", "start": start_ms, "end": end_ms, "spk": 0})
    # Last segment: start within duration, end = duration + 10ms
    last_start_ticks = 369 * tick_duration
    last_start_ms = last_start_ticks // 120
    last_end_ms = (source_duration // 120) + 10
    entries.append(
        {
            "text": "主要是这些这个问题作业而已。",
            "start": last_start_ms,
            "end": last_end_ms,
            "spk": 1,
        }
    )
    raw = [{"text": "合成", "sentence_info": entries}]
    before = deepcopy(raw)
    transcript = normalize_synthetic(
        raw,
        source_duration_ticks=source_duration,
        parameters={"speaker_diarization": {"enabled": True}},
    )
    assert len(transcript.segments) == 370
    assert transcript.segments[-1].end_ticks == source_duration
    assert transcript.segments[-1].start_ticks == last_start_ticks
    assert transcript.segments[-1].local_speaker_id == "spk_1"
    assert raw == before
    # Diarization false 同样成功
    transcript2 = normalize_synthetic(
        raw,
        source_duration_ticks=source_duration,
        parameters={"speaker_diarization": {"enabled": False}},
    )
    assert transcript2.segments[-1].end_ticks == source_duration


def test_final_fine_unit_ten_ms_clamp_without_segment_overrun() -> None:
    """最后 fine unit 独立 10ms：segment 已贴合 source_duration，仅末 token 超 10ms 应 clamp。"""
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙",
                "sentence_info": [
                    {
                        "text": "甲乙",
                        "raw_text": "甲 乙",
                        "start": 1000,
                        "end": 2000,
                        "timestamp": [[1000, 1500], [1500, 2010]],
                    }
                ],
            }
        ],
        source_duration_ticks=240_000,
        parameters={},
    )
    units = transcript.segments[0].fine_units
    assert len(units) == 2
    assert units[0].start_ticks == 120_000
    assert units[0].end_ticks == 180_000
    assert units[1].start_ticks == 180_000
    assert units[1].end_ticks == 240_000
    assert transcript.segments[0].end_ticks == 240_000


def test_final_fine_unit_overrun_above_quantum_still_empty() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙",
                "sentence_info": [
                    {
                        "text": "甲乙",
                        "raw_text": "甲 乙",
                        "start": 1000,
                        "end": 2000,
                        "timestamp": [[1000, 1500], [1500, 2010.01]],
                    }
                ],
            }
        ],
        source_duration_ticks=240_000,
        parameters={},
    )
    assert transcript.segments[0].fine_units == ()


def test_non_last_fine_unit_overrun_does_not_clamp() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙丙",
                "sentence_info": [
                    {
                        "text": "甲乙丙",
                        "raw_text": "甲 乙 丙",
                        "start": 0,
                        "end": 2000,
                        "timestamp": [[0, 500], [500, 2010], [2010, 2500]],
                    }
                ],
            }
        ],
        source_duration_ticks=300_000,
        parameters={},
    )
    # 非最后 fine unit 超界（第二个 token 即使仅超 10ms）仍应回退空
    assert transcript.segments[0].fine_units == ()


def test_non_last_segment_fine_unit_overrun_does_not_clamp() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "甲乙",
                "sentence_info": [
                    {
                        "text": "甲",
                        "raw_text": "甲 乙",
                        "start": 0,
                        "end": 1000,
                        "timestamp": [[0, 500], [500, 1010]],
                    },
                    {"text": "乙", "start": 1000, "end": 2000},
                ],
            }
        ],
        source_duration_ticks=240_000,
        parameters={},
    )
    # 首段虽为最后 fine unit 但非最后 segment，仍不 clamp
    assert transcript.segments[0].fine_units == ()


@pytest.mark.parametrize("speaker_enabled", [False, True])
def test_adjacent_twenty_millisecond_overlap_is_local_and_raw_stays_unchanged(
    speaker_enabled: bool,
) -> None:
    raw = [
        {
            "text": "合成",
            "sentence_info": [
                {"text": "甲", "start": 0, "end": 1000, "spk": 0},
                {"text": "乙", "start": 980, "end": 1500, "spk": 1},
                {"text": "丙", "start": 2000, "end": 2500, "spk": 2},
            ],
        }
    ]
    before = deepcopy(raw)

    transcript = normalize_synthetic(
        raw,
        source_duration_ticks=360_000,
        parameters={"speaker_diarization": {"enabled": speaker_enabled}},
    )

    assert [(segment.start_ticks, segment.end_ticks) for segment in transcript.segments] == [
        (0, 117_600),
        (117_600, 180_000),
        (240_000, 300_000),
    ]
    assert [segment.local_speaker_id for segment in transcript.segments] == [
        "spk_0",
        "spk_1",
        "spk_2",
    ]
    assert raw == before


def test_adjacent_overlap_uses_token_gap_and_preserves_both_fine_unit_sets() -> None:
    raw = [
        {
            "text": "合成",
            "sentence_info": [
                {
                    "text": "甲乙",
                    "raw_text": "甲 乙",
                    "start": 0,
                    "end": 500,
                    "timestamp": [[0, 400], [400, 490]],
                },
                {
                    "text": "丙丁",
                    "raw_text": "丙 丁",
                    "start": 480,
                    "end": 700,
                    "timestamp": [[500, 540], [540, 700]],
                },
            ],
        }
    ]
    before = deepcopy(raw)

    transcript = normalize_synthetic(raw)

    # The left token edge is the earliest safe point in the 20ms raw overlap;
    # the right derived start follows it without changing later raw starts.
    assert [(segment.start_ticks, segment.end_ticks) for segment in transcript.segments] == [
        (0, 58_800),
        (58_800, 84_000),
    ]
    assert [
        (unit.start_ticks, unit.end_ticks) for unit in transcript.segments[0].fine_units
    ] == [(0, 48_000), (48_000, 58_800)]
    assert [
        (unit.start_ticks, unit.end_ticks) for unit in transcript.segments[1].fine_units
    ] == [(60_000, 64_800), (64_800, 84_000)]
    assert raw == before


def test_adjacent_overlap_crossing_fine_unit_degrades_only_that_segment_units() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "合成",
                "sentence_info": [
                    {
                        "text": "甲乙",
                        "raw_text": "甲 乙",
                        "start": 0,
                        "end": 500,
                        "timestamp": [[0, 450], [450, 500]],
                    },
                    {
                        "text": "丙丁",
                        "raw_text": "丙 丁",
                        "start": 480,
                        "end": 700,
                        "timestamp": [[480, 520], [520, 700]],
                    },
                ],
            }
        ]
    )

    assert transcript.segments[0].end_ticks == 57_600
    assert transcript.segments[0].fine_units == ()
    assert all(
        segment.start_ticks <= unit.start_ticks < unit.end_ticks <= segment.end_ticks
        for segment in transcript.segments
        for unit in segment.fine_units
    )


@pytest.mark.parametrize("overlap_milliseconds", [20.01, 100])
def test_adjacent_overlap_above_twenty_milliseconds_fails_closed(
    overlap_milliseconds: float,
) -> None:
    with pytest.raises(TranscriptNormalizationError, match="not monotonic"):
        normalize_synthetic(
            [
                {
                    "text": "合成",
                    "sentence_info": [
                        {"text": "甲", "start": 0, "end": 1000},
                        {
                            "text": "乙",
                            "start": 1000 - overlap_milliseconds,
                            "end": 1500,
                        },
                    ],
                }
            ]
        )


def test_adjacent_overlap_chain_fails_closed_without_accumulating_repairs() -> None:
    with pytest.raises(TranscriptNormalizationError, match="not monotonic"):
        normalize_synthetic(
            [
                {
                    "text": "合成",
                    "sentence_info": [
                        {"text": "甲", "start": 0, "end": 1000},
                        {"text": "乙", "start": 980, "end": 1500},
                        {"text": "丙", "start": 1480, "end": 2000},
                    ],
                }
            ]
        )


def test_multiple_non_adjacent_overlaps_do_not_shift_the_middle_segment() -> None:
    transcript = normalize_synthetic(
        [
            {
                "text": "合成",
                "sentence_info": [
                    {"text": "甲", "start": 0, "end": 1000},
                    {"text": "乙", "start": 980, "end": 1500},
                    {"text": "丙", "start": 1500, "end": 2000},
                    {"text": "丁", "start": 1980, "end": 2400},
                ],
            }
        ],
        source_duration_ticks=360_000,
    )

    assert [(segment.start_ticks, segment.end_ticks) for segment in transcript.segments] == [
        (0, 117_600),
        (117_600, 180_000),
        (180_000, 237_600),
        (237_600, 288_000),
    ]
