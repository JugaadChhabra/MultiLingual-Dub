"""Turn a provider failure into something an operator can act on.

Two people use this app: a sound engineer and a video editor. Neither reads
logs, and neither should have to. A failure has to arrive as a cause, an impact
and a way out — "ElevenLabs credits exhausted, 4 clips could not be voiced" —
not as a traceback.

The one rule this module must never break: **do not guess confidently.** A
failure we do not recognise says where it broke and keeps the provider's own
words, and claims nothing else. A wrong confident cause is worse than no cause,
because it sends someone to top up an account that was never the problem.

Confidence is honest about itself. Rate limits, 5xx, auth and config are certain
because the status code alone determines them. The quota shapes below are
inferred from each provider's documented responses and are matched on several
signals at once; if a real exhausted-credit response disagrees, widen the
matching here rather than loosening it elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

PROVIDER_NAMES = {
    "elevenlabs": "ElevenLabs",
    "sarvam": "Sarvam",
    "heygen": "HeyGen",
    "gemini": "Gemini",
    "s3": "S3",
    "nas": "the NAS",
}

STAGE_NAMES = {
    "tts": "voicing",
    "translate": "translation",
    "render": "render",
    "upload": "upload",
    "nas_upload": "NAS upload",
    "download": "download",
    "stt": "transcription",
    # the video pipeline's own status values, which double as its stages
    "uploading": "upload to HeyGen",
    "generating": "render",
    "polling": "render",
    "queued": "queue",
}

TOP_UP_URLS = {
    "elevenlabs": "https://elevenlabs.io/app/subscription",
    "sarvam": "https://dashboard.sarvam.ai/",
    "heygen": "https://app.heygen.com/settings/subscriptions",
}

# HeyGen's own code for "you are out of what you paid for". Already referenced
# in services/video_pipeline/heygen_client.py for the talking-photo cap.
HEYGEN_QUOTA_CODES = {401028, 400112}

_QUOTA_WORDS = re.compile(
    r"quota[_ ]?exceeded|insufficient[_ ]?(credit|balance|quota|fund)|"
    r"out of (credit|quota)|credit[s]? (exhaust|expired|over)|"
    r"exceeded your current quota|no (remaining )?credit",
    re.I,
)
_AUTH_WORDS = re.compile(r"invalid[_ ]?api[_ ]?key|unauthorized|invalid token|authentication fail", re.I)


@dataclass
class Cause:
    """What the operator is shown, plus what the caller needs to decide next."""

    kind: str                       # quota | rate_limit | provider_down | auth | config | nas_local | upload | unknown
    title: str                      # one line, sentence case, no jargon
    detail: str = ""                # impact and what happens next
    severity: str = "error"         # error | warn | info
    retryable: bool = False         # is trying again capable of working
    provider: str | None = None
    stage: str | None = None
    action_url: str | None = None   # where to go and fix it
    action_label: str | None = None
    raw: str | None = None          # the provider's own words, kept for the disclosure
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "severity": self.severity,
            "retryable": self.retryable,
            "provider": self.provider,
            "stage": self.stage,
            "action_url": self.action_url,
            "action_label": self.action_label,
            "raw": self.raw,
            "meta": self.meta,
        }


def _status_of(exc: Any) -> int | None:
    """Mirrors services/retry.py, which already reads status codes off any
    provider exception. Kept in step with it deliberately."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _body_of(exc: Any) -> str:
    response = getattr(exc, "response", None)
    for attr in ("text", "content"):
        value = getattr(response, attr, None)
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8", "replace")
            except Exception:
                return ""
        if isinstance(value, str):
            return value
    return ""


def _text_of(exc: Any) -> str:
    try:
        return str(exc) if exc is not None else ""
    except Exception:
        return ""


