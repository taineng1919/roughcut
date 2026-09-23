"""Bounded synthetic-media and persistent-runtime fixtures for W6 gates."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from roughcut.adapters.artifact_store import write_new_json
from roughcut.adapters.project_store import ProjectStore
from roughcut.adapters.runtime_binding import (
    RuntimeBinding,
    RuntimeComponent,
    RuntimePlanEvidence,
    RuntimePython,
    RuntimeTool,
    current_architecture,
    current_platform,
    publish_runtime_binding,
)
from roughcut.application.agent_context import calculate_agent_context_hash
from roughcut.application.projects import create_project
from roughcut.application.sources import add_source
from roughcut.application.workflows import workflow_action, workflow_start, workflow_status
from roughcut.domain.bindings import SourceTranscriptBinding
from roughcut.domain.brief import EditBrief
from roughcut.domain.edit import (
    EditClip,
    MultiSourceEditDecision,
    MultiSourceEditProposal,
)
from roughcut.domain.project import ImportMode, Project, SourceAsset
from roughcut.domain.transcript import (
    TimedTranscript,
    TranscriptProvenance,
    TranscriptSegment,
)

CORE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RenderFixture:
    project_path: Path
    decision: MultiSourceEditDecision
    revision: int
    sources: tuple[SourceAsset, ...]


@dataclass(frozen=True)
class ExportReviewFixture:
    project_path: Path
    run_id: str
    revision: int
    sources: tuple[SourceAsset, ...]


def tool_paths() -> tuple[Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.fail("W6 requires an available FFmpeg/ffprobe pair")
    return Path(ffmpeg).resolve(), Path(ffprobe).resolve()


def tool_version(path: Path) -> str:
    result = subprocess.run(
        [str(path), "-version"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = (result.stdout or result.stderr).splitlines()
    assert lines and lines[0]
    return lines[0]


def run_ffmpeg(*arguments: str, timeout: float = 120) -> None:
    ffmpeg, _ffprobe = tool_paths()
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    assert result.returncode == 0, result.stderr


def make_av_source(
    path: Path,
    *,
    color: str,
    frequency: int,
    duration: float = 1.4,
    size: str = "160x90",
    rate: int = 25,
) -> None:
    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s={size}:r={rate}:d={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-shortest",
        str(path),
    )


def ffprobe_json(path: Path) -> dict[str, object]:
    _ffmpeg, ffprobe = tool_paths()
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-count_frames",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def frame_rgb(path: Path, seconds: str) -> tuple[int, int, int]:
    ffmpeg, _ffprobe = tool_paths()
    result = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            seconds,
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert len(result.stdout) == 3
    return tuple(result.stdout)  # type: ignore[return-value]


def build_persistent_runtime(
    root: Path,
    *,
    asr: bool = False,
    worker_mode: str = "success",
) -> Path:
    """Publish a disposable external binding using the real host tools.

    The ASR variant puts only a tiny deterministic local FunASR package in a
    temporary Python venv. Production runner.py and worker.py remain the code
    under test; the fixture supplies the already-audited local model seam
    without adding a production mode or worker protocol.
    """

    ffmpeg, ffprobe = tool_paths()
    runtime_root = root / "运行时 中文 with spaces"
    runtime_root.mkdir(parents=True, exist_ok=False)
    model_root = runtime_root / "外部 模型 with spaces"
    model_root.mkdir()
    model_paths: dict[str, Path] = {}
    for name, payload in (
        ("model_asr", "model.pt"),
        ("model_vad", "model.pt"),
        ("model_punc", "model.pt"),
        ("model_spk", "campplus_cn_common.bin"),
    ):
        path = model_root / name
        path.mkdir()
        (path / payload).write_bytes(f"W6 {name}".encode())
        model_paths[name] = path

    (runtime_root / "cache" / "modelscope").mkdir(parents=True)
    external_manifest = runtime_root / "外部模型 manifest.json"
    external_manifest.write_text("{}\n", encoding="utf-8")

    if asr:
        funasr_root = runtime_root / "FunASR runtime 中文 with spaces"
        venv.EnvBuilder(
            with_pip=False,
            clear=True,
            symlinks=os.name != "nt",
        ).create(funasr_root)
        python_path = _venv_interpreter(funasr_root)
        _write_deterministic_funasr_runtime(funasr_root, worker_mode)
    else:
        python_path = Path(sys.executable).resolve()

    versions = {"funasr": "1.3.14", "torch": "2.6.0", "torchaudio": "2.6.0"}
    python = RuntimePython(
        source_type="external",
        ownership="external_read_only",
        interpreter=str(python_path),
        versions=versions,
        receipt={
            "interpreter": str(python_path),
            "python_version": "3.11",
            **versions,
            "cuda_version": None,
            "cuda_available": False,
        },
    )
    components: dict[str, RuntimeComponent] = {}
    for key, name in (
        ("asr", "model_asr"),
        ("vad", "model_vad"),
        ("punc", "model_punc"),
        ("campp", "model_spk"),
    ):
        components[key] = RuntimeComponent(
            component=name,
            source_type="external",
            ownership="external_read_only",
            path=str(model_paths[name]),
            version=f"w6-{name}",
            origin=f"fixture://{name}",
            license="fixture-only",
            receipt={
                "algorithm": "sha256",
                "value": hashlib.sha256(name.encode("utf-8")).hexdigest(),
            },
        )
    binding = RuntimeBinding(
        install_root=str(runtime_root),
        platform=current_platform(),
        architecture=current_architecture(),
        profile="w6-persistent-external-fixture",
        verification_mode="full",
        python=python,
        components=components,
        ffmpeg=RuntimeTool(str(ffmpeg), tool_version(ffmpeg)),
        ffprobe=RuntimeTool(str(ffprobe), tool_version(ffprobe)),
        evidence=RuntimePlanEvidence(
            plan_hash="a" * 64,
            catalog_version="w6-fixture",
            catalog_hash="b" * 64,
            managed_root=str(runtime_root / "managed"),
            managed_manifest_sha256=None,
            external_manifest_path=str(external_manifest),
            external_manifest_sha256=hashlib.sha256(
                external_manifest.read_bytes()
            ).hexdigest(),
        ),
        schema_version=1,
    )
    binding_path = runtime_root / "runtime.json"
    publish_runtime_binding(binding_path, binding)
    return binding_path


def _venv_interpreter(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _venv_site_packages(root: Path) -> Path:
    if os.name == "nt":
        return root / "Lib" / "site-packages"
    return root / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _write_deterministic_funasr_runtime(root: Path, worker_mode: str) -> None:
    site_packages = _venv_site_packages(root)
    site_packages.mkdir(parents=True, exist_ok=True)
    funasr = site_packages / "funasr"
    funasr.mkdir()
    (funasr / "__init__.py").write_text(
        f"""import os
