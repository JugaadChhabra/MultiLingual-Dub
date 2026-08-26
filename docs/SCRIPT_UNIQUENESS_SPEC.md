# Script Uniqueness Harness — Specification

> Makes "no duplicate script" an enforced property rather than a prompt request.
> Touches `services/script_writer.py`, `services/script_history.py`, and the
> category brief. No UI changes.

---

## 1. Why

The daily horoscope set converges. Twelve scripts a day, every day, from one
model, drift toward the same openings, the same colours, the same numbers. The
current defence is `_avoid_block()` — quote recent scripts, ask the model not to
repeat them. That is a request, and nothing checks whether it was honoured.

Three separate things are broken today, all verified against the code:

| # | Problem | Evidence |
|---|---|---|
| 1 | The mandated audio tags are Devanagari, but the speech engine is `eleven_v3`, which only interprets English delivery tags | `script_writer.py:164-175` mandates `[शांत, आत्मविश्वासी आवाज़]` / `[सामान्य गति]`; `elevenlabs.py:11` pins `eleven_v3`; `video_pipeline/speech.py:6` routes the script straight into it |
| 2 | The model cannot see the colours and numbers it is told not to repeat | `script_history.py:33` truncates history to `EXCERPT_CHARS = 300`; scripts run 380–460 chars and the `शुभ रंग / जादुई अंक` line sits at the **end** of the body paragraph, so it is cut off in most entries |
| 3 | Nothing validates the output | `_collect()` (`script_writer.py:251`) only checks that each key came back non-empty. No duplicate detection, no retry |

Secondary: history is filtered by category + language only
(`script_history.py:96-99`), never by sign, so all twelve signs × seven days
arrive as one undifferentiated blob of ~84 excerpts.

## 2. Decisions

| Decision | Choice | Consequence |
|---|---|---|
| Tag vocabulary scope | **Global** — English ElevenLabs tags for every category, sets and one-off singles alike | One tag system, no branching in `_system_instruction`. Existing briefs that mention Devanagari tags need review |
| Violation handling | **Repair only offending items** | Failed signs are re-requested with their violations named; passing scripts survive untouched |
| Pools and skeleton | **Brief stays data, code enforces** | The twelve area pools and the paragraph skeleton live in the category brief, editable in the UI without a deploy. Code parses output and checks it against history — it never asserts an area is "in the pool" |

The third decision draws the line for the whole spec: **the brief says what to
write; the code checks what came back.** Anything content-shaped stays in the
textarea. Anything checkable moves into code.

---

## 3. Workstream A — Tag system

Rewrite the tag block in `_system_instruction` (`script_writer.py:153-175`) and
the matching rules in the user-prompt tail (`script_writer.py:223-227`). Both,
not one: the tail is the last thing the model reads and currently re-states
"three paragraphs, each opening with its square-bracket audio tag", which is
what overrides the brief today.

### 3.1 Closed vocabulary

A tag is exactly one English word or short phrase from this bank, in square
brackets, no comma, no Devanagari:

| Beat | Permitted tags |
|---|---|
| Hook | `warm`, `authoritative`, `confident` |
| Prediction | `reassuring`, `optimistic`, `measured`, `encouraging`, `thoughtful` |
| Transition | `pause`, `slight emphasis`, `softly` |
| Health line | `calm`, `steady` |
| Colour/number line | `bright`, `playful` |
| Closing | `uplifting`, `warm`, `sincere` |

The bank lives in code as `TAG_BANK: dict[str, tuple[str, ...]]` keyed by beat,
and is rendered into the system instruction from that single source. The
validator checks against the same dict, so prompt and check cannot drift.

### 3.2 Placement

One tag per beat, applying to the phrase that follows — not one per paragraph
and not one per sentence. Beats may be left untagged; an untagged beat is
correct, a placeholder tag is not.

The prompt asks for 5–7, which is right for a script of the horoscope's length.
The **check** derives its range from spoken length instead (`permitted_tag_count`,
~1 tag per 75 characters, floor 2, ceiling 7). A fixed 5–7 would have hard-failed
every one of the operator's short custom categories, which do not share the
horoscope's length — see §6.

### 3.3 Length accounting — changed

