# -*- coding: utf-8 -*-
"""配置持久化。

参考项目的 ``ConfigManager`` 管四个文件（``config.json`` / ``mappings.json`` /
``mouse_mappings.json`` / ``ui_state.json``），默认目录是 ``~/.danmaku_keyboard/``。
它踩过的坑：

1. 鼠标映射**完全没有持久化** —— 只有键盘映射被保存
2. ``max_danmaku_lines`` / 筛选设置 / 窗口几何每次启动静默回落到默认值
3. 保存用直接写文件，进程中途崩溃会留下半个 JSON，下次启动直接报错

这里的规定：

- 所有持久化状态放进**一个** ``settings.json``（原子写：临时文件 + ``os.replace``），
  避免"哪个设置存在哪个文件里"的认知负担
- 损坏的 JSON 自动备份为 ``settings.json.bad`` 并回落默认值，绝不阻止启动
- 支持从参考项目目录**一次性导入**旧的映射配置（仅在目标为空时）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..utils.logging_setup import get_logger
from ..utils.paths import probe_writable as _probe_writable
from . import secrets as _secrets
from .models import AppConfig

logger = get_logger("config.manager")

#: 环境变量：覆盖配置目录（测试与便携模式用）
ENV_CONFIG_DIR = "DANMAKU_KB_CONFIG_DIR"

#: 默认配置目录名
DEFAULT_DIR_NAME = ".danmaku_kb"

#: 配置文件名（唯一的持久化文件）
SETTINGS_FILE_NAME = "settings.json"

#: 参考项目的旧配置文件名，用于一次性导入
LEGACY_MAPPING_FILES = ("配置文件.json", "mappings.json")

#: 已经提醒过"回落目录"的目标（见 :func:`writable_config_dir`），避免刷日志
_fallback_warned: set = set()


def default_config_dir() -> Path:
    """返回配置目录（不创建）。

    优先 ``DANMAKU_KB_CONFIG_DIR``；其次用户主目录下的 ``~/.danmaku_kb``。

    用户目录问不出来时（``Path.home()`` 在缺 ``HOME`` / ``USERPROFILE``
    的环境里抛 ``RuntimeError``，**不是** ``OSError``）退回当前工作目录下的
    ``.danmaku_kb``：配置能存下来总比因为定位不到主目录而崩掉好，
    调用方（界面/引擎）本来就要处理"目录不可写"的分支。
    """
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override)
    try:
        return Path.home() / DEFAULT_DIR_NAME
    except RuntimeError:
        return Path.cwd() / DEFAULT_DIR_NAME


def portable_config_dir() -> Path:
    """程序旁边的配置目录（便携模式）。

    :func:`default_config_dir` 定位到的目录不可写时退到这里：程序自己
    所在的目录。打包后是 exe 同级目录，开发时是项目根目录——都是用户
    打开程序时就看得到、也一定写得进去的地方（否则程序根本跑不起来）。
    """
    if getattr(sys, "frozen", False):  # PyInstaller
        base = Path(sys.executable).resolve().parent
    else:
        # <项目根>/src/danmaku_kb/config/manager.py
        base = Path(__file__).resolve().parents[3]
    return base / DEFAULT_DIR_NAME


def adopt_settings(source_dir: Path, target_dir: Path) -> bool:
    """回落目录里还没有配置时，把原目录那份复制过来（含凭证）。

    为什么需要这一步：``writable_config_dir()`` 一旦回落到程序旁边的
    ``.danmaku_kb``，程序从此读的是**另一个** ``settings.json``。用户在主目录
    里保存过的 cookie / 映射全部"消失"——这不是没存，是读的不是同一份。
    所以换目录的同时把旧文件搬过去，至少凭证不会丢。

    只在目标**没有**配置文件时复制：目标已经有文件说明用户在新目录里用过，
    这边不能覆盖（那才是真的丢数据）。
    """
    source = Path(source_dir) / SETTINGS_FILE_NAME
    target = Path(target_dir) / SETTINGS_FILE_NAME
    if target.exists() or not source.is_file():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    except OSError as exc:
        logger.warning("迁移配置失败 %s -> %s: %s", source, target, exc)
        return False
    logger.info(
        "配置目录回落，已把 %s 复制到 %s（新目录原本没有配置，沿用旧目录的设置，含凭证）",
        source,
        target,
    )
    return True


def writable_config_dir() -> Path:
    """挑一个**真的写得进去**的配置目录。

    顺序：

    1. ``DANMAKU_KB_CONFIG_DIR`` —— 用户显式指定，照用不探测（可能不可写，
       那种情况下由调用方报错，不要偷偷换成别的地方）
    2. :func:`default_config_dir` —— 正常是 ``~/.danmaku_kb``
    3. :func:`portable_config_dir` —— 程序旁边的 ``.danmaku_kb``
    4. 临时目录下的 ``.danmaku_kb``

    为什么要探测：``~/.danmaku_kb`` 建不出来时（受限令牌 / 沙箱 / 安全
    软件拦截 / 只读漫游配置文件）旧实现会在**每一次** ``save()`` 时报一次
    "无法创建配置目录"，而用户的设置永远存不下来。换一个能写的目录至少
    让设置生效，并明确告诉用户换到哪了。
    """
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override)

    preferred = default_config_dir()
    if _probe_writable(preferred):
        return preferred

    candidates = (
        portable_config_dir(),
        Path(tempfile.gettempdir()) / DEFAULT_DIR_NAME,
    )
    for candidate in candidates:
        if candidate == preferred:
            continue
        if _probe_writable(candidate):
            # 换目录前先把旧配置搬过去：否则用户在主目录存过的凭证会在
            # 界面上"凭空消失"（他没删，只是程序读的是另一份文件）。
            adopt_settings(preferred, candidate)
            # 只提醒一次：``writable_config_dir()`` 每次找目录都要探测一遍，
            # 每次 construction 都刷一条同样的 warning 只会把日志淹掉。
            if candidate not in _fallback_warned:
                _fallback_warned.add(candidate)
                logger.warning(
                    "配置目录 %s 不可写（%s），已改用 %s",
                    preferred,
                    "受限环境或权限不足",
                    candidate,
                )
            return candidate

    logger.warning("配置目录 %s 不可写，也没有可用的替代目录；设置将无法保存", preferred)
    return preferred


class ConfigManager:
    """配置读写。

    :param config_dir: 配置目录；默认 :func:`writable_config_dir`
    :param auto_save: 是否在每次 ``save()`` 时立即落盘（默认是）
    """

    def __init__(self, config_dir: Optional[Path] = None, auto_save: bool = True) -> None:
        self.config_dir = Path(config_dir) if config_dir else writable_config_dir()
        self.auto_save = auto_save
        self.settings_path = self.config_dir / SETTINGS_FILE_NAME

        self.config: AppConfig = AppConfig()
        self.key_mappings: List[Any] = []
        self.mouse_mappings: List[Any] = []
        #: 筛选规则（有序列表，见 :class:`~danmaku_kb.filters.rules.FilterRule`）
        self.filters: List[Any] = []

    # ------------------------------------------------------------------
    # 目录
    # ------------------------------------------------------------------

    def ensure_dir(self) -> bool:
        """确保配置目录存在。"""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.error("无法创建配置目录 %s: %s", self.config_dir, exc)
            return False

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def load(self) -> bool:
        """从磁盘加载全部配置。

        返回是否成功读到文件（文件不存在不算失败，用默认值继续）。
        """
        from ..filters.rules import FilterEngine
        from ..input.mappings import (
            default_key_mappings,
            default_mouse_mappings,
            key_mappings_from_list,
            mouse_mappings_from_list,
        )

        self.key_mappings = default_key_mappings()
        self.mouse_mappings = default_mouse_mappings()
        self.filters = FilterEngine.default().rules
        self.config = AppConfig()

        if not self.settings_path.exists():
            logger.info("配置文件不存在，使用默认配置: %s", self.settings_path)
            self.adopt_credentials()
            return False

        data = self._read_json(self.settings_path)
        if not isinstance(data, Mapping):
            logger.warning("配置文件内容不是对象，使用默认配置")
            self.adopt_credentials()
            return False

        try:
            self.config = AppConfig.from_dict(data.get("config") or {})
        except Exception as exc:  # pragma: no cover - 防御性
            logger.error("配置节解析失败，使用默认值: %s", exc)
            self.config = AppConfig()

        # 映射：文件里为空列表时也认为是"用户清空了"，不回落到默认
        raw_keys = data.get("key_mappings")
        if isinstance(raw_keys, list):
            self.key_mappings = key_mappings_from_list(raw_keys)

        raw_mice = data.get("mouse_mappings")
        if isinstance(raw_mice, list):
            self.mouse_mappings = mouse_mappings_from_list(raw_mice)

        # 筛选规则：与映射一样，"文件里是空列表"视为用户清空了规则，
        # 只有**键缺失/类型不对**才回落到默认规则集。
        # （历史上这里完全没有 filters 这一节，用户编辑的规则重启即丢失。）
        if "filters" in data:
            raw_filters = data.get("filters")
            if isinstance(raw_filters, list):
                # 空列表 = 用户把规则全删了，应当尊重（``FilterEngine.from_list``
                # 对空列表回落到默认规则，那个契约是给"整份文件损坏"用的，
                # 不能套在"用户主动清空"上）。
                self.filters = [] if not raw_filters else FilterEngine.from_list(raw_filters).rules
            else:
                logger.warning("筛选规则节类型不对（%s），使用默认规则", type(raw_filters).__name__)

        self.adopt_credentials()
        self._decrypt_credentials()

        logger.info(
            "配置已加载: 键盘映射 %d 条, 鼠标映射 %d 条, 筛选规则 %d 条, 目录 %s",
            len(self.key_mappings),
            len(self.mouse_mappings),
            len(self.filters),
            self.config_dir,
        )
        return True

    # ------------------------------------------------------------------
    # 凭证跨目录找回
    # ------------------------------------------------------------------

    def candidate_config_dirs(self) -> List[Path]:
        """除自己以外、**可能**存着配置的目录（主目录 / 便携 / 临时）。"""
        dirs: List[Path] = []
        for factory in (default_config_dir, portable_config_dir):
            try:
                path = factory()
            except Exception:  # pragma: no cover - 防御性
                continue
            dirs.append(path)
        dirs.append(Path(tempfile.gettempdir()) / DEFAULT_DIR_NAME)

        result: List[Path] = []
        seen = {Path(self.config_dir)}
        for path in dirs:
            path = Path(path)
            if path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result

    @staticmethod
    def _read_credentials(path: Path) -> Optional[Tuple[str, str]]:
        """读另一份 settings.json 里的凭证；没有/读不出来返回 None。"""
        try:
            if not Path(path).is_file():
                return None
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, Mapping):
            return None
        connection = data.get("config") or {}
        if not isinstance(connection, Mapping):
            return None
        connection = connection.get("connection") or {}
        if not isinstance(connection, Mapping):
            return None
        cookie = _secrets.decrypt(str(connection.get("cookie") or ""))
        sessdata = _secrets.decrypt(str(connection.get("sessdata") or ""))
        if not cookie.strip() and not sessdata.strip():
            return None
        return cookie, sessdata

    def adopt_credentials(self) -> bool:
        """本目录里没有凭证时，从别的候选目录把凭证捡回来。

        为什么需要：用户目录不可写时程序会换成程序旁边的 ``.danmaku_kb``
        （见 :func:`writable_config_dir`），从此读的是**另一份** settings.json。
        用户在主目录里存过的 SESSDATA / Cookie 不会自动跟过来，界面上就成了
        「我填的 cookie 没了」——其实没丢，只是读的不是那份文件。

        只在本目录两条凭证**都为空**时补，且只补凭证：房间号、映射、筛选规则
        一概不动，不会覆盖用户在备用目录里的其他设置。
        """
        conn = self.config.connection
        if (conn.cookie or "").strip() or (conn.sessdata or "").strip():
            return False
        for folder in self.candidate_config_dirs():
            found = self._read_credentials(folder / SETTINGS_FILE_NAME)
            if not found:
                continue
            cookie, sessdata = found
            conn.cookie = cookie.strip() or conn.cookie
            conn.sessdata = sessdata.strip() or conn.sessdata
            logger.info(
                "当前配置目录 %s 里没有凭证，已从 %s 找回（抖音 Cookie %d 字符，B 站 SESSDATA %d 字符）",
                self.config_dir,
                folder / SETTINGS_FILE_NAME,
                len(conn.cookie or ""),
                len(conn.sessdata or ""),
            )
            return True
        return False

    def _read_json(self, path: Path) -> Any:
        """读 JSON；损坏时备份并返回 ``None``。"""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as exc:
            backup = path.with_suffix(path.suffix + ".bad")
            try:
                shutil.copy2(path, backup)
                logger.error("配置文件损坏（%s），已备份到 %s", exc, backup)
            except OSError:
                logger.error("配置文件损坏且备份失败: %s", exc)
            return None
        except OSError as exc:
            logger.error("配置文件读取失败: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def save(self) -> bool:
        """原子写全部配置。"""
        if not self.ensure_dir():
            return False

        from ..input.mappings import key_mappings_to_list, mouse_mappings_to_list

        payload = {
            "version": AppConfig.SCHEMA_VERSION,
            "config": self._encrypted_config_dict(),
            "key_mappings": key_mappings_to_list(self.key_mappings),
            "mouse_mappings": mouse_mappings_to_list(self.mouse_mappings),
            "filters": [rule.to_dict() for rule in self.filters],
        }
        return self._write_json(self.settings_path, payload)

    # ------------------------------------------------------------------
    # 凭据加密（Windows DPAPI，见 config/secrets.py）
    # ------------------------------------------------------------------

    def _decrypt_credentials(self) -> None:
        """把内存里的凭据从 ``dpapi:...`` 解回明文（就地改写）。"""
        conn = self.config.connection
        for field_name in ("cookie", "sessdata"):
            value = getattr(conn, field_name, "") or ""
            if not _secrets.is_encrypted(value):
                continue
            plain = _secrets.decrypt(value)
            setattr(conn, field_name, plain)
            if not plain:
                logger.warning(
                    "凭据 %s 解不开（换了机器/用户或文件损坏），按未登录处理，请重新扫码登录",
                    field_name,
                )

    def _encrypted_config_dict(self) -> Dict[str, Any]:
        """``config`` 节的落盘版本：两条凭据加密，其它字段原样。"""
        data = self.config.to_dict()
        connection = data.get("connection")
        if isinstance(connection, dict):
            for field_name in ("cookie", "sessdata"):
                value = connection.get(field_name) or ""
                connection[field_name] = _secrets.encrypt(str(value))
        return data

    def migrate_credentials(self) -> bool:
        """把磁盘上的明文凭据改写成密文。

        单独一个方法而不是塞进 :meth:`load`：``load()`` 可能在与用户无关的
        场景里被调用（测试、自检脚本），静默改写用户文件不合适。启动流程里
        显式调用一次即可（见 ``main.py``）。

        :return: 是否真的改写了文件
        """
        if not _secrets.dpapi_available():
            return False
        raw = self._read_json(self.settings_path)
        connection: Any = {}
        if isinstance(raw, Mapping):
            section = raw.get("config")
            if isinstance(section, Mapping):
                connection = section.get("connection") or {}
        if not isinstance(connection, Mapping):
            return False
        pending = [
            name
            for name in ("cookie", "sessdata")
            if str(connection.get(name) or "").strip()
            and not _secrets.is_encrypted(str(connection.get(name) or ""))
        ]
        if not pending:
            return False
        saved = self.save()
        if saved:
            logger.info("已把明文凭据（%s）加密为 DPAPI 密文", "、".join(pending))
        return saved

    def _write_json(self, path: Path, payload: Any) -> bool:
        """原子写：先写临时文件，再 ``os.replace``。

        直接 ``open(path, 'w')`` 会在写入中途崩溃时留下截断的 JSON；
        ``os.replace`` 在同一文件系统上是原子的。
        """
        try:
            self.ensure_dir()
            fd, tmp_name = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            except Exception:
                # 失败时清掉临时文件，避免目录里堆垃圾
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            return True
        except OSError as exc:
            logger.error("配置写入失败 %s: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # 旧配置导入
    # ------------------------------------------------------------------

    def import_legacy(self, legacy_dir: Path, overwrite: bool = False) -> Tuple[int, int]:
        """从参考项目目录导入旧映射。

        :param legacy_dir: 旧项目目录（会向下找 ``userdata/`` 与 ``src/``）
        :param overwrite: 为 False 时仅在当前映射为空的情况下导入
        :return: ``(导入的键盘映射数, 导入的鼠标映射数)``
        """
        from ..input.mappings import key_mappings_from_list, mouse_mappings_from_list

        if not overwrite and (self.key_mappings or self.mouse_mappings):
            logger.info("已有映射，跳过旧配置导入")
            return (0, 0)

        found = self._find_legacy_file(Path(legacy_dir))
        if found is None:
            logger.info("未找到旧配置文件于 %s", legacy_dir)
            return (0, 0)

        data = self._read_json(found)
        if data is None:
            return (0, 0)

        imported_keys = 0
        imported_mice = 0

        # 旧格式有三种，都要认：
        #   1. 裸 list：``[{...}, {...}]``
        #      —— 参考项目 ``userdata/配置文件.json`` 的**真实**格式，实测 12 条
        #   2. ``{"keyboard": [...], "mouse": [...]}``
        #   3. ``{"mappings": [...], "mouse_mappings": [...]}``
        # 早先这里只处理 2 和 3，并且用 ``if not isinstance(data, Mapping): return (0, 0)``
        # 把裸 list 直接拒掉 —— 于是"导入旧配置"对参考项目的真实文件**永远返回 (0, 0)**，
        # 用户看到的是"导入成功但一条都没进来"。
        if isinstance(data, list):
            raw_keys: Any = data
            raw_mice: Any = None
        elif isinstance(data, Mapping):
            raw_keys = data.get("keyboard") or data.get("mappings") or data.get("key_mappings")
            raw_mice = data.get("mouse") or data.get("mouse_mappings")
        else:
            logger.info("旧配置文件格式无法识别: %s (%s)", found, type(data).__name__)
            return (0, 0)

        if isinstance(raw_keys, list):
            mappings = key_mappings_from_list(raw_keys)
            if mappings:
                self.key_mappings = mappings
                imported_keys = len(mappings)

        if isinstance(raw_mice, list):
            mappings = mouse_mappings_from_list(raw_mice)
            if mappings:
                self.mouse_mappings = mappings
                imported_mice = len(mappings)

        logger.info("旧配置导入完成: 键盘 %d 条, 鼠标 %d 条 (来源 %s)", imported_keys, imported_mice, found)
        return (imported_keys, imported_mice)

    @staticmethod
    def _find_legacy_file(root: Path) -> Optional[Path]:
        """在旧项目目录里找配置文件。

        参考项目的路径是 ``<root>/userdata/配置文件.json``；
        也兼容直接放在根目录或 ``src/danmaku-keyboard/`` 下的情况。
        """
        if not root.exists():
            return None
        candidates: List[Path] = []
        for name in LEGACY_MAPPING_FILES:
            candidates.append(root / name)
            candidates.append(root / "userdata" / name)
            candidates.append(root / "src" / name)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    # ------------------------------------------------------------------
    # 便捷
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """恢复默认配置（不落盘）。"""
        from ..filters.rules import FilterEngine
        from ..input.mappings import default_key_mappings, default_mouse_mappings

        self.config = AppConfig()
        self.key_mappings = default_key_mappings()
        self.mouse_mappings = default_mouse_mappings()
        self.filters = FilterEngine.default().rules

    @property
    def exists(self) -> bool:
        """配置文件是否存在。"""
        return self.settings_path.exists()


__all__ = ["ConfigManager", "default_config_dir", "ENV_CONFIG_DIR"]
