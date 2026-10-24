# -*- coding: utf-8 -*-
"""本地凭据加密（Windows DPAPI）。

为什么要这一步：``settings.json`` 里存着抖音的 ``cookie`` 与 B 站的
``sessdata``，两者都**等价于账号密码**——拿到就能以用户身份登录网站。
原来它们是明文 JSON，等于「谁读到文件谁就拿到账号」：备份软件、同步网盘、
截图、把目录发给别人排错，任一条路径都会泄露。

这里用 Windows 自带的 DPAPI（``CryptProtectData`` / ``CryptUnprotectData``）
加密：密钥由 Windows 按**当前用户**派生并保管，不需要我们管理任何主密钥，
也不引入第三方依赖（``ctypes`` 直接调 ``crypt32``）。密文只对同一台机器上
的同一个用户可解——拷到别的机器/别的用户下解不开，这正是我们要的效果。

落盘格式：``"dpapi:" + base64(密文)``。没有前缀的按历史明文处理（读取时
原样返回），因此旧配置能无缝升级。非 Windows、或 DPAPI 调用失败时自动
退化为明文并记日志——加密失败不能把用户的登录状态弄丢。

.. note::

   DPAPI 默认使用**当前用户**范围（不传 ``CRYPTPROTECT_LOCAL_MACHINE``），
   所以同一账号在别的 Windows 用户下读不到。若用户以后希望「换机器继续用」，
   正确做法不是放宽加密，而是重新扫码登录。
"""

from __future__ import annotations

import base64
import ctypes
import os
from typing import Optional

from ..utils.logging_setup import get_logger

logger = get_logger("config.secrets")

#: 密文前缀。没有它的一律视为历史明文。
PREFIX = "dpapi:"

#: 附加熵（entropy）：让密文只对本程序有用，别的程序即便调用 DPAPI 也解不开
_ENTROPY = b"danmaku_kb.v2.credentials"

#: 只在 Windows 上尝试；其它平台直接退化
_IS_WINDOWS = os.name == "nt"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> "tuple[_DATA_BLOB, ctypes.Array]":
    """构造 DATA_BLOB，并**返回底层缓冲区**由调用方持有，防止被 GC 回收。"""
    size = len(data)
    buffer = ctypes.create_string_buffer(data, size)
    blob = _DATA_BLOB(size, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _crypt32() -> Optional[ctypes.WinDLL]:
    if not _IS_WINDOWS:  # pragma: no cover - 非 Windows 环境
        return None
    try:
        return ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    except (OSError, AttributeError):  # pragma: no cover - 极端环境
        logger.warning("crypt32 不可用，凭据将以明文保存")
        return None


def _protect_raw(plain: bytes) -> Optional[bytes]:
    """调用 CryptProtectData，失败返回 None。"""
    crypt32 = _crypt32()
    if crypt32 is None:
        return None
    try:
        data_in, _keep_in = _blob(plain)
        entropy_in, _keep_entropy = _blob(_ENTROPY)
        data_out = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(data_in),
            None,
            ctypes.byref(entropy_in),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
        if not ok:
            logger.warning("CryptProtectData 失败: %s", ctypes.get_last_error())
            return None
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(data_out.pbData)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("CryptProtectData 异常: %s", exc)
        return None


def _unprotect_raw(cipher: bytes) -> Optional[bytes]:
    """调用 CryptUnprotectData，失败返回 None。"""
    crypt32 = _crypt32()
    if crypt32 is None:
        return None
    try:
        data_in, _keep_in = _blob(cipher)
        entropy_in, _keep_entropy = _blob(_ENTROPY)
        data_out = _DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(data_in),
            None,
            ctypes.byref(entropy_in),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(data_out),
        )
        if not ok:
            logger.warning(
                "CryptUnprotectData 失败（密文不属于当前用户/机器，或已损坏）: %s",
                ctypes.get_last_error(),
            )
            return None
        try:
            return ctypes.string_at(data_out.pbData, data_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(data_out.pbData)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("CryptUnprotectData 异常: %s", exc)
        return None


def dpapi_available() -> bool:
    """DPAPI 是否真的可用（拿空串试加解密一次，不落盘）。"""
    if not _IS_WINDOWS:
        return False
    probe = b"probe"
    cipher = _protect_raw(probe)
    if cipher is None:
        return False
    return _unprotect_raw(cipher) == probe


def is_encrypted(value: str) -> bool:
    """是否是我们写出的 DPAPI 密文。"""
    return isinstance(value, str) and value.startswith(PREFIX) and len(value) > len(PREFIX)


def encrypt(plain: str) -> str:
    """把明文加密成 ``dpapi:...``；不可用或失败时原样返回明文。"""
    if not plain:
        return plain
    if is_encrypted(plain):  # 已是密文，避免二次加密
        return plain
    cipher = _protect_raw(plain.encode("utf-8"))
    if cipher is None:
        return plain
    return PREFIX + base64.b64encode(cipher).decode("ascii")


def decrypt(stored: str) -> str:
    """把 ``dpapi:...`` 解回明文；不是密文则原样返回。

    解不开时返回空串（而不是异常或密文）：调用方的语义是"当前没有凭证"，
    会走「请登录」分支，用户可以重新扫码；把密文当明文传给网络层只会
    产生更难懂的错误。
    """
    if not stored or not is_encrypted(stored):
        return stored or ""
    try:
        cipher = base64.b64decode(stored[len(PREFIX) :], validate=True)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        logger.warning("凭据密文 base64 损坏，按无凭证处理")
        return ""
    plain = _unprotect_raw(cipher)
    if plain is None:
        return ""
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:  # pragma: no cover - 防御性
        logger.warning("凭据解密结果不是 UTF-8，按无凭证处理")
        return ""


__all__ = [
    "PREFIX",
    "decrypt",
    "dpapi_available",
    "encrypt",
    "is_encrypted",
]
