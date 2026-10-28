import asyncio

from batch import voiceover
from batch.story_job import run_story_job
from batch.store import JobsStore
from services.story import StoryLine
from tests.test_batch_service import _settings

LINES = [
    StoryLine(text="Kiran called Moti.", emotion="Narrative"),
    StoryLine(text="Moti barked happily.", emotion="Cheerful"),
    StoryLine(text="They ran to the stream.", emotion="Excited"),
]
ACTIVITY = "thestreamstory"


class Fakes:
    """Translation, Gemini (story fix and per-row QC), TTS and S3, all faked."""

    def __init__(self, monkeypatch, *, fix=None, on_translate=None):
        self.translations: list[tuple[str, str]] = []
        self.fix_calls: list[dict] = []
        # per-row QC, the story fix's fallback: (English text, drafts by language)
        self.qc_calls: list[tuple[str, dict[str, str]]] = []
        self.tts: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, dict[str, bytes], str]] = []
        fakes = self

        async def fake_translate(text, language, _sarvam=None, _google=None):
            fakes.translations.append((text, language))
            if on_translate:
                await on_translate(len(fakes.translations))
            return language, f"draft:{language}:{text}", None

        def default_fix(title, lines, drafts, language, *, settings):
            return {line_id: f"fixed:{language}:{line.text}" for line_id, line in lines.items()}

        def fake_fix(title, lines, drafts, language, *, settings):
            fakes.fix_calls.append({"title": title, "lines": lines, "drafts": drafts, "language": language})
            return (fix or default_fix)(title, lines, drafts, language, settings=settings)

        def fake_qc(original_text, translations, target_languages, **_kwargs):
            fakes.qc_calls.append((original_text, dict(translations)))
            return {language: f"qc:{text}" for language, text in translations.items()}

        def fake_tts(text, language, settings=None):
            fakes.tts.append((language, text))
            return f"audio:{language}:{text}".encode()

        class FakeS3Client:
            def __init__(self, _config):
                pass

            def upload_language_zip(self, language, audio_files, folder_name):
                fakes.uploads.append((language, dict(audio_files), folder_name))
                return {"bucket": "fake", "key": f"{folder_name}/{language}.zip", "etag": "etag"}

        monkeypatch.setattr("batch.voiceover._translate_language_async", fake_translate)
        monkeypatch.setattr("batch.voiceover.qc_translations_batch", fake_qc)
        monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", fake_tts)
        monkeypatch.setattr("batch.voiceover.compress_mp3_bytes", lambda audio: audio)
        monkeypatch.setattr("batch.story_job.fix_story", fake_fix)
        monkeypatch.setattr("batch.service.S3Client", FakeS3Client)


def _run(languages, *, store=None, job_id="story-job"):
    store = store or JobsStore()

    async def go():
        await store.create(job_id)
        await run_story_job(
            job_id=job_id,
            title="The stream",
            activity_name=ACTIVITY,
            lines=LINES,
            target_languages=languages,
            jobs_store=store,
            settings=_settings(),
        )
        return await store.get(job_id)

    return asyncio.run(go())


def test_one_zip_per_language_named_after_the_activity(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)

    state = _run(["hi-IN", "ta-IN"])

    assert state.status == "completed", state.error
    assert sorted(language for language, _, _ in fakes.uploads) == ["Hindi", "Tamil"]
    for _language, files, folder in fakes.uploads:
        assert folder == ACTIVITY
        assert sorted(files) == [f"{ACTIVITY}1.mp3", f"{ACTIVITY}2.mp3", f"{ACTIVITY}3.mp3"]
    assert state.summary.total_rows == 3
    assert state.summary.rows_succeeded == 3
    assert state.summary.language_tasks_succeeded == 6
    assert state.summary.uploads_succeeded == 2


