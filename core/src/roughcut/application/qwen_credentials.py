"""Configure, read and clear the current user's Qwen Filetrans credential.

WP3A owns exactly three public behaviours and nothing else:

* ``configure`` / replace -- store both fields, replacing any previous record
  atomically;
* ``readiness`` -- a pure local read that never contacts Qwen or DashScope,
  never uploads audio and never validates the API Key against the provider;
* ``clear`` -- remove only the Qwen credential record, idempotently.

Readiness therefore means "locally configured", not "provider accepted" and not
"Cloud transcription enabled".  A credential that is present but rejected by the
provider with 401/403 is a WP3B runtime failure, not a WP3A readiness result,
and a never-configured credential is a setup/readiness state that never creates a
``transcribe_source`` MediaOperation.

This module never reads ``DASHSCOPE_API_KEY``/``DASHSCOPE_WORKSPACE_ID``.  Those
names stay Spike/dev/CI concepts, so production readiness can never treat an
environment variable as a fallback for credential truth.

Every returned payload is non-secret: a status, the fixed provider identity, one
boolean saying whether the Workspace ID is present, and a next action when the
record cannot be used.  Neither the API Key nor the credential store path is ever
returned.
"""

from __future__ import annotations

from pathlib import PurePath

from roughcut.adapters.qwen_credential_store import (
    CREDENTIAL_INSECURE,
    CREDENTIAL_INVALID,
    CREDENTIAL_MALFORMED,
    CREDENTIAL_NOT_CONFIGURED,
    CREDENTIAL_PROVIDER,
    CREDENTIAL_UNSUPPORTED_FORMAT,
    QwenCredentialError,
    clear_credential,
    read_credential,
    write_credential,
)

CREDENTIAL_STATUS_NOT_CONFIGURED = "not_configured"
CREDENTIAL_STATUS_CONFIGURED = "configured"
CREDENTIAL_STATUS_INVALID = "invalid"
CREDENTIAL_STATUS_INSECURE = "insecure"

QWEN_CREDENTIAL_STATUSES = frozenset(
    {
        CREDENTIAL_STATUS_NOT_CONFIGURED,
        CREDENTIAL_STATUS_CONFIGURED,
        CREDENTIAL_STATUS_INVALID,
        CREDENTIAL_STATUS_INSECURE,
    }
)

QWEN_CREDENTIAL_CONFIGURE_NEXT_ACTION = "qwen_credential_configure"

PUBLIC_INVALID_ARGUMENTS = "invalid_arguments"
PUBLIC_OPERATION_FAILED = "qwen_credential_operation_failed"

_STATUS_BY_STORE_CODE = {
    CREDENTIAL_NOT_CONFIGURED: CREDENTIAL_STATUS_NOT_CONFIGURED,
    CREDENTIAL_MALFORMED: CREDENTIAL_STATUS_INVALID,
    CREDENTIAL_UNSUPPORTED_FORMAT: CREDENTIAL_STATUS_INVALID,
    CREDENTIAL_INSECURE: CREDENTIAL_STATUS_INSECURE,
}


def qwen_credential_readiness(*, root: PurePath | None = None) -> dict[str, object]:
    """Return the non-secret local readiness object for the Qwen credential."""

    try:
        read_credential(root)
    except QwenCredentialError as error:
        return _readiness_payload(credential_readiness_status(error))
    return _readiness_payload(CREDENTIAL_STATUS_CONFIGURED)


def credential_readiness_status(error: QwenCredentialError) -> str:
    """Map one closed store failure to the closed readiness status.

    This is the single collapse of the store's internal error codes onto the
    public ``not_configured`` / ``invalid`` / ``insecure`` vocabulary, so the
    readiness surface and any caller that must stop on an unusable credential
    cannot drift apart.
    """

    return _STATUS_BY_STORE_CODE.get(error.code, CREDENTIAL_STATUS_INSECURE)


def configure_qwen_credential(
    *,
    api_key: object,
    workspace_id: object,
    root: PurePath | None = None,
) -> dict[str, object]:
    """Store or replace the credential, then read readiness back."""

    write_credential(root, api_key=api_key, workspace_id=workspace_id)
    return qwen_credential_readiness(root=root)


def clear_qwen_credential(*, root: PurePath | None = None) -> dict[str, object]:
    """Remove only the Qwen credential record, then read readiness back."""

    clear_credential(root)
    return qwen_credential_readiness(root=root)


def public_credential_error_code(error: QwenCredentialError) -> str:
    """Map one closed store failure to the closed public error code."""

    if error.code == CREDENTIAL_INVALID:
        return PUBLIC_INVALID_ARGUMENTS
    return PUBLIC_OPERATION_FAILED


def _readiness_payload(status: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": CREDENTIAL_PROVIDER,
        "status": status,
        "workspace_id_configured": status == CREDENTIAL_STATUS_CONFIGURED,
    }
    if status != CREDENTIAL_STATUS_CONFIGURED:
        payload["next_action"] = QWEN_CREDENTIAL_CONFIGURE_NEXT_ACTION
    return payload
