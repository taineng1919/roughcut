"""WP3A Windows credential security tests: real ACLs, not ``chmod``.

These tests are the Windows evidence that the credential boundary is real.  The
private ``private/`` directory is the confidentiality boundary: it must carry a
protected DACL that grants the current user SID only and inherits into its
children.  The record and the staging file only have to stay inside that
verified directory and never expose an additional allowed principal.

The DACLs are read back through advapi32 independently of the store (including a
test-built counterexample for the protected-DACL requirement), so the assertions
do not trust the code under test to describe its own boundary.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from roughcut.adapters import qwen_credential_store as store
from roughcut.adapters.qwen_credential_store import (
    CREDENTIAL_FILENAME,
    credential_store_path,
)
from roughcut.application.qwen_credentials import (
    clear_qwen_credential,
    configure_qwen_credential,
    qwen_credential_readiness,
)

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="WP3A Windows credential security requires native Windows ACL semantics",
)

ROOT = Path(__file__).resolve().parents[3]
SENTINEL = "QWEN_SUPER_SECRET_SENTINEL_123"
WORKSPACE_ID = "fakeworkspace01"
OTHER_PRINCIPAL_SIDS = (
    "S-1-1-0",  # Everyone
    "S-1-5-11",  # Authenticated Users
    "S-1-5-18",  # LOCAL SYSTEM
    "S-1-5-32-544",  # Administrators
    "S-1-5-32-545",  # Users
)
_WINDOWS_DACL_SECURITY_INFORMATION = 0x00000004
_WINDOWS_SDDL_REVISION = 1
_WINDOWS_ERROR_SUCCESS = 0
_WINDOWS_ACL_SIZE_INFORMATION = 2
_WINDOWS_ACCESS_ALLOWED_ACE_TYPE = 0x00
_WINDOWS_OBJECT_INHERIT_ACE = 0x01
_WINDOWS_CONTAINER_INHERIT_ACE = 0x02


def _windows_libraries() -> tuple[Any, Any]:
    """Independent advapi32/kernel32 oracle for the published DACL."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.GetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(ctypes.c_uint32),
    )
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    advapi32.GetAclInformation.restype = ctypes.c_int
    advapi32.GetAclInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    )
    advapi32.GetAce.restype = ctypes.c_int
    advapi32.GetAce.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    )
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    return advapi32, kernel32


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _AccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _AceHeader),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


def current_user_sid() -> str:
    completed = subprocess.run(
        ["whoami", "/user"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    match = re.search(r"(S-1-[0-9-]+)", completed.stdout)
    assert match is not None, completed.stdout
    return match.group(1)


def dacl_sddl(path: Path) -> str:
    """Read the DACL of one path back as an SDDL string."""

    advapi32, kernel32 = _windows_libraries()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        _WINDOWS_DACL_SECURITY_INFORMATION,
        None,
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    assert result == _WINDOWS_ERROR_SUCCESS, f"GetNamedSecurityInfoW failed: {result}"
    text = ctypes.c_wchar_p()
    try:
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _WINDOWS_SDDL_REVISION,
            _WINDOWS_DACL_SECURITY_INFORMATION,
            ctypes.byref(text),
            None,
        )
        assert text.value is not None
        return text.value
    finally:
        if text.value is not None:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        if descriptor.value is not None:
            kernel32.LocalFree(descriptor)


def allow_aces(path: Path) -> list[tuple[str, int]]:
    """Return ``(sid string, ace flags)`` for every access-allowed ACE.

    The ACE SIDs are converted with ``ConvertSidToStringSidW`` instead of being
    read out of the SDDL text, because SDDL renders well-known SIDs as
    two-letter abbreviations (for example ``LA`` or ``WD``).  A literal SID
    comparison is the only form that cannot silently pass on an abbreviation.
    """

    advapi32, kernel32 = _windows_libraries()
    descriptor = ctypes.c_void_p()
    acl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        _WINDOWS_DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(acl),
        None,
        ctypes.byref(descriptor),
    )
    assert result == _WINDOWS_ERROR_SUCCESS, f"GetNamedSecurityInfoW failed: {result}"
    try:
        assert acl.value is not None
        size_information = _AclSizeInformation()
        assert advapi32.GetAclInformation(
            acl,
            ctypes.byref(size_information),
            ctypes.sizeof(size_information),
            _WINDOWS_ACL_SIZE_INFORMATION,
        )
        observed: list[tuple[str, int]] = []
        for index in range(size_information.AceCount):
            ace_pointer = ctypes.c_void_p()
            assert advapi32.GetAce(acl, index, ctypes.byref(ace_pointer))
            assert ace_pointer.value is not None
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
            if ace.Header.AceType != _WINDOWS_ACCESS_ALLOWED_ACE_TYPE:
                continue
            sid_address = ctypes.addressof(ace) + _AccessAllowedAce.SidStart.offset
            sid_text = ctypes.c_wchar_p()
            assert advapi32.ConvertSidToStringSidW(
                ctypes.c_void_p(sid_address), ctypes.byref(sid_text)
            )
            try:
                assert sid_text.value is not None
                observed.append((sid_text.value, int(ace.Header.AceFlags)))
            finally:
                if sid_text.value is not None:
                    kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        return observed
    finally:
        if descriptor.value is not None:
            kernel32.LocalFree(descriptor)


