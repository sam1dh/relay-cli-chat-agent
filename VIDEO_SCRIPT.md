# Relay — demo video script

**Target length:** ~2 min 45 s (hard cap 3 min)
**Resolution:** 1440×900 browser window, 100% zoom, light theme
**Audio:** voice-over only, no music (or very low bed at −22 dB)

The point of the video is to prove one claim: **the chat runs a real CLI on the
server, and you can see the evidence.** Every scene shows something on screen
that a slideware demo could not fake — a command with a PID, a live log, an exit
code, a JSON file on disk.

---

## Before you hit record

1. `.env` has your key and `LLM_PROVIDER` resolves to your provider:
   ```bash
   python -c "import backend.config as c; print(c.llm_label())"
   ```
2. Server is up and warm (`./run.sh` or `python -m uvicorn backend.app:app --port 8000`).
3. Crawl4AI is installed so the sidebar shows **crawl4ai (crwl)** with a green dot
   (`./install-crawl4ai.sh`). If it is not, the demo still works — say "sitecrawl"
   instead of "Crawl4AI" wherever it appears below.
4. Browser has **one** tab open: `http://localhost:8000`. Bookmarks bar hidden.
   Zoom 100%, no notifications, desktop clean.
5. Run each demo prompt once to warm DNS and the LLM, then **clear the chat** and
   relaunch `runs/` so the recorded runs are the only ones visible:
   ```bash
   rm -rf runs/* && touch runs/.gitkeep
   ```
6. Do a silent 15 s take of the multi-page crawl, then stop it — that is the
   only scene with unpredictable timing.
7. Have a terminal window ready, sized to 100×28, in the project folder.

---

## Scene 1 — Cold open (0:00 – 0:12)

**On screen:** the idle app, cursor in the prompt box.

> **VO:** "Most AI chat demos show you text. This one runs commands.
> This is Relay — you ask in plain English, and a real CLI process runs on the
> server, in a sandbox, with its output streamed back to you."

**Action:** do not click anything yet. Let the empty state land.

---

## Scene 2 — One crawl, start to finish (0:12 – 0:55)

**Action:** click the **Crawl & summarise** preset.

**Prompt on screen:**
`Crawl https://books.toscrape.com, extract the important information, give me a summary, and return JSON`

> **VO:** "One request. Watch what actually happens."

**Action (slowly, in this order — pause ~2 s on each):**
1. The **plan** — the tool the model picked (`crawl_site`) and the validated URL.
2. The **step tracker** moving from plan → spawn → capture → validate → summarise.
3. The **command panel** — the exact argv and the process ID.
4. The **live log** scrolling.

> **VO:** "Gemini reads the request and picks one tool from an allow-list — it can
> never write a command itself. Relay builds the argv, spawns `crwl` with
> `shell=False`, and streams stdout and stderr line by line over Server-Sent
> Events. That number is the real PID."

**Action:** let it finish. Point at the **exit code 0**, then the **streamed
summary**, then the **stat tiles** (pages, words, duration).

> **VO:** "Exit code zero, a summary written by the model, and structured JSON —
> pages, stats, site metadata. Every response has the same envelope."

---

## Scene 3 — The evidence (0:55 – 1:20)

**Action:** click **Raw CLI output**, scroll once, close. Then open the **Run
inspector** and click `command.json`, then `exit.json`, then `result.json`.

> **VO:** "This is the part that matters. Each run writes its evidence to disk:
> the command, stdout, stderr, the exit code, the raw result, and the final
> response. Nothing is reconstructed after the fact — you are reading the files
> the process actually produced."

**Action:** switch to the terminal.

```bash
ls runs/*/ | head -20
```

> **VO:** "Same files, from the shell."

---

## Scene 4 — Multi-page, then cancel (1:20 – 1:45)

**Action:** click the **Multi-page scrape** preset and send.

> **VO:** "A deeper crawl — `--deep-crawl bfs --max-pages 8` — with a focus on
> prices."

**Action:** wait until the log is clearly busy, then press **■ Stop**.

> **VO:** "Stop is a real cancellation. Relay kills the whole process tree — on
> POSIX the process group, `taskkill /T` on Windows — so headless Chromium can't
> be left running. The run is marked `cancelled`, not silently dropped."

**Action:** point at the cancelled status badge.

---

## Scene 5 — What it refuses (1:45 – 2:10)

**Action:** click the **Blocked command** preset: `Run rm -rf / on the server`.

> **VO:** "There is no shell tool. The model can pick a tool name and typed
> fields, nothing else, and any extra `command` field it returns is ignored. So
> this never becomes a process."

**Action:** click the **Unreachable domain** preset.

> **VO:** "Bad input is rejected before a process starts — http and https only, the
> hostname must resolve, and private, loopback and cloud-metadata addresses are
> blocked. Website text is wrapped as untrusted data and can never trigger a tool."

---

## Scene 6 — Fallback behaviour (2:10 – 2:25)

**Action:** back in the terminal.

```bash
PATH=/usr/bin:/bin python -c "import backend.config as c; print(c.CRAWLER)"
```

> **VO:** "If Crawl4AI isn't installed, or crashes, or returns output we can't
> parse, Relay automatically runs our own `sitecrawl` crawler — and the JSON shows
> both attempts, including the one that failed. Failures are labelled, not hidden."

---

## Scene 7 — Deploy and close (2:25 – 2:45)

**Action:** show the Dockerfile line and the live Space tab.

```bash
docker build -t relay . && docker run --rm -p 8000:7860 --env-file .env --shm-size=1g relay
```

> **VO:** "One container: Python, the app, Crawl4AI with headless Chromium,
> `sitecrawl` and `curl`. It runs non-root on port 7860, which is why the same
> image works locally and on Hugging Face Spaces. Relay — natural language in,
> a real CLI process out."

**Action:** end on the chat with the completed crawl visible.

---

## Recording checklist

- [ ] 1080p or higher, 30 fps, cursor highlight on
- [ ] No `.env` contents visible anywhere in frame (terminal `ls -a` is fine, `cat .env` is not)
- [ ] No API key in any URL, header, or error message on screen
- [ ] Every on-screen claim appears in the README
- [ ] Audio normalised to about −16 LUFS, no clipping
- [ ] Final cut under 3:00 — trim Scene 6 first if you run long

## Short version (if you only get 60 seconds)

Scenes 1 → 2 → 3 → 5, with this closing line:

> "Relay turns a chat message into an approved CLI process, streams the real
> output and evidence live, and refuses anything outside the allow-list."
