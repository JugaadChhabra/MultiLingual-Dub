#!/usr/bin/env bash
# Runs GitHub tickets unattended, one fresh Claude session each. Per ticket:
#   1. creates a git worktree on branch ticket/<N>, cut from the current `main`
#   2. starts `claude -p` with that ticket's block from the prompt book
#      (a markdown file with "## <k> — #<N> …" headings, each followed by one
#      fenced block such as "/mattpocock-skills:implement #N …")
#   3. waits out Claude usage limits and resumes the same session
#   4. gates, cheapest first: commits exist and worktree clean, then every
#      changed .py and .js parses, then no NEW failing pytest tests vs the main
#      baseline (killed after TEST_TIMEOUT). A failing cheap gate skips the
#      expensive ones. Up to FIX_ATTEMPTS fix-up rounds if red; each round gets
#      only the new failures (short tracebacks of just those tests).
#   5. if main moved since the branch was cut (you committed mid-run), merges
#      main into the branch and gates again, so what lands is what was tested
#   6. merges into main (--no-ff), pushes (PUSH=1), and closes the issue with
#      the session's hand-off notes as a comment
# caffeinate keeps the Mac awake for the whole run, but only on AC power: with
# the lid closed on battery it still sleeps, so plug in for overnight runs.
#
# Usage:  scripts/run-tickets.sh docs/my-prompt-book.md            # every ticket in the book, in book order
#         scripts/run-tickets.sh docs/my-prompt-book.md 31 33      # just these
#         PROMPT_BOOK=docs/my-prompt-book.md scripts/run-tickets.sh 31 32   # old form, still works
# Everything below is worked out automatically; the env vars only override:
#   blockers  - GitHub's native "blocked by" links on each issue, else the
#               "(blocked by #N, #M)" note on the ticket's prompt-book heading.
#               A ticket is skipped while any blocker is still open.
#   model / effort / review - tags on the ticket's prompt-book heading, e.g.
#               "*(blocked by #47; model: sonnet; effort: low; review)*".
#               No model/effort tag = the CLI default. No "review" tag = the
#               session skips the implement skill's /code-review step.
# Env:    PROMPT_BOOK        prompt book path (instead of the first argument)
#         PY=...             python that runs the tests (default: the repo's venv)
#         BLOCKERS="33:31"   ticket:blocker pairs; replaces the lookup above
#         PUSH=0             don't push main to origin at all
#         PUSH=end           push once after the last ticket instead of after
#                            each. Every push to main builds and publishes the
#                            Docker image, and the cloud container then needs a
#                            restart, so this saves N-1 builds on a long run.
#         TEST_TIMEOUT=600   seconds before a pytest run is killed (the suite
#                            normally takes ~5s; a hang means a real network
#                            call or a deadlock, and counts as a red gate)
#         FIX_ATTEMPTS=2     fix-up rounds when gates fail
#         KEEP_WORKTREES=1   keep worktrees/branches after merging
#         MODEL_TICKETS="49:sonnet"   per-ticket model; replaces the heading tag
#         EFFORT_TICKETS="49:low"     per-ticket effort; replaces the heading tag
#         MODEL=... / EFFORT=...      default for tickets with no tag
#         REVIEW=tagged|all|none      which tickets run /code-review (default tagged)
#         BUDGET_USD=10      --max-budget-usd per claude invocation (0 = no cap).
#                            A session that hits it is marked failed, not resumed.
#         ALLOW_DIRTY=1      start even with uncommitted tracked changes in the
#                            main checkout (stashed around each merge; a
#                            conflict on restore stops the run)
#         SLIM=0             keep every MCP server and plugin in runner sessions
#                            (default drops MCP servers and SLIM_PLUGINS)
#         SLIM_PLUGINS="..." plugin ids disabled when SLIM=1
# Re-runnable: tickets that are already closed are skipped, and an existing
# ticket/<N> branch is reused. Run logs live next to the worktrees, outside
# the repo, so a run never leaves untracked files in the main checkout.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
USAGE="usage: $0 <prompt-book.md> [ticket ...]   (or PROMPT_BOOK=<file> $0 [ticket ...])"
if [ $# -gt 0 ] && [[ "$1" != *[!0-9]* ]]; then
  :                                                    # first arg is a ticket number
elif [ $# -gt 0 ]; then
  PROMPT_BOOK="$1"; shift
fi
[ -n "${PROMPT_BOOK:-}" ] || { echo "$USAGE" >&2; exit 2; }
# Accept the book path relative to the current directory or to the repo.
if [ -f "$PROMPT_BOOK" ]; then PROMPT_BOOK="$(cd "$(dirname "$PROMPT_BOOK")" && pwd)/$(basename "$PROMPT_BOOK")"
else PROMPT_BOOK="$REPO/$PROMPT_BOOK"; fi
[ -f "$PROMPT_BOOK" ] || { echo "missing prompt book: $PROMPT_BOOK" >&2; exit 2; }
BASE_BRANCH="main"
WT_ROOT="$(dirname "$REPO")/$(basename "$REPO")-worktrees"
RUN_DIR="$WT_ROOT/runs/$(date +%Y%m%d-%H%M%S)"
# Worktrees have no venv of their own (it's gitignored), so tests run with the
# main checkout's interpreter. pytest.ini's `pythonpath = .` makes the imports
# resolve against the worktree, not the main checkout.
PY="${PY:-$REPO/venv/bin/python}"

# Ticket numbers from "## <k> — #<N>" headings, in book order.
book_tickets() { grep -oE '^## [0-9]+ — #[0-9]+' "$PROMPT_BOOK" | sed -E 's/.*#//'; }
# Blockers noted on a ticket's heading, e.g. "*(blocked by #32, #33)*".
book_blockers() {
  grep -E "^## [0-9]+ — #$1([^0-9]|$)" "$PROMPT_BOOK" | grep -oiE 'blocked by[^)]*' | grep -oE '#[0-9]+' | tr -d '#'
}

# Value of a "key: value" tag on a ticket's heading, e.g. "model: sonnet".
book_tag() {
  grep -E "^## [0-9]+ — #$1([^0-9]|$)" "$PROMPT_BOOK" | grep -oiE "$2: *[A-Za-z0-9._-]+" | head -1 | sed -E 's/^[^:]*: *//'
}
# True when the heading's parenthetical carries a bare "review" tag.
book_review() {
  grep -E "^## [0-9]+ — #$1([^0-9]|$)" "$PROMPT_BOOK" | grep -qiE '[(;] *review *[;)]'
}
# Looks up <ticket> in "<ticket>:<value>" pairs, e.g. pair_value 49 "49:sonnet 51:haiku".
pair_value() { local pair; for pair in $2; do [ "${pair%%:*}" = "$1" ] && { echo "${pair#*:}"; return; }; done; }

if [ $# -gt 0 ]; then TICKETS=("$@")
else
  TICKETS=()
  while read -r t; do TICKETS+=("$t"); done < <(book_tickets)
  [ ${#TICKETS[@]} -gt 0 ] || { echo "no '## <k> — #<N>' headings in $PROMPT_BOOK" >&2; exit 2; }
fi
BLOCKERS="${BLOCKERS:-}"
PUSH="${PUSH:-1}"
FIX_ATTEMPTS="${FIX_ATTEMPTS:-2}"
TEST_TIMEOUT="${TEST_TIMEOUT:-600}"
KEEP_WORKTREES="${KEEP_WORKTREES:-0}"
MODEL_TICKETS="${MODEL_TICKETS:-}"
EFFORT_TICKETS="${EFFORT_TICKETS:-}"
MODEL="${MODEL:-}"
EFFORT="${EFFORT:-}"
REVIEW="${REVIEW:-tagged}"
BUDGET_USD="${BUDGET_USD:-10}"
SLIM="${SLIM:-1}"
# mattpocock-skills stays on: the prompt book's blocks call its implement skill.
SLIM_PLUGINS="${SLIM_PLUGINS:-superpowers@claude-plugins-official figma@claude-plugins-official frontend-design@claude-plugins-official interface-design@interface-design i-have-adhd@i-have-adhd swift-lsp@claude-plugins-official andrej-karpathy-skills@karpathy-skills}"

# Flags every runner session gets. SLIM drops MCP servers and plugins a ticket
# never needs; their tool schemas, skill listings and SessionStart hooks
# otherwise ride along on every turn.
BASE_FLAGS=()
if [ "$SLIM" = 1 ]; then
  slim_settings="$(printf '%s\n' $SLIM_PLUGINS | jq -R . | jq -sc '{enabledPlugins: (map({(.): false}) | add // {})}')"
  BASE_FLAGS+=(--strict-mcp-config --mcp-config '{"mcpServers":{}}' --settings "$slim_settings")
fi
[ "$BUDGET_USD" != 0 ] && BASE_FLAGS+=(--max-budget-usd "$BUDGET_USD")
TICKET_FLAGS=()

mkdir -p "$RUN_DIR"
SUMMARY="$RUN_DIR/summary.txt"

log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$SUMMARY" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------- keep awake
caffeinate -dimsu -w $$ &
CAFF_PID=$!
trap 'kill $CAFF_PID 2>/dev/null' EXIT
log "caffeinate running (pid $CAFF_PID); logs in $RUN_DIR"
log "prompt book: ${PROMPT_BOOK#"$REPO"/}; tickets: ${TICKETS[*]}"

# ---------------------------------------------------------------- preflight
for bin in claude gh git python3 jq; do
  command -v "$bin" >/dev/null || die "$bin not found on PATH"
done
[ -x "$PY" ] || die "no python at $PY (create the venv, or set PY=...)"
"$PY" -m pytest --version >/dev/null 2>&1 || die "pytest is not installed in $PY"
command -v node >/dev/null || log "note: node not installed; changed .js files won't be syntax-checked"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated"
[ "$(git -C "$REPO" branch --show-current)" = "$BASE_BRANCH" ] \
  || die "main checkout must be on $BASE_BRANCH"
case "$PUSH" in 0|1|end) ;; *) die "PUSH must be 0, 1 or end" ;; esac
# Uncommitted tracked changes are stashed around each merge and restored after;
# if a ticket touches the same lines, the restore conflicts and the run stops
# mid-way with a ticket merged but not closed. Refuse up front instead.
if [ "${ALLOW_DIRTY:-0}" != 1 ] && { ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; }; then
  git -C "$REPO" status --short --untracked-files=no >&2
  die "main checkout has uncommitted changes (above); commit them first, or ALLOW_DIRTY=1 to stash them around each merge"
fi

# Extracts the fenced prompt block that follows "## N — #<ticket>".
prompt_for() {
  python3 -I - "$PROMPT_BOOK" "$1" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
m = re.search(r'^## \d+ — #' + sys.argv[2] + r'\b.*?\n```\n(.*?)\n```', text, re.S | re.M)
if not m:
    sys.exit(1)
print(m.group(1))
PY
}

UNATTENDED_NOTE='

---
RUNNER NOTES (unattended run — no human is watching this session):
- You are in a git worktree on branch ticket/__N__, cut from main. Commit your work on THIS branch. Do not merge, push, switch branches, or close/edit GitHub issues; the runner does that after its own gates pass.
- Nobody can answer questions. Wherever the prompt says "ask me" or "tell me and stop", make the safe choice (skip the risky step, or stop) and explain it in your final message.
- Do not start the app (no start.sh, uvicorn, main.py, docker compose) and make no real network calls to HeyGen, Gemini, ElevenLabs, S3 or the NAS. Tests use the existing fakes.
- This worktree has no venv. Run tests with `__PY__ -m pytest <test files>` from the worktree root; pytest.ini puts the worktree on sys.path.
- The runner gates on: commits on this branch, a clean worktree, every changed .py compiling and every changed .js passing `node --check`, and pytest with no NEW failures versus main (tests that already fail on main are listed at the end of these notes; leave them alone).
- Test scope: run only the test files you add or change. Do NOT run the full suite: the runner runs it in its gate and sends you any failures. This overrides the implement skill'"'"'s "full suite at the end" step and any such line in the prompt above.
- __REVIEW__
- The file paths and line areas in the prompt above were checked when the prompt book was written. Open them directly; search the codebase only for what the prompt does not cover.
- If you change what a CONTEXT.md term means, or add a module that earns a name, update CONTEXT.md in the same commit.
- Run tests with network calls stubbed; a test that blocks on the network is killed after __TIMEOUT__s and fails the gate.
- End with a hand-off message for the operator: what changed, test results, and anything they must do: new keys for the session .env / settings, whether the cloud container needs a restart after CI publishes, and what to click through in the Video or Audio pane to see the change.'

# ------------------------------------------------------- claude + limits
# Prints the number of seconds to wait for a usage limit, or nothing if the
# text isn't a usage-limit message.
limit_wait_seconds() {
  python3 -I - "$1" <<'PY'
import re, sys, time, datetime
t = sys.argv[1]
if not re.search(r'usage limit|limit reached|hit your limit|limit will reset|resets? (at )?\d', t, re.I):
    sys.exit(0)
now = time.time()
m = re.search(r'\|(\d{10})\b', t)                     # "...limit reached|1760000000"
if m:
    print(max(300, int(m.group(1)) - int(now) + 120)); sys.exit(0)
m = re.search(r'resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)', t, re.I)
if m:
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == 'pm' else 0)
    mi = int(m.group(2) or 0)
    n = datetime.datetime.now()
    r = n.replace(hour=h, minute=mi, second=0, microsecond=0)
    if r <= n:
        r += datetime.timedelta(days=1)
    print(max(300, int((r - n).total_seconds()) + 120)); sys.exit(0)
print(1800)                                            # unknown reset: poll every 30 min
PY
}

# run_claude <worktree> <logfile> <prompt> [resume_session_id]
# Sets CLAUDE_SESSION and CLAUDE_RESULT. Returns 0 when the session finishes,
# 1 on a non-limit error. Waits and resumes through usage limits.
run_claude() {
  local wt="$1" logf="$2" prompt="$3" resume="${4:-}"
  local attempt=0
  while :; do
    attempt=$((attempt + 1))
    local args=(-p "$prompt" --dangerously-skip-permissions --output-format stream-json --verbose)
    args+=(${BASE_FLAGS[@]+"${BASE_FLAGS[@]}"} ${TICKET_FLAGS[@]+"${TICKET_FLAGS[@]}"})
    [ -n "$resume" ] && args+=(--resume "$resume")
    log "  claude run (attempt $attempt${resume:+, resuming $resume})"
    (cd "$wt" && claude "${args[@]}") >>"$logf" 2>&1
    local rc=$?

    local result_line
    result_line="$(grep '"type":"result"' "$logf" | tail -1)"
    CLAUDE_SESSION="$(printf '%s' "$result_line" | jq -r '.session_id // empty' 2>/dev/null)"
    CLAUDE_RESULT="$(printf '%s' "$result_line" | jq -r '.result // empty' 2>/dev/null)"
    local is_error subtype
    is_error="$(printf '%s' "$result_line" | jq -r '.is_error // false' 2>/dev/null)"
    subtype="$(printf '%s' "$result_line" | jq -r '.subtype // empty' 2>/dev/null)"
    if [[ "$subtype" == *budget* ]]; then
      log "  session hit the \$$BUDGET_USD budget cap ($subtype); not resuming"
      return 1
    fi
    [ -z "$CLAUDE_SESSION" ] && CLAUDE_SESSION="$(grep -o '"session_id":"[^"]*"' "$logf" | tail -1 | cut -d'"' -f4)"

    # Limit detection only looks at the final result / last lines, never at
    # code the session wrote.
    local probe="$CLAUDE_RESULT"
    if [ "$rc" -ne 0 ] || [ "$is_error" = true ] || [ -z "$result_line" ]; then
      probe="$probe $(tail -5 "$logf")"
    fi
    local wait_s=""
    if [ "$rc" -ne 0 ] || [ "$is_error" = true ] || [ "${#CLAUDE_RESULT}" -lt 400 ]; then
      wait_s="$(limit_wait_seconds "$probe")"
    fi

    if [ -n "$wait_s" ]; then
      log "  usage limit hit; sleeping $((wait_s / 60)) min (until ~$(date -v+"${wait_s}"S '+%H:%M'))"
      # Wait on the wall clock in short naps: `sleep` stops counting while
      # the Mac sleeps (lid closed on battery, which caffeinate can't stop),
      # so one long sleep can overrun the reset by hours.
      local until_ts=$(( $(date +%s) + wait_s ))
      while [ "$(date +%s)" -lt "$until_ts" ]; do sleep 60; done
      if [ -n "$CLAUDE_SESSION" ]; then
        resume="$CLAUDE_SESSION"
        prompt="You were interrupted by a usage limit. Continue exactly where you left off and finish the task, ending with the hand-off message."
      fi
      continue
    fi
    if [ "$rc" -ne 0 ] || [ "$is_error" = true ]; then
      log "  claude exited rc=$rc is_error=$is_error (see $logf)"
      [ "$attempt" -lt 3 ] && [ -n "$CLAUDE_SESSION" ] && {
        resume="$CLAUDE_SESSION"
        prompt="The previous run ended with an error. Continue where you left off and finish the task."
        sleep 60; continue; }
      return 1
    fi
    return 0
  done
}

# ---------------------------------------------------------------- gates
# with_timeout <seconds> <cmd...>: macOS has no `timeout`; perl's alarm
# survives exec, so the command is killed by SIGALRM (exit 142) when it's up.
with_timeout() { perl -e 'alarm shift; exec @ARGV' "$@"; }

# Syntax-checks one changed file; prints the error and returns 1 if it doesn't
# parse. `node --check` silently passes a broken .js that uses import/export
# (module detection swallows the error), so those are checked as .mjs.
check_syntax() {  # <worktree> <path>
  local wt="$1" f="$2" tmp rc
  case "$f" in
    *.py) (cd "$wt" && "$PY" -I -c '
import sys
try:
    compile(open(sys.argv[1]).read(), sys.argv[1], "exec")
except SyntaxError as e:
    print("%s:%s: %s" % (e.filename, e.lineno, e.msg)); print("    " + (e.text or "").rstrip()); sys.exit(1)
' "$f"); return $? ;;
    *.js) command -v node >/dev/null || return 0
          if grep -qE '^[[:space:]]*(import|export)[[:space:]{*]' "$wt/$f"; then
            tmp="$(mktemp -d)"; cp "$wt/$f" "$tmp/check.mjs"
            (cd "$tmp" && node --check check.mjs) 2>&1 | sed "s#^.*check\.mjs#$f#" | grep -vE '^\s+at |^Node\.js' | head -8
            rc="${PIPESTATUS[0]}"; rm -rf "$tmp"
            return "$rc"
          fi
          node --check "$wt/$f" 2>&1 | sed "s#$wt/##" | grep -vE '^\s+at |^Node\.js' | head -8
          return "${PIPESTATUS[0]}" ;;
  esac
  return 0
}

# failing_tests <dir> <outfile>: writes sorted failing/erroring test node ids
# to <outfile> and the raw run to <outfile>.raw. Returns pytest's exit code:
# 0 all passed, 1 some failed — anything else means the run didn't complete
# (collection error, interrupted, usage error).
failing_tests() {
  # faulthandler dumps the stuck test's stack shortly before the hard kill, so
  # a timeout report names the test and the line it was blocked on.
  (cd "$1" && with_timeout "$TEST_TIMEOUT" "$PY" -m pytest tests -q -rfE --tb=no -p no:cacheprovider \
     -o faulthandler_timeout="$(( TEST_TIMEOUT > 20 ? TEST_TIMEOUT - 10 : TEST_TIMEOUT / 2 ))" 2>&1) >"$2.raw" 2>/dev/null
  local rc=$?
  grep -E '^(FAILED|ERROR) ' "$2.raw" | sed -E 's/^(FAILED|ERROR) //; s/ - .*$//' | sort -u >"$2"
  return $rc
}
# What to show when pytest didn't finish: for a timeout, the faulthandler
# dump (which test, which line); otherwise the tail of the run.
incomplete_report() {  # <rc> <raw log>
  if [ "$1" -eq 142 ]; then
    echo "pytest was killed after ${TEST_TIMEOUT}s (TEST_TIMEOUT): a test blocked, likely on a real network call or a lock. Stack of the stuck test:"
    sed -n '/Timeout (/,$p' "$2" | grep -E 'Timeout|File "[^"]*/(tests|services|batch|api)/' \
      | sed -E 's/^[.FEsx]*(Timeout)/\1/; s#File "[^"]*/((tests|services|batch|api)/)#File "\1#' | head -25
  else
    echo "pytest did not complete (rc $1; collection or import error?):"; tail -40 "$2"
  fi
}
# Short tracebacks for just the named tests, capped. A fix-up round gets the
# failures, not the log tail.
failure_blocks() {  # <dir> <file of failing node ids>
  local ids=(); while read -r id; do [ -n "$id" ] && ids+=("$id"); done <"$2"
  [ ${#ids[@]} -gt 0 ] || return 0
  (cd "$1" && with_timeout "$TEST_TIMEOUT" "$PY" -m pytest "${ids[@]}" -q --tb=short --disable-warnings -p no:cacheprovider 2>&1) | head -300
}

log "computing $BASE_BRANCH baseline (pytest)…"
BASE_WT="$WT_ROOT/baseline"
git -C "$REPO" worktree remove --force "$BASE_WT" >/dev/null 2>&1
git -C "$REPO" worktree add --detach "$BASE_WT" "$BASE_BRANCH" >/dev/null 2>&1 || die "baseline worktree failed"
failing_tests "$BASE_WT" "$RUN_DIR/baseline-failures.txt"
base_rc=$?
git -C "$REPO" worktree remove --force "$BASE_WT" >/dev/null 2>&1
[ "$base_rc" -le 1 ] || die "pytest did not complete on $BASE_BRANCH (rc $base_rc); see $RUN_DIR/baseline-failures.txt.raw"
log "baseline: $(wc -l <"$RUN_DIR/baseline-failures.txt" | tr -d ' ') failing tests"

# gate <ticket> <worktree>: returns 0 if green, else writes reasons to $GATE_REPORT
gate() {
  local n="$1" wt="$2"
  GATE_REPORT="$RUN_DIR/ticket-$n-gate.txt"; : >"$GATE_REPORT"
  local ok=0

  # 1. Cheap: commits and a clean tree. If these fail, report only them; the
  #    syntax and test gates run on the next round.
  local ahead; ahead="$(git -C "$wt" rev-list --count "$BASE_BRANCH..HEAD")"
  [ "$ahead" -gt 0 ] || { echo "No commits on ticket/$n beyond $BASE_BRANCH." >>"$GATE_REPORT"; ok=1; }
  if [ -n "$(git -C "$wt" status --porcelain)" ]; then
    { echo "Worktree has uncommitted changes (commit or remove them):"; git -C "$wt" status --short; } >>"$GATE_REPORT"
    ok=1
  fi
  [ $ok -eq 0 ] || return $ok

  # 2. Syntax of what the branch changed. A file that doesn't parse would fail
  #    the whole suite at import, so report just these and skip the test run.
  local f out
  while read -r f; do
    [ -n "$f" ] || continue
    out="$(check_syntax "$wt" "$f")" || { { echo "$f does not parse:"; echo "$out"; echo; } >>"$GATE_REPORT"; ok=1; }
  done < <(git -C "$wt" diff --name-only --diff-filter=d "$BASE_BRANCH...HEAD")
  [ $ok -eq 0 ] || return $ok

  # 3. Full pytest suite, compared with the baseline.
  failing_tests "$wt" "$RUN_DIR/ticket-$n-failures.txt"
  local rc=$?
  if [ "$rc" -gt 1 ]; then
    incomplete_report "$rc" "$RUN_DIR/ticket-$n-failures.txt.raw" >>"$GATE_REPORT"
    return 1
  fi
  local new_fail
  new_fail="$(comm -13 "$RUN_DIR/baseline-failures.txt" "$RUN_DIR/ticket-$n-failures.txt")"
  if [ -n "$new_fail" ]; then
    printf '%s\n' "$new_fail" >"$RUN_DIR/ticket-$n-new-failures.txt"
    { echo "New failing tests (not failing on $BASE_BRANCH):"; echo "$new_fail"; echo
      echo "--- short traceback of each new failure ---"
      failure_blocks "$wt" "$RUN_DIR/ticket-$n-new-failures.txt"; } >>"$GATE_REPORT"
    ok=1
  fi
  return $ok
}

# ---------------------------------------------------------------- merge
merge_ticket() {
  local n="$1" branch="ticket/$1"
  [ "$(git -C "$REPO" branch --show-current)" = "$BASE_BRANCH" ] || { log "  main checkout left $BASE_BRANCH"; return 1; }
  local stashed=0
  if ! git -C "$REPO" diff --quiet || ! git -C "$REPO" diff --cached --quiet; then
    git -C "$REPO" stash push -m "run-tickets: pre-merge #$n" >/dev/null && stashed=1
  fi
  if ! git -C "$REPO" merge --no-ff "$branch" -m "Merge $branch: $(gh issue view "$n" --json title -q .title)

Closes #$n" >>"$RUN_DIR/merges.txt" 2>&1; then
    git -C "$REPO" merge --abort 2>/dev/null
    [ $stashed = 1 ] && git -C "$REPO" stash pop >/dev/null
    log "  merge conflict merging $branch; aborted"
    return 1
  fi
  if [ $stashed = 1 ] && ! git -C "$REPO" stash pop >/dev/null 2>&1; then
    log "  WARNING: your uncommitted changes conflicted after merging #$n; they're kept in 'git stash list'. Stopping."
    exit 1
  fi
  if [ "$PUSH" = 1 ]; then
    git -C "$REPO" push origin "$BASE_BRANCH" >>"$RUN_DIR/push.txt" 2>&1 || { log "  push failed (see push.txt)"; return 1; }
  fi
  return 0
}

# The push's "Closes #N" may already have closed the issue, and `gh issue
# close --comment` on a closed issue drops the comment. So comment first, then
# close (a no-op if GitHub got there first).
close_ticket() {
  local n="$1" handoff="$2" sha; sha="$(git -C "$REPO" rev-parse --short HEAD)"
  local body; body="$(printf 'Implemented by the unattended runner and merged into `%s` at %s (gates: changed files parse, pytest with no new failures vs %s).\n\n## Hand-off\n\n%s' \
    "$BASE_BRANCH" "$sha" "$BASE_BRANCH" "$handoff")"
  gh issue comment "$n" --body "${body:0:60000}" >/dev/null || return 1
  [ "$(issue_state "$n")" = CLOSED ] || gh issue close "$n" >/dev/null
}

# ---------------------------------------------------------------- main loop
# Blockers: BLOCKERS env if set, else GitHub's native "blocked by" links, plus
# any "(blocked by #N)" on the prompt-book heading.
blockers_for() {
  local pair
  if [ -n "$BLOCKERS" ]; then
    for pair in $BLOCKERS; do [ "${pair%%:*}" = "$1" ] && echo "${pair#*:}"; done
    return 0
  fi
  { (cd "$REPO" && gh api "repos/{owner}/{repo}/issues/$1/dependencies/blocked_by" --jq '.[].number' 2>/dev/null)
    book_blockers "$1"; } | sort -un
}
issue_state() { gh issue view "$1" --json state -q .state 2>/dev/null; }

DONE=() FAILED=()
for n in "${TICKETS[@]}"; do
  log "=== ticket #$n ==="
  if [ "$(issue_state "$n")" = CLOSED ]; then log "  already closed; skipping"; DONE+=("$n"); continue; fi
  blocked=""
  for b in $(blockers_for "$n"); do [ "$(issue_state "$b")" = CLOSED ] || blocked="$blocked #$b"; done
  if [ -n "$blocked" ]; then log "  blocked by$blocked (still open); skipping"; FAILED+=("$n(blocked)"); continue; fi

  prompt="$(prompt_for "$n")" || { log "  no prompt block for #$n in prompt book"; FAILED+=("$n"); continue; }
  # Per-ticket model / effort / review: env pairs, else heading tags, else defaults.
  t_model="$(pair_value "$n" "$MODEL_TICKETS")"; t_model="${t_model:-$(book_tag "$n" model)}"; t_model="${t_model:-$MODEL}"
  t_effort="$(pair_value "$n" "$EFFORT_TICKETS")"; t_effort="${t_effort:-$(book_tag "$n" effort)}"; t_effort="${t_effort:-$EFFORT}"
  TICKET_FLAGS=()
  [ -n "$t_model" ] && TICKET_FLAGS+=(--model "$t_model")
  [ -n "$t_effort" ] && TICKET_FLAGS+=(--effort "$t_effort")
  case "$REVIEW" in
    all) t_review=1 ;;
    none) t_review=0 ;;
    *) if book_review "$n"; then t_review=1; else t_review=0; fi ;;
  esac
  if [ "$t_review" = 1 ]; then
    review_note="Review: run /code-review once at the end, as the implement skill says, and fix what it confirms."
  else
    review_note="Review: SKIP the implement skill's /code-review step for this ticket. The runner's gates stand in for it."
  fi
  log "  model ${t_model:-default}, effort ${t_effort:-default}, review $([ "$t_review" = 1 ] && echo on || echo off)"

  note="${UNATTENDED_NOTE//__N__/$n}"
  note="${note//__PY__/$PY}"
  note="${note//__TIMEOUT__/$TEST_TIMEOUT}"
  prompt="$prompt${note//__REVIEW__/$review_note}
- Already failing on $BASE_BRANCH: $(tr '\n' ';' <"$RUN_DIR/baseline-failures.txt")"

  wt="$WT_ROOT/ticket-$n"; branch="ticket/$n"
  if [ ! -d "$wt" ]; then
    if git -C "$REPO" show-ref --quiet "refs/heads/$branch"; then
      git -C "$REPO" worktree add "$wt" "$branch" >/dev/null 2>&1
    else
      git -C "$REPO" worktree add -b "$branch" "$wt" "$BASE_BRANCH" >/dev/null 2>&1
    fi || { log "  could not create worktree"; FAILED+=("$n"); continue; }
  fi

  logf="$RUN_DIR/ticket-$n-claude.jsonl"
  if ! run_claude "$wt" "$logf" "$prompt"; then FAILED+=("$n"); continue; fi
  session="$CLAUDE_SESSION"; handoff="$CLAUDE_RESULT"
  printf '%s\n' "$handoff" >"$RUN_DIR/ticket-$n-handoff.md"

  green=1
  for ((i = 0; i <= FIX_ATTEMPTS; i++)); do
    log "  gates (round $((i + 1)))"
    if gate "$n" "$wt"; then green=0; break; fi
    log "  gates red: $(head -1 "$GATE_REPORT")"
    [ "$i" -eq "$FIX_ATTEMPTS" ] && break
    fix_prompt="The runner's verification gates failed for ticket #$n. Fix the problems below, rerun the relevant tests, commit on this branch, and end with an updated hand-off message.

$(cat "$GATE_REPORT")"
    run_claude "$wt" "$logf" "$fix_prompt" "$session" || break
    session="$CLAUDE_SESSION"; handoff="$CLAUDE_RESULT"
    printf '%s\n' "$handoff" >"$RUN_DIR/ticket-$n-handoff.md"
  done
  # main moved under the branch (you committed mid-run): bring it in and
  # gate again, so the merge into main is exactly what was tested.
  if [ $green -eq 0 ] && ! git -C "$wt" merge-base --is-ancestor "$BASE_BRANCH" HEAD; then
    log "  $BASE_BRANCH moved since ticket/$n was cut; merging it in and re-gating"
    if ! git -C "$wt" merge --no-edit "$BASE_BRANCH" >>"$RUN_DIR/ticket-$n-catchup.txt" 2>&1; then
      git -C "$wt" merge --abort 2>/dev/null
      log "  conflict merging $BASE_BRANCH into ticket/$n"; green=1
    elif ! gate "$n" "$wt"; then
      log "  gates red after catching up with $BASE_BRANCH: $(head -1 "$GATE_REPORT")"; green=1
    fi
  fi
  if [ $green -ne 0 ]; then
    log "  #$n NOT merged; worktree kept at $wt, gate report $GATE_REPORT"
    FAILED+=("$n"); continue
  fi

  if ! merge_ticket "$n"; then FAILED+=("$n"); continue; fi
  close_ticket "$n" "$handoff" || log "  WARNING: merged but commenting on/closing #$n failed"
  log "  #$n merged into $BASE_BRANCH and closed"
  DONE+=("$n")
  if [ "$KEEP_WORKTREES" != 1 ]; then
    git -C "$REPO" worktree remove --force "$wt" >/dev/null 2>&1
    git -C "$REPO" branch -d "$branch" >/dev/null 2>&1
  fi
done

if [ "$PUSH" = end ] && [ ${#DONE[@]} -gt 0 ]; then
  if git -C "$REPO" push origin "$BASE_BRANCH" >>"$RUN_DIR/push.txt" 2>&1; then
    log "pushed $BASE_BRANCH once for the whole run"
  else
    log "WARNING: final push failed (see push.txt); merges are local only, and their issues were closed"
  fi
fi
log "=== finished: done [${DONE[*]:-}] failed/skipped [${FAILED[*]:-}] ==="
log "hand-off notes: $RUN_DIR/ticket-*-handoff.md"
[ ${#FAILED[@]} -eq 0 ]
