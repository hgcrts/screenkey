# 弹幕键盘 V2.1.0 / Danmaku Keyboard V2.1.0

> 直播间弹幕 → 真实键鼠操作。抖音 + B站双平台，Windows 10 / 11 x64。
> Live-room comments → real keyboard & mouse actions. Douyin + Bilibili, Windows 10/11 x64.

---

## 中文

### 下载

- `DanmakuKeyboard-V2.1.0-win64.zip`（42.7 MiB）：解压后双击 `弹幕键盘V2/弹幕键盘V2.exe`，**免安装、免 Python**。
- 校验：`SHA256SUMS.txt` 里是对应 zip 的 SHA-256。

### 这个版本做了什么

1. **抖音直播间**：实测已连接（直播间 594976188049，30 秒收 140 条，含弹幕/进场/点赞/在线人数），签名走本地 Node 环境。
2. **B站直播间**：实测已连接（直播间 21414905，45 秒收 103 条）。修掉了三个卡了很久的问题：
   - 接口缺 WBI 签名导致全接口 `-352`；
   - `room_init` 参数用错（必须 `?id=`）；
   - Cookie 作用域写错，导致登录态根本没送到 `api.bilibili.com`，弹幕昵称被 B站按游客规则打码成 `伊***`。
3. **弹幕窗口显示设置**：按消息类型过滤（弹幕/礼物/进场/点赞/关注/粉丝团/SC/上舰/其他）、显示昵称/时间/平台、字号、行数、自动滚动、窗口置顶。
4. **凭据加密**：cookie / SESSDATA 落盘用 Windows DPAPI 加密（`dpapi:` 前缀），旧明文配置首次启动自动迁移。
5. **协议补齐**：B站 `INTERACT_WORD_V2`（进场/关注）、`WATCHED_CHANGE`（看过人数）、`ONLINE_RANK_COUNT`（在线人数）。
6. **工程化**：git 版本控制、`pyproject.toml`、`constraints.txt` 锁版本、pre-commit 钩子（pyflakes + 全量测试）、1058 项离线测试全过。

### 首次使用

1. 双击 `弹幕键盘V2.exe`；
2. 选平台、填房间号；
3. 抖音：本地需要 Node.js（`npm install` 后签名服务在 `tools/`）；B站：扫码登录后会自动保存加密 cookie；
4. 在「按键映射」里把弹幕关键词绑到按键，点「开始」。

### 注意

- 仅支持 Windows 10/11（需要真实的 SendInput 注入；Linux/macOS 不适用）。
- 需要管理员权限才能往游戏里注入按键。
- `src/danmaku_kb/data/sign.js` 是第三方抖音 SDK 代码，仅限研究学习，禁止商用（详见 LICENSE 里的第三方声明）。
- 本项目与抖音、B站官方无关，使用风险自负。

---

## English

### Download

- `DanmakuKeyboard-V2.1.0-win64.zip` (42.7 MiB): unzip and run `弹幕键盘V2/弹幕键盘V2.exe`. No installer, no Python needed.
- Verify with the SHA-256 in `SHA256SUMS.txt`.

### What's in this release

1. **Douyin (TikTok China) live rooms** — verified live (room 594976188049, 140 events in 30 s: comments, enters, likes, viewer counts). Request signing runs against a local Node.js runtime.
2. **Bilibili live rooms** — verified live (room 21414905, 103 events in 45 s). Three long-standing bugs fixed:
   - missing WBI signature made every API return `-352`;
   - `room_init` was called with the wrong parameter (`?id=` is required);
   - the auth cookie was scoped to the wrong host, so the login never reached `api.bilibili.com` and Bilibili masked every nickname as `伊***` for guests.
3. **Danmaku window display settings** — filter by event type (comment / gift / enter / like / follow / fan club / super chat / guard / other), toggle nickname, timestamp and platform, font size, line limit, auto-scroll, always-on-top.
4. **Encrypted credentials** — cookies and SESSDATA are stored with Windows DPAPI (`dpapi:` prefix); existing plaintext configs are migrated on first launch.
5. **Protocol coverage** — Bilibili `INTERACT_WORD_V2` (enter/follow), `WATCHED_CHANGE` (viewers), `ONLINE_RANK_COUNT` (online count).
6. **Engineering** — git history, `pyproject.toml`, pinned `constraints.txt`, pre-commit hook (pyflakes + full test suite), 1058 offline tests passing.

### Quick start

1. Run `弹幕键盘V2.exe`.
2. Pick a platform and enter a room ID.
3. Douyin needs Node.js locally (`npm install`, signing service under `tools/`); Bilibili needs a QR-code login, which stores an encrypted cookie automatically.
4. In "Key mapping", bind comment keywords to keys, then press Start.

### Notes

- Windows 10/11 only — real key/mouse injection uses `SendInput`; Linux/macOS are unsupported.
- Administrator privileges are required to inject input into games.
- `src/danmaku_kb/data/sign.js` is third-party Douyin SDK code, bundled for study only, not for commercial use (see the third-party notice in LICENSE).
- Not affiliated with Douyin or Bilibili. Use at your own risk.
