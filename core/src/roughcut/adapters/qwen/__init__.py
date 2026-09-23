"""Qwen Filetrans cloud ASR transport and normalization adapters.

The fixed V1 Cloud service identity lives here so the transport adapter and the
normalizer describe exactly one evidenced service from a single source of truth.
There is no region probing, endpoint fallback, provider registry or
user-supplied endpoint anywhere in this package.

The accepted API Key / Workspace ID shape is also defined here so that the
transport adapter and the Roughcut credential store cannot drift apart.
"""

import re
import unicodedata

MODEL_NAME = "qwen-audio-3.0-asr-flash-filetrans"
REGION = "cn-beijing"
TRANSPORT_MODE = "temporary_upload"
CLOUD_AUDIO_PROFILE = "16_khz_mono_flac"
LANGUAGE_HINTS = ("zh", "en")
CHANNEL_ID = 0
BACKEND = "qwen_filetrans"

# One shared Workspace ID identity shape.  It is a non-secret service identity,
# not a secret, and it is the only accepted Qwen workspace grammar in Core.
WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

# A credential is carried into an HTTP header, a log line and a terminal, so
# every control, format, line/paragraph-separator and surrogate character is an
# injection shape and is never accepted.  The set is expressed in Unicode
# general categories so that C0/C1 controls, DEL, NEL, zero-width and bidi
# format characters, U+2028/U+2029 line breaks and lone surrogates are rejected
# together rather than by an incomplete literal list.  This is still not a
# secret-strength or charset rule: length, prefix and alphabet stay free, and a
# no-break space or an ordinary space is not rejected here.
CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def credential_fields_are_valid(*, api_key: object, workspace_id: object) -> bool:
    """Return whether one injected credential has the frozen accepted shape.

    This is a shape check only.  It is deliberately not a secret-strength
    validator and not a provider authentication check: an API Key that is empty
    or carries any control/format/line-separator/surrogate character, and a
    Workspace ID outside the one workspace grammar, are the only rejected forms,
    because those are the only forms the frozen V1 transport cannot carry
    safely.
    """

    if not isinstance(api_key, str) or not api_key.strip():
        return False
    if any(
        unicodedata.category(character) in CREDENTIAL_FORBIDDEN_CHARACTER_CATEGORIES
        for character in api_key
    ):
        return False
    return (
        isinstance(workspace_id, str)
        and WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is not None
    )