Today the 380–460 target is measured "tags included" (`script_writer.py:171`).
Under the new system, 6 English tags cost ~70 characters against a budget sized
from a hand-authored sheet that carried 3 short Devanagari ones. Spoken content
would silently shrink by ~15%.

**Change:** `TARGET_CHARS_LOW`/`TARGET_CHARS_HIGH` measure the **tag-stripped**
text. `MAX_SCRIPT_CHARS = 1000` continues to measure the **raw** string, because
that limit comes from the HeyGen field and the single-script textarea, which
receive the tags.

**The ask and the acceptance are separate numbers**, and this is the point of
the section. Every spoken character is paid for twice — once to synthesise, once
as video runtime — so the prompt asks for the shortest length that still carries
all six beats, while the check merely tolerates what comes back.

| | Value | Role |
|---|---|---|
| `ASK_CHARS_LOW` / `ASK_CHARS_HIGH` | 350–420 | What the prompt requests. The only number that gets tuned |
| `ACCEPT_CHARS_LOW` / `ACCEPT_CHARS_HIGH` | 300–550 | What the check tolerates, both bounds **soft**. The floor is a thinness guard |

The model writes to whatever ceiling it is given and overshoots by ~5–10%: told
380–460 it produced ~479; told 450–550 it produced ~555. Asking 350–420 lands
around 412–454 — measured on a live run, all twelve inside acceptance with zero
length violations, and ~20% cheaper than the 550 ask. Widening acceptance to
chase the overshoot is a treadmill; acceptance is set once to what is genuinely
tolerable, and the ask is what moves. 550 spoken plus ~85 characters of tags is
~635 raw, still far inside the 1000 limit.

`fit_to_limit()` keeps its current job — the last-resort guard on the raw
string — but gains a caller-visible signal that it fired, so truncation is a
validation failure (§6) rather than a log line nobody reads.

---

## 4. Workstream B — Structured history

### 4.1 Schema

`DraftScriptState` (`script_history.py:36`) gains four parsed fields:

```python
class DraftScriptState(BaseModel):
    title: str
    script: str
    areas: list[str] = Field(default_factory=list)
    colour: str | None = None
    number: int | None = None
    tags: list[str] = Field(default_factory=list)
```

All defaulted, so records written before this change load unchanged. Old
records simply contribute no facts — the window heals as new days are written.
No migration script.

### 4.2 Extraction

Parsed at `record()` time, from the fixed skeleton, in a new
`services/script_parse.py`:

| Field | Rule |
|---|---|
| `areas` | The hook line between `आज ` and the terminal `!`, split on `,` and ` और ` |
| `colour` | `शुभ रंग:` up to the `\|` |
| `number` | The Devanagari digits inside brackets on the colour/number line (`U+0966`–`U+096F`), converted to `int` |
| `tags` | Every `\[([^\]]*)\]` in document order |

Extraction is **best-effort and non-fatal**. A script that does not match the
skeleton records empty facts and is caught by the validator (§6) instead — a
parse failure must never lose an otherwise-good draft.

### 4.3 Retrieval

A new `facts()` returns parsed facts grouped per item key; `recent()` keeps
returning prose. Two windows, deliberately different:

- **Facts window — 10 days, all twelve signs.** Compact: areas, colour, number,
  tag list. ~120 rows at ~60 chars is ~7 kB of prompt.
- **Prose window — 3 days, all twelve signs.** Excerpts as today, for phrasing
  avoidance. ~36 rows.

`HISTORY_DAYS = 10` and `PROSE_DAYS = 3` live in `script_validate.py`, which
both the store and the prompt import — stated twice they drift, and the prompt
then names a window the store never read. Net prompt size is roughly flat
against today's 84-excerpt blob, while covering a longer window.

`before=` semantics are unchanged: regenerating a day must not be told to avoid
the draft it is replacing (`script_history.py:91-93`).

---

## 5. Workstream C — Prompt assembly

`_avoid_block()` is replaced by two blocks:

1. **Facts table**, per sign, listing the last 10 days of areas / colour /
   number / tags with an explicit "do not reuse" instruction.
2. **Prose excerpts**, the last 3 days, with the existing "do not reuse their
   phrasing, openings, imagery or predictions" wording.

