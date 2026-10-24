# 幕键 v3.0.0 / ScreenKey v3.0.0

> 直播间弹幕 → 真实键鼠操作。抖音 + B站双平台，Windows 10 / 11 x64。

## 改名

项目从「弹幕键盘V2 / DanmakuKeyboard-V2」正式更名为 **幕键 / ScreenKey**。
功能和配置格式没有变，只是名字、图标和安装包文件名换了：

| | 旧 | 新 |
|---|---|---|
| 程序名 | 弹幕键盘V2 | **幕键 ScreenKey** |
| exe | `弹幕键盘V2.exe` | **`ScreenKey.exe`** |
| 配置目录 | `~/.danmaku_kb` | **`~/.screenkey`**（首次启动自动迁移） |
| 环境变量 | `DANMAKU_KB_*` | **`SCREENKEY_*`**（旧名仍兼容读取） |
| 仓库 | `danmaku-keyboard-v2` | **`screenkey`** |

旧的 `~/.danmaku_kb` 目录会保留、不会删；新目录拿到的是当时的一份拷贝，登录凭证不用重新扫码。

## 图标

- 全新的应用图标：深色底 + 键帽 + 弹幕气泡（16/24/32/48/64/128/256 多尺寸）
- exe 图标、任务栏图标、窗口图标统一使用它
- 生成器在 `tools/make_icon.py`，改配色/形状后重跑即可

## 同时带来的修复（v2.1.0 之后累计）

- **B站昵称显示为 `伊***`**：根因是 cookie 只发给了 `live.bilibili.com`（host-only），
  API 与弹幕服务器都拿不到登录态。现在 SESSDATA 同时投放到
  `live./api./passport./message./chat.bilibili.com`，并支持 SESSDATA 与完整 Cookie 串。
- **B站进场/关注/在线人数**：新增 `INTERACT_WORD_V2`、`WATCHED_CHANGE`、
  `ONLINE_RANK_COUNT` 解析（原先这些消息在日志里只有 `unknown cmd=`）。
- **凭据加密**：Windows 上用 DPAPI 加密保存 SESSDATA / Cookie，配置里不再是明文。
- **依赖可复现**：`constraints.txt` 锁定 39 个包的实测版本。
- **测试**：1058 passed / 7 skipped（另有 25 项子进程用例需要非受限桌面环境）。

## 安装

下载 `ScreenKey-v3.0.0-win64.zip`，解压后双击 `ScreenKey.exe`。免安装，不需要 Python。

SHA-256 见 Release 附件里的 `SHA256SUMS.txt`。

## 完整变更

见仓库 `docs/开发文档.md` 与 git 历史。
