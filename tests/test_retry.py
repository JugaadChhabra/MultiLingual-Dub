from services.retry import is_retryable


def test_is_retryable_for_transport_disconnect_messages() -> None:
    assert is_retryable(RuntimeError("Server disconnected without sending a response."))
    assert is_retryable(
        RuntimeError(
            "peer closed connection without sending complete message body "
            "(received 17996 bytes, expected 79038)"
        )
    )


def test_is_retryable_for_rate_limit_status() -> None:
    class RateLimitError(Exception):
        status_code = 429

    assert is_retryable(RateLimitError())


def test_is_retryable_rejects_language_mismatch() -> None:
    assert not is_retryable(RuntimeError("Source and target languages must be different"))
