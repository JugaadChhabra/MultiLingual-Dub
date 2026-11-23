#!/bin/bash
# Installs Comp Creator on the Mac mini as a background service (LaunchAgent),
# headless, listening on 127.0.0.1:8765. Content Hub talks to it from there.
# Safe to run again: it is also how you update to a newer build.
#
#   bash comp_gen/macos/install.sh            # builds from comp_gen/source (needs Go)
#   bash install.sh /path/to/CompCreator      # or installs a binary built elsewhere:
#        cd comp_gen/source && GOOS=darwin GOARCH=arm64 go build -ldflags "-s -w" -o CompCreator .
#
# It runs as a LaunchAgent (your login session), not a system daemon, because the
# NAS share is mounted per user in Finder: a daemon would not see /Volumes/<share>.

set -u
LABEL="com.contenthub.compcreator"
ADDR="127.0.0.1:8765"
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/Library/Application Support/CompCreator"
LOGS="$HOME/Library/Logs/CompCreator"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

ok()   { echo "  [ok]   $*"; }
warn() { echo "  [warn] $*"; }
fail() { echo "  [FAIL] $*"; exit 1; }

[ "$(uname)" = "Darwin" ] || fail "This script is for macOS."

# 1. ffmpeg (Homebrew). The service finds it in /opt/homebrew/bin even though
#    launchd's PATH does not include that folder.
FF="$(command -v ffmpeg || true)"
[ -n "$FF" ] || fail "ffmpeg is not installed. Run: brew install ffmpeg"
if "$FF" -hide_banner -encoders 2>/dev/null | grep -q h264_videotoolbox; then
  ok "ffmpeg with Apple hardware encoding ($FF)"
else
  warn "this ffmpeg has no h264_videotoolbox: comps will encode on the CPU (slower)"
fi

# 2. The binary
mkdir -p "$DATA" "$LOGS"
if [ $# -ge 1 ]; then
  [ -f "$1" ] || fail "no binary at $1"
  cp "$1" "$DATA/CompCreator.new"
else
  command -v go >/dev/null || fail "Go is not installed. Build on another Mac and pass the binary: bash install.sh ./CompCreator"
  (cd "$HERE/../source" && go build -ldflags "-s -w" -o "$DATA/CompCreator.new" .) || fail "build failed"
fi
xattr -d com.apple.quarantine "$DATA/CompCreator.new" 2>/dev/null
chmod +x "$DATA/CompCreator.new"
ok "binary ready"

# 3. Don't kill comps in flight. The queue survives a restart, but a running
#    comp starts again from 0%.
if curl -fs --max-time 2 "http://$ADDR/api/queue" -o /tmp/cc_queue.json; then
  if grep -q '"state":"running"' /tmp/cc_queue.json; then
    rm -f "$DATA/CompCreator.new"
    fail "comps are being made right now. Let them finish, then run this again."
  fi
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
mv "$DATA/CompCreator.new" "$DATA/CompCreator"

# 4. Settings: only written the first time, never overwritten.
if [ ! -f "$DATA/config.json" ]; then
  cp "$HERE/config.example.json" "$DATA/config.json"
  warn "wrote a starting config.json - check the folders in it: $DATA/config.json"
else
  ok "keeping existing config.json"
fi

# 5. LaunchAgent: start at login, restart if it ever exits.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DATA/CompCreator</string>
    <string>--dir</string><string>$DATA</string>
    <string>--addr</string><string>$ADDR</string>
    <string>--no-browser</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/engine.log</string>
  <key>StandardErrorPath</key><string>$LOGS/engine.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST" || fail "launchctl could not load $PLIST"
ok "service loaded ($LABEL)"

for _ in $(seq 1 20); do
  curl -fs --max-time 2 "http://$ADDR/api/config" -o /tmp/cc_cfg.json && break
  sleep 1
done
[ -s /tmp/cc_cfg.json ] || fail "the service did not answer on $ADDR. Log: $LOGS/engine.log"
ok "answering on http://$ADDR"
python3 - /tmp/cc_cfg.json <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))["status"]
for k, label in [("ffmpeg", "ffmpeg found"), ("rhymes_ok", "rhymes folder reachable"),
                 ("intro_ok", "intro found"), ("outro_ok", "outro found")]:
    print(("  [ok]   " if s.get(k) else "  [warn] NOT ") + label)
print("  [info] GPU encoder:", s.get("gpu") or "none (CPU)")
PY
echo ""
echo "Logs:  $LOGS/engine.log"
echo "Stop:  launchctl bootout gui/$(id -u)/$LABEL"
