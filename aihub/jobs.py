# -*- coding: utf-8 -*-
"""后台任务管理：扫描 / 出图分析 / 更新检查。任务状态保存在内存。"""
import threading
import copy
import json
import time
import traceback

from . import config as cfgmod, service_control
from . import images, scan, updater

_jobs = {}
_lock = threading.Lock()


def _new_job(name):
    return {"name": name, "status": "running", "progress": "", "started": time.time(),
            "finished": None, "error": None}


def get_jobs():
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)


def running(name):
    with _lock:
        j = _jobs.get(name)
        return j and j["status"] == "running"


def start(name, target, *args):
    gate = service_control.GATE
    if not gate.enter():
        return None
    with _lock:
        j = _jobs.get(name)
        if j and j["status"] == "running":
            gate.leave()
            return None
        job = _new_job(name)
        _jobs[name] = job

    def run():
        try:
            target(*args)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            job["finished"] = time.time()
            gate.leave()

    t = threading.Thread(target=run, daemon=True, name=name)
    try:
        t.start()
    except Exception:
        job['status'] = 'error'
        job['error'] = '后台线程未能启动。'
        job['finished'] = time.time()
        gate.leave()
        raise
    return job


def progress(name, msg):
    with _lock:
        j = _jobs.get(name)
        if j:
            j["progress"] = msg


# ---------- 任务定义 ----------

def run_full_pipeline(db, cfg, do_images=True):
    """完整流水线：文件扫描 -> 模型库构建 -> 出图分析（单任务，进度统一走 scan）。"""

    def target():
        def cb(m):
            progress("scan", m)
        result = scan.scan_all(db, cfg, progress_cb=cb)
        stats = scan.build_models(db, cfg, result, progress_cb=cb)
        db.set_meta("scan_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        db.set_meta("scan_file_count", str(result["file_count"]))
        if do_images:
            cb("出图分析中…")
            image_stats = images.run_image_scan(db, cfg, progress_cb=cb)
            if image_stats.get("failed_roots"):
                raise RuntimeError("图库扫描部分失败；未清理失败来源的历史图片与引用：" +
                                   "；".join(image_stats["failed_roots"]))
        cb(f"完成：文件 {result['file_count']}，模型目录 {stats['catalog']}+{stats['filesystem']}")

    start("scan", target)


def run_update_check(db, cfg, scope="all", limit=0):
    """批量更新检查。scope: all | unchecked | pending"""
    snapshot = copy.deepcopy(cfg)
    selected_scope = scan.source_scope(snapshot)

    def ensure_current():
        keys = ('ai_root', 'workspace_managed', 'scan_roots', 'aliases', 'scan_exclude_paths', 'ignore_dirs')
        if any(cfg.get(key) != snapshot.get(key) for key in keys) or scan.source_scope(cfg) != selected_scope:
            raise ValueError('工作环境或模型来源已改变，停止旧范围的更新检查。')

    class ScopedRows(list):
        def __iter__(self):
            for row in super().__iter__():
                ensure_current()
                yield row

    class ScopedDB:
        def __getattr__(self, name):
            return getattr(db, name)

        def upsert_model(self, row):
            from . import organization
            with organization.LOCK:
                ensure_current()
                return db.upsert_model(row)

    def target():
        ensure_current()
        def cb(m):
            ensure_current()
            progress("check-updates", m)
        if scope == "all":
            cond = "mtype IN ('Checkpoint','LoRA','Diffusion','VAE','ControlNet','Embedding','IPAdapter','TextEncoder')"
        elif scope == "unchecked":
            cond = "update_state='unchecked'"
        else:
            cond = "update_state IN ('unchecked','error','unknown','maybe')"
        source_sql, source_args = selected_scope
        sql = f"SELECT * FROM models WHERE {cond} AND missing=0 AND {source_sql} ORDER BY mtype, size DESC"
        args = list(source_args)
        if limit:
            sql += ' LIMIT ?'
            args.append(max(0, int(limit)))
        rows = ScopedRows(db.query(sql, args))
        cb(f"待检查 {len(rows)} 个模型…")
        if not rows:
            progress("check-updates", "没有需要检查的模型")
            return
        ck = updater.Checker(ScopedDB(), snapshot, progress_cb=cb)
        summary = ck.check_many(rows)
        ensure_current()
        db.set_meta("update_check_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        progress("check-updates", f"完成：{json.dumps(summary, ensure_ascii=False)}")

    start("check-updates", target)
