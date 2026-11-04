"""Credential scanning and redaction shared by Stage C (publication, prompts, task history) and every path that
stores or serves provider/model text (worker errors, Desk read models). Stdlib only: no app import, no cycle."""
import base64
import functools
import json
import logging
import re

# Credential shapes: specific enough to refuse a publication on. JWTs are recognised by decoding (_jwts), not by shape.
# The coder's exact Claude token format (known kind and version, long body) after anything but the tail of a word
# (`task-`, `desk-`, `risk-` end in two lowercase letters) unless its body is random-looking (6+ uppercase letters,
# as a real token's base64 body has): `ab<token>`, `xsk-ant-...`, `_sk-ant-...` match, `task-ant-api01-build-...`
# does not.
_CREDENTIAL_SHAPES = (r"(?-i:(?:(?<![a-z]{2})|(?=sk-ant-[a-z]{3,5}\d{2}-(?:[a-z0-9_-]{0,40}+[A-Z]){6}))"   # case-sensitive
                      r"sk-ant-(?:oat|api|ort|sid|admin)\d{2}-[\w-]{40,}+)|"
                      r"(?<!\w)(?:sk-ant-[\w-]{8,}|github_pat_\w{8,}|gh[pousr]_\w{16,}|sk-[\w-]{16,}|xox[abprs]-[\w-]{8,}"
                      r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
_CREDENTIAL = re.compile(_CREDENTIAL_SHAPES)
# A dotted base64url run, matched only at its start and possessively: linear time on any input (an unanchored
# backtracking pattern rescanned every suffix of a long run, quadratic in its length). `=` admits padded segments.
_DOTTED = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]++=*+(?:\.(?:[A-Za-z0-9_-]++=*+)?+){2,}")
_MAX_JWT_HEADER = 2048   # a JOSE header is a few hundred bytes; longer segments are never decoded
# Joining for split-credential detection: line breaks (with their indentation and continuation backslashes) and
# string-literal boundaries (`"a" "b"`, `'a' + 'b'`) vanish, so a token wrapped over lines or written as adjacent
# literals reads whole again. Spaces inside a line stay, so words do not glue onto a token and hide it.
_LINE_JOIN = re.compile(r"(?<![ \t])[ \t]*+\\?[ \t]*+(?:\r\n?|\n)[ \t]*+")
_LITERAL_JOIN = re.compile(r"[\"'][ \t]*+(?:\+[ \t]*+)?[rubfRUBF]{0,2}[\"']")
_COMMENTS = re.compile(r"#[^\r\n]*")


@functools.lru_cache(maxsize=1024)
def _json_object_segment(segment: str) -> bool:
    # JSON permits leading whitespace, so its base64 header need not start with `e`.
    if len(segment) < 3 or len(segment) > _MAX_JWT_HEADER:
        return False
    segment = segment.rstrip("=")
    try:
        return isinstance(json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))), dict)
    except (ValueError, UnicodeDecodeError):
        return False


def _jwts(text: str) -> list[tuple[int, int]]:
    """Non-overlapping spans of JWT-shaped tokens: a header that base64url-decodes to a JSON object (any formatting,
    padded or not), a non-empty payload (not parsed: odd claims are still masked) and a signature segment (empty for
    alg=none). Every part of a dotted run is tried as a header, so a prefix (`x.eyJ...`) cannot hide one."""
    spans = []
    for run in _DOTTED.finditer(text):
        parts, position, i = run.group(0).split("."), run.start(), 0
        starts = [position]
        for part in parts[:-1]:
            starts.append(starts[-1] + len(part) + 1)
        while i < len(parts) - 2:
            if parts[i + 1] and _json_object_segment(parts[i]):
                spans.append((starts[i], starts[i + 2] + len(parts[i + 2])))
                i += 3
            else:
                i += 1
    return spans


def _mask_jwts(text: str) -> str:
    out, last = [], 0
    for start, end in _jwts(text):
        out += [text[last:start], "***"]
        last = end
    return "".join(out) + text[last:]


def _joined_forms(text: str) -> set[str]:
    # Keep both forms: a comment can separate literals or contain the credential itself.
    return {_LITERAL_JOIN.sub("", _LINE_JOIN.sub("", t)) for t in (text, _COMMENTS.sub("", text))}


def _has_credential(text: str) -> bool:
    """A credential shape or a JWT, including split literals, comments and wrapped lines."""
    return any(_CREDENTIAL.search(t) or _jwts(t) for t in {text, *_joined_forms(text)})



