# ScreenKey

**English** | [简体中文](../README.md)

Turn live-stream chat messages into real keyboard and mouse input.
A viewer types `left`, and the streamer's PC presses the left arrow key — right now.

Supports **Bilibili** and **Douyin (TikTok China)**. The Douyin path speaks the real
WebSocket danmaku protocol (not a mock, not page polling); the Bilibili path is built on
`blivedm` with a self-implemented WBI request signer so that the signed `getDanmuInfo`
endpoint actually answers instead of returning `-352`.

## Download

Don't want to install Python? Grab the packaged build (Windows 10/11 x64, no installation):

**[⬇ Download ScreenKey-v2.1.0-win64.zip](https://github.com/hgcrts/screenkey/releases/latest)** (42.7 MiB — unzip and run `ScreenKey.exe`; the SHA-256 lives in `SHA256SUMS.txt` on the release page.)

To run from source or hack on the code, see [Install (from source)](#install-from-source).

> This document describes behaviour that exists in the code. Anything unverified or
> limited is stated as such — there is no marketing copy here.
> Version `2.1.0`. **Windows only.**
>
> The exhaustive (Chinese) reference — every CLI flag, every config field, every
> failure post-mortem — lives in [README.md](../README.md) and [docs/]().

---

## What it does

```
fetch danmaku  →  filter rules  →  map to keys  →  inject input
(Bilibili/Douyin)  (ALLOW/DROP/…)  (exact/prefix/… )  (SendInput / scancode)
```

- **Fetch layer** — Bilibili over `blivedm`; Douyin over a hand-written
  `protobuf_lite` + `websockets` client whose `signature` parameter is computed by a
  resident Node.js process running Douyin's own obfuscated JS.
- **UI layer** — a single PyQt5 window with four tabs (Connection / Mapping / Options / Log)
  plus a floating danmaku window.
- **Actuator layer** — Win32 `SendInput` / `keybd_event`; "game mode" switches to hardware
  scan codes to get past DirectInput-based filtering.

Fetching runs on a background thread and injection deliberately runs on another thread —
`SendInput` is thread-agnostic Win32, so a busy UI never delays the keys.

---

## Feature highlights

| Capability | Notes |
| --- | --- |
| **Real Douyin danmaku** | WebSocket + hand-written protobuf + local Node.js `signature`. Requires Node.js installed. |
| **Bilibili danmaku** | `blivedm` + built-in WBI signer (`danmaku/bilibili_wbi.py`); QR-code login (`login/bilibili_qr.py`); parses `INTERACT_WORD_V2` (enter/follow), `ONLINE_RANK_COUNT` and `WATCHED_CHANGE` (viewer counts) itself. |
| **Ordered filter rules** | First match wins; `ALLOW` / `DROP` / `DISPLAY_ONLY` / `REWRITE`. |
| **Key & mouse mapping** | Exact / contains / prefix / regex / "sensing" (repeated characters → hold). Hold actions have a configurable duration. |
| **Game mode** | Emits hardware scan codes to bypass `SendInput` filtering. |
| **Global hotkey** | `Ctrl+Shift+P` pauses/resumes injection, works in exclusive-fullscreen games. |
| **Focus-window filter** | Inject only while a chosen window is foreground (title / process / class, exact or regex). Off by default. |
| **Injection diagnostics** | Startup self-check + `--check-env`: distinguishes "this process is restricted" from "the target window has higher integrity", and proves it with a **loopback injection test** (move the cursor 1 px, read it back). |
| **Simulation mode** | Exercises the whole pipeline without any live room. |
| **Anti key-storm** | Per-mapping cooldown, global cooldown, per-user cooldown, content de-duplication, and a per-message trigger cap. |
| **Single config file** | One atomic `settings.json`; corrupt files are backed up as `.bad` and replaced by defaults. |

Out of the box: **25 keyboard mappings** + **9 mouse mappings** (`w/s/a/d`, arrows, space,
enter, esc, number keys, `q/e/r/f`, left/right click, wheel …).

---

## Requirements

| Item | Requirement |
| --- | --- |
| OS | **Windows 10 / 11** (input injection is pure Win32) |
| Python | **3.12** (developed and verified on `3.12.10`) |
| Node.js | **Douyin only** — computes the WebSocket `signature`. Not needed for Bilibili or simulation. |
| Bilibili SESSDATA | Optional: guest mode still receives danmaku. |
| Douyin cookie | Optional: can connect without it, a real cookie improves the success rate. |

---

## Install (from source)

```bat
git clone <this-repo>
cd <repo>
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> ⚠️ `blivedm` pins `brotli==1.0.9`, which has **no wheel for Python 3.12** — pip will try
> to compile it. With Visual Studio C++ Build Tools installed this works; otherwise use
> Python 3.11 for the venv, or install `brotli==1.0.9` separately first.
> Do **not** upgrade brotli to 1.2.x: `blivedm` pins `==1.0.9` exactly.

## Run

```bat
启动.bat              :: silent GUI (pythonw, no console)
启动.bat --console    :: GUI with console output
启动.bat --selftest   :: offline self-test only (no GUI, no network)
```

or directly:

```bat
.venv\Scripts\python.exe src\screenkey\main.py
```

`main.py` bootstraps `src` on `sys.path` itself — no `PYTHONPATH` needed, any working
directory works.

## Prebuilt binaries

Grab the `screenkey.1.0-win64.zip` from the Releases page, extract it **whole**
and run `ScreenKey.exe` inside it. The build is PyInstaller *onedir*: the executable is
only ~6 MB, the bulk is the Qt runtime — copying the `.exe` alone will not run.

```bat
ScreenKey.exe --selftest    :: verify the bundle
ScreenKey.exe --check-env   :: verify this machine can deliver input
```

---

## CLI

```
screenkey [-h] [--version] [--debug] [--log-dir DIR] [--config-dir DIR]
           [--selftest] [--check-env] [--simulate]
           [--platform {douyin,bilibili}] [--room ROOM] [--no-hotkey]
```

Exit codes: `0` ok, `1` bad arguments, `2` missing dependency, `3` self-test failure.

## Configuration

| Content | Default |
| --- | --- |
| Config | `%USERPROFILE%\.screenkey\settings.json` (`--config-dir`, `SCREENKEY_CONFIG_DIR`) |
| Logs | `%USERPROFILE%\.screenkey\logs\` (`--log-dir`, `SCREENKEY_LOG_DIR`) |

Credentials (Bilibili SESSDATA, Douyin cookie) are stored **DPAPI-encrypted** on Windows
(`dpapi:` prefix) and fall back to plaintext where DPAPI is unavailable.

---

## Verification status (honest version)

| Capability | Evidence |
| --- | --- |
| Douyin live danmaku | Real captures: 8 messages from room `117606132773`; 29 messages in 45 s elsewhere. |
| Bilibili live danmaku | Real captures: room `34348`, 41 events per 30 s at both fetcher and engine level; `INTERACT_WORD_V2` / viewer-count stats confirmed on room `21414905`. |
| Filter → map → inject | 108 input-layer tests, offline and against the frozen exe; visible in simulation mode. |
| Bilibili QR login | State machine and QR rendering covered by 60 tests; a real scan is done by hand. |
| Bilibili nickname masking | Root-caused and fixed (2026-10-24): SESSDATA used to live in a **host-only cookie**, so the danmaku WebSocket host never received it and the server treated the client as a guest (`uid=0`, `伊***`). Now it travels in a session-level `Cookie` header. Verified: 26 danmaku in 40 s, 0 masked, 0 zero-uid. |
| Focus-window filter | 96 tests covering matching and fail-closed semantics. |
| Input injection diagnostics | Reproducibly reports "loopback injection: **ineffective**" inside restricted hosts — which is exactly the honest answer. |
| Douyin login | Cookie import only — the platform returns `error_code: 4031` to pure HTTP clients, so QR login is not implementable without a browser automation dependency. |

**1094 offline tests** (`pytest tests -q`, ~15 s, no network, no GUI, no real key events),
`pyflakes` clean, `--selftest` green. On a normal desktop 1058 pass and 7 skip; the
remaining 25 spawn child processes and fail inside restricted sandboxes.

---

## Known limitations

1. **Douyin needs Node.js** — the `signature` parameter cannot be reproduced in pure Python.
   Without it the connection is dropped with code `1006` in ~155 ms, and the program says so.
2. **Douyin rooms are often silent** — no messages ≠ broken connection.
3. **Bilibili masking when logged out** — the server masks nicknames (`伊***`) for guests;
   the app now carries the cookie to every host, so a valid login shows full names.
4. **Douyin has no QR login** — cookie import only (anti-bot).
5. **Windows only**, and **kernel-level anti-cheat ignores injected input** — using macros
   there may get you banned.
6. **Douyin field IDs drift** with platform revisions; parsing uses candidates, but a big
   revision can break a message type.
7. **`sign.js` is third-party obfuscated Douyin SDK code** — research use only, do not
   redistribute commercially.
8. **The bundle is large** (~102 MB, 222 files) because PyQt5 ships the whole Qt runtime.
9. **Exclusive-fullscreen games** need borderless/windowed mode for the "always on top"
   danmaku window to stay visible.
10. **In restricted hosts** (sandboxes, tokens below the target window, security software)
    Windows silently discards injected input. `--check-env` proves it with the loopback test.

---

## Development

```bat
.venv\Scripts\python.exe -m pytest tests -q      :: 1094 offline tests
.venv\Scripts\python.exe -m pyflakes src tools tests
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean main.spec
```

Layout: `src/screenkey/{danmaku,ui,config,input,login,utils}`, `tests/` (32 files),
`tools/build_helper.py` (works around PyQt5's non-ASCII-path bug by building through an
ASCII junction), `docs/` (Chinese design notes, acceptance flow, post-mortems).

---

## License & disclaimer

Source code: see [LICENSE](LICENSE).

- `src/screenkey/data/sign.js` is **third-party obfuscated Douyin SDK code**; its copyright
  belongs to Douyin/ByteDance. It is bundled for research purposes only — **no commercial
  use, no redistribution**.
- Automating input in online games may violate their terms and lead to bans. **Use at your
  own risk.**
- Respect each streaming platform's terms of service.