@pytest.fixture
def windows_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "wp3a credential home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    return home


def run_icacls(*arguments: str) -> None:
    completed = subprocess.run(
        ["icacls", *arguments], check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:  # pragma: no cover - runner privilege dependent
        pytest.skip(f"icacls failed: {completed.stderr}")


def assert_no_other_principal(path: Path) -> None:
    """Every access-allowed ACE names the current user and nobody else.

    This is the record/staging requirement: the file lives inside the verified
    private directory and must not add an allowed principal of its own.  It does
    not require the file's own DACL to be protected.
    """

    aces = allow_aces(path)
    assert [sid for sid, _flags in aces] == [current_user_sid()], aces
    for principal in OTHER_PRINCIPAL_SIDS:
        assert principal not in [sid for sid, _flags in aces], aces


def assert_private_directory_dacl(directory: Path) -> None:
    """The private container is the protected current-user-only boundary.

    Only this directory has to carry a protected DACL, and its ACE has to
    inherit into children so a file created inside it is born current-user-only.
    """

    assert_no_other_principal(directory)
    flags = allow_aces(directory)[0][1]
    assert flags & _WINDOWS_OBJECT_INHERIT_ACE, allow_aces(directory)
    assert flags & _WINDOWS_CONTAINER_INHERIT_ACE, allow_aces(directory)
    # "D:P" is a protected DACL: no inheritable ACE from any parent container
    # can widen the credential boundary later.
    assert dacl_sddl(directory).startswith("D:P"), dacl_sddl(directory)


def test_default_location_is_user_level_and_private(windows_home: Path) -> None:
    payload = configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    assert payload["status"] == "configured"
    path = credential_store_path()
    assert path == windows_home / ".roughcut" / "private" / CREDENTIAL_FILENAME
    assert path.parent.parent.name == ".roughcut"
    assert_private_directory_dacl(path.parent)
    # A chmod-based implementation would leave SYSTEM and Administrators in the
    # DACL, so naming exactly one allow ACE is the direct assertion that a real
    # ACL was published instead of a file attribute.
    assert_no_other_principal(path)
    assert qwen_credential_readiness()["status"] == "configured"


def test_atomic_replacement_publishes_a_fresh_private_acl(windows_home: Path) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    configure_qwen_credential(api_key="rotated-secret", workspace_id="rotatedws")

    path = credential_store_path()
    assert_private_directory_dacl(path.parent)
    assert_no_other_principal(path)
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == "rotated-secret"
    kept = sorted(entry.name for entry in path.parent.iterdir())
    assert kept == [CREDENTIAL_FILENAME]


def test_staging_file_stays_inside_the_private_boundary(
    windows_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    published: list[tuple[Path, list[tuple[str, int]]]] = []
    real_replace = os.replace

    def observe(source: object, destination: object) -> None:
        staged = Path(source)
        published.append((staged, allow_aces(staged)))
        real_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", observe)

    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    assert len(published) == 1
    staged, staged_aces = published[0]
    assert staged.parent == credential_store_path().parent
    # The staging file is created inside the verified private container and
    # inherits its current-user-only access; it never adds a principal of its
    # own and it does not need a protected DACL of its own.
    assert [sid for sid, _flags in staged_aces] == [current_user_sid()], staged_aces
    assert not staged.exists()
    left_over = sorted(
        entry.name
        for entry in credential_store_path().parent.iterdir()
        if entry.name != CREDENTIAL_FILENAME
    )
    assert left_over == []


def test_clear_removes_the_record_and_leaves_no_secret(windows_home: Path) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    assert credential_store_path().exists()

    payload = clear_qwen_credential()

    assert payload["status"] == "not_configured"
    assert not credential_store_path().exists()
    assert credential_store_path().parent.is_dir()
    assert qwen_credential_readiness()["status"] == "not_configured"


def test_clear_removes_a_crash_leftover_staging_copy_on_windows(
    windows_home: Path,
) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    directory = credential_store_path().parent
    staging = directory / ".qwen-filetrans.abandoned.tmp"
    staging.write_text(SENTINEL, encoding="utf-8")
    unrelated = directory / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    # Residue inside the verified private boundary is cleanup work: the
    # canonical record still decides readiness.
    assert qwen_credential_readiness()["status"] == "configured"

    clear_qwen_credential()

    assert not credential_store_path().exists()
    assert not staging.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert qwen_credential_readiness()["status"] == "not_configured"

    # The staging copy must also be removed when no record exists at all, and no
    # secret may survive anywhere under the temporary HOME.
    staging.write_text(SENTINEL, encoding="utf-8")
    clear_qwen_credential()
    assert not staging.exists()
    for candidate in windows_home.rglob("*"):
        if candidate.is_file():
            assert SENTINEL not in candidate.read_text(encoding="utf-8", errors="ignore")


def test_a_widened_install_root_does_not_widen_the_credential_boundary(
    windows_home: Path,
) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    install_root = credential_store_path().parent.parent
    directory = credential_store_path().parent

    # The shared ~/.roughcut keeps the project's inherited directory convention
    # (it holds runtime.json, the managed components and the cache), so a wider
    # install root is allowed; the protected credential directory is what keeps
    # the credential private.
    run_icacls(str(install_root), "/grant", "*S-1-1-0:(OI)(CI)(R)")
    assert_private_directory_dacl(directory)
    assert_no_other_principal(credential_store_path())
    assert qwen_credential_readiness()["status"] == "configured"


def test_an_unprotected_private_directory_dacl_is_reported_as_insecure(
    windows_home: Path,
) -> None:
    """``SE_DACL_PROTECTED`` on the private container is a verified requirement.

    The directory is the Windows confidentiality boundary, so a protected DACL
    is what keeps the current-user-only property from being widened later by an
    inheritable ACE on a parent directory.  The counterexample keeps the
    container's own ACEs current-user-only and clears only the protection bit,
    so this test fails if the store verifies the principal set but not
    ``SE_DACL_PROTECTED``.
    """

    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    install_root = credential_store_path().parent.parent
    directory = credential_store_path().parent
    assert_private_directory_dacl(directory)

    # Make the install root a non-inheritable current-user-only folder first, so
    # clearing protection on the container cannot pull in a second principal and
    # the protected bit is the only property under test.  ``icacls
    # /inheritance:r`` is not enough here: it leaves inheritance enabled, so the
    # parent ACEs come straight back.  The assertions below still prove the
    # counterexample is a current-user-only unprotected directory.
    store._windows_set_current_user_only_dacl(install_root, inherit_to_children=False)
    run_icacls(str(directory), "/inheritance:e")

    assert not dacl_sddl(directory).startswith("D:P"), dacl_sddl(directory)
    assert [sid for sid, _flags in allow_aces(directory)] == [current_user_sid()]
    assert qwen_credential_readiness()["status"] == "insecure"


def test_a_wider_dacl_is_reported_as_insecure_and_never_repaired(
    windows_home: Path,
) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path()
    run_icacls(str(path), "/grant", "*S-1-1-0:(R)")

    widened_aces = allow_aces(path)
    widened_sids = [sid for sid, _flags in widened_aces]
    assert current_user_sid() in widened_sids, widened_aces
    assert any(sid != current_user_sid() for sid in widened_sids), widened_aces
    widened_sddl = dacl_sddl(path)

    assert qwen_credential_readiness()["status"] == "insecure"
    assert allow_aces(path) == widened_aces
    assert dacl_sddl(path) == widened_sddl


def test_reparse_point_target_is_refused(windows_home: Path) -> None:
    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)
    path = credential_store_path()
    outside = windows_home / "outside-record.json"
    path.rename(outside)
    try:
        os.symlink(outside, path)
    except OSError as error:  # pragma: no cover - runner privilege dependent
        outside.rename(path)
        pytest.skip(f"Windows symbolic-link privilege is unavailable: {error}")

    assert qwen_credential_readiness()["status"] == "insecure"
    assert outside.exists()


def test_non_ascii_and_space_home_path_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "用户 目录 with spaces"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home

    configure_qwen_credential(api_key=SENTINEL, workspace_id=WORKSPACE_ID)

    path = credential_store_path()
    assert path.parent.parent == home / ".roughcut"
    assert_private_directory_dacl(path.parent)
    assert_no_other_principal(path)
    assert qwen_credential_readiness()["status"] == "configured"
    assert clear_qwen_credential()["status"] == "not_configured"


def test_windows_cli_and_mcp_never_echo_the_secret(windows_home: Path) -> None:
    def child_environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment["USERPROFILE"] = str(windows_home)
        environment["HOME"] = str(windows_home)
        return environment

    cli = subprocess.run(
        [sys.executable, "-m", "roughcut.cli", "qwen-credential-configure", "--json"],
        cwd=ROOT,
        env=child_environment(),
        input=json.dumps({"api_key": SENTINEL, "workspace_id": WORKSPACE_ID}),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert cli.returncode == 0, cli.stderr
    assert SENTINEL not in cli.stdout
    assert SENTINEL not in cli.stderr
    assert json.loads(cli.stdout)["credential"]["status"] == "configured"

    mcp = subprocess.run(
        [sys.executable, "-m", "roughcut.mcp"],
        cwd=ROOT,
        env=child_environment(),
        input=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "qwen_credential_configure",
                    "arguments": {"api_key": SENTINEL, "workspace_id": "bad id"},
                },
            }
        )
        + "\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    assert mcp.returncode == 0, mcp.stderr
    assert SENTINEL not in mcp.stdout
    assert SENTINEL not in mcp.stderr
    assert json.loads(mcp.stdout)["result"]["isError"] is True

    assert_private_directory_dacl(credential_store_path().parent)
    assert_no_other_principal(credential_store_path())
