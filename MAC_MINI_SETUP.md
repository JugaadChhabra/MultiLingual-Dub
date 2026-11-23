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
      └── reads the rhymes from the NAS share (/Volumes/RHYMES)
```

---

## 1. Before you start (one time)

On the Mac mini, signed in as the user that will run Content Hub:

1. **Homebrew.** If `brew` isn't installed, install it from https://brew.sh.
2. **ffmpeg and Go** (Go builds the Comp engine):
   ```bash
   brew install ffmpeg go
   ```
3. **Docker Desktop** for Mac (Apple silicon). Open it once. In Settings → General,
   turn on **Start Docker Desktop when you sign in**.
4. **The rhymes share.** In Finder: Go → Connect to Server → `smb://<NAS address>/RHYMES`.
   Tick **Remember this password in my keychain**. It appears as `/Volumes/RHYMES`.
   - So it comes back after a restart: System Settings → General → Login Items →
     **+** → pick the RHYMES volume.
   - If the share has a different name, note it for step 4 below.
5. **Survive power cuts.** The Comp engine and the share both need someone logged in.
   - System Settings → Users & Groups → **Automatically log in as** this user.
     macOS only offers this when FileVault is off.
   - Keep it awake: `sudo pmset -a sleep 0 autorestart 1`

## 2. Put the files on the Mac mini

You need one folder (for example `~/content-hub`) holding:

| From the repo | |
|---|---|
| `docker-compose.yml` | |
| `setup-mac-mini.sh` | |
| `Start Content Hub.command`, `Stop Content Hub.command` | |
| `comp_gen/` (the whole folder) | the Comp engine; the `.exe` files are not needed |
| `.env` | copied from your laptop, never from git: it holds the API keys |

The easiest way to get them is to clone the repo there and copy `.env` into it:

```bash
git clone https://github.com/JugaadChhabra/MultiLingual-Dub.git ~/content-hub
cp /path/to/.env ~/content-hub/.env
```

**If Content Hub already runs on the mini from another folder**, don't make a second one.
Copy the new `docker-compose.yml`, `setup-mac-mini.sh` and `comp_gen/` into that folder instead,
and run step 3 there. Two folders would be two Docker projects fighting over port 8080.

## 3. Run the setup script

Wait until GitHub has finished building the image for the latest push to `main`
(the repo's **Actions** tab shows a green tick), then:

```bash
cd ~/content-hub
bash setup-mac-mini.sh
```

It checks the files, Docker, and port conflicts with other apps on the mini (e.g. Midas).
It refuses to restart Content Hub while jobs are running. Then it pulls and starts
Content Hub and installs the Comp engine. Read the end of its output:

- `[ok] Comp engine ... answering on http://127.0.0.1:8765`
- `[ok] Content Hub can reach the Comp engine`: the Compilations page will work.
- The addresses to share with the office: `http://<name>.local:8080` and `http://<ip>:8080`.

Any `[warn]` line says what's wrong. The common ones are in step 6.

## 4. Point the Comp engine at the library

The first install writes a starting settings file:

```
~/Library/Application Support/CompCreator/config.json
```

Open it and check the three folders. Use the real share name if it isn't `RHYMES`:

```json
"rhymes_folder":      "/Volumes/RHYMES",
"intro_outro_folder": "/Volumes/RHYMES/Intro & Outro",
"output_folder":      "/Volumes/RHYMES/Comps Outs",
```

The library is laid out the same as on the old Windows PC: one folder per language
(`Hindi/`, `Gujarati/`, …), an `Intro & Outro/` folder, and comps saved into
`Comps Outs/<Language>/` with a `<name> - Timestamps.txt` beside each.

After editing, restart the engine:

```bash
launchctl kickstart -k gui/$(id -u)/com.contenthub.compcreator
```

## 5. Check it works

1. Open `http://localhost:8080/comps` on the mini. You should see the language tabs and rhyme thumbnails.
2. Pick two short rhymes and click **Create comp**.
3. It shows **Making**, then **Saving**, then **Ready**. Click **Open** to watch it and copy the chapters.
4. Check that the file is in `Comps Outs/<Language>/` on the NAS.
5. From another computer on the wifi, open `http://<mac-mini-name>.local:8080`.

## 6. If something's wrong

| You see | Do this |
|---|---|
| Compilations says **"can't be made right now"** | The engine isn't running. Look at `~/Library/Logs/CompCreator/engine.log`, then restart it with the `launchctl kickstart` line from step 4. |
| **"The rhymes library isn't reachable"** | The NAS share isn't mounted. Reconnect it in Finder (step 1.4), then click Try again. |
| **"ffmpeg is missing"** | `brew install ffmpeg`, then restart the engine. |
| Setup says **"cannot reach host.docker.internal:8765"** | Docker can't see the engine. Re-run `bash comp_gen/macos/install.sh`. If it persists, ask for the `--addr` change (the engine then listens beyond the Mac itself). |
| A comp shows **Not on NAS** | It was made, but the copy to the NAS failed (usually the share dropped). Reconnect the share, then click **Save to NAS again**. Nothing is re-encoded. |
| Port 8080 conflict, or another project with the same name | The setup script says which. Usually Content Hub is already running from another folder: see the note in step 2. |

## Updating later

- **Content Hub:** push to `main`, wait for the build, then run `bash setup-mac-mini.sh` again.
- **Comp engine:** after pulling changes to `comp_gen/`, run `bash comp_gen/macos/install.sh`.
  It refuses while a comp is being made. Your `config.json` is kept.

## Where things are

| What | Where |
|---|---|
| Content Hub logs | `docker compose logs -f autodub` (in the folder) |
| Content Hub data | `output/` in the folder |
| Comp engine settings | `~/Library/Application Support/CompCreator/config.json` |
| Comp engine log | `~/Library/Logs/CompCreator/engine.log` |
| Comps being made (local copies, deleted 48 h after saving to the NAS) | `~/Library/Application Support/CompCreator/work/` |
| Stop the Comp engine | `launchctl bootout gui/$(id -u)/com.contenthub.compcreator` |
| Start it again | `bash comp_gen/macos/install.sh` |
