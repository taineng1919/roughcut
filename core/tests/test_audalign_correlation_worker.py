"""Direct tests for the pinned Audalign Correlation worker raw contract."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from roughcut.adapters.audalign import correlation_worker as worker
from roughcut.domain.alignment import audalign_correlation_typed_config


def _raw_result(
    *,
    basename: str = "main.wav",
    offsets: list[float] | None = None,
) -> dict[str, object]:
    values = [0.0, 0.125] if offsets is None else offsets
    count = len(values)
    entry = {
        "locality_samples": [None] * count,
        "offset_samples": [0, 1000][:count],
        "locality_seconds": [None] * count,
        "offset_seconds": values,
        "confidence": [0.5] * count,
        "sample_rate": 8000,
        "scaling_factor": 0.25,
    }
    return {
        "match_time": 0.01,
        "match_info": {basename: entry},
        "rankings": {"match_info": {basename: 8}},
    }


def test_worker_validates_real_raw_shape_and_emits_minimal_payload() -> None:
    payload = worker._minimal_payload_from_raw(
        _raw_result(), against_path="/var/tmp/main.wav", expected_sample_rate=8000
    )

    assert payload == {
        "match_info": {
            "offset_seconds": [0.0, 0.125],
            "sample_rate": 8000,
        }
    }
    assert "confidence" not in str(payload)
    assert "rankings" not in payload


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda raw: raw.pop("rankings"), id="missing-root-field"),
        pytest.param(lambda raw: raw.__setitem__("extra", None), id="extra-root-field"),
        pytest.param(
            lambda raw: raw["match_info"].__setitem__("other.wav", raw["match_info"]["main.wav"]),
            id="unknown-basename",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("extra", None),
            id="extra-entry-field",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].pop("confidence"),
            id="missing-entry-field",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("offset_seconds", 0),
            id="wrong-offset-type",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("offset_samples", [0]),
            id="mismatched-offset-length",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("confidence", [0.5]),
            id="mismatched-confidence-length",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("locality_samples", [0, None]),
            id="malformed-locality",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("sample_rate", 44100),
            id="wrong-sample-rate",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("scaling_factor", float("nan")),
            id="nonfinite-scaling",
        ),
        pytest.param(lambda raw: raw.__setitem__("match_time", float("inf")), id="nonfinite-time"),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("offset_seconds", [float("nan"), 0.1]),
            id="nonfinite-offset",
        ),
        pytest.param(
            lambda raw: raw["match_info"]["main.wav"].__setitem__("confidence", [0.5, float("inf")]),
            id="nonfinite-confidence",
        ),
        pytest.param(
            lambda raw: raw["rankings"].__setitem__("extra", 1),
            id="extra-ranking-field",
        ),
        pytest.param(
            lambda raw: raw["rankings"].__setitem__("match_info", {"other.wav": 8}),
            id="ranking-basename-mismatch",
        ),
        pytest.param(
            lambda raw: raw["rankings"]["match_info"].__setitem__("main.wav", "8"),
            id="ranking-wrong-type",
        ),
    ],
)
def test_worker_rejects_raw_shape_drift(mutate) -> None:
    raw = _raw_result()
    mutate(raw)
    with pytest.raises(RuntimeError):
        worker._minimal_payload_from_raw(
            raw, against_path="/var/tmp/main.wav", expected_sample_rate=8000
        )


def test_worker_rejects_multiple_or_missing_expected_basename() -> None:
    raw = _raw_result(basename="not-main.wav")
    with pytest.raises(RuntimeError):
        worker._minimal_payload_from_raw(
            raw, against_path="/var/tmp/main.wav", expected_sample_rate=8000
        )

    raw = _raw_result()
    raw["match_info"]["other.wav"] = raw["match_info"]["main.wav"]  # type: ignore[index]
    with pytest.raises(RuntimeError):
        worker._minimal_payload_from_raw(
            raw, against_path="/var/tmp/main.wav", expected_sample_rate=8000
        )


def test_worker_rejects_empty_match_and_accepts_no_match() -> None:
    with pytest.raises(RuntimeError):
        worker._minimal_payload_from_raw(
            _raw_result(offsets=[]),
            against_path="/var/tmp/main.wav",
            expected_sample_rate=8000,
        )
    assert worker._minimal_payload_from_raw(
        None, against_path="/var/tmp/main.wav", expected_sample_rate=8000
    ) == {"match_info": None}


def test_worker_build_recognizer_rejects_pinned_typed_config_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = audalign_correlation_typed_config()
    fake_audalign = ModuleType("audalign")
    fake_recognizers = ModuleType("audalign.recognizers")
    fake_correcognize = ModuleType("audalign.recognizers.correcognize")

    class FakeRecognizer:
        def __init__(self) -> None:
            self.config = SimpleNamespace(**config)

    fake_correcognize.CorrelationRecognizer = FakeRecognizer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "audalign", fake_audalign)
    monkeypatch.setitem(sys.modules, "audalign.recognizers", fake_recognizers)
    monkeypatch.setitem(sys.modules, "audalign.recognizers.correcognize", fake_correcognize)

    assert isinstance(worker._build_recognizer(config), FakeRecognizer)

    drifted = dict(config)
    drifted["sample_rate"] = 44100
    with pytest.raises(RuntimeError, match="config drift"):
        worker._build_recognizer(drifted)

    missing = dict(config)
    missing.pop("SCALING_16_BIT")
    with pytest.raises(RuntimeError, match="not closed"):
        worker._build_recognizer(missing)


def _install_fake_audalign(
    monkeypatch: pytest.MonkeyPatch,
    raw_result: object,
    *,
    recognizer_config: dict[str, object] | None = None,
) -> None:
    config = (
        audalign_correlation_typed_config()
        if recognizer_config is None
        else recognizer_config
    )
    fake_audalign = ModuleType("audalign")
    fake_recognizers = ModuleType("audalign.recognizers")
    fake_correcognize = ModuleType("audalign.recognizers.correcognize")

    class FakeRecognizer:
        def __init__(self) -> None:
            self.config = SimpleNamespace(**config)

    def recognize(_target: str, _against: str, *, recognizer: object) -> object:
        assert isinstance(recognizer, FakeRecognizer)
        return raw_result

    fake_correcognize.CorrelationRecognizer = FakeRecognizer  # type: ignore[attr-defined]
    fake_audalign.recognize = recognize  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "audalign", fake_audalign)
    monkeypatch.setitem(sys.modules, "audalign.recognizers", fake_recognizers)
    monkeypatch.setitem(
        sys.modules, "audalign.recognizers.correcognize", fake_correcognize
    )


def _run_worker_main(
    tmp_path, monkeypatch: pytest.MonkeyPatch, raw_result: object, *, expected_config=None
):
    target = tmp_path / "probe.wav"
    against = tmp_path / "main.wav"
    output = tmp_path / "worker.json"
    config = (
        audalign_correlation_typed_config()
        if expected_config is None
        else expected_config
    )
    _install_fake_audalign(monkeypatch, raw_result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "correlation_worker.py",
            "--target-wav",
            str(target),
            "--against-wav",
            str(against),
            "--json-output",
            str(output),
            "--expected-config",
            json.dumps(config),
        ],
    )
    return worker.main(), output


def test_worker_main_emits_only_minimal_closed_payload(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, output = _run_worker_main(tmp_path, monkeypatch, _raw_result())

    assert status == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "match_info": {
            "offset_seconds": [0.0, 0.125],
            "sample_rate": 8000,
        }
    }
    assert "confidence" not in output.read_text(encoding="utf-8")
    assert "rankings" not in output.read_text(encoding="utf-8")


def test_worker_main_none_is_no_candidate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, output = _run_worker_main(tmp_path, monkeypatch, None)

    assert status == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {"match_info": None}


@pytest.mark.parametrize(
    "raw_result",
    [
        pytest.param({"match_info": {}}, id="malformed-raw"),
        pytest.param(_raw_result(basename="wrong.wav"), id="wrong-basename"),
    ],
)
def test_worker_main_rejects_raw_or_basename_drift(
    tmp_path, monkeypatch: pytest.MonkeyPatch, raw_result: object
) -> None:
    with pytest.raises(RuntimeError):
        _run_worker_main(tmp_path, monkeypatch, raw_result)
    assert not (tmp_path / "worker.json").exists()


def test_worker_main_rejects_config_drift(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = audalign_correlation_typed_config()
    drifted["sample_rate"] = 44100
    with pytest.raises(RuntimeError, match="config drift"):
        _run_worker_main(tmp_path, monkeypatch, _raw_result(), expected_config=drifted)
    assert not (tmp_path / "worker.json").exists()
