# 幕键 · ScreenKey

简体中文 | [English](README.en.md)

> 观众在直播间发「左」，你的电脑就按一下左方向键。

把直播弹幕变成真实的键盘和鼠标操作。支持 **抖音** 和 **B站** 双平台，Windows 免安装开箱即用。

[![Release](https://img.shields.io/github/v/release/hgcrts/screenkey?label=%E4%B8%8B%E8%BD%BD&color=blue)](https://github.com/hgcrts/screenkey/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-lightgrey)

---

## 能做什么

- **弹幕驱动键鼠**：任意关键词映射到单个按键、组合键、鼠标点击或滚轮
- **八类事件可分别映射**：弹幕、礼物、进场、点赞、关注、粉丝团、醒目留言、上舰
- **防刷屏**：全局冷却、每个观众触发上限、可只响应屏幕上的指定消息
- **焦点窗口过滤**：只在指定游戏处于前台时注入，切出去就停
- **弹幕显示窗**：按消息类型筛选、显示昵称/时间、调字号、窗口置顶、一键导出
- **先试不真按**：模拟模式把「将会按什么键」写进日志，确认无误再开真实注入

## 下载

**不想装 Python**：直接下打包版，解压双击 `ScreenKey.exe`。

➡️ **[ScreenKey-v3.0.1-win64.zip](https://github.com/hgcrts/screenkey/releases/latest)**（约 76 MB，已内置 Node 运行时，SHA-256 见 Release 页）

**想改代码**：

```bat
git clone https://github.com/hgcrts/screenkey.git
cd screenkey
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m screenkey
```

## 三步开始

1. **选平台、填房间号**（B站支持扫码登录，抖音填 Cookie）
2. **绑定按键**：在「按键映射」里写关键词 → 对应键
3. **点「开始」**：弹幕一来就注入

> 往游戏里注入按键需要管理员权限；第一次跑建议先开「模拟模式」验证映射。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11，x64（注入依赖 `SendInput`，Linux/macOS 不支持） |
| Python | 3.11+（仅源码运行需要） |
| 抖音 | 无额外依赖（签名用的 JS 运行时已内置在发布包中） |
| B站 | 无额外依赖；登录状态下昵称完整显示 |

## 文档

| 想知道 | 看这里 |
| --- | --- |
| 现在到底能做什么、验证到什么程度 | [docs/项目现状.md](docs/项目现状.md) |
| 代码怎么分层、线程怎么跑 | [docs/架构.md](docs/架构.md) |
| 安装细节、CLI、配置项、测试怎么跑 | [docs/开发文档.md](docs/开发文档.md) |
| 出问题了 | [docs/开发文档.md 的常见问题](docs/开发文档.md#常见问题) |

## 名字变更

v3.0.0 起，项目从「弹幕键盘V2」更名为 **幕键 / ScreenKey**（同一个程序、同一批功能）。
旧版下载的文件名仍是 `DanmakuKeyboard-V2.1.0-win64.zip`，可以直接用新版覆盖解压；
配置文件会从旧的 `~/.danmaku_kb` 自动迁移到 `~/.screenkey`，登录凭证不用重新扫码。

## 许可证

[MIT](LICENSE)。`src/screenkey/data/sign.js` 是第三方抖音 SDK 代码，仅限研究学习，不适用 MIT（详见 LICENSE 里的第三方声明）。

本项目与抖音、B站官方无关，使用前请遵守平台规则，风险自负。