# Additions to the base scanner above (redaction only; publication keeps the base precision): text holding a JWT
# header followed by  or a private-key marker is withheld whole, and credential-word assignments are masked.
_ENV_ASSIGN = (
    r"\b[A-Za-z0-9_]{0,60}?(?i:secret|password|passwd|private_key|api_key|access_key|token)[A-Za-z0-9_]{0,40}+['\"]?+"
    r"\s*+[:=]\s*+(?!['\"]?+(?i:fixture|example|dummy|placeholder|changeme|fake|xxx|redacted)\b)"
    r"(?:\"[^\"\n]{4,}+\"|'[^'\n]{4,}+'|[^\s'\",;]{8,}+)"
    r"|(?i:\b[a-z][a-z0-9+.-]{0,30}+)://[^@/\s:{}$<>%?#]*+:[^@/\s{}$<>%?#]++@")
# Multi-line secrets whose marker is one line and whose body is many: masking the marker would keep the body.
_PRIVATE_KEY = re.compile(r"PRIVATE KEY(?: BLOCK)?-----|PuTTY-User-Key-File-\d|Private-Lines: \d")
# A whole base64url segment followed by , up to 16 KiB (x5c headers run to a few KiB). Anchored and possessive.
_HEADER_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,16384}+=*+(?=\.)")
_WITHHELD = "[withheld: credential-shaped content]"


def _is_jose_header(segment: str) -> bool:
    # `{` encodes to `e`: characters glued before the header (`log_eyJ...`) are skipped by retrying at each of the
    # first 8 `e` within 64 characters. Bounded, so a hostile run costs at most 9 decodes.
    segment = segment.rstrip("=")
    starts = [0] + [i for i in range(1, min(64, len(segment))) if segment[i] == "e"][:8]
    return any(_decodes_to_jose(segment[i:]) for i in starts)


@functools.lru_cache(maxsize=4096)
def _decodes_to_jose(segment: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except ValueError:
        return False
    if not raw.lstrip(b" \t\r\n").startswith(b"{"):   # not a JSON object: rejected before parsing
        return False
    try:
        header = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(header, dict) and "alg" in header   # parsed, so an escaped key (\u0061lg) counts too


def _has_jose_header(text: str) -> bool:
    """A JWT/JWS header (RFC 7515: a JSON object with "alg") followed by `.`: enough to withhold text, since a token's
    payload and signature can sit on the next line or after a split."""
    return any(_is_jose_header(m.group(0)) for m in _HEADER_CANDIDATE.finditer(text))


def _must_withhold(text: str) -> bool:
    """A secret spread over lines (JWT, PEM/OpenSSH/PGP/PuTTY key): only withholding the whole text keeps it out."""
    return bool(_PRIVATE_KEY.search(text)) or _has_jose_header(text)

# Masking also takes key=value heuristics; they over-match ordinary code (`token = acquire(...)`), so they mask only.
# `['"]?` after the key covers JSON ("refresh_token": "...") as well as `token=...`; a bare `rt_...` value is masked
# too. Not in _CREDENTIAL_SHAPES: publication blocking keeps its precision (`rt_` also starts ordinary identifiers).
_KV_BASE = (r"|(?:bearer|token|authorization)\s*+(?:[:=]\s*+)?[\w.~+/=-]{12,}"
            r"|(?:api[_-]?key|secret|password|passwd|token)['\"]?\s*+[:=]\s*+['\"]?[^\s'\"]{6,}"
            r"|(?<![\w-])(?-i:rt_)[\w.-]{32,}+")   # a bare Codex refresh token (its JSON key gone): masked only
_SECRETS = re.compile(r"(?i)(?:" + _CREDENTIAL_SHAPES + _KV_BASE + ")")
# Second pass after the base masking (which keeps its leftmost, whole-value matches): credential-word assignments
# the base misses, e.g. a quoted value with spaces.
_ENV = re.compile(_ENV_ASSIGN)


def _redact_secrets(text: str) -> str:
    """URL userinfo and well-known credential shapes, masked. Output that reaches task history or a prompt passes here."""
    if _must_withhold(text):
        return _WITHHELD
    # Check joined fragments before masking can erase their header and leave claims/signatures behind.
    if any(joined != text and (_CREDENTIAL.search(joined) or _jwts(joined)) for joined in _joined_forms(text)):
        return _WITHHELD
    masked = _ENV.sub("***", _SECRETS.sub("***", re.sub(r"://[^@/\s]+@", "://***@", _mask_jwts(text))))
    return masked


def _redact(text: str) -> str:
    return _redact_secrets(text)[:300]


UNAVAILABLE = "[unavailable]"


def redact(text: str) -> str:
    """_redact_secrets for text bound for the database, the Desk or a notification, failing closed: a scanner error
    yields a fixed placeholder, never the raw text."""
    try:
        return _redact_secrets(text)
    except Exception:  # noqa: BLE001 - any failure, including a non-string, must not let the text through
        logging.getLogger("company.redact").error("redaction_failed")   # fixed name: no text, no exception detail
        return UNAVAILABLE
