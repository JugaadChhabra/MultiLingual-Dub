# Setting up Content Hub on the Mac mini

Everything on the office Mac mini, start to finish: Content Hub (in Docker) and
the Comp engine behind the Compilations page (outside Docker, as a background
service). Do it once; the same steps are how you update later.

When it's done, anyone on the office wifi opens `http://<mac-mini-name>.local:8080`
in a browser. Nothing is installed on their computers.

```
Office Mac mini
├── Docker Desktop → Content Hub         port 8080, what people open
└── Comp engine (background service)     only Content Hub talks to it
      ├── uses the Mac's hardware video encoder
      └── reads the rhymes from the NAS (/Volumes/DATA/Animations/Rhymes)
```

---

## 1. Before you start (one time)

On the Mac mini, signed in as the user that will run Content Hub:

1. **Homebrew.** If `brew` isn't installed, install it from https://brew.sh.
2. **ffmpeg** (the Comp engine uses it to make comps):
   ```bash
   brew install ffmpeg
   ```
3. **Docker Desktop** for Mac (Apple silicon). Open it once. In Settings → General,
   turn on **Start Docker Desktop when you sign in**.
4. **The rhymes share.** In Finder: Go → Connect to Server → `smb://10.1.1.3/DATA` (the same share video jobs are saved to).
   Tick **Remember this password in my keychain**. It appears as `/Volumes/DATA`; the rhymes are in `/Volumes/DATA/Animations/Rhymes`.
   - So it comes back after a restart: System Settings → General → Login Items →
     **+** → pick the DATA volume.
   - If the share has a different name, note it for step 4 below.
5. **Survive power cuts.** The Comp engine and the share both need someone logged in.
   - System Settings → Users & Groups → **Automatically log in as** this user.
     macOS only offers this when FileVault is off.
   - Keep it awake: `sudo pmset -a sleep 0 autorestart 1`

## 2. Put the files on the Mac mini

Content Hub is deployed as its Docker image, and the Comp engine ships inside that
image too, so the mini never gets the code. One folder holds just:

| File | |
|---|---|
| `docker-compose.yml` | which image to run, on which port |
| `setup-mac-mini.sh` | starts or updates everything |
| `Start Content Hub.command`, `Stop Content Hub.command` | double-click start and stop |
| `.env` | API keys and NAS settings, copied from your laptop |

Copy them from the repo once. You only copy them again if one of these four files changes.
Docker also creates `output/` there for Content Hub's data.

## 3. Run the setup script

Wait until GitHub has finished building the image for the latest push to `main`
(the repo's **Actions** tab shows a green tick), then:

```bash
cd ~/content-hub
bash setup-mac-mini.sh
```

It checks the files, Docker, and port conflicts with other apps on the mini (e.g. Midas).
It refuses to restart Content Hub while jobs are running. Then it pulls and starts
Content Hub, copies the Comp engine out of the same image and installs it as a
background service. Read the end of its output:

- `[ok] Comp engine ... answering on http://127.0.0.1:8765`
- `[ok] Content Hub can reach the Comp engine`: the Compilations page will work.
- The addresses to share with the office: `http://<name>.local:8080` and `http://<ip>:8080`.

Any `[warn]` line says what's wrong. The common ones are in step 6.

## 4. Point the Comp engine at the library

The first install writes a starting settings file:

```
~/Library/Application Support/CompCreator/config.json
```

Open it and check two folders: where the intro and outro are, and where finished comps
are saved. Both are on the DATA share, next to the video jobs' character folders:

```json
"intro_outro_folder": "/Volumes/DATA/Animations/Rhymes/Intro & Outro",
"output_folder":      "/Volumes/DATA/Animations/Rhymes/Comps Outs",
```

Comps are saved into `Comps Outs/<Language>/`, with a `<name> - Timestamps.txt` beside each.
The rhymes themselves aren't set here: people paste a folder on the Compilations page (step 5), either on the NAS or on the Mac mini itself inside the engine account's home folder (e.g. `~/Movies/Rhymes`). Comps are always saved to Comps Outs on the NAS.

After editing, restart the engine:

```bash
launchctl kickstart -k gui/$(id -u)/com.contenthub.compcreator
```

## 5. Check it works

1. Open `http://localhost:8080/comps` on the mini. Paste a folder of rhyme videos from the NAS
   (e.g. `\\10.1.1.3\DATA\Animations\Rhymes\Hindi`, or `/Volumes/DATA/...` from a Mac) and click
   **Add folder**. Its thumbnails appear, and the folder is remembered in that browser.
2. Pick two short rhymes and click **Create comp**.
3. It shows **Making**, then **Saving**, then **Ready**. Click **Open** to watch it and copy the chapters.
4. Check that the file is in `Comps Outs/<Language>/` on the NAS.
5. From another computer on the wifi, open `http://<mac-mini-name>.local:8080`.

## 6. If something's wrong

| You see | Do this |
|---|---|
| Compilations says **"can't be made right now"** | The engine isn't running. Look at `~/Library/Logs/CompCreator/engine.log`, then restart it with the `launchctl kickstart` line from step 4. |
| **"The NAS isn't reachable"** or **"The DATA share isn't connected"** | The share isn't mounted on the mini. Reconnect it in Finder (step 1.4), then click Try again. |
| A pasted folder says **"no folder at that path"** but it's there | Recent macOS blocks background programs from network drives and from Desktop, Documents and Downloads. System Settings → Privacy & Security → Full Disk Access → **+** → ⌘⇧G → `~/Library/Application Support/CompCreator/` → `CompCreator`, then restart the engine (step 4). |
| **"ffmpeg is missing"** | `brew install ffmpeg`, then restart the engine. |
| Setup says **"cannot reach host.docker.internal:8765"** | Docker can't see the engine. Run `bash setup-mac-mini.sh` again. If it persists, ask for the `--addr` change (the engine then listens beyond the Mac itself). |
| A comp shows **Not on NAS** | It was made, but the copy to the NAS failed (usually the share dropped). Reconnect the share, then click **Save to NAS again**. Nothing is re-encoded. |
| Port 8080 conflict, or another project with the same name | The setup script says which. Usually Content Hub is already running from another folder: see the note in step 2. |

## Updating later

Push to `main`, wait for the build, then on the mini run `bash setup-mac-mini.sh`.
That updates Content Hub and the Comp engine together. It won't restart while jobs or
comps are running, and the engine's `config.json` is kept.

## Where things are

| What | Where |
|---|---|
| Content Hub logs | `docker compose logs -f autodub` (in the folder) |
| Content Hub data | `output/` in the folder |
| Comp engine settings | `~/Library/Application Support/CompCreator/config.json` |
| Comp engine log | `~/Library/Logs/CompCreator/engine.log` |
| Comps being made (local copies, deleted 48 h after saving to the NAS) | `~/Library/Application Support/CompCreator/work/` |
| Stop the Comp engine | `launchctl bootout gui/$(id -u)/com.contenthub.compcreator` |
| Start it again | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.contenthub.compcreator.plist` |