def test_tts_receives_the_fixed_text_with_its_emotion_tag(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)

    _run(["hi-IN"])

    assert fakes.tts == [
        ("hi-IN", "[Narrative] fixed:hi-IN:Kiran called Moti."),
        ("hi-IN", "[Cheerful] fixed:hi-IN:Moti barked happily."),
        ("hi-IN", "[Excited] fixed:hi-IN:They ran to the stream."),
    ]
    assert not any("draft:" in text for _, text in fakes.tts)


def test_one_story_fix_per_non_english_language_seeing_the_whole_story(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)

    state = _run(["hi-IN", "en-IN", "ta-IN"])

    assert state.status == "completed", state.error
    assert sorted(call["language"] for call in fakes.fix_calls) == ["hi-IN", "ta-IN"]
    hindi = next(call for call in fakes.fix_calls if call["language"] == "hi-IN")
    assert hindi["title"] == "The stream"
    assert list(hindi["lines"]) == ["1", "2", "3"]
    assert hindi["drafts"]["2"] == "draft:hi-IN:Moti barked happily."
    assert fakes.qc_calls == []


def test_english_is_voiced_verbatim_without_translation_or_gemini(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)

    state = _run(["en-IN"])

    assert state.status == "completed", state.error
    assert fakes.translations == []
    assert fakes.fix_calls == []
    assert fakes.qc_calls == []
    assert [text for _, text in fakes.tts] == [
        "[Narrative] Kiran called Moti.",
        "[Cheerful] Moti barked happily.",
        "[Excited] They ran to the stream.",
    ]


def test_a_failed_story_fix_falls_back_to_per_row_qc_for_that_language(monkeypatch) -> None:
    def fix(title, lines, drafts, language, *, settings):
        if language == "ta-IN":
            raise RuntimeError("gemini down")
        return {line_id: f"fixed:{line.text}" for line_id, line in lines.items()}

    fakes = Fakes(monkeypatch, fix=fix)

    state = _run(["hi-IN", "ta-IN"])

    assert state.status == "completed", state.error
    assert sorted(language for language, _, _ in fakes.uploads) == ["Hindi", "Tamil"]
    tamil = [text for language, text in fakes.tts if language == "ta-IN"]
    assert tamil == [
        "[Narrative] qc:draft:ta-IN:Kiran called Moti.",
        "[Cheerful] qc:draft:ta-IN:Moti barked happily.",
        "[Excited] qc:draft:ta-IN:They ran to the stream.",
    ]
    # only Tamil's drafts went through per-row QC, one call per line
    assert [drafts for _, drafts in fakes.qc_calls] == [
        {"ta-IN": "draft:ta-IN:Kiran called Moti."},
        {"ta-IN": "draft:ta-IN:Moti barked happily."},
        {"ta-IN": "draft:ta-IN:They ran to the stream."},
    ]
    assert state.summary.language_tasks_succeeded == 6
    assert state.summary.language_tasks_failed == 0
    assert state.summary.story_fix_fallback_lines == 3
    assert state.summary.story_fix_warning == "story fix failed for ta-IN — used line-by-line fix"


def test_a_fix_that_returns_nothing_usable_falls_back_for_the_whole_language(monkeypatch) -> None:
    fakes = Fakes(monkeypatch, fix=lambda *_a, **_k: {})

    state = _run(["hi-IN"])

    assert len(fakes.qc_calls) == 3
    assert len(fakes.uploads[0][1]) == 3
    assert state.summary.story_fix_fallback_lines == 3
    assert state.summary.story_fix_warning == "story fix failed for hi-IN — used line-by-line fix"


