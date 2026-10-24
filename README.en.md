# Danmaku Keyboard V2

[简体中文](README.md) | **English**

> A viewer types `left` in your live stream — your PC presses the left arrow key.

Turn live-stream comments into real keyboard and mouse input. Supports **Bilibili** and **Douyin (TikTok China)**, packaged for Windows with no installation.

[![Release](https://img.shields.io/github/v/release/hgcrts/danmaku-keyboard-v2?label=download&color=blue)](https://github.com/hgcrts/danmaku-keyboard-v2/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-lightgrey)

---

## What it does

- **Comments drive input**: map any keyword to a single key, a key combo, a mouse click or the scroll wheel
- **Eight event types, mapped separately**: comment, gift, enter, like, follow, fan club, super chat, guard
- **Anti-spam controls**: global cooldown, per-viewer limits, react only to messages shown on screen
- **Focus-window filter**: inject only while your game is the foreground window
- **Danmaku window**: filter by event type, toggle nickname/timestamp, font size, always-on-top, export
- **Dry run first**: simulation mode logs what would be pressed before you enable real injection

## Download

**No Python needed** — grab the packaged build, unzip, run `弹幕键盘V2.exe`.

➡️ **[DanmakuKeyboard-V2.1.0-win64.zip](https://github.com/hgcrts/danmaku-keyboard-v2/releases/latest)** (~42 MB, SHA-256 on the release page)

**From source**:

```bat
git clone https://github.com/hgcrts/danmaku-keyboard-v2.git
cd danmaku-keyboard-v2
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m danmaku_kb
```

## Three steps

1. **Pick a platform, enter a room ID** (Bilibili supports QR login; Douyin needs a cookie)
2. **Bind keys**: in "Key mapping", write keyword → key
3. **Press Start**: input is injected as comments arrive

> Injecting into games requires administrator privileges. Use simulation mode first to verify your mappings.

## Requirements

| Item | Requirement |
| --- | --- |
| OS | Windows 10 / 11, x64 (injection uses `SendInput`; Linux/macOS unsupported) |
| Python | 3.11+ (source runs only) |
| Douyin | Local Node.js required (the `signature` parameter cannot be reproduced in pure Python) |
| Bilibili | No extra dependency; nicknames show in full when logged in |

## Documentation

All long-form docs are in Chinese today:

| Question | Read |
| --- | --- |
| What works, with what evidence | [docs/项目现状.md](docs/项目现状.md) |
| Architecture and threading | [docs/架构.md](docs/架构.md) |
| Install details, CLI, config, tests | [docs/development-guide.en.md](docs/development-guide.en.md) |
| Troubleshooting | [FAQ in docs/开发文档.md](docs/开发文档.md#常见问题) |

## License

[MIT](LICENSE). `src/danmaku_kb/data/sign.js` is third-party Douyin SDK code bundled for study only and is **not** covered by the MIT license (see the third-party notice in LICENSE).

Not affiliated with Douyin or Bilibili. Follow each platform's terms; use at your own risk.
