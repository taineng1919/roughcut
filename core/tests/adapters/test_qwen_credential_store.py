"""Store-level WP3A tests for the Roughcut Qwen credential file.

Every test uses an explicit temporary store root or a temporary HOME.  No test
reads, creates or removes a real user credential.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unicodedata
from pathlib import Path

import pytest

from roughcut.adapters import qwen_credential_store as store
from roughcut.adapters.qwen import (
    CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES,
    credential_fields_are_valid,
)
from roughcut.adapters.qwen.filetrans import (
    QwenFiletransConfig,
    QwenFiletransError,
    require_credentials,
)
from roughcut.adapters.qwen_credential_store import (
    CREDENTIAL_FILENAME,
    CREDENTIAL_FORMAT_VERSION,
    CREDENTIAL_INSECURE,
    CREDENTIAL_INVALID,
    CREDENTIAL_MALFORMED,
    CREDENTIAL_NOT_CONFIGURED,
    CREDENTIAL_UNSUPPORTED_FORMAT,
    CREDENTIAL_WRITE_FAILED,
    PRIVATE_DIRECTORY_NAME,
    QwenCredentialError,
    clear_credential,
    credential_store_path,
    read_credential,
    write_credential,
)
from roughcut.application.qwen_credentials import qwen_credential_readiness

SENTINEL = "QWEN_SUPER_SECRET_SENTINEL_123"
WORKSPACE_ID = "fakeworkspace01"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits are not a Windows ACL"
)

# Every injection shape a credential must never carry: C0 controls (NUL, ESC,
# vertical tab, form feed), CR/LF/TAB, DEL, C1 control NEL, zero-width and bidi
# `Cf` format characters, U+2028/U+2029 line separators and a lone surrogate.
FORBIDDEN_API_KEY_CHARACTERS = (
    "\x00",
    "\x01",
    "\x07",
    "\x0b",
    "\x0c",
    "\x1b",
    "\x7f",
    "\x85",
    "\n",
    "\r",
    "\t",
    "\u200b",
    "\u202e",
    "\ufeff",
    "\u2028",
    "\u2029",
    "\ud800",
)
# The rule is an injection-shape rule, not a charset rule: printable spacing
# and non-ASCII letters stay accepted.
ACCEPTED_API_KEY_CHARACTERS = (" ", "\u00a0", "-", "_", "\u4e2d")
# Representative injection shapes for store behaviour tests; the complete
# Unicode case matrix is asserted once, by the shared-validator test below.
REPRESENTATIVE_FORBIDDEN_CHARACTERS = ("\x00", "\n", "\u202e")


def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "roughcut root"
    root.mkdir(parents=True, exist_ok=True)
    return root


def symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except OSError as error:  # pragma: no cover - Windows privilege dependent
        if os.name == "nt":
            pytest.skip(f"Windows symbolic-link privilege is unavailable: {error}")
        raise


def stored_record(root: Path) -> dict[str, object]:
    payload = json.loads(credential_store_path(root).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def write_raw(root: Path, payload: object) -> Path:
    """Publish a valid private record, then replace its content in place."""

    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path(root)
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def left_over_staging_names(directory: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith(".qwen-filetrans.")
    )


# --------------------------------------------------------------------------
# location and shape
# --------------------------------------------------------------------------


def test_store_is_user_level_and_separate_from_runtime_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    path = credential_store_path()

    assert path.parent.parent == Path.home() / ".roughcut"
    assert path.parent.name == PRIVATE_DIRECTORY_NAME
    assert path.name == CREDENTIAL_FILENAME
    assert path.name != "runtime.json"


def test_configure_writes_only_the_one_closed_record(tmp_path: Path) -> None:
    root = store_root(tmp_path)

    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    assert stored_record(root) == {
        "format_version": CREDENTIAL_FORMAT_VERSION,
        "api_key": SENTINEL,
        "workspace_id": WORKSPACE_ID,
    }
    credential = read_credential(root)
    assert credential.api_key == SENTINEL
    assert credential.workspace_id == WORKSPACE_ID
    assert SENTINEL not in repr(credential)


def test_configure_replaces_the_previous_record(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    write_credential(root, api_key="rotated-secret", workspace_id="rotatedworkspace")

    assert stored_record(root)["api_key"] == "rotated-secret"
    assert read_credential(root).workspace_id == "rotatedworkspace"
    for entry in (root / PRIVATE_DIRECTORY_NAME).iterdir():
        content = entry.read_text(encoding="utf-8")
        assert SENTINEL not in content
        assert WORKSPACE_ID not in content


def test_clear_removes_only_the_record_and_is_idempotent(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    unrelated = root / "runtime.json"
    unrelated.write_text("{}", encoding="utf-8")
    models = root / "models"
    models.mkdir()

    clear_credential(root)
    clear_credential(root)

    assert not credential_store_path(root).exists()
    assert unrelated.read_text(encoding="utf-8") == "{}"
    assert models.is_dir()
    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)
    assert failure.value.code == CREDENTIAL_NOT_CONFIGURED


def test_clear_removes_a_crash_leftover_staging_copy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    root = home / ".roughcut"
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    staging = directory / ".qwen-filetrans.abandoned.tmp"
    staging.write_text(SENTINEL, encoding="utf-8")
    unrelated = directory / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    # Residue inside the verified private boundary is cleanup work, not a
    # confidentiality breach: the canonical record still decides readiness.
    assert qwen_credential_readiness(root=root)["status"] == "configured"

    clear_credential(root)

    assert not credential_store_path(root).exists()
    assert not staging.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert qwen_credential_readiness(root=root)["status"] == "not_configured"
    leaked = [
        path
        for path in home.rglob("*")
        if path.is_file()
        and SENTINEL in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []


def test_clear_removes_a_staging_copy_when_the_record_is_already_absent(
    tmp_path: Path,
) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    os.unlink(credential_store_path(root))
    staging = directory / ".qwen-filetrans.abandoned.tmp"
    staging.write_text(SENTINEL, encoding="utf-8")

    assert qwen_credential_readiness(root=root)["status"] == "not_configured"

    clear_credential(root)
    clear_credential(root)

    assert not staging.exists()
    assert not credential_store_path(root).exists()
    assert directory.is_dir()
    assert qwen_credential_readiness(root=root)["status"] == "not_configured"


def test_configure_removes_a_stale_staging_copy_of_a_secret(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    stale = directory / ".qwen-filetrans.abandoned.tmp"
    stale.write_text(SENTINEL, encoding="utf-8")
    unrelated = directory / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    # Residue inside the verified private boundary does not change the
    # canonical readiness answer.
    assert qwen_credential_readiness(root=root)["status"] == "configured"

    write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)

    assert not stale.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert left_over_staging_names(directory) == []
    assert qwen_credential_readiness(root=root)["status"] == "configured"
    assert read_credential(root).api_key == "rotated-secret"


@POSIX_ONLY
def test_the_private_boundary_is_the_credential_directory_not_the_install_root(
    tmp_path: Path,
) -> None:
    root = store_root(tmp_path)
    os.chmod(root, 0o755)

    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    os.chmod(root, 0o755)

    directory = credential_store_path(root).parent
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(credential_store_path(root)).st_mode) == 0o600
    assert read_credential(root).api_key == SENTINEL
    assert qwen_credential_readiness(root=root)["status"] == "configured"

    os.chmod(directory, 0o755)
    assert qwen_credential_readiness(root=root)["status"] == "insecure"


# --------------------------------------------------------------------------
# POSIX private permissions
# --------------------------------------------------------------------------


@POSIX_ONLY
def test_configure_creates_a_private_directory_and_file(tmp_path: Path) -> None:
    root = store_root(tmp_path)

    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    directory = credential_store_path(root).parent
    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(credential_store_path(root)).st_mode) == 0o600


@POSIX_ONLY
def test_configure_keeps_private_mode_under_a_permissive_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root(tmp_path)
    previous = os.umask(0o000)
    try:
        write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
        write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)
    finally:
        os.umask(previous)

    directory = credential_store_path(root).parent
    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(credential_store_path(root)).st_mode) == 0o600


@POSIX_ONLY
def test_read_fails_closed_on_a_wider_record_without_repairing_it(
    tmp_path: Path,
) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path(root)
    os.chmod(path, 0o644)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o644


@POSIX_ONLY
def test_read_fails_closed_on_a_wider_private_directory(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    os.chmod(directory, 0o755)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE
    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o755


@POSIX_ONLY
def test_configure_rebuilds_a_wider_private_directory(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    os.chmod(directory, 0o755)

    write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)

    assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(credential_store_path(root)).st_mode) == 0o600
    assert read_credential(root).api_key == "rotated-secret"


@POSIX_ONLY
def test_read_fails_closed_on_a_hard_linked_record(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    os.link(credential_store_path(root), tmp_path / "second-link.json")

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE


# --------------------------------------------------------------------------
# symlink / reparse boundary
# --------------------------------------------------------------------------


def test_read_fails_closed_on_a_symlinked_record(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path(root)
    outside = tmp_path / "outside-record.json"
    path.rename(outside)
    symlink_or_skip(outside, path)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE
    assert outside.exists()


def test_read_fails_closed_on_a_symlinked_private_directory(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    outside = tmp_path / "outside-private"
    directory.rename(outside)
    symlink_or_skip(outside, directory)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE


def test_read_fails_closed_on_a_symlinked_install_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-root"
    write_credential(outside, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    root = tmp_path / "linked root"
    symlink_or_skip(outside, root)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE


def test_configure_and_clear_refuse_a_symlinked_install_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-root"
    write_credential(outside, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    root = tmp_path / "linked root"
    symlink_or_skip(outside, root)

    with pytest.raises(QwenCredentialError) as failure:
        write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)
    assert failure.value.code == CREDENTIAL_INSECURE

    with pytest.raises(QwenCredentialError) as failure:
        clear_credential(root)
    assert failure.value.code == CREDENTIAL_INSECURE
    assert credential_store_path(outside).exists()


def test_read_fails_closed_when_the_install_root_is_a_file(tmp_path: Path) -> None:
    root = tmp_path / "roughcut root"
    root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE


def test_read_fails_closed_when_the_record_is_a_directory(tmp_path: Path) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path(root)
    path.unlink()
    path.mkdir()

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_INSECURE


# --------------------------------------------------------------------------
# malformed / unsupported records
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ("not json at all", CREDENTIAL_MALFORMED),
        ("[]", CREDENTIAL_MALFORMED),
        (
            {"format_version": CREDENTIAL_FORMAT_VERSION, "api_key": SENTINEL},
            CREDENTIAL_MALFORMED,
        ),
        (
            {
                "format_version": CREDENTIAL_FORMAT_VERSION,
                "api_key": SENTINEL,
                "workspace_id": WORKSPACE_ID,
                "extra": 1,
            },
            CREDENTIAL_MALFORMED,
        ),
        (
            {
                "format_version": CREDENTIAL_FORMAT_VERSION,
                "api_key": SENTINEL,
                "workspace_id": 12,
            },
            CREDENTIAL_MALFORMED,
        ),
        (
            {
                "format_version": CREDENTIAL_FORMAT_VERSION,
                "api_key": "",
                "workspace_id": WORKSPACE_ID,
            },
            CREDENTIAL_MALFORMED,
        ),
        (
            {
                "format_version": CREDENTIAL_FORMAT_VERSION,
                "api_key": SENTINEL,
                "workspace_id": "not a workspace",
            },
            CREDENTIAL_MALFORMED,
        ),
        (
            {
                "format_version": CREDENTIAL_FORMAT_VERSION + 1,
                "api_key": SENTINEL,
                "workspace_id": WORKSPACE_ID,
            },
            CREDENTIAL_UNSUPPORTED_FORMAT,
        ),
        (
            {"format_version": True, "api_key": SENTINEL, "workspace_id": WORKSPACE_ID},
            CREDENTIAL_MALFORMED,
        ),
    ],
)
def test_read_reports_the_closed_record_failures(
    tmp_path: Path, payload: object, code: str
) -> None:
    root = store_root(tmp_path)
    write_raw(root, payload)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == code
    assert SENTINEL not in str(failure.value)


def test_read_reports_not_configured_for_an_absent_record(tmp_path: Path) -> None:
    root = store_root(tmp_path)

    with pytest.raises(QwenCredentialError) as failure:
        read_credential(root)

    assert failure.value.code == CREDENTIAL_NOT_CONFIGURED


# --------------------------------------------------------------------------
# atomicity, interruption and staging cleanup
# --------------------------------------------------------------------------


def test_publish_failure_keeps_the_previous_record_and_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("injected publish failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)

    with pytest.raises(QwenCredentialError) as failure:
        write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)

    assert failure.value.code == CREDENTIAL_WRITE_FAILED
    assert read_credential(root).api_key == SENTINEL
    assert left_over_staging_names(directory) == []


def test_fsync_failure_keeps_the_previous_record_and_no_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent

    def fail_fsync(descriptor: int) -> None:
        raise OSError("injected fsync interruption")

    monkeypatch.setattr(store.os, "fsync", fail_fsync)

    with pytest.raises(QwenCredentialError) as failure:
        write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)

    assert failure.value.code == CREDENTIAL_WRITE_FAILED
    assert read_credential(root).api_key == SENTINEL
    assert left_over_staging_names(directory) == []


def test_staging_creation_failure_leaves_no_partial_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = store_root(tmp_path)
    directory = root / PRIVATE_DIRECTORY_NAME
    directory.mkdir(mode=0o700)

    def fail_mkstemp(*args: object, **kwargs: object) -> object:
        raise OSError("injected staging failure")

    monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

    with pytest.raises(QwenCredentialError) as failure:
        write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    assert failure.value.code == CREDENTIAL_WRITE_FAILED
    assert not credential_store_path(root).exists()
    assert left_over_staging_names(directory) == []


@POSIX_ONLY
def test_permission_denied_publish_keeps_the_previous_record(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:  # pragma: no cover - root bypasses directory modes
        pytest.skip("root bypasses the directory permission")
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path(root).parent
    os.chmod(directory, 0o500)
    try:
        with pytest.raises(QwenCredentialError) as failure:
            write_credential(root, api_key="rotated-secret", workspace_id=WORKSPACE_ID)
        assert failure.value.code == CREDENTIAL_WRITE_FAILED
        assert credential_store_path(root).read_text(encoding="utf-8").count(
            SENTINEL
        ) == 1
    finally:
        os.chmod(directory, 0o700)
    assert read_credential(root).api_key == SENTINEL


def test_published_record_is_never_half_written(tmp_path: Path) -> None:
    root = store_root(tmp_path)

    for index in range(4):
        write_credential(root, api_key=f"{SENTINEL}-{index}", workspace_id=WORKSPACE_ID)
        raw = credential_store_path(root).read_bytes()
        assert raw.endswith(b"\n")
        assert json.loads(raw.decode("utf-8"))["api_key"] == f"{SENTINEL}-{index}"


# --------------------------------------------------------------------------
# secret persistence and redaction
# --------------------------------------------------------------------------


def test_secret_persists_only_in_the_canonical_credential_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    write_credential(home, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    canonical = credential_store_path(home)
    matches = [
        path
        for path in home.rglob("*")
        if path.is_file()
        and SENTINEL in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert matches == [canonical]


def test_secret_is_absent_from_every_error_message(tmp_path: Path) -> None:
    root = store_root(tmp_path)

    with pytest.raises(QwenCredentialError) as invalid:
        write_credential(root, api_key=SENTINEL, workspace_id="bad workspace")
    assert invalid.value.code == CREDENTIAL_INVALID
    assert SENTINEL not in str(invalid.value)

    write_raw(root, "not json at all")
    with pytest.raises(QwenCredentialError) as malformed:
        read_credential(root)
    assert malformed.value.code == CREDENTIAL_MALFORMED
    assert SENTINEL not in str(malformed.value)


# --------------------------------------------------------------------------
# one shared credential-shape validator
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "api_key",
    ["", "   ", "sk-ok", "with\nnewline", "with\ttab", "with\rcarriage", "\r\n"],
)
@pytest.mark.parametrize(
    "workspace_id",
    ["", "fakeworkspace01", "with space", "with/slash", "a" * 65, "a" * 64, "Mixed_ok-1"],
)
def test_store_and_transport_agree_on_the_accepted_credential_shape(
    tmp_path: Path, api_key: str, workspace_id: str
) -> None:
    root = store_root(tmp_path)
    expected = credential_fields_are_valid(api_key=api_key, workspace_id=workspace_id)

    transport_accepts = True
    try:
        require_credentials(
            QwenFiletransConfig(api_key=api_key, workspace_id=workspace_id)
        )
    except QwenFiletransError:
        transport_accepts = False

    store_accepts = True
    try:
        write_credential(root, api_key=api_key, workspace_id=workspace_id)
    except QwenCredentialError as error:
        assert error.code == CREDENTIAL_INVALID
        store_accepts = False

    assert expected == transport_accepts == store_accepts


def test_configure_rejects_invalid_input_without_touching_the_store(
    tmp_path: Path,
) -> None:
    root = store_root(tmp_path)

    for api_key, workspace_id in (("", WORKSPACE_ID), (SENTINEL, "bad workspace")):
        with pytest.raises(QwenCredentialError) as failure:
            write_credential(root, api_key=api_key, workspace_id=workspace_id)
        assert failure.value.code == CREDENTIAL_INVALID

    assert not (root / PRIVATE_DIRECTORY_NAME).exists()


@pytest.mark.parametrize("character", FORBIDDEN_API_KEY_CHARACTERS)
def test_the_shared_validator_rejects_every_forbidden_category(
    character: str,
) -> None:
    api_key = f"sk{character}value"

    assert CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES == {"Cc", "Cf", "Cs", "Zl", "Zp"}
    assert unicodedata.category(character) in CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES
    assert credential_fields_are_valid(api_key=api_key, workspace_id=WORKSPACE_ID) is False


@pytest.mark.parametrize("character", ACCEPTED_API_KEY_CHARACTERS)
def test_printable_api_key_characters_stay_accepted(
    tmp_path: Path, character: str
) -> None:
    api_key = f"sk{character}value"
    root = store_root(tmp_path)

    assert unicodedata.category(character) not in (
        CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES
    )
    assert credential_fields_are_valid(api_key=api_key, workspace_id=WORKSPACE_ID) is True
    require_credentials(QwenFiletransConfig(api_key=api_key, workspace_id=WORKSPACE_ID))
    write_credential(root, api_key=api_key, workspace_id=WORKSPACE_ID)
    assert read_credential(root).api_key == api_key


@pytest.mark.parametrize("character", REPRESENTATIVE_FORBIDDEN_CHARACTERS)
def test_an_injected_character_never_reaches_the_credential_file(
    tmp_path: Path, character: str
) -> None:
    root = store_root(tmp_path)
    write_credential(root, api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    with pytest.raises(QwenCredentialError) as failure:
        write_credential(
            root,
            api_key=f"replacement{character}key",
            workspace_id=WORKSPACE_ID,
        )

    assert failure.value.code == CREDENTIAL_INVALID
    assert read_credential(root).api_key == SENTINEL
    raw = credential_store_path(root).read_text(encoding="utf-8")
    assert f"replacement{character}key" not in raw
    assert SENTINEL in raw
