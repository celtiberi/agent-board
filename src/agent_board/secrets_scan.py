"""Runtime secret collection and active scrubbing for messages, records, and text uploads."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlparse, urlunparse

REDACTED = "[REDACTED]"

DEFAULT_MASK_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "private_key",
        "secret_key",
        "auth_token",
        "webhook_secret",
        "hmac_secret",
    }
)

_SECRET_ENV_KEY_TOKENS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASS",
    "PWD",
    "AUTH",
    "DSN",
    "DATABASE_URL",
    "REDIS_URL",
    "AMQP_URL",
    "PRIVATE",
    "CREDENTIAL",
    "CRED",
    "BEARER",
    "SIGNING",
    "ENCRYPT",
    "WEBHOOK",
    "COOKIE",
    "SESSION",
)

_NON_SECRET_ENV_SUFFIXES: tuple[str, ...] = (
    "_ENV",
    "_HOST",
    "_PORT",
    "_LEVEL",
    "_MODEL",
    "_DIR",
    "_PATH",
    "_REGION",
)

_NON_SECRET_EXACT_KEYS: frozenset[str] = frozenset(
    {
        "ENV",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "HOST",
        "PORT",
        "MODEL",
        "DEBUG",
        "TZ",
        "LANG",
        "LC_ALL",
        "HOME",
        "USER",
        "PWD",
        "SHELL",
        "TERM",
    }
)

_NON_SECRET_VALUES: frozenset[str] = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "none",
        "null",
        "production",
        "development",
        "staging",
        "testing",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "standard",
        "detailed",
        "concise",
    }
)

_EXAMPLE_DOTENV_SUFFIXES: tuple[str, ...] = (
    ".example",
    ".sample",
    ".template",
    ".dist",
)

_SECRET_QUERY_KEYS: frozenset[str] = frozenset(
    {"apikey", "api_key", "token", "secret", "password", "key", "auth"}
)

_IGNORE_VALUE_PREFIXES: tuple[str, ...] = (
    "vault:",
    "env:",
    "<",
    "$",
    "redacted",
    "[redacted]",
    "example",
    "placeholder",
    "your_",
    "your-",
    "replace_with",
    "changeme",
    "dummy",
    "test_",
    "fake_",
)

_IGNORE_EXACT_WORDS: frozenset[str] = frozenset(
    {
        "none",
        "null",
        "true",
        "false",
        "empty",
        "unset",
        "missing",
        "rotated",
        "expired",
        "valid",
        "invalid",
        "required",
        "configured",
        "redacted",
        "[redacted]",
    }
)

_NUMBER_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_PATH_PREFIX_RE = re.compile(r"^(?:/|\./|\.\./|~/|[A-Za-z]:\\)")
_EXPORT_PREFIX_RE = re.compile(r"^export\s+")

_URL_RE = re.compile(
    r"(?i)\b(?:https?|postgres(?:ql)?|mysql|redis|rediss|amqp|amqps|mongodb(?:\+srv)?|imap|imaps|smtp|smtps|ftp)://[^\s<>\"'`]+"
)

_RAW_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bab_[A-Za-z0-9_-]{24,}"),
    re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35,}"),
    re.compile(r"\bgh[pous]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bFlyV1\s+fm[12]_[A-Za-z0-9+/=_-]{20,}"),
    re.compile(r"\bfo1_[A-Za-z0-9_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)

_AUTH_HEADER_RE = re.compile(r"(?i)\b(Authorization\s*:\s*(?:Bearer|Basic)\s+)([A-Za-z0-9._+/=-]{12,})")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)([A-Za-z0-9._+/=-]{20,})")

_LABELED_PASSWORD_RE = re.compile(
    r"(?i)\b(bridge\s+password|imap\s+password|app\s+password|password|passwd|passphrase)"
    r"(\s*[=:]\s*['\"`]?)([^\s'\"`\n,;]{6,})(['\"`]?)"
)

_LABELED_SECRET_RE = re.compile(
    r"(?i)\b(api[ _-]?key|apikey|client_secret|webhook_secret|signing_secret|"
    r"access_token|refresh_token|auth_token|secret_key|private_key|token|secret)"
    r"(\s*[=:]\s*['\"`]?)([A-Za-z0-9._+/=-]{12,})(['\"`]?)"
)


def _strip_inline_comment(raw: str) -> str:
    for idx, ch in enumerate(raw):
        if ch == "#" and idx > 0 and raw[idx - 1].isspace():
            return raw[:idx].rstrip()
    return raw.rstrip()


def parse_dotenv_file(path: Path) -> list[tuple[str, str]]:
    """Parse KEY=VALUE pairs safely without shell execution."""
    entries: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return entries

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        raw_key, raw_val = stripped.split("=", 1)
        key = _EXPORT_PREFIX_RE.sub("", raw_key.strip(), count=1).strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        else:
            val = _strip_inline_comment(val)
        entries.append((key, val))
    return entries


def _is_example_dotenv(path: Path) -> bool:
    lower_name = path.name.lower()
    return any(lower_name.endswith(suf) for suf in _EXAMPLE_DOTENV_SUFFIXES)


def _normalize_key(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def _is_ignored_value(val: str) -> bool:
    lowered = val.strip().lower()
    if not lowered or lowered in _IGNORE_EXACT_WORDS:
        return True
    return any(lowered.startswith(prefix) for prefix in _IGNORE_VALUE_PREFIXES)


def _is_masked_placeholder(val: str) -> bool:
    return set(val.strip()) <= {"*", "x", "X", "."}


def _is_secret_env_key(name: str) -> bool:
    upper = name.strip().upper()
    if not upper or upper in _NON_SECRET_EXACT_KEYS:
        return False
    if upper.endswith(_NON_SECRET_ENV_SUFFIXES) and not any(
        tok in upper for tok in ("SECRET", "TOKEN", "PASS", "PRIVATE", "AUTH")
    ):
        return False
    return any(tok in upper for tok in _SECRET_ENV_KEY_TOKENS)


def _is_url_like(val: str) -> bool:
    return "://" in val and not val.startswith("://")


def _extract_url_secrets(val: str) -> tuple[bool, list[str]]:
    """Return (url_itself_has_secret, extracted_secret_parts)."""
    try:
        parsed = urlparse(val)
    except ValueError:
        return False, []
    if not parsed.scheme or not parsed.netloc:
        return False, []
    found: list[str] = []
    has_secret = False
    if parsed.password and len(parsed.password) >= 4:
        has_secret = True
        found.append(parsed.password)
        unquoted_pw = unquote(parsed.password)
        if unquoted_pw and unquoted_pw != parsed.password:
            found.append(unquoted_pw)
    if parsed.query:
        for segment in parsed.query.split("&"):
            if "=" not in segment:
                continue
            qk, qv = segment.split("=", 1)
            if unquote(qk).strip().lower() in _SECRET_QUERY_KEYS and len(qv) >= 4:
                has_secret = True
                found.append(qv)
                unq = unquote(qv)
                if unq and unq != qv:
                    found.append(unq)
    if has_secret:
        found.append(val)
    return has_secret, found


def _should_keep_secret_value(val: str, *, min_len: int = 8, allow_plain_url: bool = False) -> bool:
    stripped = val.strip()
    if len(stripped) < min_len or _is_ignored_value(stripped):
        return False
    if stripped.lower() in _NON_SECRET_VALUES:
        return False
    if _NUMBER_RE.match(stripped):
        return False
    if _PATH_PREFIX_RE.match(stripped) and "://" not in stripped:
        return False
    if _is_url_like(stripped):
        if allow_plain_url:
            return True
        has_secret, _ = _extract_url_secrets(stripped)
        return has_secret
    return True


def _collect_env_pair(key: str, val: str, collected: set[str]) -> None:
    stripped = val.strip()
    if not stripped:
        return
    if _is_url_like(stripped):
        has_secret, url_secrets = _extract_url_secrets(stripped)
        if has_secret:
            collected.update(s for s in url_secrets if len(s) >= 4 and not _is_ignored_value(s))
        if _is_secret_env_key(key) and _should_keep_secret_value(stripped, min_len=8, allow_plain_url=True):
            collected.add(stripped)
        return
    if _is_secret_env_key(key) and _should_keep_secret_value(stripped, min_len=8):
        collected.add(stripped)


def _scrub_url_credentials(text: str) -> str:
    def _replace_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        suffix = ""
        while raw_url and raw_url[-1] in ").,;]>'\"":
            suffix = raw_url[-1] + suffix
            raw_url = raw_url[:-1]
        if not raw_url:
            return match.group(0)

        url_to_parse = raw_url.replace(f":{REDACTED}@", ":__REDACTED__@")
        try:
            parsed = urlparse(url_to_parse)
        except ValueError:
            return match.group(0)
        if not parsed.scheme or not parsed.netloc:
            return match.group(0)

        new_netloc = parsed.netloc
        netloc_changed = False
        if "@" in parsed.netloc:
            userinfo, _, hostpart = parsed.netloc.rpartition("@")
            if ":" in userinfo:
                user, _, pw = userinfo.partition(":")
                if pw == "__REDACTED__":
                    new_netloc = f"{user}:{REDACTED}@{hostpart}"
                else:
                    unq_pw = unquote(pw)
                    if len(unq_pw) >= 4 and not _is_ignored_value(unq_pw) and not _is_masked_placeholder(unq_pw):
                        new_netloc = f"{user}:{REDACTED}@{hostpart}"
                        netloc_changed = True

        new_query = parsed.query
        query_changed = False
        if parsed.query:
            rebuilt_parts: list[str] = []
            for segment in parsed.query.split("&"):
                if "=" in segment:
                    qk, qv = segment.split("=", 1)
                    unq_val = unquote(qv)
                    if (
                        unquote(qk).strip().lower() in _SECRET_QUERY_KEYS
                        and len(unq_val) >= 4
                        and not _is_ignored_value(unq_val)
                        and not _is_masked_placeholder(unq_val)
                    ):
                        rebuilt_parts.append(f"{qk}={REDACTED}")
                        query_changed = True
                        continue
                rebuilt_parts.append(segment)
            new_query = "&".join(rebuilt_parts)

        if not netloc_changed and not query_changed:
            return match.group(0)

        rebuilt = urlunparse(parsed._replace(netloc=new_netloc, query=new_query))
        return rebuilt + suffix

    return _URL_RE.sub(_replace_url, text)


def _scrub_prefixed_match(match: re.Match[str]) -> str:
    prefix, val = match.group(1), match.group(2)
    if _is_ignored_value(val) or _is_masked_placeholder(val):
        return match.group(0)
    return f"{prefix}{REDACTED}"


def _scrub_labeled_match(match: re.Match[str]) -> str:
    label, sep, val, closing = match.group(1), match.group(2), match.group(3), match.group(4)
    if _is_ignored_value(val) or _is_masked_placeholder(val):
        return match.group(0)
    return f"{label}{sep}{REDACTED}{closing}"


class SecretRedactor:
    """Collects runtime secrets and actively scrubs secrets from strings and JSON structures."""

    def __init__(self, secrets: Sequence[str] = (), extra_keys: Sequence[str] = ()) -> None:
        import json

        expanded: set[str] = set()
        for raw_s in secrets:
            if not raw_s:
                continue
            expanded.add(raw_s)
            escaped = json.dumps(raw_s, ensure_ascii=False)[1:-1]
            if escaped and escaped != raw_s:
                expanded.add(escaped)
            if len(raw_s) >= 8:
                url_q = quote(raw_s, safe="")
                if url_q and url_q != raw_s:
                    expanded.add(url_q)
                url_qp = quote_plus(raw_s)
                if url_qp and url_qp != raw_s:
                    expanded.add(url_qp)
        unique_secrets = sorted(expanded, key=len, reverse=True)
        self.secrets: tuple[str, ...] = tuple(unique_secrets)
        self.mask_keys: frozenset[str] = DEFAULT_MASK_KEYS | frozenset(
            _normalize_key(k) for k in extra_keys if str(k).strip()
        )
        if self.secrets:
            pattern = "|".join(re.escape(s) for s in self.secrets)
            self._pattern: re.Pattern[str] | None = re.compile(pattern)
        else:
            self._pattern = None

    @classmethod
    def from_environment(
        cls,
        *,
        extra_secrets: Sequence[str] = (),
        env_files: Sequence[str | Path] = (),
        extra_keys: Sequence[str] = (),
    ) -> SecretRedactor:
        """Collect secret values from env_files, os.environ, and explicit extra_secrets."""
        collected: set[str] = set()

        for raw_file in env_files:
            p = Path(raw_file).expanduser()
            if p.is_file() and not _is_example_dotenv(p):
                for key, val in parse_dotenv_file(p):
                    _collect_env_pair(key, val, collected)

        for key, val in os.environ.items():
            if val:
                _collect_env_pair(key, val, collected)

        for secret in extra_secrets:
            if secret and len(secret) >= 8 and not _is_ignored_value(secret):
                collected.add(secret)

        return cls(secrets=sorted(collected), extra_keys=extra_keys)

    def scrub_text(self, text: str) -> tuple[str, bool]:
        """Return (scrubbed_text, changed) with runtime secrets, URLs, tokens, and labeled secrets redacted."""
        if not text:
            return text, False

        out = text
        if self._pattern is not None:
            out = self._pattern.sub(REDACTED, out)

        out = _scrub_url_credentials(out)

        for pattern in _RAW_TOKEN_PATTERNS:
            out = pattern.sub(REDACTED, out)

        out = _AUTH_HEADER_RE.sub(_scrub_prefixed_match, out)
        out = _BEARER_RE.sub(_scrub_prefixed_match, out)
        out = _LABELED_PASSWORD_RE.sub(_scrub_labeled_match, out)
        out = _LABELED_SECRET_RE.sub(_scrub_labeled_match, out)

        return out, out != text

    def scrub_data(self, value: Any) -> tuple[Any, bool]:
        """Recursively scrub secrets and mask sensitive dict keys inside JSON-like structures."""
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            changed = False
            out: dict[str, Any] = {}
            for k, v in value.items():
                key_str = str(k)
                scrubbed_k, k_changed = self.scrub_text(key_str)
                if k_changed:
                    changed = True
                if _normalize_key(key_str) in self.mask_keys and isinstance(v, str):
                    if v.strip() and not _is_ignored_value(v) and not _is_masked_placeholder(v):
                        out[scrubbed_k] = REDACTED
                        changed = True
                    else:
                        out[scrubbed_k] = v
                else:
                    scrubbed_v, v_changed = self.scrub_data(v)
                    if v_changed:
                        changed = True
                    out[scrubbed_k] = scrubbed_v
            return out, changed
        if isinstance(value, list):
            changed = False
            items: list[Any] = []
            for item in value:
                scrubbed_item, item_changed = self.scrub_data(item)
                if item_changed:
                    changed = True
                items.append(scrubbed_item)
            return items, changed
        return value, False


DEFAULT_REDACTOR = SecretRedactor()


def scrub_text_and_warnings(text: str, redactor: SecretRedactor | None = None) -> tuple[str, list[str]]:
    """Scrub text using the given redactor (or default pattern redactor) and return warnings."""
    active = redactor or DEFAULT_REDACTOR
    clean, changed = active.scrub_text(text)
    return clean, (["secret redacted"] if changed else [])


def scan_for_secrets(text: str, redactor: SecretRedactor | None = None) -> list[str]:
    """Return warning strings if text contains a secret that would be redacted."""
    _, warnings = scrub_text_and_warnings(text, redactor=redactor)
    return warnings