The brief supplies the pools, the skeleton and the phrasing angles. The code
supplies the history and the tag bank. Neither restates the other.

---

## 6. Workstream D — Validator and repair

A new `validate_drafts(drafts, *, history, same_day) -> list[Violation]` in
`services/script_validate.py`. Rules, with severity:

| Rule | Severity | Check |
|---|---|---|
| Bracket contains Devanagari, or a comma, or a multi-word compound | **hard** | Regex over every bracket |
| Bracket content not in `TAG_BANK` | **hard** | Set membership |
| `जादुई अंक` outside 10–99 | **hard** | Parsed `number` |
| `जादुई अंक` missing **when the rest of the set has one** | **hard** | §6.3 |
| Colour repeated across the twelve signs **today** | **hard** | Set over same-day drafts |
| Number repeated across the twelve signs **today** | **hard** | Set over same-day drafts |
| Colour repeated for this sign in the 10-day window | **hard** | Normalised compare, §6.1 |
| Number repeated for this sign in the 10-day window | **hard** | Set over window |
| `fit_to_limit()` truncated the script | **hard** | Signal from §3.3 |
| Tag count outside the length-derived range | **soft** | §3.2, §6.3 |
| Tag-stripped length outside 300–550 | **soft** | Count, §3.3 |
| Area combination reused for this sign in the 10-day window | **soft** | Frozenset of 3, §6.2 |
| Same beat tag-set as this sign's previous day | **soft** | Ordered tuple compare |

**Hard** violations trigger repair. **Soft** violations are logged and, if still
present after the repair budget, accepted — they degrade quality, they do not
make a script wrong.

### 6.1 Colour normalisation

`गहरा रूबी रेड` and `रूबी रेड` must count as the same colour. Strip a leading
modifier from a small list (`गहरा`, `गाढ़ा`, `हल्का`, `चमकीला`, `फीका`, `सुनहरा`
when qualifying), then compare on the remaining token set with containment —
one colour contained in the other is a collision.

This is a heuristic and will have both false positives and false negatives. It
is a **soft-to-hard borderline**: treat as hard, but a false positive costs only
one repair round, which is acceptable.

### 6.2 Combination arithmetic — a real constraint

A 6-item pool yields C(6,3) = 20 distinct combinations. Requiring 10 distinct
combinations over 10 days uses half of them: comfortable.

A **5-item pool yields exactly C(5,3) = 10** — zero slack over a 10-day window,
and unsatisfiable on day 11. The harness draft permits "5–6 candidate domains".

**Requirement:** every pool in the brief carries at least 6 areas. The validator
cannot check this (pools live in the brief by decision §2), so the area-combo
rule stays **soft** — it must not be able to make generation fail outright when
the pool is too small. Document the 6-minimum in the brief itself.

Order within the combination is ignored for uniqueness — the harness asks for
varied naming order as surface variety, which the prose window already covers.

### 6.3 The operator's own categories

The tag and length rules live in the global system instruction, which a category
brief cannot opt out of. The operator has custom categories — their own video,
motion and script prompts under names they chose — and those are not horoscopes:
they are shorter, they have no zodiac set, and most carry no lucky number.

Two rules would have hard-failed every one of them, so both are scoped by what
the set actually contains rather than by configuration. No new store field, no
UI control, no migration:

| Rule | How it scopes itself |
|---|---|
| Lucky number required | `expects_number` is true only if **some** script in the set produced one. A category that never uses numbers is never asked for one; a set where eleven have one and the twelfth does not still fails the twelfth |
| Tag count | Derived from spoken length, and **soft**. A flat read is a quality problem; only the vocabulary is functional, because a Devanagari bracket reaches the voice engine and breaks the audio |

The colour rules need no scoping — a category with no colour line produces no
colour, and a rule with nothing to compare never fires.

What is **not** scoped, deliberately:

- **Tag vocabulary** stays global and hard. It is a property of `eleven_v3`, not
  of any category.
- **Truncation** stays hard everywhere. A script cut at the render limit has lost
  its closing line, which is a broken video whatever the category.

The one thing this cannot protect: a custom brief that *explicitly* instructs
Devanagari tags, copied from the old horoscope style. That contradicts the
system instruction and will fail. Grep the saved briefs for `[` before deploying.

