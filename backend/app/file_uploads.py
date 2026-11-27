"""Ephemeral user-selected attachments. No bytes reach planner, logs or SQLite."""
from __future__ import annotations

import base64
import re
import secrets
import time

MAX_BYTES = 5 * 1024 * 1024
TTL_SECONDS = 600
_NAME = re.compile(r"^[\w .()-]{1,120}\.(?:pdf|png|jpe?g|txt|md|csv|docx|xlsx)$", re.I)
_uploads: dict[str, tuple[float, str, str]] = {}


def put(file_name: str, encoded: str) -> str:
    if not _NAME.fullmatch(file_name) or len(encoded) > 7_000_000:
        raise ValueError("unsupported filename or attachment size")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception:
        raise ValueError("invalid attachment encoding") from None
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("attachment must be between 1 byte and 5 MiB")
    purge()
    token = secrets.token_urlsafe(32)
    _uploads[token] = (time.monotonic() + TTL_SECONDS, file_name, encoded)
    return token


def get(token: str) -> tuple[str, str] | None:
    purge()
    found = _uploads.get(token)
    return (found[1], found[2]) if found else None


def purge() -> None:
    now = time.monotonic()
    for key, record in list(_uploads.items()):
        if record[0] <= now:
            del _uploads[key]


def remove(token: str) -> None:
    _uploads.pop(token, None)


def extract_text(token: str) -> tuple[str, str]:
    """Return ``(file_name, plain_text)`` for an uploaded resume.

    Stream E (R4-10): attachments used to reach the planner as a
    filename only. This reads the stored bytes back through
    :mod:`app.extractors.resume` (TXT / PDF / DOCX) so a resume upload
    yields parsed text the job-discovery flow can use. Raises
    ``KeyError`` for an unknown/expired token and ``ValueError`` for an
    unsupported or unreadable file. The stored bytes are unchanged.
    """
    from .extractors.resume import extract_text as _resume_text

    found = get(token)
    if found is None:
        raise KeyError("unknown or expired upload token")
    file_name, encoded = found
    raw = base64.b64decode(encoded, validate=True)
    return file_name, _resume_text(raw, file_name)


def parse_resume_upload(token: str):
    """Return ``(file_name, ResumeProfile)`` for an uploaded resume."""
    from .extractors.resume import parse_resume

    file_name, text = extract_text(token)
    return file_name, parse_resume(text)