def _heygen_code(body: str) -> int | None:
    try:
        payload = json.loads(body)
    except Exception:
        return None
    for key in ("code", "error_code"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        value = error.get("code")
        if isinstance(value, int):
            return value
    return None


def infer_provider(exc: Any) -> str | None:
    """Work out who failed when the call site does not know.

    Most failures arrive as a provider SDK's own exception, so the module it was
    defined in names the provider. Falling back to the message keeps httpx and
    requests errors attributable too.
    """
    module = getattr(type(exc), "__module__", "") or ""
    text = f"{module} {_text_of(exc)} {_body_of(exc)}".lower()
    for key in ("elevenlabs", "sarvam", "heygen", "gemini"):
        if key in text:
            return key
    if "boto" in text or "s3" in module.lower():
        return "s3"
    return None


def classify(exc: Any, *, provider: str | None, stage: str | None = None) -> Cause:
    """Never raises. Worst case is an honest 'unknown'."""
    try:
        if provider is None:
            provider = infer_provider(exc)
        return _classify(exc, provider=provider, stage=stage)
    except Exception:  # a broken classifier must not break the job it describes
        return Cause(
            kind="unknown",
            title="Something failed and could not be identified",
            detail="The raw message is below.",
            provider=provider,
            stage=stage,
            raw=_text_of(exc) or None,
        )


def _classify(exc: Any, *, provider: str | None, stage: str | None) -> Cause:
    name = PROVIDER_NAMES.get(provider or "", (provider or "").title() or "The pipeline")
    stage_name = STAGE_NAMES.get(stage or "", stage or "")
    status = _status_of(exc)
    body = _body_of(exc)
    text = _text_of(exc)
    haystack = f"{body}\n{text}"
    raw = (body or text or "").strip() or None

    def base(**kw) -> Cause:
        kw.setdefault("provider", provider)
        kw.setdefault("stage", stage)
        kw.setdefault("raw", raw)
        # titles are sentences — "the NAS failed" must not open lowercase
        title = kw.get("title") or ""
        if title[:1].islower():
            kw["title"] = title[0].upper() + title[1:]
        return Cause(**kw)

    # ── config: nothing was ever going to work ──────────────────────────────
    # MissingSettingError carries the exact keys, so read them rather than
    # picking words out of its message.
    keys = getattr(exc, "keys", None)
    if not isinstance(keys, (list, tuple)):
        keys = re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", text) if "is not set" in text else None
    if keys:
        listed = ", ".join(keys)
        return base(
            kind="config",
            title=f"{listed} is not set" if len(keys) == 1 else f"{len(keys)} settings are missing",
            detail=("The run cannot start until it is configured."
                    if len(keys) == 1 else f"Missing: {listed}. The run cannot start until they are set."),
            retryable=False,
            action_label="Open settings",
            meta={"keys": list(keys)},
        )

    # ── NAS silently writing locally: a success that is not one ─────────────
    if "LOCAL mode" in text or "NAS_MODE" in text:
        return base(
            kind="nas_local",
            title="Saved locally, not to the NAS",
            detail="NAS_MODE is not set to smb, so the files went to a local folder "
                   "instead of the share. Nothing reached the NAS.",
            severity="warn",
            retryable=False,
        )

    # ── quota. Matched on several signals so a stray word cannot trigger it. ─
    heygen_code = _heygen_code(body) if provider == "heygen" else None
    quota_by_code = heygen_code in HEYGEN_QUOTA_CODES if heygen_code is not None else False
    quota_by_words = bool(_QUOTA_WORDS.search(haystack))
    # 402 means payment required in any API that bothers to use it
    if quota_by_code or quota_by_words or status == 402:
        detail = {
            "elevenlabs": "Clips could not be voiced. Translation is unaffected — "
                          "everything already rendered is safe.",
            "sarvam": "Translation fell back to Google Translate automatically, so the run "
                      "continues; wording may differ slightly on the affected rows.",
            "heygen": "Rows before this one finished and are already on the NAS.",
        }.get(provider or "", "The run stopped at this step.")
        return base(
            kind="quota",
            title=f"{name} credits exhausted",
            detail=detail,
            severity="warn" if provider == "sarvam" else "error",
            retryable=False,
            action_url=TOP_UP_URLS.get(provider or ""),
            action_label=f"Open {name}" if provider in TOP_UP_URLS else None,
            meta={"code": heygen_code} if heygen_code else {},
        )

    # ── auth: a key that is wrong, not spent ────────────────────────────────
    if _AUTH_WORDS.search(haystack) or status in (401, 403):
        return base(
            kind="auth",
            title=f"{name} rejected the API key",
            detail="The key is missing, wrong, or no longer has access. This is not a "
                   "credits problem — topping up will not fix it.",
            retryable=False,
            action_label="Open settings",
        )

    # ── rate limit: slow down, do not stop ──────────────────────────────────
    if status == 429:
        return base(
            kind="rate_limit",
            title=f"{name} is rate limiting us",
            detail="Too many requests at once. The pipeline backs off and retries "
                   "automatically — no action needed unless it keeps happening.",
            severity="warn",
            retryable=True,
        )

    # ── their outage, not ours ──────────────────────────────────────────────
    if status is not None and status >= 500:
        return base(
            kind="provider_down",
            title=f"{name} is having problems",
            detail=f"{name} returned a server error, so this is on their side rather than "
                   "yours. Safe to retry in a few minutes.",
            retryable=True,
        )

    # ── storage ─────────────────────────────────────────────────────────────
    if stage in ("upload", "nas_upload"):
        return base(
            kind="upload",
            title="Rendered, but not uploaded",
            detail="The files exist on this machine but did not reach their destination. "
                   "Retrying the upload does not re-render anything.",
            retryable=True,
        )

    # ── the honest fallback ─────────────────────────────────────────────────
    where = f" at the {stage_name} step" if stage_name else ""
    return base(
        kind="unknown",
        title=f"{name} failed{where}",
        detail="This is not a failure the app recognises, so it is not guessing at the "
               "cause. The exact message is below.",
        retryable=True,
    )