### 6.4 Repair loop

Inside `write_daily_scripts`, after `_collect()`:

1. Validate. No hard violations → record and return.
2. Build a repair request naming **only the offending item keys** and, for each,
   its specific violations in plain terms ("colour रूबी रेड was used for मेष on
   2026-08-21; number ४२ is already used by कन्या today").
3. Re-request that subset. Splice the results over the failures.
4. Repeat to `MAX_REPAIR_ATTEMPTS = 2`.
5. Still hard-violating → raise `ScriptWriterError` listing the violations.

Step 5 follows the existing contract: the operator gets twelve good signs or an
error, never a set that is quietly wrong (`script_writer.py:254-256`).

The repair call loses the cross-sign variation the single-call design buys
(`script_writer.py:197-199`) — the model can no longer vary the twelve against
each other. That is acceptable because the same-day collision rules are
evaluated against the **full** set including untouched scripts, so the validator
provides the cross-check the single call was providing implicitly.

Model fallthrough (`script_writer.py:230-244`) is unchanged and sits outside the
repair loop: a model that fails to produce parseable JSON is still swapped out
before repair is attempted.

---

## 7. Brief changes

The category brief in `static/pane-video.js:21` is the seed for new categories
and needs three edits:

1. **Number format** — currently exemplified as `सात (७)`, single-digit. Must
   become double-digit, 10–99, e.g. `सत्रह (१७)`.
2. **Area pools** — add the twelve pools, minimum 6 areas each (§6.2).
3. **Phrasing angles** — add the five rotation angles.

Remove any reference to Devanagari tags. Existing saved categories are **not**
migrated; an operator editing an old category will see its old brief. Flag this
in the release note.

---

## 8. Tests

Extending `tests/test_script_writer.py` and `tests/test_script_history.py`, plus
new `tests/test_script_validate.py` and `tests/test_script_parse.py`.

| Test | Asserts |
|---|---|
| Devanagari bracket rejected | `[सामान्य गति]` is a hard violation |
| Comma bracket rejected | `[calm, confident]` is a hard violation |
| Off-bank tag rejected | `[thrilled]` is a hard violation |
| Same-day colour collision | Two signs sharing a colour is a hard violation |
| Window colour collision | `गहरा रूबी रेड` vs `रूबी रेड` for one sign within 10 days collides |
| Number range | `९` and `१०५` are hard violations; `१७` passes |
| Repair splices | One failing sign is re-requested; the other eleven are byte-identical |
| Repair budget | Three consecutive failures raise `ScriptWriterError` |
| Soft violation accepted | Area-combo reuse survives the budget and returns |
| Parse tolerance | A skeleton-violating script records empty facts, does not raise |
| Legacy record loads | A `DayScriptsState` without the four new fields deserialises |
| Length measured stripped | A script inside acceptance after tag-stripping passes though raw length exceeds it |
| Ask is tighter than acceptance | The prompt quotes only 350–420; 550 never reaches the model |
| Facts reach the prompt | 10 days of colours/numbers appear in the assembled prompt |

Existing tests that assert the Devanagari tag shape will fail and must be
updated — that is the intended signal, not collateral damage.

---

## 9. Out of scope

- UI changes. No new fields, no validation surfaced in the browser beyond the
  existing error path.
- Per-category tag configuration (rejected in §2).
- Pruning `output/` history, which is never pruned by design
  (`script_history.py:12-13`).
- Migrating saved category briefs.
- Any change to the AutoDub dub pipeline. This is `/videogen` only.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Colour normalisation false positives cost repair rounds | Bounded by `MAX_REPAIR_ATTEMPTS`; heuristic list is small and tunable |
| Repair loop increases cost per generation | Only offending items are re-requested; expected steady-state is zero repairs |
| A 5-item pool makes area rotation unsatisfiable | Rule kept soft; 6-minimum documented in the brief |
| eleven_v3 tag behaviour is not contractually documented | Tag bank is a single dict, cheap to revise once real audio is heard |
| Structured extraction couples code to the skeleton | Extraction is non-fatal; a skeleton change degrades facts to empty rather than breaking generation |