def test_lines_the_fix_left_out_fall_back_alone(monkeypatch) -> None:
    def fix(title, lines, drafts, language, *, settings):
        return {
            line_id: ("" if line_id == "3" else f"fixed:{line.text}")
            for line_id, line in lines.items()
            if line_id != "2"
        }

    fakes = Fakes(monkeypatch, fix=fix)

    state = _run(["hi-IN"])

    assert fakes.qc_calls == [
        ("Moti barked happily.", {"hi-IN": "draft:hi-IN:Moti barked happily."}),
        ("They ran to the stream.", {"hi-IN": "draft:hi-IN:They ran to the stream."}),
    ]
    assert [text for _, text in fakes.tts] == [
        "[Narrative] fixed:Kiran called Moti.",
        "[Cheerful] qc:draft:hi-IN:Moti barked happily.",
        "[Excited] qc:draft:hi-IN:They ran to the stream.",
    ]
    [(_, files, _)] = fakes.uploads
    assert len(files) == 3
    assert state.summary.language_tasks_failed == 0
    assert state.summary.story_fix_fallback_lines == 2
    assert "hi-IN" in state.summary.story_fix_warning


def test_a_line_whose_fixed_length_moved_too_far_is_flagged_but_used(monkeypatch) -> None:
    def fix(title, lines, drafts, language, *, settings):
        fixed = {line_id: drafts[line_id] for line_id in lines}
        fixed["1"] = "x"  # far shorter than its draft
        fixed["3"] = drafts["3"] * 3  # far longer
        return fixed

    fakes = Fakes(monkeypatch, fix=fix)

    state = _run(["hi-IN"])

    assert state.summary.story_fix_flagged_lines == 2
    assert state.summary.story_fix_fallback_lines == 0
    assert state.summary.story_fix_warning is None
    assert fakes.tts[0] == ("hi-IN", "[Narrative] x")
    assert fakes.qc_calls == []


def test_a_tts_retry_re_speaks_the_fixed_text_without_re_translating(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)
    calls = {"n": 0}

    def flaky_tts(text, language, settings=None):
        calls["n"] += 1
        fakes.tts.append((language, text))
        if calls["n"] == 2:
            raise RuntimeError("rate limited")
        return b"audio"

    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", flaky_tts)

    state = _run(["hi-IN"])

    assert state.summary.language_tasks_succeeded == 3
    assert state.summary.rows_succeeded == 3
    # the retried line is spoken again with its fixed text
    assert fakes.tts[-1] == ("hi-IN", "[Cheerful] fixed:hi-IN:Moti barked happily.")
    assert len(fakes.tts) == 4
    assert len(fakes.translations) == 3
    assert len(fakes.fix_calls) == 1
    assert fakes.qc_calls == []


def test_a_translation_retry_re_translates_only_that_line_with_per_row_qc(monkeypatch) -> None:
    fakes = Fakes(monkeypatch)
    real_translate = voiceover._translate_language_async
    calls = {"n": 0}

    async def flaky_translate(text, language, _sarvam=None, _google=None):
        calls["n"] += 1
        if calls["n"] == 2:
            fakes.translations.append((text, language))
            return language, None, "sarvam down"
        return await real_translate(text, language, _sarvam, _google)

    monkeypatch.setattr("batch.voiceover._translate_language_async", flaky_translate)

    state = _run(["hi-IN"])

    assert state.summary.language_tasks_succeeded == 3
    assert state.summary.language_tasks_failed == 0
    # the story fix never saw line 2; the retry translated it again and QC'd it alone
    assert list(fakes.fix_calls[0]["lines"]) == ["1", "3"]
    assert [text for text, _ in fakes.translations].count("Moti barked happily.") == 2
    assert fakes.qc_calls == [("Moti barked happily.", {"hi-IN": "draft:hi-IN:Moti barked happily."})]
    assert ("hi-IN", "[Cheerful] qc:draft:hi-IN:Moti barked happily.") in fakes.tts


