"""Standalone, ticket-bound Windows program updater (standard library only).

This file is copied from the installed program before the application exits.
It intentionally contains its own archive allowlist and verifier so the code
being replaced is never imported to perform the replacement.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import traceback
import urllib.parse
import uuid
import zipfile

TICKET_SCHEMA = "ai-hub-update-ticket-v1"
TX_SCHEMA = "ai-hub-update-transaction-v1"
LOCK_SCHEMA = "ai-hub-update-lock-v1"
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_FILES = 10000
SHA_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
TX_RE = __import__("re").compile(r"^[0-9a-f]{32}$")
VERSION_RE = __import__("re").compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
ROOT_FILES = {"server.py", "launcher.pyw", "start.vbs", "start.bat", "debug.bat", ".gitignore",
              "THIRD_PARTY_NOTICES.md", "README.md", "AGENTS.md"}
SUBDIR_EXTS = {"aihub": {".py"}, "frontend": {".js", ".html", ".css", ".svg", ".ico", ".png"},
               "desktop": {".py", ".cs", ".manifest", ".txt", ".md"}, "tests": {".py", ".js"},
               "tools": {".py"}, "docs": {".md"}}
EXCLUDED = {"data", "backups", "vendor", "runtime", "__pycache__", "_tmp", ".git"}
RESERVED = {"con", "prn", "aux", "nul"} | {"com%d" % i for i in range(1, 10)} | {"lpt%d" % i for i in range(1, 10)}


class SafeFailure(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path):
    path = Path(path)
    st = os.lstat(path)
    if (stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400
            or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1):
        raise SafeFailure("not a private regular file")
    return st


def check_dir(path):
    path = Path(os.path.abspath(path))
    for item in reversed((path, *path.parents)):
        if not os.path.lexists(item):
            raise SafeFailure("directory is missing")
        st = os.lstat(item)
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise SafeFailure("directory reparse point")
        if item != path and not stat.S_ISDIR(st.st_mode):
            raise SafeFailure("directory path collision")
    if not path.is_dir():
        raise SafeFailure("not a directory")


def hash_file(path):
    regular(path)
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    check_dir(path.parent)
    if os.path.lexists(path):
        regular(path)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temp, "xb") as stream:
            if os.name != "nt":
                os.chmod(temp, 0o600)
            stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_json(path, maximum=8 * 1024 * 1024):
    path = Path(path)
    check_dir(path.parent)
    st = regular(path)
    if st.st_size > maximum:
        raise SafeFailure("oversized json")
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative(value, allow_manifest=False):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise SafeFailure("unsafe relative path")
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise SafeFailure("path escape")
    if allow_manifest and value == "manifest.json":
        return value
    parts = value.split("/")
    for part in parts:
        if part[-1:] in (" ", ".") or any(ord(ch) < 32 or ch in '<>"|?*' for ch in part):
            raise SafeFailure("invalid Windows path component")
        if part.split(".", 1)[0].casefold() in RESERVED:
            raise SafeFailure("reserved Windows name")
    if value in ROOT_FILES or value == "AI Hub.exe":
        return value
    path = PurePosixPath(value)
    if len(path.parts) < 2 or path.parts[0] not in SUBDIR_EXTS:
        raise SafeFailure("outside program allowlist")
    if EXCLUDED.intersection(part.casefold() for part in path.parts):
        raise SafeFailure("protected data path")
    if path.suffix.lower() not in SUBDIR_EXTS[path.parts[0]]:
        raise SafeFailure("outside program extension allowlist")
    return value


def process_identity(pid):
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
        handle = kernel.OpenProcess(0x1000 | 0x00100000 | 0x001000, False, pid)
        if not handle:
            raise SafeFailure("process identity unavailable")
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise SafeFailure("process start time unavailable")
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                raise SafeFailure("process image unavailable")
            return {"pid": pid, "start_filetime": str((created.dwHighDateTime << 32) | created.dwLowDateTime),
                    "executable_path": os.path.realpath(buf.value)}, handle
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    try:
        proc = Path("/proc") / str(pid)
        raw = (proc / "stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return {"pid": pid, "start_filetime": str(fields[19]),
                "executable_path": os.path.realpath(proc / "exe")}, None
    except (OSError, IndexError) as exc:
        raise SafeFailure("process identity unavailable") from exc


def same_path(left, right):
    return os.path.normcase(os.path.realpath(left)).casefold() == os.path.normcase(os.path.realpath(right)).casefold()


def hold_identity(expected, executable_must_match=None):
    if not isinstance(expected, dict) or set(expected) != {"pid", "start_filetime", "executable_path"}:
        raise SafeFailure("bad process identity")
    if type(expected["pid"]) is not int or expected["pid"] <= 0 or not isinstance(expected["start_filetime"], str):
        raise SafeFailure("bad process identity")
    actual, handle = process_identity(expected["pid"])
    if actual["start_filetime"] != expected["start_filetime"] or not same_path(actual["executable_path"], expected["executable_path"]):
        if handle:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.CloseHandle(handle)
        raise SafeFailure("process identity mismatch")
    if executable_must_match and not same_path(actual["executable_path"], executable_must_match):
        if handle:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle.restype = wintypes.BOOL
            kernel.CloseHandle(handle)
        raise SafeFailure("desktop executable mismatch")
    return actual, handle


def wait_processes(handles, timeout):
    if os.name != "nt":
        ids = [item["pid"] for item in handles]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            alive = []
            for pid in ids:
                try:
                    process_identity(pid)
                    alive.append(pid)
                except SafeFailure:
                    pass
            if not alive:
                return True
            time.sleep(0.1)
        return False
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForMultipleObjects.argtypes = (ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_ulong)
    kernel.WaitForMultipleObjects.restype = ctypes.c_ulong
    active = [handle for _identity, handle in handles if handle]
    deadline = time.monotonic() + timeout
    while active:
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        if not remaining:
            return False
        arr = (ctypes.c_void_p * len(active))(*[int(handle) for handle in active])
        result = kernel.WaitForMultipleObjects(len(active), arr, True, min(remaining, 1000))
        if result == 0x00000000:
            return True
        if result == 0x00000102:
            continue
        if result == 0x00000080:
            return True
        raise SafeFailure("process wait failed")
    return True


def acquire_file_lock(path, deadline):
    path = Path(path)
    check_dir(path.parent)
    if os.path.lexists(path):
        regular(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    stream = os.fdopen(fd, "r+b", buffering=0)
    if os.fstat(fd).st_nlink != 1:
        stream.close()
        raise SafeFailure("transaction lock is linked")
    if os.fstat(fd).st_size == 0:
        stream.write(b"0")
        os.fsync(fd)
    while time.monotonic() < deadline:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except (OSError, BlockingIOError):
            time.sleep(0.1)
    stream.close()
    return None


def unlock_file(stream):
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


def tx_state(txdir, state, **fields):
    value = {"schema": TX_SCHEMA, "transaction_id": txdir.name, "state": state,
             "updated_at": int(time.time()), **fields}
    atomic_json(txdir / "transaction.json", value)
    return value


def verify_ticket(ticket_path):
    ticket_path = Path(ticket_path).resolve(strict=True)
    check_dir(ticket_path.parent)
    ticket = read_json(ticket_path, 4 * 1024 * 1024)
    if not isinstance(ticket, dict) or ticket.get("schema") != TICKET_SCHEMA:
        raise SafeFailure("ticket schema invalid")
    txid = ticket.get("transaction_id")
    if not isinstance(txid, str) or not TX_RE.fullmatch(txid):
        raise SafeFailure("ticket id invalid")
    root = Path(ticket.get("install_root", "")).resolve(strict=True)
    txdir = Path(ticket.get("transaction_dir", "")).resolve(strict=True)
    data_root = Path(ticket.get("data_root", "")).resolve(strict=True)
    expected_txdir = root / "data" / "app-updates" / "transactions" / txid
    if not same_path(txdir, expected_txdir) or not same_path(ticket_path, txdir / "ticket.json"):
        raise SafeFailure("ticket path mismatch")
    if not same_path(data_root, root / "data"):
        raise SafeFailure("data root mismatch")
    check_dir(root)
    check_dir(txdir)
    helper_path = txdir / "app_update_helper.py"
    if not same_path(Path(__file__).resolve(), helper_path) or hash_file(helper_path) != ticket.get("helper_sha256"):
        raise SafeFailure("helper digest mismatch")
    if not isinstance(ticket.get("package_sha256"), str) or not SHA_RE.fullmatch(ticket["package_sha256"]):
        raise SafeFailure("package digest missing")
    package = root / "data" / "app-updates" / "packages" / (ticket.get("release_id", "") + ".zip")
    if not same_path(ticket.get("package_path", ""), package) or hash_file(package) != ticket["package_sha256"]:
        raise SafeFailure("package path or digest mismatch")
    if package.stat().st_size > MAX_PACKAGE_BYTES:
        raise SafeFailure("package too large")
    if not VERSION_RE.fullmatch(str(ticket.get("version", ""))):
        raise SafeFailure("version invalid")
    if not isinstance(ticket.get("source_commit"), str) or not __import__("re").fullmatch("[0-9a-f]{40}", ticket["source_commit"]):
        raise SafeFailure("source commit invalid")
    manifest, raw_manifest, contents = verify_archive(package.read_bytes(), ticket["version"])
    if sha(raw_manifest) != ticket.get("manifest_sha256"):
        raise SafeFailure("archive manifest digest mismatch")
    if manifest.get("kind") != "Windows-x64":
        raise SafeFailure("wrong package kind")
    rows = ticket.get("files")
    if not isinstance(rows, list) or len(rows) != len(contents) + 1 or len(rows) > MAX_FILES + 1:
        raise SafeFailure("ticket file list invalid")
    by_name, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise SafeFailure("ticket row invalid")
        name = row.get("path")
        if name == "manifest.json":
            if not row.get("metadata"):
                raise SafeFailure("manifest transaction entry invalid")
        else:
            safe_relative(name)
            if name not in contents:
                raise SafeFailure("ticket file not in package")
        if name.casefold() in seen:
            raise SafeFailure("ticket path collision")
        seen.add(name.casefold())
        if not isinstance(row.get("new_sha256"), str) or not SHA_RE.fullmatch(row["new_sha256"]):
            raise SafeFailure("ticket target digest invalid")
        if type(row.get("bytes")) is not int or row["bytes"] < 0:
            raise SafeFailure("ticket file size invalid")
        if name != "manifest.json" and (len(contents[name]) != row["bytes"] or sha(contents[name]) != row["new_sha256"]):
            raise SafeFailure("ticket package bytes mismatch")
        if row.get("preserve") is not (name == "AGENTS.md"):
            raise SafeFailure("ticket preservation flags invalid")
        backup_path = row.get("backup_path")
        if not isinstance(backup_path, str) or not backup_path.startswith("backups/") or "/" in backup_path[len("backups/"):]:
            raise SafeFailure("ticket backup path invalid")
        baseline = row.get("baseline_sha256")
        if baseline is not None and (not isinstance(baseline, str) or not SHA_RE.fullmatch(baseline)):
            raise SafeFailure("ticket baseline digest invalid")
        by_name[name] = row
    if {n for n in by_name if n != "manifest.json"} != set(contents):
        raise SafeFailure("ticket and package inventory differ")
    if not isinstance(ticket.get("installed_manifest"), dict):
        raise SafeFailure("installed manifest missing")
    installed = ticket["installed_manifest"]
    if installed.get("version") != ticket["version"] or installed.get("desktop_shell_version") != ticket["version"] or installed.get("source_commit") != ticket["source_commit"]:
        raise SafeFailure("installed manifest identity mismatch")
    serialized = json.dumps(installed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    manifest_row = by_name["manifest.json"]
    if sha(serialized) != manifest_row["new_sha256"] or len(serialized) != manifest_row["bytes"]:
        raise SafeFailure("installed manifest ticket digest mismatch")
    return ticket, root, txdir, package, contents, by_name, serialized


def verify_archive(blob, expected_version):
    if not blob or len(blob) > MAX_PACKAGE_BYTES:
        raise SafeFailure("package size limit")
    try:
        archive = zipfile.ZipFile(__import__("io").BytesIO(blob), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise SafeFailure("bad package ZIP") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise SafeFailure("package entry count")
        listed, info_map, expanded = set(), {}, 0
        for info in infos:
            if not info.filename.startswith("AI-Hub/") or info.is_dir():
                raise SafeFailure("package prefix invalid")
            name = info.filename[len("AI-Hub/"):]
            if name != "manifest.json":
                safe_relative(name)
            if name.casefold() in listed:
                raise SafeFailure("case-fold duplicate")
            listed.add(name.casefold())
            mode = (info.external_attr >> 16) & 0xffff
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG) or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise SafeFailure("special archive entry")
            if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
                raise SafeFailure("file size limit")
            expanded += info.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise SafeFailure("expanded size limit")
            if info.file_size > 1024 * 1024 and not info.compress_size or (info.compress_size and info.file_size > info.compress_size * 1000):
                raise SafeFailure("compression ratio limit")
            info_map[name] = info
        if "manifest.json" not in info_map:
            raise SafeFailure("manifest missing")
        raw_manifest = archive.read(info_map["manifest.json"])
        if len(raw_manifest) > 2 * 1024 * 1024:
            raise SafeFailure("manifest size limit")
        manifest = json.loads(raw_manifest.decode("utf-8"))
        if not isinstance(manifest, dict) or set(manifest) != {"version", "kind", "user_data_included", "files"}:
            raise SafeFailure("manifest fields invalid")
        if manifest.get("version") != expected_version or manifest.get("kind") != "Windows-x64" or manifest.get("user_data_included") is not False:
            raise SafeFailure("manifest identity invalid")
        rows = manifest.get("files")
        if not isinstance(rows, list) or not rows or len(rows) > MAX_FILES:
            raise SafeFailure("manifest inventory invalid")
        contents, row_names = {}, set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                raise SafeFailure("manifest row invalid")
            name = safe_relative(row["path"])
            if name.casefold() in row_names:
                raise SafeFailure("manifest case-fold duplicate")
            row_names.add(name.casefold())
            if name == "AGENTS.md":
                pass  # Kept in the archive and hash-checked, then never installed.
            if type(row["bytes"]) is not int or row["bytes"] < 0 or row["bytes"] > MAX_FILE_BYTES:
                raise SafeFailure("manifest length invalid")
            if not isinstance(row["sha256"], str) or not SHA_RE.fullmatch(row["sha256"]):
                raise SafeFailure("manifest digest invalid")
            info = info_map.get(name)
            if info is None or info.file_size != row["bytes"]:
                raise SafeFailure("manifest inventory mismatch")
            data = archive.read(info)
            if len(data) != row["bytes"] or sha(data) != row["sha256"]:
                raise SafeFailure("file digest mismatch")
            contents[name] = data
        if set(info_map) != set(contents) | {"manifest.json"} or "AI Hub.exe" not in contents:
            raise SafeFailure("archive has unlisted or missing files")
        return manifest, raw_manifest, contents


def program_inventory(root):
    found = {}
    for name in ROOT_FILES | {"AI Hub.exe"}:
        path = root / name
        if os.path.lexists(path):
            found[name] = hash_file(path)
    for folder, extensions in SUBDIR_EXTS.items():
        base = root / folder
        if not os.path.lexists(base):
            continue
        st = os.lstat(base)
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400 or not stat.S_ISDIR(st.st_mode):
            raise SafeFailure("program directory unsafe")
        for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
            current = Path(current)
            kept = []
            for directory in dirs:
                path = current / directory
                st = os.lstat(path)
                if directory.casefold() in EXCLUDED:
                    continue
                if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400 or not stat.S_ISDIR(st.st_mode):
                    raise SafeFailure("program directory contains reparse point")
                kept.append(directory)
            dirs[:] = kept
            for filename in files:
                path = current / filename
                rel = path.relative_to(root).as_posix()
                if path.suffix.lower() in extensions and not EXCLUDED.intersection(part.casefold() for part in PurePosixPath(rel).parts):
                    found[rel] = hash_file(path)
    return found


def verify_baseline(ticket, root, rows):
    current = program_inventory(root)
    recorded = {name for name, row in rows.items() if name != "manifest.json" and row.get("baseline_sha256") is not None}
    if set(current) != recorded:
        raise SafeFailure("unrecorded program files")
    for name, digest in current.items():
        if rows[name]["baseline_sha256"] != digest:
            raise SafeFailure("program changed after prepare")
    for name, row in rows.items():
        if name == "manifest.json":
            manifest_path = root / "manifest.json"
            if hash_file(manifest_path) != row["baseline_sha256"]:
                raise SafeFailure("install manifest changed after prepare")
            continue
        target = root.joinpath(*name.split("/"))
        if row.get("baseline_sha256") is None and os.path.lexists(target):
            raise SafeFailure("new program file collides with local file")
        parent = target.parent
        if not os.path.exists(parent):
            continue
        check_dir(parent)
        if not target.exists():
            siblings = {entry.name.casefold(): entry.name for entry in parent.iterdir()}
            if target.name.casefold() in siblings:
                raise SafeFailure("case-insensitive destination collision")


def verify_installed_ledger(root, current):
    manifest = read_json(root / "manifest.json", 4 * 1024 * 1024)
    if not isinstance(manifest, dict) or not {"version", "desktop_shell_version", "files"}.issubset(manifest):
        raise SafeFailure("installed ledger invalid")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise SafeFailure("installed ledger inventory invalid")
    recorded = {}
    for row in entries:
        if not isinstance(row, dict) or not {"path", "bytes", "sha256"}.issubset(row):
            raise SafeFailure("installed ledger row invalid")
        name = safe_relative(row["path"])
        if name == "AGENTS.md":
            continue
        if name in recorded or type(row["bytes"]) is not int or not isinstance(row["sha256"], str) or not SHA_RE.fullmatch(row["sha256"]):
            raise SafeFailure("installed ledger row invalid")
        recorded[name] = row
    current = {name: digest for name, digest in current.items() if name != "AGENTS.md"}
    if set(recorded) != set(current):
        raise SafeFailure("installed program file inventory differs from ledger")
    for name, digest in current.items():
        path = root.joinpath(*name.split("/"))
        if recorded[name]["sha256"] != digest or recorded[name]["bytes"] != path.stat().st_size:
            raise SafeFailure("unrecorded local program change")
    return manifest


def mutex_name(root):
    resolved = os.path.realpath(root)
    return "Local\\AIHub-desktop-" + hashlib.sha256(resolved.upper().encode("utf-8")).hexdigest()[:24]


def acquire_desktop_mutex(name, timeout):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise SafeFailure("desktop mutex create failed")
    result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
    if result not in (0, 0x80):
        kernel.CloseHandle(handle)
        return None
    return kernel, handle


def release_desktop_mutex(item):
    if item:
        kernel, handle = item
        with contextlib.suppress(Exception):
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def sqlite_evidence(root, txdir):
    database = root / "data" / "aihub.db"
    if not os.path.lexists(database):
        return None
    regular(database)
    target = txdir / "database-evidence.sqlite"
    if os.path.lexists(target):
        raise SafeFailure("database evidence already exists")
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=8)
    backup = sqlite3.connect(str(target), timeout=8)
    try:
        source.backup(backup, pages=256, sleep=0.05)
        result = backup.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise SafeFailure("database evidence quick check failed")
        backup.commit()
    finally:
        backup.close()
        source.close()
    if os.name != "nt":
        os.chmod(target, 0o600)
    return {"sha256": hash_file(target), "bytes": target.stat().st_size}


def write_journal(txdir, value):
    atomic_json(txdir / "journal.json", value)


def apply_files(root, txdir, ticket, contents, rows, manifest_bytes):
    # Copy and verify every backup before making the first replacement.
    backup_dir = txdir / "backups"
    if not backup_dir.is_dir():
        backup_dir.mkdir()
    journal_entries = []
    payloads = dict(contents)
    payloads["manifest.json"] = manifest_bytes
    for name in [n for n in sorted(payloads) if n != "manifest.json"] + ["manifest.json"]:
        row = rows[name]
        entry = {"path": name, "baseline_sha256": row.get("baseline_sha256"),
                 "new_sha256": row["new_sha256"], "backup_name": Path(row["backup_path"]).name,
                 "backup_sha256": None, "state": "prepared"}
        if row.get("preserve"):
            entry["state"] = "preserved"
            journal_entries.append(entry)
            continue
        old_sha = row.get("baseline_sha256")
        if old_sha is not None:
            target = root.joinpath(*name.split("/"))
            backup = backup_dir / entry["backup_name"]
            with open(target, "rb") as src, open(backup, "xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            entry["backup_sha256"] = hash_file(backup)
            if entry["backup_sha256"] != old_sha:
                raise SafeFailure("program backup digest mismatch")
        journal_entries.append(entry)
    journal = {"schema": "ai-hub-update-journal-v1", "files": journal_entries}
    write_journal(txdir, journal)
    tx_state(txdir, "applying", service_identity=ticket["service_identity"],
             helper_identity=process_identity(os.getpid())[0], journal_schema=journal["schema"])
    applied = 0
    # The install manifest is the last replacement and therefore the commit record.
    order = [n for n in sorted(payloads) if n != "manifest.json"] + ["manifest.json"]
    for name in order:
            row = rows[name]
            index = next(i for i, item in enumerate(journal_entries) if item["path"] == name)
            if row.get("preserve"):
                continue
            target = root.joinpath(*name.split("/"))
            if os.path.lexists(target):
                regular(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            check_dir(target.parent)
            temp = target.with_name("." + target.name + ".update-" + ticket["transaction_id"])
            if os.path.lexists(temp):
                raise SafeFailure("update temporary path already exists")
            journal_entries[index]["state"] = "applying"
            write_journal(txdir, journal)
            with open(temp, "xb") as stream:
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                stream.write(payloads[name])
                stream.flush()
                os.fsync(stream.fileno())
            if hash_file(temp) != row["new_sha256"]:
                raise SafeFailure("temporary replacement digest mismatch")
            os.replace(temp, target)
            sync_dir(target.parent)
            if hash_file(target) != row["new_sha256"]:
                raise SafeFailure("post-write digest mismatch")
            journal_entries[index]["state"] = "applied"
            write_journal(txdir, journal)
            applied += 1
    # Recheck every written file, not only the install manifest.
    for name, row in rows.items():
        if row.get("preserve"):
            continue
        if hash_file(root.joinpath(*name.split("/"))) != row["new_sha256"]:
            raise SafeFailure("post-install inventory mismatch")
    return journal, applied


def rollback(root, txdir, journal):
    retained, restored = 0, 0
    entries = journal.get("files", [])
    for item in reversed(entries):
        if item.get("state") not in ("applying", "applied", "restore_failed"):
            continue
        name = safe_relative(item.get("path"), allow_manifest=True)
        target = root.joinpath(*name.split("/"))
        if not os.path.lexists(target):
            current = None
        else:
            current = hash_file(target)
        old_sha, new_sha = item.get("baseline_sha256"), item.get("new_sha256")
        if current == old_sha:
            item["state"] = "rolled_back"
            restored += 1
            write_journal(txdir, journal)
            continue
        if current != new_sha:
            item["state"] = "retained_external_change"
            retained += 1
            write_journal(txdir, journal)
            continue
        if old_sha is None:
            target.unlink()
            sync_dir(target.parent)
        else:
            backup = txdir / "backups" / item["backup_name"]
            if hash_file(backup) != old_sha or item.get("backup_sha256") != old_sha:
                item["state"] = "restore_failed"
                retained += 1
                write_journal(txdir, journal)
                continue
            temp = target.with_name("." + target.name + ".rollback-" + uuid.uuid4().hex)
            with open(backup, "rb") as src, open(temp, "xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if hash_file(temp) != old_sha:
                temp.unlink()
                item["state"] = "restore_failed"
                retained += 1
                write_journal(txdir, journal)
                continue
            os.replace(temp, target)
            sync_dir(target.parent)
        item["state"] = "rolled_back"
        restored += 1
        write_journal(txdir, journal)
    return {"restored": restored, "retained": retained,
            "state": "recovered_rollback" if not retained else "recovery_partial"}


def result(root, txid, state, **extra):
    value = {"schema": "ai-hub-update-result-v1", "transaction_id": txid,
             "state": state, "updated_at": int(time.time()), **extra}
    atomic_json(root / "data" / "app-updates" / "result.json", value)
    return value


def execute(ticket_path):
    ticket, root, txdir, package, contents, rows, manifest_bytes = verify_ticket(ticket_path)
    manager_lock_path = root / "data" / "app-updates" / "transaction.lock"
    marker_path = root / "data" / "app-updates" / "install-lock.json"
    timeout = max(20, min(300, int(ticket.get("timeout_seconds", 180))))
    deadline = time.monotonic() + timeout
    trace = os.environ.get("AIHUB_HELPER_TRACE")

    def _trace(event, **fields):
        if trace:
            print(json.dumps({"trace": event, "t": round(time.monotonic(), 3),
                              "deadline_left": round(deadline - time.monotonic(), 3), **fields},
                             default=str), file=sys.stderr, flush=True)
    # The service retains the per-install lock until the desktop has accepted
    # shutdown. Publish ready first; take over the OS lock only after both held
    # process identities have exited. The durable marker blocks every new launch
    # during this lock handoff window.
    lock = None
    desktop_handle = service_handle = None
    mutex = None
    helper_id = None
    final_state = None
    restart_after_release = False
    try:
        marker = read_json(marker_path, 32768)
        if marker.get("schema") != LOCK_SCHEMA or marker.get("transaction_id") != ticket["transaction_id"]:
            raise SafeFailure("transaction marker mismatch")
        tx = read_json(txdir / "transaction.json", 128 * 1024)
        if tx.get("transaction_id") != ticket["transaction_id"] or tx.get("state") not in ("prepared", "helper_starting"):
            raise SafeFailure("transaction state mismatch")
        helper_id, _ = process_identity(os.getpid())
        service_id, service_handle = hold_identity(ticket["service_identity"])
        desktop_id, desktop_handle = hold_identity(ticket["desktop_identity"], root / "AI Hub.exe")
        if service_id["pid"] == desktop_id["pid"]:
            raise SafeFailure("desktop and service identities overlap")
        if ticket.get("mutex_name") != mutex_name(root):
            raise SafeFailure("desktop singleton identity mismatch")
        verify_baseline(ticket, root, rows)
        current = program_inventory(root)
        verify_installed_ledger(root, current)
        tx_state(txdir, "helper_waiting", service_identity=service_id,
                 helper_identity=helper_id, desktop_identity=desktop_id)
        ready = {"schema": "ai-hub-update-ready-v1", "transaction_id": ticket["transaction_id"],
                 "helper_pid": helper_id["pid"], "helper_start_filetime": helper_id["start_filetime"],
                 "helper_sha256": ticket["helper_sha256"]}
        atomic_json(txdir / "ready.json", ready)
        deadline = time.monotonic() + timeout
        commit_path, cancel_path = txdir / "commit.json", txdir / "cancel.json"
        committed = False
        while time.monotonic() < deadline:
            if os.path.lexists(cancel_path):
                value = read_json(cancel_path, 32768)
                if value.get("schema") != "ai-hub-update-cancel-v1" or value.get("transaction_id") != ticket["transaction_id"]:
                    raise SafeFailure("cancel identity mismatch")
                tx_state(txdir, "cancelled", service_identity=service_id, helper_identity=helper_id)
                result(root, ticket["transaction_id"], "cancelled", restart=False)
                final_state = "cancelled"
                return final_state
            if os.path.lexists(commit_path):
                value = read_json(commit_path, 32768)
                if value.get("schema") != "ai-hub-update-commit-v1" or value.get("transaction_id") != ticket["transaction_id"]:
                    raise SafeFailure("commit identity mismatch")
                committed = True
                break
            time.sleep(0.1)
        if not committed:
            tx_state(txdir, "failed", service_identity=service_id, helper_identity=helper_id,
                     error_code="commit_timeout")
            result(root, ticket["transaction_id"], "failed", restart=False, error_code="commit_timeout")
            final_state = "failed"
            return final_state
        remaining = max(0, deadline - time.monotonic())
        _trace("commit_seen", remaining=remaining)
        if not wait_processes([(desktop_id, desktop_handle), (service_id, service_handle)], remaining):
            _trace("wait_processes_false")
            tx_state(txdir, "failed", service_identity=service_id, helper_identity=helper_id,
                     error_code="process_exit_timeout")
            result(root, ticket["transaction_id"], "failed", restart=False, error_code="process_exit_timeout")
            final_state = "failed"
            return final_state
        _trace("processes_exited", desktop_handle=bool(desktop_handle), service_handle=bool(service_handle))
        # acquire_file_lock takes an absolute deadline; the desktop-mutex call
        # below takes a relative timeout. Keep the two conventions distinct.
        lock = acquire_file_lock(manager_lock_path, deadline)
        _trace("lock_attempt_done", acquired=lock is not None)
        if lock is None:
            tx_state(txdir, "failed", service_identity=service_id, helper_identity=helper_id,
                     error_code="transaction_lock_timeout")
            result(root, ticket["transaction_id"], "failed", restart=False, error_code="transaction_lock_timeout")
            final_state = "failed"
            return final_state
        marker = read_json(marker_path, 32768)
        if marker.get("schema") != LOCK_SCHEMA or marker.get("transaction_id") != ticket["transaction_id"]:
            raise SafeFailure("transaction marker lost during handoff")
        mutex = acquire_desktop_mutex(ticket["mutex_name"], max(0, deadline - time.monotonic()))
        if os.name == "nt" and mutex is None:
            tx_state(txdir, "failed", service_identity=service_id, helper_identity=helper_id,
                     error_code="desktop_mutex_timeout")
            result(root, ticket["transaction_id"], "failed", restart=False, error_code="desktop_mutex_timeout")
            final_state = "failed"
            return final_state
        # Last baseline check happens after both app processes have fully exited.
        verify_baseline(ticket, root, rows)
        db_evidence = sqlite_evidence(root, txdir)
        _journal, applied = apply_files(root, txdir, ticket, contents, rows, manifest_bytes)
        tx_state(txdir, "succeeded", service_identity=service_id, helper_identity=helper_id,
                 version=ticket["version"], applied_files=applied,
                 database_evidence=db_evidence)
        result(root, ticket["transaction_id"], "succeeded", version=ticket["version"],
               applied_files=applied, restarted=False, restart_requested=ticket["restart"],
               restart_error=None, database_evidence=db_evidence)
        final_state = "succeeded"
        restart_after_release = bool(ticket["restart"])
        return final_state
    except BaseException as exc:
        # Only roll back when an applying journal exists. Validation failures occur
        # before any write and leave the install untouched.
        try:
            journal = read_json(txdir / "journal.json", 8 * 1024 * 1024)
            if any(item.get("state") in ("applying", "applied", "restore_failed") for item in journal.get("files", [])):
                recovered = rollback(root, txdir, journal)
                final_state = "failed" if recovered["state"] == "recovered_rollback" else "recovery_partial"
                tx_state(txdir, final_state, service_identity=ticket.get("service_identity"),
                         helper_identity=helper_id, restored_files=recovered["restored"],
                         retained_files=recovered["retained"], error_code="apply_failed")
                result(root, ticket["transaction_id"], final_state, restart=False,
                       restored_files=recovered["restored"], retained_files=recovered["retained"],
                       error_code="apply_failed")
            elif any(item.get("state") == "retained_external_change" for item in journal.get("files", [])):
                final_state = "recovery_partial"
                tx_state(txdir, final_state, service_identity=ticket.get("service_identity"),
                         helper_identity=helper_id, error_code="apply_failed")
                result(root, ticket["transaction_id"], final_state, restart=False, error_code="apply_failed")
            else:
                final_state = "failed"
                tx_state(txdir, "failed", service_identity=ticket.get("service_identity"),
                         helper_identity=helper_id, error_code="helper_failed")
                result(root, ticket["transaction_id"], "failed", restart=False, error_code="helper_failed")
        except BaseException:
            final_state = "recovery_required"
            with contextlib.suppress(Exception):
                tx_state(txdir, "recovery_required", service_identity=ticket.get("service_identity"),
                         helper_identity=helper_id, error_code="recovery_required")
                result(root, ticket["transaction_id"], "recovery_required", restart=False,
                       error_code="recovery_required")
        return final_state
    finally:
        release_desktop_mutex(mutex)
        if os.name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for handle in (desktop_handle, service_handle):
                if handle:
                    kernel.CloseHandle(handle)
        # Preserve a marker when recovery is incomplete; next startup must replay
        # the durable journal and must never launch mixed program files.
        if lock is not None and final_state in ("succeeded", "failed", "cancelled"):
            try:
                marker = read_json(marker_path, 32768)
                if marker.get("transaction_id") == ticket["transaction_id"]:
                    marker_path.unlink()
                    sync_dir(marker_path.parent)
            except (OSError, ValueError, TypeError):
                pass
        unlock_file(lock)
        # Launch the just-written, exact in-install executable only after the
        # marker, OS lock, and desktop singleton mutex have all been released.
        if restart_after_release:
            try:
                executable = root / "AI Hub.exe"
                subprocess.Popen([str(executable)], cwd=str(root), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                current = read_json(root / "data" / "app-updates" / "result.json", 128 * 1024)
                current["restarted"] = True
                current["restart_error"] = None
                atomic_json(root / "data" / "app-updates" / "result.json", current)
            except (OSError, ValueError, TypeError):
                with contextlib.suppress(Exception):
                    current = read_json(root / "data" / "app-updates" / "result.json", 128 * 1024)
                    current["restarted"] = False
                    current["restart_error"] = "restart_failed"
                    atomic_json(root / "data" / "app-updates" / "result.json", current)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ticket", required=True)
    args = parser.parse_args(argv)
    try:
        state = execute(args.ticket)
        return 0 if state in ("succeeded", "cancelled") else 2
    except BaseException:
        # stderr is discarded by the hidden desktop spawn but captured by test
        # harnesses; a silent exit code alone made lock-handoff failures opaque.
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
