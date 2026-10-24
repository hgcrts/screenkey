# v3.0.1 — 抖音签名运行时随包内置

## 修了什么

v3.0.0 的发布包里，抖音签名依赖的 Node.js 运行时**没有被打进去**：sign.js 从 Python 包里找得到，但启动它的 node.exe 是从开发者的 `E:\nodejs\node.exe` 直接拷贝进包的，打包脚本清理后就成了 `找不到 Node 运行时`。别人下载包、只跑抖音的话，会看到签名引擎起不来 → 连不上抖音。

这一版把 node.exe 当作正式依赖打进包里：

- `_internal/node/node.exe` 随分发，**用户不需要再单独安装 Node.js**
- 启动器优先用随包运行时，其次才是环境变量 `SCREENKEY_NODE`、最后是系统 PATH 里的 node
- 构建机首次打包时自动挑一个官方 node.exe 缓存到 `build/node-runtime/`，后续构建直接用缓存
- 新增 7 项测试（`tests/test_node_runtime.py`）覆盖 onedir / onefile / 环境变量 / 缺失四种布局；原有测试套件回归无变化

## 下载

`ScreenKey-v3.0.1-win64.zip`（约 76 MiB）——解压任意目录，双击 `ScreenKey.exe`。

**这次请删掉旧的解压目录**（旧包缺 node 运行时），或者直接解压覆盖后确认目录里有 `_internal\node\node.exe`。

## 验证

- `ScreenKey.exe --selftest` 全部通过
- 以冻结态加载打包产物，`find_node()` 解析到 `_internal\node\node.exe`，签名引擎真实产出签名（`md5 → 16 字符签名`）
- 全量测试：除 25 项需要真实子进程/junction 的用例在受限环境失败外全部通过

## 升级提示

- 配置目录仍是 `%USERPROFILE%\.screenkey`，从 v3.0.0 升级无需迁移
- 系统要求不变：Windows 10/11 x64，**不再需要 Node.js**