def test_a_failed_fallback_qc_is_retried_from_the_draft_without_re_translating(monkeypatch) -> None:
    fakes = Fakes(monkeypatch, fix=lambda *_a, **_k: {})
    calls = {"n": 0}

    def flaky_qc(original_text, translations, target_languages, **_kwargs):
        calls["n"] += 1
        fakes.qc_calls.append((original_text, dict(translations)))
        if calls["n"] == 1:
            raise RuntimeError("gemini down")
        return {language: f"qc:{text}" for language, text in translations.items()}

    monkeypatch.setattr("batch.voiceover.qc_translations_batch", flaky_qc)

    state = _run(["hi-IN"])

    assert state.summary.language_tasks_succeeded == 3
    assert state.summary.rows_succeeded == 3
    assert len(fakes.translations) == 3
    assert fakes.qc_calls[-1] == ("Kiran called Moti.", {"hi-IN": "draft:hi-IN:Kiran called Moti."})
    assert fakes.tts[-1] == ("hi-IN", "[Narrative] qc:draft:hi-IN:Kiran called Moti.")


def test_cancellation_stops_the_job(monkeypatch) -> None:
    store = JobsStore()
    job_id = "story-cancel"

    async def cancel_after_first(n):
        if n == 1:
            await store.request_cancel(job_id)

    fakes = Fakes(monkeypatch, on_translate=cancel_after_first)

    state = _run(["hi-IN"], store=store, job_id=job_id)

    assert state.status == "cancelled"
    assert len(fakes.translations) == 1
    assert fakes.fix_calls == []
    assert fakes.tts == []
    assert fakes.uploads == []


def test_cancelling_while_speaking_uploads_the_clips_already_spoken(monkeypatch) -> None:
    store = JobsStore()
    job_id = "story-cancel-speaking"
    fakes = Fakes(monkeypatch)
    loop: dict = {}

    def tts_then_cancel(text, language, settings=None):
        # TTS runs in a worker thread, so the cancel is handed back to the loop
        fakes.tts.append((language, text))
        asyncio.run_coroutine_threadsafe(store.request_cancel(job_id), loop["it"]).result()
        return b"audio"

    monkeypatch.setattr("batch.voiceover._generate_elevenlabs_audio_bytes", tts_then_cancel)

    async def go():
        loop["it"] = asyncio.get_running_loop()
        await store.create(job_id)
        await run_story_job(
            job_id=job_id, title="T", activity_name=ACTIVITY, lines=LINES,
            target_languages=["hi-IN"], jobs_store=store, settings=_settings(),
        )
        return await store.get(job_id)

    state = asyncio.run(go())

    assert state.status == "cancelled"
    assert len(fakes.tts) == 1
    [(_, files, folder)] = fakes.uploads
    assert folder == ACTIVITY and sorted(files) == [f"{ACTIVITY}1.mp3"]


class RecordingStore(JobsStore):
    """A jobs store that keeps every summary it was asked to persist."""

    def __init__(self) -> None:
        super().__init__()
        self.summaries = []

    async def update_summary(self, job_id, summary) -> None:
        self.summaries.append(summary.model_copy(deep=True))
        await super().update_summary(job_id, summary)


def test_progress_moves_through_translating_fixing_then_voicing(monkeypatch) -> None:
    Fakes(monkeypatch)
    store = RecordingStore()

    state = _run(["hi-IN", "ta-IN"], store=store)

    assert state.status == "completed", state.error
    stages = [s.story_stage for s in store.summaries]
    # each stage appears, in order, and never comes back
    seen = [stage for i, stage in enumerate(stages) if stage and stage != stages[i - 1]]
    assert seen == ["translating", "fixing", "voicing"]
    translating = [s.story_rows_translated for s in store.summaries if s.story_stage == "translating"]
    assert translating[0] == 0 and translating[-1] == len(LINES)
    fixing = [s for s in store.summaries if s.story_stage == "fixing"]
    assert {s.story_languages_to_fix for s in fixing} == {2}
    assert fixing[-1].story_languages_fixed == 2
    voicing = [s for s in store.summaries if s.story_stage == "voicing"]
    assert voicing[0].rows_processed == 0 and voicing[0].stage_started_at is not None


def test_an_english_only_story_goes_straight_to_voicing(monkeypatch) -> None:
    Fakes(monkeypatch)
    store = RecordingStore()

    _run(["en-IN"], store=store)

    assert {s.story_stage for s in store.summaries} - {None} == {"voicing"}
