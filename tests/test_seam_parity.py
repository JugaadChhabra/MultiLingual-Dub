"""Keeps every adapter's signature identical to the interface it implements.

ABC inheritance catches a *missing* method at instantiation. It does not catch a
renamed keyword or a changed default — and with no type checker in CI, nothing
else would either. A fake that has quietly drifted from the real adapter is a
test suite that passes while production breaks.
"""
from __future__ import annotations

import inspect

import pytest

from services.video_pipeline.heygen_renderer import HeyGenRenderer
from services.video_pipeline.renderer import VideoRenderer
from services.video_pipeline.speech import ElevenLabsSpeech, SpeechSynth
from tests.fakes import FakeRenderer, FakeSpeech

SEAMS = [
    (VideoRenderer, HeyGenRenderer, FakeRenderer),
    (SpeechSynth, ElevenLabsSpeech, FakeSpeech),
]


def _abstract_methods(interface: type) -> list[str]:
    return sorted(interface.__abstractmethods__)


@pytest.mark.parametrize("interface,real,fake", SEAMS, ids=lambda c: c.__name__)
def test_adapters_match_the_interface_signature(interface, real, fake) -> None:
    methods = _abstract_methods(interface)
    assert methods, f"{interface.__name__} declares no abstract methods"

    for name in methods:
        expected = inspect.signature(getattr(interface, name))
        for adapter in (real, fake):
            actual = inspect.signature(getattr(adapter, name))
            assert actual == expected, (
                f"{adapter.__name__}.{name}{actual} does not match "
                f"{interface.__name__}.{name}{expected}"
            )


@pytest.mark.parametrize("interface,real,fake", SEAMS, ids=lambda c: c.__name__)
def test_adapters_are_async_where_the_interface_is(interface, real, fake) -> None:
    for name in _abstract_methods(interface):
        for adapter in (real, fake):
            assert inspect.iscoroutinefunction(getattr(adapter, name)), (
                f"{adapter.__name__}.{name} must be async"
            )


def test_an_incomplete_adapter_cannot_be_constructed() -> None:
    class MissingDownload(VideoRenderer):
        async def upload_audio(self, *, content, content_type): ...
        async def upload_photo(self, *, content, content_type): ...
        async def clear_photos(self): ...
        async def submit(self, **kwargs): ...
        async def await_render(self, *, video_id): ...

    with pytest.raises(TypeError, match="download"):
        MissingDownload()