import sys
import time
from pathlib import Path

sys.stderr.write('deterministic dependency diagnostic: 中文\\n')
_MODE = {worker_mode!r}


class AutoModel:
    def __init__(self, **options):
        print('deterministic model diagnostic: 中文')
        if _MODE == 'worker_failure':
            raise RuntimeError('deterministic worker failure')
        self.options = options

    def generate(self, **options):
        print('deterministic generation diagnostic: 中文')
        if _MODE == 'native_noise':
            os.write(1, 'native stdout diagnostic: 中文\\n'.encode('utf-8'))
            os.write(2, 'native stderr diagnostic: 中文\\n'.encode('utf-8'))
        if _MODE == 'timeout':
            marker = os.environ.get('W6_WORKER_MARKER')
            if marker:
                Path(marker).write_text('started', encoding='utf-8')
            time.sleep(10)
            if marker:
                Path(marker).write_text('finished', encoding='utf-8')
        if _MODE == 'malformed':
            return {{'malformed': True}}
        return [{{
            'text': '中文 Windows 路径。',
            'sentence_info': [{{
                'text': '中文 Windows 路径。',
                'raw_text': '中文 Windows 路径',
                'start': 0,
                'end': 800,
                'timestamp': [[0, 200], [200, 500], [500, 800]],
            }}],
        }}]
