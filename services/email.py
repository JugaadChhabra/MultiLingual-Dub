from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from services.runtime_config import RuntimeConfig, read_setting

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class EmailSettings:
    """Entirely optional: with any of these unset, batch notifications are
    skipped rather than failing the batch. Contributes no required keys."""

    api_key: str
    from_address: str
    to_addresses: tuple[str, ...]

    REQUIRED: tuple[str, ...] = ()

    @classmethod
    def resolve(cls, session: RuntimeConfig | None = None) -> EmailSettings:
        raw_recipients = read_setting("NOTIFY_EMAILS", session)
        return cls(
            api_key=read_setting("RESEND_API_KEY", session),
            from_address=read_setting("RESEND_FROM_ADDRESS", session),
            to_addresses=tuple(a.strip() for a in raw_recipients.split(",") if a.strip()),
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.from_address and self.to_addresses)


def send_batch_summary_email(
    *,
    total: int,
    succeeded: int,
    failed: int,
    failed_rows: list[dict],
    resend_api_key: str,
    from_address: str,
    to_addresses: list[str],
) -> None:
    subject = f"Bhaktidhaam Batch Complete — {succeeded}/{total} videos ready"

    text_lines = [
        "Batch job summary",
        "",
        f"Total scripts:  {total}",
        f"Generated OK:   {succeeded}",
        f"Failed:         {failed}",
    ]
    if failed_rows:
        text_lines += ["", "Failed rows:"]
        for row in failed_rows:
            text_lines.append(f"  Row {row['row_index']} — {row['video_title']}: {row['error']}")

    failed_html = ""
    if failed_rows:
        items = "".join(
            f"<li>Row {r['row_index']} — {r['video_title']}: {r['error']}</li>"
            for r in failed_rows
        )
        failed_html = f"<br><b>Failed rows:</b><ul>{items}</ul>"

    html_body = f"""
<p><b>Batch job summary</b></p>
<table cellpadding="4">
  <tr><td>Total scripts</td><td><b>{total}</b></td></tr>
  <tr><td>Generated OK</td><td><b>{succeeded}</b></td></tr>
  <tr><td>Failed</td><td><b>{failed}</b></td></tr>
</table>
{failed_html}
"""

    payload = {
        "from": from_address,
        "to": to_addresses,
        "subject": subject,
        "text": "\n".join(text_lines),
        "html": html_body,
    }

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            _RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()

    logger.info("Batch summary email sent to %s", to_addresses)
