"""Roughcut-owned, user-level, provider-specific Qwen credential store.

WP3A owns exactly one persisted record: the current user's Qwen Filetrans API
Key plus Workspace ID, in one Roughcut-owned private file under the Roughcut
user-level root.  There is no credential registry, provider interface, account
pool, credential ID, keyring, encryption-at-rest framework or Host secret
package, and there is no generic secret CRUD surface.

Boundaries that are deliberate and load-bearing:

* The location is the Roughcut user-level root (``~/.roughcut``), never the
  repository, never a Project, never ``runtime.json`` and never an environment
  variable.  ``DASHSCOPE_API_KEY``/``DASHSCOPE_WORKSPACE_ID`` are Spike/dev/CI
  concepts only and are never read here, so an environment variable can never
  become credential truth.
* The private directory (``0700``) and the record file (``0600``) are created
  with explicitly restrictive modes, so the process umask can only remove
  permission bits and never add them.
* On Windows the ``private/`` directory is the credential confidentiality
  boundary: it carries an explicit protected DACL (``SE_DACL_PROTECTED``) that
  grants the current user SID only and inherits into its children, so a widened
  parent directory cannot add a principal.  The record and the staging file are
  ordinary non-reparse files created inside that verified directory, and each is
  verified to expose no additional allowed principal.  ``chmod`` is never used
  as a stand-in for a Windows ACL.
* An existing record whose permissions are wider than private is reported as
  insecure and is never silently re-chmod-ed into apparent safety before being
  read.  One exception is deliberate and narrow: an explicit configure rebuilds
  the private container before it publishes a brand new record.
* Publication is same-directory staged, fsynced, verified and atomically
  replaced, so a failure cannot leave a half-written, empty or truncated record
  and the old valid record survives any failure that happens before publication.
* The staging namespace is reserved to this store, and both configure and clear
  remove stale staging copies.  A leftover staging copy is cleanup residue: it
  still sits inside the verified private boundary, is never read as the
  canonical record and cannot widen access, so readiness keeps reporting the
  canonical record's own state.  Clear still removes an owned staging file when
  the canonical record is already gone, and an unrelated file is never deleted.
* The install root (``~/.roughcut``) is shared with the runtime binding, the
  managed components and the component cache, so it keeps the project's existing
  directory convention.  It is checked for symlinks, reparse points and type; the
  private permission boundary is the credential's own ``private/`` directory and
  the record file.

Every error carries one closed code and one fixed message.  No code path
interpolates the API Key, the Workspace ID or any file content into a message,
so a credential value cannot reach a public response, a log line or an
exception string.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Any

from roughcut.adapters.qwen import BACKEND, credential_fields_are_valid
from roughcut.adapters.runtime_binding import default_install_root

CREDENTIAL_FORMAT_VERSION = 1
CREDENTIAL_PROVIDER = BACKEND
PRIVATE_DIRECTORY_NAME = "private"
CREDENTIAL_FILENAME = "qwen-filetrans.json"

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

_TEMPORARY_PREFIX = ".qwen-filetrans."
_TEMPORARY_SUFFIX = ".tmp"
_RECORD_FIELDS = frozenset({"format_version", "api_key", "workspace_id"})

CREDENTIAL_NOT_CONFIGURED = "not_configured"
CREDENTIAL_INVALID = "invalid"
CREDENTIAL_UNSUPPORTED_FORMAT = "unsupported_format"
CREDENTIAL_MALFORMED = "malformed"
CREDENTIAL_INSECURE = "insecure"
CREDENTIAL_WRITE_FAILED = "write_failed"

QWEN_CREDENTIAL_ERROR_CODES = frozenset(
    {
        CREDENTIAL_NOT_CONFIGURED,
        CREDENTIAL_INVALID,
        CREDENTIAL_UNSUPPORTED_FORMAT,
        CREDENTIAL_MALFORMED,
        CREDENTIAL_INSECURE,
        CREDENTIAL_WRITE_FAILED,
    }
)

_MESSAGES = {
    CREDENTIAL_NOT_CONFIGURED: "Roughcut Qwen credential 尚未配置",
    CREDENTIAL_INVALID: "Roughcut Qwen credential 输入不合法",
    CREDENTIAL_UNSUPPORTED_FORMAT: "Roughcut Qwen credential 的内部格式版本不受支持",
    CREDENTIAL_MALFORMED: "Roughcut Qwen credential 记录已损坏",
    CREDENTIAL_INSECURE: "Roughcut Qwen credential 的安全边界不满足要求",
    CREDENTIAL_WRITE_FAILED: "Roughcut Qwen credential 写入失败",
}


class QwenCredentialError(RuntimeError):
    """One closed, secret-free credential-store failure."""

    def __init__(self, code: str, message: str) -> None:
        if code not in QWEN_CREDENTIAL_ERROR_CODES:
            raise ValueError("unknown Qwen credential error code")
        super().__init__(message)
        self.code = code


def _error(code: str) -> QwenCredentialError:
    return QwenCredentialError(code, _MESSAGES[code])


@dataclass(frozen=True)
class QwenCredential:
    """One validated record; ``repr`` never exposes the API Key."""

    api_key: str = field(repr=False)
    workspace_id: str = field(repr=False)


def credential_store_path(root: PurePath | None = None) -> Path:
    """Return the one credential record path for this user.

    ``root`` exists only so tests and MCP/CLI request handling can address an
    explicit store root; production callers pass nothing and get the canonical
    Roughcut user-level root.
    """

    base = Path(default_install_root()) if root is None else Path(root)
    return base / PRIVATE_DIRECTORY_NAME / CREDENTIAL_FILENAME


def read_credential(root: PurePath | None = None) -> QwenCredential:
    """Read and validate the record without ever repairing it."""

    path = credential_store_path(root)
    _require_safe_container(path)
    _require_private_container(path.parent, create=False)
    details = _lstat(path)
    if details is None:
        raise _error(CREDENTIAL_NOT_CONFIGURED)
    _require_private_record(path, details)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise _error(CREDENTIAL_INSECURE) from error
    return _parse_record(raw)


def write_credential(
    root: PurePath | None = None,
    *,
    api_key: object,
    workspace_id: object,
) -> None:
    """Create or atomically replace the record with a brand new one."""

    if not isinstance(api_key, str) or not isinstance(workspace_id, str):
        raise _error(CREDENTIAL_INVALID)
    if not credential_fields_are_valid(api_key=api_key, workspace_id=workspace_id):
        raise _error(CREDENTIAL_INVALID)
    path = credential_store_path(root)
    _require_install_root(path.parent.parent, create=True)
    _require_private_container(path.parent, create=True)
    record = {
        "format_version": CREDENTIAL_FORMAT_VERSION,
        "api_key": api_key,
        "workspace_id": workspace_id,
    }
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _publish_private_record(path, raw + b"\n")


def clear_credential(root: PurePath | None = None) -> None:
    """Remove the Qwen credential record and every owned staging copy.

    Idempotent when the record and the owned staging copies are already absent.
    A publish interrupted between staging and replacement leaves an owned staging
    file that can hold a real API Key, so an explicit clear removes it whether or
    not the canonical record still exists; unrelated files are never touched.
    """

    path = credential_store_path(root)
    directory = path.parent
    if _lstat(directory) is None:
        return
    _require_safe_container(path)
    _remove_owned_staging(directory)
    if _lstat(path) is None:
        return
    try:
        os.unlink(path)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error


def _parse_record(raw: bytes) -> QwenCredential:
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _error(CREDENTIAL_MALFORMED) from error
    if not isinstance(payload, dict) or set(payload) != _RECORD_FIELDS:
        raise _error(CREDENTIAL_MALFORMED)
    version = payload.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _error(CREDENTIAL_MALFORMED)
    if version != CREDENTIAL_FORMAT_VERSION:
        raise _error(CREDENTIAL_UNSUPPORTED_FORMAT)
    api_key = payload.get("api_key")
    workspace_id = payload.get("workspace_id")
    if not isinstance(api_key, str) or not isinstance(workspace_id, str):
        raise _error(CREDENTIAL_MALFORMED)
    if not credential_fields_are_valid(api_key=api_key, workspace_id=workspace_id):
        raise _error(CREDENTIAL_MALFORMED)
    return QwenCredential(api_key=api_key, workspace_id=workspace_id)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _error(CREDENTIAL_INSECURE) from error


def _is_unsafe_target(details: os.stat_result) -> bool:
    """Reject a symbolic link or a Windows reparse point at one path component."""

    if stat.S_ISLNK(details.st_mode):
        return True
    if os.name == "nt":
        return int(getattr(details, "st_reparse_tag", 0)) != 0
    return False


def _mode_is_private(mode: int) -> bool:
    return stat.S_IMODE(mode) & 0o077 == 0


def _require_install_root(root: Path, *, create: bool) -> None:
    """Refuse a symlinked or reparse-point Roughcut user-level root.

    This reuses the existing project rule for the same directory
    (``publish_runtime_binding`` rejects a symbolic-linked install root), so the
    credential store does not invent a second, laxer filesystem convention.
    """

    details = _lstat(root)
    if details is None:
        if not create:
            return
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _error(CREDENTIAL_WRITE_FAILED) from error
        details = _lstat(root)
    if details is None:
        raise _error(CREDENTIAL_WRITE_FAILED)
    if _is_unsafe_target(details) or not stat.S_ISDIR(details.st_mode):
        raise _error(CREDENTIAL_INSECURE)


def _require_safe_container(path: Path) -> None:
    """Fail closed before any read or delete touches a credential path."""

    _require_install_root(path.parent.parent, create=False)
    details = _lstat(path.parent)
    if details is None:
        raise _error(CREDENTIAL_NOT_CONFIGURED)
    if _is_unsafe_target(details) or not stat.S_ISDIR(details.st_mode):
        raise _error(CREDENTIAL_INSECURE)


def _require_private_container(directory: Path, *, create: bool) -> None:
    details = _lstat(directory)
    if details is None:
        if not create:
            raise _error(CREDENTIAL_NOT_CONFIGURED)
        _create_private_directory(directory)
        details = _lstat(directory)
    if details is None:
        raise _error(CREDENTIAL_WRITE_FAILED)
    if _is_unsafe_target(details) or not stat.S_ISDIR(details.st_mode):
        raise _error(CREDENTIAL_INSECURE)
    if _path_is_private(directory, details, directory=True):
        return
    if not create:
        raise _error(CREDENTIAL_INSECURE)
    # An explicit configure deliberately rebuilds its own private container
    # before publishing a brand new record.  It never reuses or re-reads the
    # old record, so tightening the container cannot launder an unknown file
    # into apparent trust.
    _harden_private_directory(directory)
    hardened = _lstat(directory)
    if hardened is None or not _path_is_private(directory, hardened, directory=True):
        raise _error(CREDENTIAL_INSECURE)


def _create_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error
    details = _lstat(directory)
    if details is None:
        raise _error(CREDENTIAL_WRITE_FAILED)
    if _is_unsafe_target(details) or not stat.S_ISDIR(details.st_mode):
        raise _error(CREDENTIAL_INSECURE)
    _harden_private_directory(directory)


def _require_private_record(path: Path, details: os.stat_result) -> None:
    if _is_unsafe_target(details) or not stat.S_ISREG(details.st_mode):
        raise _error(CREDENTIAL_INSECURE)
    if os.name != "nt" and details.st_nlink != 1:
        raise _error(CREDENTIAL_INSECURE)
    if not _path_is_private(path, details, directory=False):
        raise _error(CREDENTIAL_INSECURE)


def _path_is_private(
    path: Path, details: os.stat_result, *, directory: bool
) -> bool:
    """True when this path never grants an access principal other than the user.

    The private directory is the Windows boundary and must additionally carry a
    protected DACL whose ACE inherits into children; the record only has to stay
    inside that verified directory without adding an allowed principal of its
    own.
    """

    if os.name == "nt":
        return _windows_dacl_is_private(path, directory=directory)
    return _mode_is_private(details.st_mode)


def _harden_private_directory(directory: Path) -> None:
    if os.name == "nt":
        # The container ACE must also inherit into children, so a staging file
        # created inside the private directory never starts from an empty
        # inherited DACL that could deny its own creation or later cleanup.
        _windows_set_current_user_only_dacl(directory, inherit_to_children=True)
        return
    try:
        os.chmod(directory, PRIVATE_DIRECTORY_MODE)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error


def _restrict_new_record_file(descriptor: int) -> None:
    """Tighten the freshly staged record file on POSIX.

    Windows needs nothing here: the file is created inside the verified private
    directory and inherits its current-user-only DACL, and
    ``_publish_private_record`` still refuses to publish unless the staged file
    exposes no additional allowed principal.
    """

    if os.name == "nt":
        return
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error


def _owned_staging_paths(directory: Path) -> list[Path]:
    """List this store's own staging names in exactly one directory level.

    ``OSError`` is left to the caller so configure and clear can map a
    directory that cannot be listed onto their own closed code.
    """

    with os.scandir(directory) as entries:
        return [
            Path(entry.path)
            for entry in entries
            if entry.name.startswith(_TEMPORARY_PREFIX)
            and entry.name.endswith(_TEMPORARY_SUFFIX)
        ]


def _remove_owned_staging(directory: Path) -> None:
    """Delete only this store's own staging names from one directory level.

    A publish that was interrupted between staging and replacement would
    otherwise leave a second copy of a real API Key behind in the credential
    directory.
    """

    try:
        stale = _owned_staging_paths(directory)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error
    for candidate in stale:
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _error(CREDENTIAL_WRITE_FAILED) from error


def _publish_private_record(path: Path, raw: bytes) -> None:
    directory = path.parent
    _remove_owned_staging(directory)
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            dir=directory,
            prefix=_TEMPORARY_PREFIX,
            suffix=_TEMPORARY_SUFFIX,
        )
        temporary = Path(name)
        try:
            _restrict_new_record_file(descriptor)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _require_private_record(temporary, os.lstat(temporary))
        os.replace(temporary, path)
        temporary = None
        _require_private_record(path, os.lstat(path))
        _fsync_directory(directory)
    except QwenCredentialError:
        raise
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise _error(CREDENTIAL_WRITE_FAILED) from error
    finally:
        os.close(descriptor)


# ---------------------------------------------------------------------------
# Windows-native private DACL primitive
#
# `chmod` on Windows only toggles the read-only attribute, so it can never
# stand in for an ACL.  These primitives use advapi32/kernel32 through ctypes
# from the standard library: no pywin32, no ACL helper library and no
# SecretManager framework.  ``private/`` is the boundary: it is published with a
# protected DACL whose only access-allowed ACE grants the current user SID and
# inherits into children.  Records and staging files are only verified not to
# expose an additional allowed principal, because their access already comes
# from that verified container.
# ---------------------------------------------------------------------------

_WINDOWS_TOKEN_QUERY = 0x0008
_WINDOWS_TOKEN_USER = 1
_WINDOWS_SE_FILE_OBJECT = 1
_WINDOWS_DACL_SECURITY_INFORMATION = 0x00000004
_WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_WINDOWS_SE_DACL_PROTECTED = 0x1000
_WINDOWS_ACL_REVISION = 2
_WINDOWS_ACL_SIZE_INFORMATION = 2
_WINDOWS_ACCESS_ALLOWED_ACE_TYPE = 0x00
_WINDOWS_ACCESS_DENIED_ACE_TYPE = 0x01
_WINDOWS_OBJECT_INHERIT_ACE = 0x01
_WINDOWS_CONTAINER_INHERIT_ACE = 0x02
_WINDOWS_FILE_ALL_ACCESS = 0x001F01FF
_WINDOWS_ERROR_SUCCESS = 0
_WINDOWS_ACL_HEADER_BYTES = 8
_WINDOWS_ACE_HEADER_AND_MASK_BYTES = 8


class _WindowsAceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_uint16),
    ]


class _WindowsAccessAllowedAce(ctypes.Structure):
    _fields_ = [
        ("Header", _WindowsAceHeader),
        ("Mask", ctypes.c_uint32),
        ("SidStart", ctypes.c_uint32),
    ]


class _WindowsAclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


class _WindowsTokenUser(ctypes.Structure):
    _fields_ = [
        ("Sid", ctypes.c_void_p),
        ("Attributes", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _windows_libraries() -> tuple[Any, Any]:
    """Configure and cache the two Windows libraries this store needs."""

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)

    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.OpenProcessToken.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetTokenInformation.restype = ctypes.c_int
    advapi32.GetTokenInformation.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    )
    advapi32.CopySid.restype = ctypes.c_int
    advapi32.CopySid.argtypes = (ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
    advapi32.GetLengthSid.restype = ctypes.c_uint32
    advapi32.GetLengthSid.argtypes = (ctypes.c_void_p,)
    advapi32.EqualSid.restype = ctypes.c_int
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    advapi32.InitializeAcl.restype = ctypes.c_int
    advapi32.InitializeAcl.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    )
    advapi32.AddAccessAllowedAceEx.restype = ctypes.c_int
    advapi32.AddAccessAllowedAceEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.SetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.GetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
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
    advapi32.GetSecurityDescriptorControl.restype = ctypes.c_int
    advapi32.GetSecurityDescriptorControl.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_uint32),
    )
    return advapi32, kernel32


def _windows_current_user_sid() -> Any:
    """Copy the current process token user SID into an owned buffer."""

    advapi32, kernel32 = _windows_libraries()
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _WINDOWS_TOKEN_QUERY, ctypes.byref(token)
    ):
        raise _error(CREDENTIAL_INSECURE)
    try:
        size = ctypes.c_uint32(0)
        advapi32.GetTokenInformation(
            token, _WINDOWS_TOKEN_USER, None, 0, ctypes.byref(size)
        )
        if size.value == 0:
            raise _error(CREDENTIAL_INSECURE)
        information = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token,
            _WINDOWS_TOKEN_USER,
            ctypes.cast(information, ctypes.c_void_p),
            size.value,
            ctypes.byref(size),
        ):
            raise _error(CREDENTIAL_INSECURE)
        sid = ctypes.cast(information, ctypes.POINTER(_WindowsTokenUser)).contents.Sid
        if not sid:
            raise _error(CREDENTIAL_INSECURE)
        length = advapi32.GetLengthSid(ctypes.c_void_p(sid))
        if length == 0:
            raise _error(CREDENTIAL_INSECURE)
        owned = ctypes.create_string_buffer(length)
        if not advapi32.CopySid(
            length, ctypes.cast(owned, ctypes.c_void_p), ctypes.c_void_p(sid)
        ):
            raise _error(CREDENTIAL_INSECURE)
        return owned
    finally:
        kernel32.CloseHandle(token)


def _windows_set_current_user_only_dacl(
    path: Path, *, inherit_to_children: bool
) -> None:
    """Publish one protected DACL granting the current user SID only."""

    advapi32, _kernel32 = _windows_libraries()
    sid = _windows_current_user_sid()
    sid_pointer = ctypes.cast(sid, ctypes.c_void_p)
    acl_size = (
        _WINDOWS_ACL_HEADER_BYTES
        + _WINDOWS_ACE_HEADER_AND_MASK_BYTES
        + advapi32.GetLengthSid(sid_pointer)
    )
    acl = ctypes.create_string_buffer(acl_size)
    if not advapi32.InitializeAcl(
        ctypes.cast(acl, ctypes.c_void_p), acl_size, _WINDOWS_ACL_REVISION
    ):
        raise _error(CREDENTIAL_WRITE_FAILED)
    ace_flags = 0
    if inherit_to_children:
        ace_flags = _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_CONTAINER_INHERIT_ACE
    if not advapi32.AddAccessAllowedAceEx(
        ctypes.cast(acl, ctypes.c_void_p),
        _WINDOWS_ACL_REVISION,
        ace_flags,
        _WINDOWS_FILE_ALL_ACCESS,
        sid_pointer,
    ):
        raise _error(CREDENTIAL_WRITE_FAILED)
    result = advapi32.SetNamedSecurityInfoW(
        str(path),
        _WINDOWS_SE_FILE_OBJECT,
        _WINDOWS_DACL_SECURITY_INFORMATION
        | _WINDOWS_PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.cast(acl, ctypes.c_void_p),
        None,
    )
    if result != _WINDOWS_ERROR_SUCCESS:
        raise _error(CREDENTIAL_WRITE_FAILED)


def _windows_dacl_is_private(path: Path, *, directory: bool) -> bool:
    """True when this path never grants an access principal other than the user.

    For the private directory this additionally requires a protected DACL (no
    inheritable ACE from a parent can widen it later) and an inheritable
    current-user ACE, so files created inside it are born with current-user-only
    access.  For the record and staging files only the principal set is checked:
    their access is inherited from the already verified directory, and requiring
    a protected bit on every file is not what keeps the credential private.
    """

    advapi32, kernel32 = _windows_libraries()
    sid = _windows_current_user_sid()
    sid_pointer = ctypes.cast(sid, ctypes.c_void_p)
    descriptor = ctypes.c_void_p()
    acl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _WINDOWS_SE_FILE_OBJECT,
        _WINDOWS_DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(acl),
        None,
        ctypes.byref(descriptor),
    )
    if result != _WINDOWS_ERROR_SUCCESS:
        return False
    try:
        if not acl.value:
            return False
        if directory and not _windows_dacl_is_protected(advapi32, descriptor):
            return False
        return _windows_acl_grants_only(
            advapi32, acl, sid_pointer, require_inheritable_ace=directory
        )
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


def _windows_dacl_is_protected(
    advapi32: Any, descriptor: ctypes.c_void_p
) -> bool:
    """True when the descriptor carries the ``SE_DACL_PROTECTED`` control bit."""

    control = ctypes.c_uint16()
    revision = ctypes.c_uint32()
    if not advapi32.GetSecurityDescriptorControl(
        descriptor, ctypes.byref(control), ctypes.byref(revision)
    ):
        return False
    return bool(control.value & _WINDOWS_SE_DACL_PROTECTED)


def _windows_acl_grants_only(
    advapi32: Any,
    acl: ctypes.c_void_p,
    sid_pointer: ctypes.c_void_p,
    *,
    require_inheritable_ace: bool = False,
) -> bool:
    size_information = _WindowsAclSizeInformation()
    if not advapi32.GetAclInformation(
        acl,
        ctypes.byref(size_information),
        ctypes.sizeof(size_information),
        _WINDOWS_ACL_SIZE_INFORMATION,
    ):
        return False
    if size_information.AceCount == 0:
        return False
    inheritable = False
    for index in range(size_information.AceCount):
        ace_pointer = ctypes.c_void_p()
        if not advapi32.GetAce(acl, index, ctypes.byref(ace_pointer)):
            return False
        if not ace_pointer.value:
            return False
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(_WindowsAccessAllowedAce)).contents
        ace_type = ace.Header.AceType
        if ace_type == _WINDOWS_ACCESS_DENIED_ACE_TYPE:
            continue
        if ace_type != _WINDOWS_ACCESS_ALLOWED_ACE_TYPE:
            return False
        sid_address = (
            ctypes.addressof(ace) + _WindowsAccessAllowedAce.SidStart.offset
        )
        if not advapi32.EqualSid(
            ctypes.c_void_p(sid_address), sid_pointer
        ):
            return False
        if (ace.Header.AceFlags & _WINDOWS_OBJECT_INHERIT_ACE) and (
            ace.Header.AceFlags & _WINDOWS_CONTAINER_INHERIT_ACE
        ):
            inheritable = True
    return inheritable or not require_inheritable_ace
