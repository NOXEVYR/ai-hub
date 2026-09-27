"""Bounded, instance-authenticated application update manager.

The updater deliberately has a smaller trust surface than the model downloader:
one fixed HTTPS feed, a fixed release archive layout, and a standalone helper
which revalidates everything before writing into the program directory.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

FEED_URL = "https://raw.githubusercontent.com/NOXEVYR/ai-hub/feat/aihub-collaboration-2.7.0/updates/candidate.json"
PACKAGE_BASE = "https://raw.githubusercontent.com/NOXEVYR/ai-hub/"
CHANNEL = "candidate"
MAX_FEED_BYTES = 64 * 1024
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
NETWORK_TIMEOUT = 12
HELPER_WAIT_SECONDS = 180
PREPARE_GRACE_SECONDS = 90
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ID_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?-[0-9a-f]{16}$")
TXID_RE = re.compile(r"^[0-9a-f]{32}$")
FEED_SCHEMA = "ai-hub-update-v1"
TICKET_SCHEMA = "ai-hub-update-ticket-v1"
LOCK_SCHEMA = "ai-hub-update-lock-v1"
TX_SCHEMA = "ai-hub-update-transaction-v1"

# Independent copy of the release scope. Keep aligned with package_release.py.
ROOT_FILES = {"server.py", "launcher.pyw", "start.vbs", "start.bat", "debug.bat",
              ".gitignore", "THIRD_PARTY_NOTICES.md", "README.md", "AGENTS.md"}
SUBDIR_EXTS = {"aihub": {".py"}, "frontend": {".js", ".html", ".css", ".svg", ".ico", ".png"},
               "desktop": {".py", ".cs", ".manifest", ".txt", ".md"},
               "tests": {".py", ".js"}, "tools": {".py"}, "docs": {".md"}}
EXCLUDED_PARTS = {"data", "backups", "vendor", "runtime", "__pycache__", "_tmp", ".git"}
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


class UpdateBusyError(RuntimeError):
    """An updater operation conflicts with another operation or live work."""


class UpdateSecurityError(ValueError):
    """Remote or local update inputs violate the fixed update contract."""


class UpdateNotNewerError(UpdateSecurityError):
    """Feed version is not newer than the installed version; never installable."""


def _safe_version(value):
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise UpdateSecurityError("版本号格式无效。")
    match = VERSION_RE.fullmatch(value)
    if match.group(4) and any(part.isdigit() and len(part) > 1 and part.startswith("0")
                              for part in match.group(4).split(".")):
        raise UpdateSecurityError("预发布版本的数字标识不能包含前导零。")
    return value


def _version_key(value):
    _safe_version(value)
    match = VERSION_RE.fullmatch(value)
    nums = tuple(int(match.group(i)) for i in (1, 2, 3))
    pre = match.group(4)
    if pre is None:
        return nums, (1, ())
    items = []
    for part in pre.split("."):
        items.append((0, int(part)) if part.isdigit() else (1, part))
    return nums, (0, tuple(items))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within(path, parent):
    try:
        return os.path.commonpath([_canonical(path), _canonical(parent)]) == _canonical(parent)
    except (OSError, ValueError):
        return False


def _is_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _check_tree(path, allow_missing=False):
    """Reject links and non-directory ancestors before resolving the path."""
    path = Path(os.path.abspath(path))
    chain = list(reversed((path, *path.parents)))
    for item in chain:
        if not os.path.lexists(item):
            if allow_missing:
                continue
            raise UpdateSecurityError("更新目录不存在。")
        info = os.lstat(item)
        if _is_reparse(info):
            raise UpdateSecurityError("更新目录不能经过链接或重解析点。")
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise UpdateSecurityError("更新目录路径被普通文件占用。")
        if item == path and not (stat.S_ISDIR(info.st_mode) or (allow_missing and not os.path.lexists(item))):
            raise UpdateSecurityError("更新目录不是普通目录。")


def _private_file(path):
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    import ctypes
    from ctypes import wintypes
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    if not convert("D:P(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _atomic_json(path, value):
    path = Path(path)
    _check_tree(path.parent, allow_missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        info = os.lstat(path)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateSecurityError("更新状态文件不能是链接或特殊文件。")
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temp, "xb") as stream:
            _private_file(temp)
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _fsync_dir(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path, max_bytes=1024 * 1024):
    path = Path(path)
    _check_tree(path.parent)
    info = os.lstat(path)
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
        raise UpdateSecurityError("更新状态文件无效。")
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise UpdateSecurityError("更新包路径无效。")
    if value.startswith("/") or value.startswith("//"):
        raise UpdateSecurityError("更新包路径不能是绝对路径。")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise UpdateSecurityError("更新包路径越界。")
    for part in parts:
        if part[-1:] in (" ", ".") or any(ord(ch) < 32 or ch in '<>"|?*' for ch in part):
            raise UpdateSecurityError("更新包路径包含 Windows 不支持的名称。")
        if part.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
            raise UpdateSecurityError("更新包包含 Windows 保留名称。")
    if value in ROOT_FILES or value == "manifest.json":
        return value
    if value == "AI Hub.exe":
        return value
    path = PurePosixPath(value)
    if len(path.parts) < 2 or path.parts[0] not in SUBDIR_EXTS:
        raise UpdateSecurityError("更新包超出程序文件白名单。")
    if EXCLUDED_PARTS.intersection(part.casefold() for part in path.parts):
        raise UpdateSecurityError("更新包包含受保护的数据或运行目录。")
    if path.suffix.lower() not in SUBDIR_EXTS[path.parts[0]]:
        raise UpdateSecurityError("更新包文件类型超出程序白名单。")
    return value


def _allowed_current_file(relative):
    try:
        _safe_relative(relative)
        return True
    except UpdateSecurityError:
        return False


def _validate_feed(raw, current_version):
    if len(raw) > MAX_FEED_BYTES:
        raise UpdateSecurityError("更新信息超过大小限制。")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise UpdateSecurityError("更新信息格式无效。") from exc
    if not isinstance(doc, dict) or set(doc) != {"schema", "app", "channel", "version", "commit", "package", "notes", "min_updater_version"}:
        raise UpdateSecurityError("更新信息字段无效。")
    if doc["schema"] != FEED_SCHEMA or doc["app"] != "ai-hub" or doc["channel"] != CHANNEL:
        raise UpdateSecurityError("更新来源或频道无效。")
    version = _safe_version(doc["version"])
    if VERSION_RE.fullmatch(version).group(5) is not None:
        raise UpdateSecurityError("更新发布版本暂不支持构建元数据。")
    min_version = _safe_version(doc["min_updater_version"])
    _safe_version(current_version)
    if _version_key(current_version) < _version_key(min_version):
        raise UpdateSecurityError("当前更新组件过旧，需先安装新版。")
    if _version_key(version) <= _version_key(current_version):
        raise UpdateNotNewerError("候选版本不高于当前版本。")
    if not isinstance(doc["commit"], str) or not COMMIT_RE.fullmatch(doc["commit"]):
        raise UpdateSecurityError("候选版本提交标识无效。")
    package = doc["package"]
    if not isinstance(package, dict) or set(package) != {"bytes", "sha256"}:
        raise UpdateSecurityError("更新包信息无效。")
    if type(package["bytes"]) is not int or package["bytes"] <= 0:
        raise UpdateSecurityError("更新包大小无效。")
    if not isinstance(package["sha256"], str) or not SHA_RE.fullmatch(package["sha256"]):
        raise UpdateSecurityError("更新包摘要无效。")
    if not isinstance(doc["notes"], str) or len(doc["notes"]) > 8192 or any(ord(c) < 32 and c not in "\r\n\t" for c in doc["notes"]):
        raise UpdateSecurityError("更新说明无效。")
    release_id = version + "-" + package["sha256"][:16]
    doc = dict(doc)
    doc["release_id"] = release_id
    doc["package_url"] = PACKAGE_BASE + doc["commit"] + "/releases/AI-Hub-v" + version + "-Windows-x64.zip"
    return doc


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if old.scheme != "https" or new.scheme != "https" or (new.hostname or "").casefold() != (old.hostname or "").casefold():
            raise UpdateSecurityError("更新服务器跳转到不受信任的主机。")
        if new.username or new.password:
            raise UpdateSecurityError("更新服务器跳转地址无效。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _network_fetch(url, maximum, stop_event=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or parsed.username or parsed.password:
        raise UpdateSecurityError("更新地址超出固定 HTTPS 来源。")
    opener = urllib.request.build_opener(_SameHostRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Yaohe-Updater/2.12", "Accept-Encoding": "identity"})
    deadline = time.monotonic() + (45 if maximum <= MAX_FEED_BYTES else 180)
    with opener.open(request, timeout=min(NETWORK_TIMEOUT, 4)) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != parsed.hostname:
            raise UpdateSecurityError("更新服务器响应来源无效。")
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                if int(length) > maximum:
                    raise UpdateSecurityError("更新文件超过 50 MiB 限制。")
            except ValueError as exc:
                raise UpdateSecurityError("更新服务器长度信息无效。") from exc
        chunks, total = [], 0
        while True:
            if stop_event is not None and stop_event.is_set():
                raise TimeoutError("update request cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("update request deadline exceeded")
            chunk = response.read(min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise UpdateSecurityError("更新文件超过大小限制。")
            chunks.append(chunk)
        return b"".join(chunks)


def _fetch(fetcher, url, maximum, stop_event=None):
    if fetcher is None:
        return _network_fetch(url, maximum, stop_event)
    # Test-only injection is intentionally a tiny byte-returning interface.
    data = fetcher(url, maximum)
    if not isinstance(data, (bytes, bytearray)) or len(data) > maximum:
        raise UpdateSecurityError("测试下载器返回了无效数据。")
    return bytes(data)


def _zip_manifest(blob, expected_version=None):
    if not isinstance(blob, bytes) or not blob or len(blob) > MAX_PACKAGE_BYTES:
        raise UpdateSecurityError("更新包大小无效或超过 50 MiB。")
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(blob), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise UpdateSecurityError("更新包不是有效 ZIP 文件。") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_FILES:
            raise UpdateSecurityError("更新包文件数量无效。")
        all_names, content_infos, expanded = set(), {}, 0
        for info in infos:
            raw_name = info.filename
            if not isinstance(raw_name, str) or not raw_name.startswith("AI-Hub/") or info.is_dir():
                raise UpdateSecurityError("更新包目录结构无效。")
            name = raw_name[len("AI-Hub/"):]
            if name == "manifest.json":
                relative = name
            else:
                relative = _safe_relative(name)
            key = relative.casefold()
            if key in all_names:
                raise UpdateSecurityError("更新包存在大小写冲突或重复文件。")
            all_names.add(key)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG):
                raise UpdateSecurityError("更新包不能包含链接或特殊文件。")
            if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UpdateSecurityError("更新包压缩方式无效。")
            if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
                raise UpdateSecurityError("更新包单文件超过大小限制。")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise UpdateSecurityError("更新包展开后超过大小限制。")
            if info.file_size > 1024 * 1024 and info.compress_size == 0 or (info.compress_size and info.file_size > info.compress_size * 1000):
                raise UpdateSecurityError("更新包压缩比异常。")
            content_infos[relative] = info
        if "manifest.json" not in content_infos:
            raise UpdateSecurityError("更新包缺少文件清单。")
        try:
            raw_manifest = archive.read(content_infos["manifest.json"])
            if len(raw_manifest) > 2 * 1024 * 1024:
                raise UpdateSecurityError("更新包清单过大。")
            manifest = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
            raise UpdateSecurityError("更新包清单无效。") from exc
        if not isinstance(manifest, dict) or set(manifest) != {"version", "kind", "user_data_included", "files"}:
            raise UpdateSecurityError("更新包清单字段无效。")
        version = _safe_version(manifest["version"])
        if expected_version is not None and version != expected_version:
            raise UpdateSecurityError("更新包版本与候选信息不一致。")
        if manifest["kind"] != "Windows-x64" or manifest["user_data_included"] is not False:
            raise UpdateSecurityError("更新包平台或数据边界无效。")
        rows = manifest["files"]
        if not isinstance(rows, list) or not rows or len(rows) > MAX_ARCHIVE_FILES:
            raise UpdateSecurityError("更新包文件清单无效。")
        listed, contents = {}, {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                raise UpdateSecurityError("更新包清单项目无效。")
            path = _safe_relative(row["path"])
            key = path.casefold()
            if key in listed or path == "AGENTS.md":
                # AGENTS.md is generated by release packaging but local policy owns it.
                if path != "AGENTS.md" or key in listed:
                    raise UpdateSecurityError("更新包清单包含冲突或不可覆盖的路径。")
            if type(row["bytes"]) is not int or row["bytes"] < 0 or row["bytes"] > MAX_FILE_BYTES:
                raise UpdateSecurityError("更新包清单大小无效。")
            if not isinstance(row["sha256"], str) or not SHA_RE.fullmatch(row["sha256"]):
                raise UpdateSecurityError("更新包清单摘要无效。")
            info = content_infos.get(path)
            if info is None or info.file_size != row["bytes"]:
                raise UpdateSecurityError("更新包文件与清单不一致。")
            try:
                data = archive.read(info)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                raise UpdateSecurityError("更新包文件校验失败。") from exc
            if len(data) != row["bytes"] or _sha(data) != row["sha256"]:
                raise UpdateSecurityError("更新包文件摘要校验失败。")
            listed[path] = row
            contents[path] = data
        if set(content_infos) != set(listed) | {"manifest.json"}:
            raise UpdateSecurityError("更新包包含清单外文件。")
        if "AI Hub.exe" not in listed:
            raise UpdateSecurityError("Windows 更新包缺少桌面程序。")
        return manifest, raw_manifest, contents


def _hash_regular(path):
    info = os.lstat(path)
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise UpdateSecurityError("程序文件不能是链接或特殊文件。")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _program_files(root):
    root = Path(root)
    found = {}
    for name in ROOT_FILES:
        path = root / name
        if os.path.lexists(path):
            found[name] = _hash_regular(path)
    exe = root / "AI Hub.exe"
    if os.path.lexists(exe):
        found["AI Hub.exe"] = _hash_regular(exe)
    for folder, extensions in SUBDIR_EXTS.items():
        base = root / folder
        if not os.path.lexists(base):
            continue
        if _is_reparse(os.lstat(base)) or not base.is_dir():
            raise UpdateSecurityError("程序目录不能是链接或特殊文件。")
        for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
            current_path = Path(current)
            kept = []
            for dirname in dirs:
                child = current_path / dirname
                if dirname.casefold() in EXCLUDED_PARTS:
                    continue
                st = os.lstat(child)
                if _is_reparse(st) or not stat.S_ISDIR(st.st_mode):
                    raise UpdateSecurityError("程序目录含有重解析点或非目录项。")
                kept.append(dirname)
            dirs[:] = kept
            for filename in files:
                file_path = current_path / filename
                rel = file_path.relative_to(root).as_posix()
                if file_path.suffix.lower() not in extensions or EXCLUDED_PARTS.intersection(part.casefold() for part in PurePosixPath(rel).parts):
                    continue
                found[rel] = _hash_regular(file_path)
    return found


def _verify_installed_manifest(root, current_files, current_version):
    """Require the bootstrap-installed file ledger to match the live program."""
    path = Path(root) / "manifest.json"
    try:
        installed = _read_json(path, 4 * 1024 * 1024)
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateBusyError("安装目录缺少有效的程序文件清单，不能安全覆盖。") from exc
    if not isinstance(installed, dict) or not {"version", "desktop_shell_version", "files"}.issubset(installed):
        raise UpdateBusyError("安装程序文件清单格式不兼容，不能安全覆盖。")
    try:
        _safe_version(installed["version"])
        _safe_version(installed["desktop_shell_version"])
    except UpdateSecurityError as exc:
        raise UpdateBusyError("安装程序文件清单版本无效，不能安全覆盖。") from exc
    if installed["version"] != current_version:
        raise UpdateBusyError("程序版本与安装文件清单不一致，请先修复安装。")
    entries = installed.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ARCHIVE_FILES:
        raise UpdateBusyError("安装程序文件清单无效，不能安全覆盖。")
    recorded = {}
    for item in entries:
        if not isinstance(item, dict) or not {"path", "bytes", "sha256"}.issubset(item):
            raise UpdateBusyError("安装程序文件清单条目无效。")
        try:
            rel = _safe_relative(item["path"])
        except UpdateSecurityError as exc:
            raise UpdateBusyError("安装程序文件清单超出程序白名单。") from exc
        if rel == "manifest.json" or rel.casefold() in recorded:
            raise UpdateBusyError("安装程序文件清单存在重复或递归路径。")
        if type(item["bytes"]) is not int or item["bytes"] < 0 or not isinstance(item["sha256"], str) or not SHA_RE.fullmatch(item["sha256"]):
            raise UpdateBusyError("安装程序文件清单摘要无效。")
        recorded[rel.casefold()] = {"path": rel, "bytes": item["bytes"], "sha256": item["sha256"]}
    actual_names = {name.casefold() for name in current_files if name != "AGENTS.md"}
    recorded_names = {key for key, row in recorded.items() if row["path"] != "AGENTS.md"}
    if actual_names != recorded_names:
        raise UpdateBusyError("安装目录的程序文件与安装清单不匹配，已停止更新。")
    for name, digest in current_files.items():
        if name == "AGENTS.md":
            continue
        row = recorded.get(name.casefold())
        file_path = Path(root).joinpath(*name.split("/"))
        if row is None or row["path"] != name or row["sha256"] != digest or row["bytes"] != file_path.stat().st_size:
            raise UpdateBusyError("本机程序文件与安装清单摘要不匹配，已停止更新。")
    return installed


def _installed_manifest_bytes(release, package_files, existing_manifest, root):
    rows = []
    for name, data in sorted(package_files.items()):
        if name == "AGENTS.md":
            preserved = Path(root) / "AGENTS.md"
            if not preserved.exists():
                continue
            data = preserved.read_bytes()
        rows.append({"path": name, "bytes": len(data), "sha256": _sha(data)})
    payload = {"version": release["version"], "desktop_shell_version": release["version"],
               "display_name": existing_manifest.get("display_name") or "曜核",
               "source_commit": release["commit"],
               "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "files": rows}
    return payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _process_identity(pid):
    """Return process creation time and image path for a live local PID."""
    if type(pid) is not int or pid <= 0:
        raise UpdateSecurityError("桌面进程标识无效。")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                           ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME))
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                       ctypes.POINTER(wintypes.DWORD))
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            raise UpdateSecurityError("无法验证桌面进程身份。")
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise UpdateSecurityError("无法读取桌面进程创建时间。")
            start = str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                raise UpdateSecurityError("无法读取桌面程序路径。")
            return {"pid": pid, "start_filetime": start, "executable_path": os.path.realpath(buf.value)}
        finally:
            kernel.CloseHandle(handle)
    try:
        proc = Path(f"/proc/{pid}")
        raw_stat = (proc / "stat").read_text()
        fields = raw_stat[raw_stat.rfind(")") + 2:].split()
        image = os.path.realpath(proc / "exe")
        return {"pid": pid, "start_filetime": str(fields[19]), "executable_path": image}
    except (OSError, IndexError, ValueError) as exc:
        if pid == os.getpid():
            return {"pid": pid, "start_filetime": "0", "executable_path": os.path.realpath(os.sys.executable)}
        raise UpdateSecurityError("无法验证桌面进程身份。") from exc


def _same_path(left, right):
    return os.path.normcase(os.path.realpath(left)).casefold() == os.path.normcase(os.path.realpath(right)).casefold()


def _lock_file(path, blocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        info = os.lstat(path)
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UpdateSecurityError("更新事务锁无效。")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stream = os.fdopen(fd, "r+b", buffering=0)
    try:
        if os.fstat(fd).st_nlink != 1:
            raise UpdateSecurityError("更新事务锁不能是硬链接。")
        if os.fstat(fd).st_size == 0:
            stream.write(b"0")
            os.fsync(fd)
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except (OSError, BlockingIOError):
            stream.close()
            return None
        return stream
    except BaseException:
        with contextlib.suppress(Exception):
            stream.close()
        raise


def _unlock_file(stream):
    if stream is None:
        return
    with contextlib.suppress(Exception):
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()


def _process_is_same(identity):
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int or not isinstance(identity.get("start_filetime"), str):
        return False
    try:
        current = _process_identity(identity["pid"])
        return current["start_filetime"] == identity["start_filetime"]
    except (OSError, ValueError, UpdateSecurityError):
        return False


def _write_tx(txdir, state, **extra):
    path = Path(txdir) / "transaction.json"
    value = {"schema": TX_SCHEMA, "transaction_id": Path(txdir).name, "state": state,
             "updated_at": int(time.time()), **extra}
    _atomic_json(path, value)
    return value


def _transaction_dir(root, transaction_id):
    if not isinstance(transaction_id, str) or not TXID_RE.fullmatch(transaction_id):
        raise UpdateSecurityError("更新事务标识无效。")
    data = Path(root).resolve() / "data" / "app-updates"
    path = data / "transactions" / transaction_id
    if not _within(path, data / "transactions") or path.parent != data / "transactions":
        raise UpdateSecurityError("更新事务路径无效。")
    _check_tree(path.parent)
    _check_tree(path)
    return path


def _identity_valid(identity, root=None):
    if not isinstance(identity, dict) or set(identity) != {"pid", "start_filetime", "executable_path"}:
        return False
    if type(identity["pid"]) is not int or identity["pid"] <= 0 or not isinstance(identity["start_filetime"], str) or not identity["start_filetime"].isdigit():
        return False
    if not isinstance(identity["executable_path"], str) or not os.path.isabs(identity["executable_path"]):
        return False
    if root is not None and not _same_path(identity["executable_path"], Path(root) / "AI Hub.exe"):
        return False
    actual = _process_identity(identity["pid"])
    if actual["start_filetime"] != identity["start_filetime"]:
        return False
    if not _same_path(actual["executable_path"], identity["executable_path"]):
        return False
    return True


def _record_result(root, result):
    path = Path(root) / "data" / "app-updates" / "result.json"
    _atomic_json(path, result)


def _recover_transaction(root, txdir):
    """Roll back only files still equal to this transaction's payload."""
    txdir = Path(txdir)
    journal_path = txdir / "journal.json"
    try:
        journal = _read_json(journal_path, 8 * 1024 * 1024)
    except FileNotFoundError:
        _write_tx(txdir, "recovered_no_write")
        return {"state": "recovered_no_write", "restored": 0, "retained": 0}
    entries = journal.get("files") if isinstance(journal, dict) else None
    if journal.get("schema") != "ai-hub-update-journal-v1" or not isinstance(entries, list) or len(entries) > MAX_ARCHIVE_FILES:
        _write_tx(txdir, "recovery_required", error_code="journal_invalid")
        raise RuntimeError("软件更新中断，恢复记录无效；请保留该事务目录并联系支持。")
    restored = retained = 0
    root = Path(root)
    for item in reversed(entries):
        try:
            rel = _safe_relative(item["path"])
            target = root.joinpath(*rel.split("/"))
            if not _within(target, root):
                raise UpdateSecurityError("恢复路径越界。")
            if item.get("state") not in ("applying", "applied", "restoring", "restore_failed"):
                continue
            expected_new = item.get("new_sha256")
            if os.path.lexists(target):
                actual = _hash_regular(target)
                if item.get("baseline_sha256") is not None and actual == item.get("baseline_sha256"):
                    item["state"] = "rolled_back"
                    journal["files"] = entries
                    _atomic_json(journal_path, journal)
                    restored += 1
                    continue
                if item.get("baseline_sha256") is None and actual != expected_new:
                    retained += 1
                    item["state"] = "retained_external_change"
                    journal["files"] = entries
                    _atomic_json(journal_path, journal)
                    continue
                if actual != expected_new:
                    retained += 1
                    item["state"] = "retained_external_change"
                    journal["files"] = entries
                    _atomic_json(journal_path, journal)
                    continue
            backup = txdir / "backups" / (item["backup_name"] or "")
            if item.get("baseline_sha256") is None:
                if os.path.lexists(target) and _hash_regular(target) == expected_new:
                    target.unlink()
                    _fsync_dir(target.parent)
                item["state"] = "rolled_back"
                restored += 1
            else:
                if not _within(backup, txdir / "backups") or not backup.is_file() or _hash_regular(backup) != item.get("backup_sha256") or item.get("backup_sha256") != item.get("baseline_sha256"):
                    raise UpdateSecurityError("事务备份校验失败。")
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name("." + target.name + ".rollback-" + uuid.uuid4().hex)
                with open(backup, "rb") as src, open(temp, "xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                if _hash_regular(temp) != item["baseline_sha256"]:
                    temp.unlink()
                    raise UpdateSecurityError("事务备份写入校验失败。")
                os.replace(temp, target)
                _fsync_dir(target.parent)
                item["state"] = "rolled_back"
                restored += 1
            _atomic_json(journal_path, journal)
        except (KeyError, OSError, ValueError, TypeError, UpdateSecurityError):
            retained += 1
            item["state"] = "recovery_error"
            _atomic_json(journal_path, journal)
    state = "recovered_rollback" if not retained else "recovery_partial"
    _write_tx(txdir, state, restored_files=restored, retained_files=retained)
    return {"state": state, "restored": restored, "retained": retained}


def startup_guard(root):
    """Block startup during a live update and recover only a verified dead helper."""
    root = Path(root).resolve()
    updates = root / "data" / "app-updates"
    marker = updates / "install-lock.json"
    if not os.path.lexists(marker):
        return {"blocked": False, "recovered": False}
    lock = _lock_file(updates / "transaction.lock", blocking=False)
    if lock is None:
        raise UpdateBusyError("曜核正在安装软件更新，请稍后重试。")
    try:
        try:
            value = _read_json(marker, 32 * 1024)
        except (FileNotFoundError, ValueError, OSError) as exc:
            raise RuntimeError("软件更新事务标记无法验证，已阻止启动以保护程序文件。") from exc
        if not isinstance(value, dict) or value.get("schema") != LOCK_SCHEMA or not TXID_RE.fullmatch(str(value.get("transaction_id", ""))):
            raise RuntimeError("软件更新事务标记无效，已阻止启动以保护程序文件。")
        txid = value["transaction_id"]
        txdir = _transaction_dir(root, txid)
        tx = _read_json(txdir / "transaction.json", 128 * 1024)
        if tx.get("transaction_id") != txid or tx.get("schema") != TX_SCHEMA:
            raise RuntimeError("软件更新事务状态无效，已阻止启动。")
        if tx.get("state") in {"succeeded", "failed", "cancelled", "recovered_no_write", "recovered_rollback"}:
            marker.unlink()
            _fsync_dir(updates)
            return {"blocked": False, "recovered": tx.get("state", "").startswith("recovered")}
        service_alive = _process_is_same(tx.get("service_identity"))
        helper_alive = _process_is_same(tx.get("helper_identity"))
        if service_alive or helper_alive:
            raise UpdateBusyError("曜核正在安装软件更新，请稍后重试。")
        age = max(0, time.time() - float(value.get("created_at", time.time())))
        if tx.get("state") in {"prepared", "helper_starting"} and age < PREPARE_GRACE_SECONDS:
            raise UpdateBusyError("曜核正在准备软件更新，请稍后重试。")
        if tx.get("state") in {"applying", "recovery_required", "recovery_partial"}:
            recovered = _recover_transaction(root, txdir)
            if recovered["state"] != "recovered_rollback":
                _write_tx(txdir, "recovery_required", retained_files=recovered["retained"])
                raise RuntimeError("软件更新已中断且需要人工处理，已阻止启动以保护程序文件。")
            _record_result(root, {"schema": "ai-hub-update-result-v1", "transaction_id": txid,
                                  "state": recovered["state"], "restored_files": recovered["restored"],
                                  "retained_files": recovered["retained"], "restart": False,
                                  "updated_at": int(time.time())})
        else:
            _write_tx(txdir, "recovered_no_write")
            _record_result(root, {"schema": "ai-hub-update-result-v1", "transaction_id": txid,
                                  "state": "recovered_no_write", "restored_files": 0,
                                  "retained_files": 0, "restart": False, "updated_at": int(time.time())})
        marker.unlink()
        _fsync_dir(updates)
        return {"blocked": False, "recovered": True}
    finally:
        _unlock_file(lock)


class UpdateManager:
    def __init__(self, root, current_version, gate=None, fetcher=None):
        self.root = Path(root).resolve()
        self.current_version = _safe_version(current_version)
        self.data_root = self.root / "data"
        self.base = self.data_root / "app-updates"
        self.gate = gate
        self.fetcher = fetcher
        _check_tree(self.root)
        _check_tree(self.data_root, allow_missing=True)
        _check_tree(self.base, allow_missing=True)
        self.base.mkdir(parents=True, exist_ok=True)
        _check_tree(self.base)
        self.packages = self.base / "packages"
        self.transactions = self.base / "transactions"
        self.packages.mkdir(exist_ok=True)
        self.transactions.mkdir(exist_ok=True)
        _check_tree(self.packages)
        _check_tree(self.transactions)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake_scheduler = threading.Event()
        self._scheduler = None
        self._worker = None
        self._worker_kind = None
        self._txn_lock = None
        self._closed = False
        self._settings = {"auto_check": True, "auto_install": False}
        settings_path = self.base / "settings.json"
        if settings_path.exists():
            try:
                saved = _read_json(settings_path, 16 * 1024)
                if isinstance(saved, dict) and type(saved.get("auto_check")) is bool and type(saved.get("auto_install")) is bool:
                    self._settings = {"auto_check": saved["auto_check"] or saved["auto_install"],
                                      "auto_install": saved["auto_install"]}
            except (OSError, ValueError, TypeError):
                pass
        self._state = "idle"
        self._release = None
        self._error_code = None
        self._message = ""
        self._downloaded_bytes = 0
        self._ready_release_id = None
        self._restore_state()

    def _restore_state(self):
        path = self.base / "status.json"
        try:
            saved = _read_json(path, 128 * 1024)
            if isinstance(saved, dict):
                self._release = saved.get("release") if isinstance(saved.get("release"), dict) else None
                rid = saved.get("ready_release_id")
                if (isinstance(rid, str) and RELEASE_ID_RE.fullmatch(rid)
                        and self._release and _version_key(self._release.get("version", "0.0.0")) > _version_key(self.current_version)
                        and self._valid_cached_package(rid, self._release)):
                    self._ready_release_id = rid
                    self._state = "ready"
                    self._message = "更新包已验证，可安装。"
                    return
                if (self._release and saved.get("state") == "available"
                        and _version_key(self._release.get("version", "0.0.0")) > _version_key(self.current_version)):
                    self._state = "available"
                    self._message = "发现候选版更新。"
                    return
                self._release = None
                self._ready_release_id = None
                self._state = "idle"
        except (OSError, ValueError, TypeError, UpdateSecurityError):
            pass
        result_path = self.base / "result.json"
        try:
            result = _read_json(result_path, 128 * 1024)
            if result.get("state") in ("succeeded", "recovered_rollback", "recovered_partial"):
                self._state = "succeeded" if result["state"] == "succeeded" else "failed"
                self._message = "软件更新已完成。" if self._state == "succeeded" else "已恢复更新前的程序文件。"
            elif result.get("state") == "failed":
                self._state = "failed"
                self._error_code = result.get("error_code", "install_failed")
                self._message = "软件更新未完成，程序文件已保留或恢复。"
        except (OSError, ValueError, TypeError):
            pass

    def _valid_cached_package(self, release_id, release):
        if not isinstance(release, dict) or release.get("release_id") != release_id:
            return False
        package_path = self.packages / (release_id + ".zip")
        try:
            if not _within(package_path, self.packages) or _hash_regular(package_path) != release["package"]["sha256"]:
                return False
            blob = package_path.read_bytes()
            manifest, raw, _contents = _zip_manifest(blob, release["version"])
            return (manifest["kind"] == "Windows-x64" and len(blob) == release["package"]["bytes"]
                    and _sha(blob) == release["package"]["sha256"] and bool(raw))
        except (OSError, KeyError, TypeError, ValueError, UpdateSecurityError):
            return False

    def _snapshot_locked(self):
        release = self._release or {}
        latest = release.get("version")
        package = release.get("package") or {}
        return {"state": self._state, "current_version": self.current_version,
                "latest_version": latest, "release_id": release.get("release_id"),
                "bytes": package.get("bytes"), "downloaded_bytes": self._downloaded_bytes,
                "auto_check": self._settings["auto_check"], "auto_install": self._settings["auto_install"],
                "channel": "candidate", "notes": release.get("notes", ""),
                "error_code": self._error_code, "message": self._message,
                "safe_message": self._message}

    def status(self):
        with self._lock:
            return self._snapshot_locked()

    def _persist_locked(self):
        _atomic_json(self.base / "status.json", {"state": self._state, "release": self._release,
                      "ready_release_id": self._ready_release_id, "updated_at": int(time.time())})

    def _start_worker(self, kind, function):
        with self._lock:
            if self._closed:
                raise UpdateBusyError("软件更新组件已关闭。")
            if self._worker is not None and self._worker.is_alive():
                return self._snapshot_locked()
            worker = threading.Thread(target=self._worker_entry, args=(kind, function),
                                      name="aihub-app-update-" + kind, daemon=True)
            self._worker, self._worker_kind = worker, kind
            worker.start()
            return self._snapshot_locked()

    def _worker_entry(self, kind, function):
        admitted = False
        try:
            if self.gate is not None:
                admitted = bool(self.gate.enter())
                if not admitted:
                    raise UpdateBusyError("曜核当前正准备退出，请稍后重试更新。")
            function()
            # Automatic installation is staged in the background only. Exit and
            # installation remain an explicit tray action owned by the desktop.
            with self._lock:
                auto_stage = (kind == "check" and self._settings["auto_install"]
                              and self._state == "available" and self._release is not None)
                release_id = self._release.get("release_id") if auto_stage else None
                if auto_stage and self._release["package"]["bytes"] > MAX_PACKAGE_BYTES:
                    auto_stage, release_id = False, None
                if auto_stage:
                    self._state, self._message = "downloading", "正在后台下载并验证候选版更新。"
            if release_id:
                self._do_download(release_id)
        except UpdateBusyError as exc:
            with self._lock:
                if self._ready_release_id:
                    self._state, self._message = "ready", str(exc)
                else:
                    self._state, self._message = "failed", str(exc)
                self._error_code = "busy"
                self._persist_locked()
        except UpdateSecurityError as exc:
            with self._lock:
                if self._ready_release_id:
                    self._state = "ready"
                else:
                    self._state = "failed"
                self._error_code, self._message = "invalid_update", str(exc)
                self._persist_locked()
        except Exception:
            with self._lock:
                if self._ready_release_id and self._valid_cached_package(self._ready_release_id, self._release):
                    self._state, self._message = "ready", "网络检查失败；已验证的更新包仍可安装。"
                else:
                    self._state, self._message = "failed", "连接候选更新源失败，请稍后重试。"
                self._error_code = "network_error"
                self._persist_locked()
        finally:
            if admitted:
                self.gate.leave()
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None
                    self._worker_kind = None

    def check(self):
        with self._lock:
            if self._state in ("checking", "downloading", "prepared", "applying"):
                return self._snapshot_locked()
            self._state, self._error_code, self._message = "checking", None, "正在检查候选版更新。"
        return self._start_worker("check", self._do_check)

    def _do_check(self):
        raw = _fetch(self.fetcher, FEED_URL, MAX_FEED_BYTES, self._stop)
        try:
            release = _validate_feed(raw, self.current_version)
        except UpdateNotNewerError:
            with self._lock:
                self._release = None
                self._ready_release_id = None
                self._state, self._error_code, self._message = "idle", None, "当前已是此候选版或更新版本。"
                self._persist_locked()
            return
        if release["package"]["bytes"] > MAX_PACKAGE_BYTES:
            with self._lock:
                self._release = release
                self._state, self._error_code = "available", "manual_download"
                self._message = "更新包超过 50 MiB，请从候选版发布页手动下载。"
                self._persist_locked()
            return
        with self._lock:
            self._release = release
            if self._ready_release_id == release["release_id"] and self._valid_cached_package(self._ready_release_id, release):
                self._state, self._error_code, self._message = "ready", None, "更新包已验证，可安装。"
            else:
                self._ready_release_id = None
                self._state, self._error_code, self._message = "available", None, "发现候选版更新。"
            self._persist_locked()

    def download(self, release_id):
        if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
            raise ValueError("候选版本标识无效。")
        with self._lock:
            # Version/ID race: a mismatched release_id is a validation error
            # regardless of worker state, so it must be checked before the
            # busy branch (busy is reserved for same-ID duplicate requests).
            if not self._release or self._release.get("release_id") != release_id:
                raise ValueError("候选版本已变化，请重新检查更新。")
            if self._worker is not None and self._worker.is_alive():
                if self._worker_kind == "download":
                    return self._snapshot_locked()
                raise UpdateBusyError("另一个软件更新操作正在进行。")
            if self._state not in ("available", "ready", "failed"):
                raise UpdateBusyError("当前状态不能下载软件更新。")
            if self._release["package"]["bytes"] > MAX_PACKAGE_BYTES:
                raise ValueError("更新包超过 50 MiB，请手动下载。")
            if self._valid_cached_package(release_id, self._release):
                self._ready_release_id, self._state = release_id, "ready"
                self._message, self._error_code = "更新包已验证，可安装。", None
                self._persist_locked()
                return self._snapshot_locked()
            self._state, self._error_code, self._message = "downloading", None, "正在下载并验证更新包。"
            self._downloaded_bytes = 0
        return self._start_worker("download", lambda: self._do_download(release_id))

    def _do_download(self, release_id):
        with self._lock:
            release = dict(self._release or {})
        if release.get("release_id") != release_id:
            raise UpdateBusyError("候选版本已变化，下载已取消。")
        expected = release["package"]["bytes"]
        if expected > MAX_PACKAGE_BYTES:
            raise UpdateSecurityError("更新包超过 50 MiB 限制。")
        blob = _fetch(self.fetcher, release["package_url"], MAX_PACKAGE_BYTES, self._stop)
        with self._lock:
            self._downloaded_bytes = len(blob)
        if len(blob) != expected or _sha(blob) != release["package"]["sha256"]:
            raise UpdateSecurityError("更新包大小或 SHA-256 与候选信息不一致。")
        _zip_manifest(blob, release["version"])
        target = self.packages / (release_id + ".zip")
        if os.path.lexists(target):
            if _hash_regular(target) != release["package"]["sha256"]:
                raise UpdateSecurityError("缓存位置已有内容不同的文件，未覆盖。")
        else:
            temp = self.packages / ("." + release_id + "." + uuid.uuid4().hex + ".part")
            try:
                with open(temp, "xb") as stream:
                    _private_file(temp)
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
                if _hash_regular(temp) != release["package"]["sha256"]:
                    raise UpdateSecurityError("临时缓存摘要校验失败。")
                os.replace(temp, target)
                _fsync_dir(self.packages)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()
        with self._lock:
            if not self._release or self._release.get("release_id") != release_id:
                raise UpdateBusyError("候选版本已变化，已保留下载文件但未标记为可安装。")
            self._ready_release_id = release_id
            self._state, self._error_code, self._message = "ready", None, "更新包已验证，可安装。"
            self._downloaded_bytes = len(blob)
            self._persist_locked()

    def settings(self, auto_check, auto_install):
        if type(auto_check) is not bool or type(auto_install) is not bool:
            raise ValueError("自动更新设置必须是布尔值。")
        with self._lock:
            if self._state in ("prepared", "applying"):
                raise UpdateBusyError("更新事务进行中，暂不能修改设置。")
            self._settings = {"auto_check": auto_check or auto_install, "auto_install": auto_install}
            _atomic_json(self.base / "settings.json", self._settings)
            snapshot = self._snapshot_locked()
        if self._settings["auto_check"]:
            self._wake_scheduler.set()
            self.start_scheduler()
        return snapshot

    def start_scheduler(self):
        with self._lock:
            if self._closed or not self._settings["auto_check"]:
                return None
            if self._scheduler is not None and self._scheduler.is_alive():
                self._wake_scheduler.set()
                return {"thread": self._scheduler, "stop_event": self._stop}
            self._stop.clear()
            self._wake_scheduler.clear()
            thread = threading.Thread(target=self._schedule, name="aihub-app-update-scheduler", daemon=True)
            self._scheduler = thread
            thread.start()
            return {"thread": thread, "stop_event": self._stop}

    def _schedule(self):
        delay = 0
        while not self._stop.is_set():
            if self._wake_scheduler.wait(delay):
                self._wake_scheduler.clear()
                if self._stop.is_set():
                    return
            with self._lock:
                enabled = self._settings["auto_check"]
            if enabled:
                try:
                    self.check()
                except Exception:
                    pass
            delay = 12 * 60 * 60

    def prepare(self, release_id, restart, desktop):
        if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
            raise ValueError("候选版本标识无效。")
        if type(restart) is not bool:
            raise ValueError("重启选项必须是布尔值。")
        if not isinstance(desktop, dict) or set(desktop) != {"pid", "start_filetime"}:
            raise ValueError("桌面进程身份字段无效。")
        if type(desktop.get("pid")) is not int or not isinstance(desktop.get("start_filetime"), str) or not desktop["start_filetime"].isdigit():
            raise ValueError("桌面进程身份无效。")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise UpdateBusyError("软件更新操作正在运行。")
            if self._state != "ready" or self._ready_release_id != release_id or not self._release or self._release.get("release_id") != release_id:
                raise UpdateBusyError("更新包尚未验证或候选版本已变化。")
            if not self._valid_cached_package(release_id, self._release):
                self._state, self._ready_release_id = "available", None
                self._persist_locked()
                raise UpdateBusyError("更新包缓存校验失败，请重新下载。")
            desktop_identity = _process_identity(desktop["pid"])
            if desktop_identity["start_filetime"] != desktop["start_filetime"]:
                raise ValueError("桌面进程已变化，请重新打开更新窗口。")
            if not _same_path(desktop_identity["executable_path"], self.root / "AI Hub.exe"):
                raise ValueError("桌面进程并非当前曜核安装。")
            exe = self.root / "AI Hub.exe"
            if not exe.is_file() or _is_reparse(os.lstat(exe)):
                raise ValueError("当前安装缺少有效的 AI Hub.exe。")
            package_path = self.packages / (release_id + ".zip")
            blob = package_path.read_bytes()
            manifest, raw_manifest, package_files = _zip_manifest(blob, self._release["version"])
            baseline = _program_files(self.root)
            package_set = set(package_files)
            installed_manifest = _verify_installed_manifest(self.root, baseline, self.current_version)
            unexpected = set(baseline) - package_set
            if unexpected:
                raise UpdateBusyError("安装目录存在候选包之外的程序文件，已停止更新。")
            rows = []
            txid = uuid.uuid4().hex
            txdir = self.transactions / txid
            txlock = _lock_file(self.base / "transaction.lock", blocking=False)
            if txlock is None:
                raise UpdateBusyError("已有软件更新事务正在准备或安装。")
            try:
                if os.path.lexists(self.base / "install-lock.json"):
                    raise UpdateBusyError("已有软件更新事务尚未完成。")
                txdir.mkdir()
                _check_tree(txdir)
                helper_source = self.root / "tools" / "app_update_helper.py"
                if not helper_source.is_file() or _is_reparse(os.lstat(helper_source)):
                    raise UpdateSecurityError("安装内缺少可信更新辅助程序。")
                helper_path = txdir / "app_update_helper.py"
                with open(helper_source, "rb") as src, open(helper_path, "xb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                _private_file(helper_path)
                helper_sha = _hash_regular(helper_path)
                service_identity = _process_identity(os.getpid())
                for rel, new_data in sorted(package_files.items()):
                    path = self.root.joinpath(*rel.split("/"))
                    old_sha = baseline.get(rel)
                    preserve = rel == "AGENTS.md"
                    if old_sha is not None and _hash_regular(path) != old_sha:
                        raise UpdateBusyError("程序文件在准备更新时已变化，请重新启动更新流程。")
                    backup_name = hashlib.sha256(rel.encode("utf-8")).hexdigest() + ".bak"
                    rows.append({"path": rel, "baseline_sha256": old_sha,
                                 "new_sha256": _sha(new_data), "bytes": len(new_data),
                                 "backup_path": "backups/" + backup_name,
                                 "preserve": preserve})
                installed_payload, installed_bytes = _installed_manifest_bytes(
                    self._release, package_files, installed_manifest, self.root)
                manifest_path = self.root / "manifest.json"
                rows.append({"path": "manifest.json", "baseline_sha256": _hash_regular(manifest_path),
                             "new_sha256": _sha(installed_bytes), "bytes": len(installed_bytes),
                             "backup_path": "backups/" + hashlib.sha256(b"manifest.json").hexdigest() + ".bak",
                             "preserve": False, "metadata": True})
                (txdir / "backups").mkdir()
                transaction = {"schema": TX_SCHEMA, "transaction_id": txid, "state": "prepared",
                               "created_at": int(time.time()), "updated_at": int(time.time()),
                               "service_identity": service_identity, "helper_identity": None}
                _atomic_json(txdir / "transaction.json", transaction)
                _atomic_json(txdir / "journal.json", {"schema": "ai-hub-update-journal-v1", "files": []})
                ticket = {"schema": TICKET_SCHEMA, "transaction_id": txid,
                          "install_root": str(self.root), "data_root": str(self.data_root.resolve()),
                          "transaction_dir": str(txdir.resolve()), "package_path": str(package_path.resolve()),
                          "package_sha256": self._release["package"]["sha256"],
                          "manifest_sha256": _sha(raw_manifest), "release_id": release_id,
                          "version": self._release["version"], "helper_sha256": helper_sha,
                          "source_commit": self._release["commit"],
                          "installed_manifest": installed_payload,
                          "files": rows, "service_identity": service_identity,
                          "desktop_identity": desktop_identity, "restart": restart,
                          "timeout_seconds": HELPER_WAIT_SECONDS,
                          "mutex_name": "Local\\AIHub-desktop-" + hashlib.sha256(str(self.root).upper().encode("utf-8")).hexdigest()[:24]}
                ticket_path = txdir / "ticket.json"
                _atomic_json(ticket_path, ticket)
                _atomic_json(self.base / "install-lock.json", {"schema": LOCK_SCHEMA,
                             "transaction_id": txid, "transaction_dir": str(txdir.resolve()),
                             "created_at": int(time.time())})
                self._txn_lock = txlock
                txlock = None
                self._state, self._message, self._error_code = "prepared", "已准备更新，等待桌面确认退出。", None
                self._persist_locked()
                return {"transaction_id": txid, "helper_path": str(helper_path),
                        "ticket_path": str(ticket_path), "ready_path": str(txdir / "ready.json"),
                        "commit_path": str(txdir / "commit.json"), "cancel_path": str(txdir / "cancel.json")}
            except BaseException:
                if txlock is not None:
                    _unlock_file(txlock)
                if os.path.lexists(self.base / "install-lock.json"):
                    with contextlib.suppress(Exception):
                        marker = _read_json(self.base / "install-lock.json", 32 * 1024)
                        if marker.get("transaction_id") == txid:
                            (self.base / "install-lock.json").unlink()
                if txdir.exists():
                    # Never recursively remove a transaction directory. It may contain evidence.
                    _write_tx(txdir, "prepare_failed")
                raise

    def cancel(self, transaction_id):
        if not isinstance(transaction_id, str) or not TXID_RE.fullmatch(transaction_id):
            raise ValueError("更新事务标识无效。")
        txdir = _transaction_dir(self.root, transaction_id)
        with self._lock:
            if self._state != "prepared":
                raise UpdateBusyError("当前没有可取消的准备事务。")
            tx = _read_json(txdir / "transaction.json", 128 * 1024)
            if tx.get("transaction_id") != transaction_id or tx.get("state") not in ("prepared", "helper_starting", "helper_waiting"):
                raise ValueError("更新事务已失效或不匹配。")
            if os.path.lexists(txdir / "commit.json"):
                raise UpdateBusyError("更新已提交给桌面助手，无法取消。")
            cancel_path = txdir / "cancel.json"
            _atomic_json(cancel_path, {"schema": "ai-hub-update-cancel-v1", "transaction_id": transaction_id})
            marker = self.base / "install-lock.json"
            try:
                lock_info = _read_json(marker, 32 * 1024)
                if lock_info.get("transaction_id") == transaction_id:
                    # The service still holds the OS lock until this API call.
                    # The helper can signal readiness but cannot mutate before
                    # commit and process exit, so cancellation is race-free here.
                    marker.unlink()
                    _fsync_dir(self.base)
                    _write_tx(txdir, "cancelled")
                    _record_result(self.root, {"schema": "ai-hub-update-result-v1",
                                  "transaction_id": transaction_id, "state": "cancelled",
                                  "restart": False, "updated_at": int(time.time())})
                    _unlock_file(self._txn_lock)
                    self._txn_lock = None
            except (FileNotFoundError, OSError, ValueError, TypeError):
                pass
            self._state, self._message, self._error_code = "ready", "更新事务已取消。", None
            self._persist_locked()
            return {"transaction_id": transaction_id, "cancelled": True}

    def close(self):
        self._stop.set()
        # The scheduler may be sleeping in Event.wait(12h); _stop alone never
        # wakes it. Wake it so the bounded join below actually completes.
        self._wake_scheduler.set()
        with self._lock:
            self._closed = True
            threads = [thread for thread in (self._scheduler, self._worker) if thread is not None and thread is not threading.current_thread()]
        deadline = time.monotonic() + 15
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        # Prepared marker remains durable for helper handoff/recovery; closing the
        # service releases only the OS lock so the standalone helper can take over.
        _unlock_file(self._txn_lock)
        self._txn_lock = None
