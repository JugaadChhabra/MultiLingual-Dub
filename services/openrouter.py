"""A minimal OpenRouter client: one chat call that must come back as JSON.

OpenRouter puts many providers' models behind one OpenAI-shaped endpoint and one
credit balance, so a model can be swapped from .env without new code or a new
account. Only what the script writer needs is here — a system prompt, a user
prompt, and a JSON schema the reply has to satisfy.
"""
from __future__ import annotations

import httpx

API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Twelve long scripts from a pro-tier model can take well over a minute.
TIMEOUT_SECONDS = 300.0


class OpenRouterError(Exception):
    """A failed call, carrying the HTTP-style status OpenRouter reported.

    ``status_code`` is what services/retry.py and services/errors.py read, so a
    429 is retried, a 402 is reported as spent credit, and neither has to parse
    this message. OpenRouter sometimes returns 200 with an error body when the
    upstream provider fails mid-call; that arrives here with the code from the
    body rather than the 200.
    """

    def __init__(self, status_code: int | None, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"{status_code} {message}" if status_code else message)


def _error_from(body: object) -> tuple[int | None, str] | None:
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return None
    error = body["error"]
    code = error.get("code")
    return (code if isinstance(code, int) else None), str(error.get("message") or error)


class OpenRouterClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def complete_json(self, *, model: str, system: str, prompt: str, schema: dict) -> str:
        """The reply text, which the schema constrains to a JSON object.

        ``require_parameters`` keeps OpenRouter from routing to a provider for
        this model that would silently ignore the schema and return free text.
        """
        response = httpx.post(
            API_URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "scripts", "strict": True, "schema": schema},
                },
                "provider": {"require_parameters": True},
            },
            timeout=TIMEOUT_SECONDS,
        )
        try:
            body = response.json()
        except ValueError:
            body = None

        failure = _error_from(body)
        if response.status_code >= 400:
            code, message = failure or (None, response.text[:500])
            raise OpenRouterError(response.status_code, message)
        if failure:
            raise OpenRouterError(*failure)

        try:
            return body["choices"][0]["message"]["content"] or ""
        except (TypeError, KeyError, IndexError) as exc:
            raise OpenRouterError(None, f"reply had no message content: {str(body)[:500]}") from exc
