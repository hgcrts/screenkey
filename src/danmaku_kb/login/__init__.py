"""登录 / 凭证获取。

* :mod:`~danmaku_kb.login.bilibili_qr` —— B 站扫码登录（官方接口，真能用）。
* :mod:`~danmaku_kb.login.douyin` —— 抖音 Cookie 引导（抖音已无法纯 HTTP 扫码，
  见该模块 docstring 的说明）。
* :mod:`~danmaku_kb.login.qr_image` —— 二维码 PNG 渲染，缺 segno 时回落占位图。

包标记，不做包级再导出：使用方一律按子模块导入
（``from ..login.bilibili_qr import QRCodeSession``）。
"""
