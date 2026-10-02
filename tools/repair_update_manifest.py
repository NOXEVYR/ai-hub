"""Repair affected 2.13.0-2.13.4 ZIP ledgers without changing program/data files.

Run this tool from a separate, newer unpacked release. Preview is the default;
--apply verifies every program hash, backs up the ledger, then adds the one
metadata field required by the old updater. No downloads or process termination.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aihub import app_update as update


def repair(root, apply=False):
    root = Path(root).absolute()
    update._check_tree(root)
    path = root / "manifest.json"
    original_sha = update._hash_regular(path)
    ledger = update._read_json(path, 4 * 1024 * 1024)
    version = ledger.get("version") if isinstance(ledger, dict) else None
    if version == "2.13.1":
        raise ValueError("2.13.1 还存在更新器版本不一致问题，请使用独立解压的完整新版；仅修复清单不足以完成升级。")
    if version not in {"2.13.0", "2.13.2", "2.13.3", "2.13.4"}:
        raise ValueError("本修复仅适用于 2.13.0、2.13.2、2.13.3 和 2.13.4 的解压安装；其他版本未修改。")
    files = update._program_files(root)
    update._verify_installed_manifest(root, files, version)
    if "desktop_shell_version" in ledger:
        return {"status": "already_compatible", "version": version, "written": False}
    # Only the exact legacy Windows ZIP schema is accepted by the verifier.
    result = {"status": "preview", "version": version, "verified_files": len(files), "written": False}
    if not apply:
        return result
    base = root / "data" / "app-updates"
    update._check_tree(base, allow_missing=True)
    base.mkdir(parents=True, exist_ok=True)
    lock = update._lock_file(base / "transaction.lock", blocking=False)
    if lock is None:
        raise update.UpdateBusyError("更新事务仍在运行，请完成或取消后重试修复。")
    try:
        if (base / "install-lock.json").exists():
            raise update.UpdateBusyError("仍有未完成的更新事务，未修改安装清单。")
        update._verify_installed_manifest(root, update._program_files(root), version)
        if update._hash_regular(path) != original_sha:
            raise update.UpdateBusyError("安装清单已变化，请重新预览。")
        backups = base / "manifest-repairs"
        update._check_tree(backups, allow_missing=True)
        backups.mkdir(exist_ok=True)
        backup = backups / ("manifest-" + uuid.uuid4().hex + ".json")
        raw = path.read_bytes()
        if update._sha(raw) != original_sha:
            raise update.UpdateBusyError("安装清单已变化，未执行修复。")
        with backup.open("xb") as stream:
            stream.write(raw)
        updated = dict(ledger, desktop_shell_version=version)
        update._atomic_json(path, updated)
        result.update(status="repaired", written=True, backup=str(backup))
        return result
    finally:
        update._unlock_file(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing AI-Hub installation folder")
    parser.add_argument("--apply", action="store_true", help="Apply after validating and backing up the manifest")
    args = parser.parse_args()
    try:
        print(json.dumps(repair(args.root, args.apply), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "not_repaired", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