""",
        encoding="utf-8",
    )
    (site_packages / "numpy.py").write_text(
        """class _DType:
    name = 'float32'


class _Samples:
    def __init__(self, count):
        self.dtype = _DType()
        self.shape = (count,)

    def __itruediv__(self, _value):
        return self


class _Integers:
    def __init__(self, count):
        self.count = count

    def astype(self, _dtype):
        return _Samples(self.count)


float32 = object()


def frombuffer(payload, dtype):
    del dtype
    return _Integers(len(payload) // 2)
""",
        encoding="utf-8",
    )
    for name, version in (
        ("funasr", "1.3.14"),
        ("torch", "2.6.0"),
        ("torchaudio", "2.6.0"),
    ):
        metadata = site_packages / f"{name}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )


def seed_source_project(root: Path, source_path: Path) -> tuple[Path, Project, SourceAsset]:
    project_path = root / "项目 中文 with spaces"
    project = create_project(project_path, "W6 ASR 中文 project")
    imported = add_source(
        project_path,
        source_path,
        ImportMode.LINKED,
        expected_revision=project.revision,
    )
    return project_path, imported, imported.sources[0]


def seed_export_review_project(
    root: Path,
    source_paths: tuple[Path, Path],
    *,
    import_modes: tuple[ImportMode, ImportMode] | None = None,
) -> ExportReviewFixture:
    project_path = root / "项目 中文 Export with spaces"
    project = create_project(project_path, "W6 Export 中文 project")
    modes = import_modes or (ImportMode.LINKED, ImportMode.LINKED)
    if len(modes) != len(source_paths):
        raise ValueError("source paths and import modes must have the same length")
    imported = project
    for source_path, import_mode in zip(source_paths, modes, strict=True):
        imported = add_source(
            project_path,
            source_path,
            import_mode,
            expected_revision=imported.revision,
        )
    transcripts: dict[str, TimedTranscript] = {}
    for index, source in enumerate(imported.sources):
        transcript_id = f"tr_w6_export_{index}"
        transcripts[source.source_id] = TimedTranscript(
            schema_version=1,
            transcript_version_id=transcript_id,
            source_id=source.source_id,
            parent_version_id=None,
            provenance=TranscriptProvenance(
                backend="w6-fixture",
                package_version="fixture",
                models={},
                parameters={},
                raw_result_path=f"raw-asr/{source.source_id}/fixture.json",
                started_at="fixture",
                completed_at="fixture",
                exit_status=0,
            ),
            language="zh-CN",
            segments=(
                TranscriptSegment(
                    segment_id=f"seg_w6_export_{index}_a",
                    start_ticks=0,
                    end_ticks=72_000,
                    original_text=f"素材{index} 开场。",
                    corrected_text=None,
                    local_speaker_id=None,
                    person_id=None,
                    confidence=None,
                    fine_units=(),
                    editorial_mark="unmarked",
                ),
                TranscriptSegment(
                    segment_id=f"seg_w6_export_{index}_b",
                    start_ticks=72_000,
                    end_ticks=144_000,
                    original_text=f"素材{index} 重点。",
                    corrected_text=None,
                    local_speaker_id=None,
                    person_id=None,
                    confidence=None,
                    fine_units=(),
                    editorial_mark="unmarked",
                ),
            ),
        )
        write_new_json(
            project_path
            / "transcripts"
            / source.source_id
            / f"{transcript_id}.json",
            transcripts[source.source_id].to_dict(),
        )
    imported = replace(
        imported,
        revision=imported.revision + 1,
        settings={**imported.settings, "width": 160, "height": 90},
        active_transcript_versions={
            source_id: transcript.transcript_version_id
            for source_id, transcript in transcripts.items()
        },
    )
    ProjectStore(project_path).save(imported, expected_revision=imported.revision - 1)

    run_id = "wfr_w6_export"
    started = workflow_start(
        project_path,
        run_id,
        [source.source_id for source in imported.sources],
    )
    scope_basis = started.status["confirmation_bases"]["scope"]["basis"]
    scoped = workflow_action(
        project_path,
        run_id,
        "act_w6_export_scope",
        "approve_scope",
        {
            "schema_version": 1,
            "confirmation_basis": scope_basis,
            "source_authorizations": [
                {
                    "source_id": source.source_id,
                    "transcribe": False,
                    "speaker_diarization": False,
                }
                for source in imported.sources
            ],
        },
    )
    workflow_action(
        project_path,
        run_id,
        "act_w6_export_brief",
        "confirm_brief",
        {
            "schema_version": 1,
            "confirmation_basis": scoped.status["confirmation_bases"]["brief"][
                "basis"
            ],
            "theme": "Windows A B A",
            "target_duration_ticks": 216_000,
            "focus": ["顺序"],
            "allow_reorder": True,
            "speaker_resolution_waivers": [],
        },
    )
    outline = workflow_action(
        project_path,
        run_id,
        "act_w6_export_outline",
        "submit_outline",
        {
            "schema_version": 1,
            "title": "Windows A B A",
            "opening": "开场",
            "sections": [
                {
                    "section_id": f"section_w6_export_{index}",
                    "title": title,
                    "summary": title,
                    "target_duration_ticks": 54_000,
                }
                for index, title in enumerate(("A", "B", "A", "结尾"), start=1)
            ],
            "ending": "结尾",
            "required_content_coverage": [],
            "narration_status": "none",
        },
    )
    workflow_action(
        project_path,
        run_id,
        "act_w6_export_outline_approve",
        "approve_outline",
        {
            "schema_version": 1,
            "outline_ref": outline.status["presented_subjects"]["outline_ref"],
        },
    )
    status = workflow_status(project_path, run_id)
    brief_ref = status["workflow_run"]["artifact_refs"]["brief"]
    assert isinstance(brief_ref, dict)
    brief = EditBrief.from_dict(
        json.loads(
            (project_path / "briefs" / f"{brief_ref['artifact_id']}.json").read_text(
                encoding="utf-8"
            )
        )
    )
    bindings = tuple(
        SourceTranscriptBinding(
            source.source_id,
            transcripts[source.source_id].transcript_version_id,
        )
        for source in imported.sources
    )
    context_hash = calculate_agent_context_hash(
        project_path,
        project=ProjectStore(project_path).load(),
        bindings=bindings,
        brief=brief,
    )
    blocks = [
        {
            "block_id": "block_w6_export_a1",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": imported.sources[0].source_id,
                    "transcript_version_id": transcripts[imported.sources[0].source_id].transcript_version_id,
                    "segment_id": "seg_w6_export_0_a",
                    "start_ticks": 0,
                    "end_ticks": 72_000,
                }
            ],
            "canonical_text": "素材0 开场。",
        },
        {
            "block_id": "block_w6_export_b",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": imported.sources[1].source_id,
                    "transcript_version_id": transcripts[imported.sources[1].source_id].transcript_version_id,
                    "segment_id": "seg_w6_export_1_a",
                    "start_ticks": 0,
                    "end_ticks": 72_000,
                }
            ],
            "canonical_text": "素材1 开场。",
        },
        {
            "block_id": "block_w6_export_a2",
            "kind": "source_excerpt",
            "refs": [
                {
                    "source_id": imported.sources[0].source_id,
                    "transcript_version_id": transcripts[imported.sources[0].source_id].transcript_version_id,
                    "segment_id": "seg_w6_export_0_b",
                    "start_ticks": 72_000,
                    "end_ticks": 144_000,
                }
            ],
            "canonical_text": "素材0 重点。",
        },
    ]
    draft = workflow_action(
        project_path,
        run_id,
        "act_w6_export_draft",
        "submit_draft",
        {
            "schema_version": 1,
            "parent_draft_ref": None,
            "display_title": "Windows A B A 初稿",
            "source_bindings": [binding.to_dict() for binding in bindings],
            "brief_ref": brief_ref,
            "context_hash": context_hash,
            "blocks": blocks,
            "scoped_mutable_block_ids": [],
        },
    )
    draft_ref = draft.receipt.mutation
    assert draft_ref is not None
    approved_draft = workflow_action(
        project_path,
        run_id,
        "act_w6_export_draft_approve",
        "approve_draft",
        {
            "schema_version": 1,
            "content_draft_ref": {
                "artifact_id": draft_ref.artifact_id,
                "schema_version": draft_ref.schema_version,
                "content_hash": draft_ref.content_hash,
            },
        },
    )
    proposal_ref = approved_draft.workflow_run.artifact_refs["proposal"]
    assert proposal_ref is not None
    workflow_action(
        project_path,
        run_id,
        "act_w6_export_adopt",
        "adopt_roughcut",
        {"schema_version": 1, "proposal_ref": proposal_ref.to_dict()},
    )
    final_project = ProjectStore(project_path).load()
    return ExportReviewFixture(
        project_path=project_path,
        run_id=run_id,
        revision=final_project.revision,
        sources=final_project.sources,
    )


def seed_render_project(root: Path, source_paths: tuple[Path, Path]) -> RenderFixture:
    project_path = root / "项目 中文 Render with spaces"
    project = create_project(project_path, "W6 Render 中文 project")
    imported = project
    for source_path in source_paths:
        imported = add_source(
            project_path,
            source_path,
            ImportMode.LINKED,
            expected_revision=imported.revision,
        )
    bindings = tuple(
        SourceTranscriptBinding(source.source_id, f"tr_w6_{index}")
        for index, source in enumerate(imported.sources)
    )
    ranges = (
        ("clip_a_first", 14_400, 86_400),
        ("clip_b_middle", 14_400, 86_400),
        ("clip_a_last", 14_400, 86_400),
    )
    source_indexes = (0, 1, 0)
    clips = tuple(
        EditClip(
            clip_id,
            imported.sources[source_index].source_id,
            bindings[source_index].transcript_version_id,
            f"seg_w6_{index}",
            source_in,
            source_out,
            "W6 bounded synthetic order",
            f"W6 marker {index}",
        )
        for index, ((clip_id, source_in, source_out), source_index) in enumerate(
            zip(ranges, source_indexes, strict=True)
        )
    )
    brief = EditBrief(
        "brief_w6_render",
        "Windows A B A order",
        sum(clip.duration_ticks for clip in clips),
        ("order",),
        True,
    )
    proposal = MultiSourceEditProposal(
        "proposal_w6_render",
        imported.revision,
        None,
        bindings,
        brief,
        "c" * 64,
        clips,
        sum(clip.duration_ticks for clip in clips),
        "w6-fixture",
    )
    decision = MultiSourceEditDecision(
        "edit_w6_render",
        proposal,
        imported.revision + 1,
        "w6-fixture",
    )
    write_new_json(
        project_path / "edits" / f"{decision.edit_version_id}.json",
        decision.to_dict(),
    )
    updated = replace(
        imported,
        revision=decision.project_revision,
        settings={**imported.settings, "width": 160, "height": 90},
        active_transcript_versions={
            binding.source_id: binding.transcript_version_id
            for binding in bindings
        },
        active_edit_version_id=decision.edit_version_id,
    )
    ProjectStore(project_path).save(updated, expected_revision=imported.revision)
    return RenderFixture(
        project_path=project_path,
        decision=decision,
        revision=updated.revision,
        sources=updated.sources,
    )


@contextmanager
def deny_shared_read(path: Path) -> Iterator[None]:
    """Hold a Windows CreateFileW handle with no sharing for proxy regression."""

    if os.name != "nt":
        yield
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    invalid = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        0x80000000,
        0,
        None,
        3,
        0x00000080,
        None,
    )
    if handle == invalid:
        error = ctypes.get_last_error()
        raise OSError(error, "CreateFileW could not hold proxy output")
    try:
        yield
    finally:
        close_handle(handle)


def runtime_environment(runtime_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["ROUGHCUT_RUNTIME_BINDING"] = str(runtime_path)
    environment.pop("ROUGHCUT_FFMPEG_COMMAND", None)
    environment.pop("ROUGHCUT_FFPROBE_COMMAND", None)
    environment.pop("ROUGHCUT_FUNASR_PYTHON", None)
    environment.pop("ROUGHCUT_FUNASR_MODEL_ROOT", None)
    return environment
