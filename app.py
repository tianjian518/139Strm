# -*- coding: utf-8 -*-
"""
139Strm —— 移动云盘（139）STRM 生成器与 302 直链服务

设计要点：
  * 只做移动云盘，不依赖 OpenList，不需要境外中转。
  * 生成的 .strm 内容指向本机 /d/<file_id>，
    播放时服务端换取移动云盘直链后 302 跳转，视频流不经过本机。
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, jsonify, render_template, request, redirect, Response

from yun139 import crypto
from yun139.client import Yun139Client, Yun139Error, CLOUD_TYPES
from yun139.strm import (StrmGenerator, DEFAULT_MEDIA_EXT, DEFAULT_COPY_EXT,
                         sanitize_name, CancelError)
from yun139 import cas as cas_mod
from yun139.cas import CASRestorer, CASError, is_cas_name

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(BASE_DIR, "config.json"))

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# 直链缓存：file_id -> (url, 过期时间戳)
_link_cache = {}
_cache_lock = threading.Lock()
LINK_TTL = 2 * 3600  # 直链有时效，缓存 2 小时

# 后台生成任务（队列式：可串行执行多个任务）
_task_state = {
    "running": False,
    "progress": "",
    "result": None,
    "started_at": None,
    "current": None,
    "queue_total": 0,
    "queue_done": 0,
    "results": [],
    "stop_requested": False,   # 用户请求终止当前运行
}
_task_lock = threading.Lock()
_current_gen = None            # 当前正在运行的 StrmGenerator 实例，供 stop 接口中断

# 定时同步运行状态（与单次手动任务分开记录）
_schedule_meta = {
    "last_run_at": None,
    "last_result": None,
    "next_run_at": None,
}


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

DEFAULT_CONFIG = {
    "authorization": "",
    "cloud_type": "personal_new",
    "mail_cookies": "",
    "username": "",
    "cloud_id": "",
    "output_dir": "/strm",
    "base_url": "",            # 留空则自动取请求中的 Host
    "media_ext": DEFAULT_MEDIA_EXT,
    "copy_ext": DEFAULT_COPY_EXT,
    "min_size_mb": 0,
    "recursive": True,
    # ---- CAS 秒传还原 ----
    "cas_enabled": True,       # 是否对 .cas 文件做秒传还原播放
    "cas_temp_ttl": 300,       # 还原出的临时文件保留秒数后自动删除
    "cas_allow_all_ext": False,  # False=只还原视频；True=任何后缀都还原
    "cas_temp_dir_id": "",     # 记住临时目录 ID，避免重启后重复创建
    # ---- 同步模式 ----
    "sync_mode": "incremental",  # incremental=跳过已生成；force=全量覆盖重新生成
    "keep_root": True,           # 生成时在输出目录保留"选定目录"这一层（结构更清晰）
    # ---- 定时同步 ----
    "schedule_enabled": False,
    "schedule_kind": "off",       # off / daily / interval
    "schedule_hour": 3,
    "schedule_minute": 0,
    "schedule_interval_hours": 6,
    "schedule_folder": "/",
    "schedule_sync_mode": "incremental",
    "schedule_root_name": "",     # 定时任务套壳用的目录名（留空则不套壳）
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                cfg.update(json.load(fp))
        except (ValueError, OSError):
            pass
    # 环境变量优先级最高，方便 Docker 部署
    env_token = os.environ.get("YUN139_AUTHORIZATION")
    if env_token:
        cfg["authorization"] = env_token
    if os.environ.get("YUN139_CLOUD_TYPE"):
        cfg["cloud_type"] = os.environ["YUN139_CLOUD_TYPE"]
    if os.environ.get("YUN139_OUTPUT_DIR"):
        cfg["output_dir"] = os.environ["YUN139_OUTPUT_DIR"]
    return cfg


def save_config(cfg):
    saveable = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump(saveable, fp, ensure_ascii=False, indent=2)


# ----------------------------------------------------------------------
# 任务存储（每个任务 = 一个云盘目录 + 一套生成选项）
# ----------------------------------------------------------------------

def get_tasks_path():
    d = os.path.dirname(CONFIG_PATH)
    return os.path.join(d, "tasks.json")


def load_tasks():
    p = get_tasks_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_tasks(tasks):
    p = get_tasks_path()
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as fp:
        json.dump(tasks, fp, ensure_ascii=False, indent=2)


def build_client(cfg=None):
    cfg = cfg or load_config()
    return Yun139Client(
        authorization=cfg.get("authorization", ""),
        cloud_type=cfg.get("cloud_type", "personal_new"),
        mail_cookies=cfg.get("mail_cookies", ""),
        username=cfg.get("username", ""),
        cloud_id=cfg.get("cloud_id", ""),
    )


# CAS 秒传还原器（按 authorization 缓存，换号自动重建）
_restorers = {}
_restorer_lock = threading.Lock()


def get_restorer(cfg, client):
    """
    按账号缓存还原器。

    注意：每次请求都会新建 client，不能拿 client 身份来判断是否复用，
    否则临时目录会被反复创建。
    """
    key = cfg.get("authorization", "")
    with _restorer_lock:
        rest = _restorers.get(key)
        if rest is None:
            rest = CASRestorer(client)
            _restorers[key] = rest
        # 每次都同步最新 client 与配置，client 只是一次性的会话载体
        rest.client = client
        rest.temp_ttl = int(cfg.get("cas_temp_ttl") or 300)
        rest.allow_all_ext = bool(cfg.get("cas_allow_all_ext"))
        rest.set_temp_dir(cfg.get("cas_temp_dir_id") or "")
        return rest


def remember_temp_dir(cfg, dir_id):
    """把临时目录 ID 写回配置，保证重启后还能复用同一个目录。"""
    if cfg.get("cas_temp_dir_id") == dir_id:
        return
    cfg["cas_temp_dir_id"] = dir_id
    try:
        save_config(cfg)
    except OSError:
        pass


def get_base_url(cfg, fallback=""):
    """
    取得写入 strm 的访问地址。

    注意 request 是线程局部对象，后台任务线程里取不到，
    因此必须在主线程先把 host_url 取好再传进来（fallback）。
    """
    url = cfg.get("base_url") or ""
    if not url:
        url = fallback
    return url.rstrip("/")


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})


# ----------------------------------------------------------------------
# 配置接口
# ----------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    token = cfg.get("authorization") or ""
    masked = (token[:8] + "..." + token[-6:]) if len(token) > 20 else ("已配置" if token else "")
    safe = dict(cfg)
    safe["authorization"] = ""
    safe["authorization_masked"] = masked
    safe["configured"] = bool(token)
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    cfg = load_config()
    data = request.get_json(force=True) or {}
    for key in DEFAULT_CONFIG:
        if key in data:
            val = data[key]
            if key in ("media_ext", "copy_ext") and isinstance(val, str):
                val = [x.strip().lstrip(".") for x in val.split(",") if x.strip()]
            if key == "min_size_mb":
                val = float(val or 0)
            if key == "recursive":
                val = bool(val)
            cfg[key] = val
    # 空字符串表示不修改已有凭据
    if not cfg.get("authorization"):
        old = load_config().get("authorization", "")
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                    cfg["authorization"] = json.load(fp).get("authorization", old)
            except (ValueError, OSError):
                pass
    save_config(cfg)
    with _cache_lock:
        _link_cache.clear()
    return jsonify({"ok": True})


@app.route("/api/test", methods=["POST"])
def api_test():
    cfg = load_config()
    data = request.get_json(force=True) or {}
    if data.get("authorization"):
        cfg["authorization"] = data["authorization"]
    if data.get("cloud_type"):
        cfg["cloud_type"] = data["cloud_type"]
    try:
        client = build_client(cfg)
        client.init()
        files = client.list_files("/")
        expire = client.get_expire_time()
        return jsonify({
            "ok": True,
            "account": client.account,
            "host": client.personal_host,
            "root_items": len(files),
            "expire": expire.isoformat() if expire else None,
        })
    except Yun139Error as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ----------------------------------------------------------------------
# 浏览
# ----------------------------------------------------------------------

@app.route("/api/list")
def api_list():
    cfg = load_config()
    folder = request.args.get("folder", "/")
    try:
        client = build_client(cfg)
        client.init()
        items = client.list_files(folder)
        return jsonify({
            "ok": True,
            "folder": folder,
            "items": [it.to_dict() for it in items],
        })
    except Yun139Error as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ----------------------------------------------------------------------
# STRM 生成
# ----------------------------------------------------------------------

def run_strm_job(cfg, folder, force=None, fallback_host=""):
    """
    真正执行一次 STRM 生成（手动 / 任务 / 定时都会走到这里）。

    所有生成选项都从 cfg 读取（调用方负责把全局配置与任务选项合并好）。
    返回 summary 字典；任何异常都被包成 summary，不会向外抛，避免后台线程静默崩溃。
    """
    try:
        client = build_client(cfg)
        client.init()
        base_url = get_base_url(cfg, fallback_host)
        if not base_url:
            raise ValueError(
                "无法确定访问地址，请在配置中填写 base_url（如 http://192.168.1.10:8025）"
            )
        if force is None:
            force = (cfg.get("sync_mode") or "incremental") == "force"
        delete_orphans = bool(cfg.get("delete_orphans", force))
        keep_root = cfg.get("keep_root", True)
        root_name = cfg.get("root_name") or None
        if not keep_root or not root_name:
            root_name = None
        gen = StrmGenerator(
            client=client, base_url=base_url,
            output_dir=cfg.get("output_dir") or "/strm",
            media_ext=cfg.get("media_ext"), copy_ext=cfg.get("copy_ext"),
            min_size_mb=float(cfg.get("min_size_mb", 0) or 0),
            recursive=bool(cfg.get("recursive", True)),
            include_cas=bool(cfg.get("cas_enabled", True)),
            force=bool(force),
            delete_orphans=delete_orphans,
        )
        global _current_gen
        with _task_lock:
            _current_gen = gen
        try:
            gen.generate(folder, "", root_name=root_name)
            gen.clean_orphans()
        except CancelError:
            return {**gen.summary(), "cancelled": True}
        finally:
            with _task_lock:
                _current_gen = None
        return gen.summary()
    except CancelError:
        return {"cancelled": True, "errors": ["生成已被手动终止"],
                "created": 0, "updated": 0, "skipped": 0,
                "copied": 0, "logs": []}
    except Exception as exc:
        with _task_lock:
            _current_gen = None
        return {"errors": [f"{type(exc).__name__}: {exc}"],
                "created": 0, "updated": 0, "skipped": 0,
                "copied": 0, "logs": []}


def _task_to_job(task, cfg):
    """把一个任务（或单次请求的选项）合并进全局配置，得到一次生成用的 job。"""
    job_cfg = dict(cfg)
    for k in ("sync_mode", "delete_orphans", "keep_root", "root_name",
              "recursive", "min_size_mb", "media_ext", "copy_ext"):
        if task.get(k) is not None:
            job_cfg[k] = task[k]
    return {"name": task.get("name", "任务"), "folder": task.get("folder", "/"),
            "cfg": job_cfg}


def _claim_running():
    """尝试占用后台生成槽位；已在跑则返回 False。"""
    with _task_lock:
        if _task_state["running"]:
            return False
        _task_state["running"] = True
        return True


def _enqueue_jobs(jobs):
    """串行执行多个生成任务，统一占用 _task_state 槽位。"""
    if not jobs:
        return False
    if not _claim_running():
        return False
    with _task_lock:
        _task_state.update({
            "running": True,
            "progress": "队列已建立，等待执行",
            "result": None,
            "started_at": datetime.now().isoformat(),
            "current": None,
            "queue_total": len(jobs),
            "queue_done": 0,
            "results": [],
        })

    def worker():
        try:
            for i, job in enumerate(jobs):
                with _task_lock:
                    if _task_state.get("stop_requested"):
                        _task_state["progress"] = "已手动终止"
                        break
                    _task_state["current"] = job["name"]
                    _task_state["progress"] = f"正在处理任务 {i+1}/{len(jobs)}：{job['name']}"
                try:
                    summary = run_strm_job(
                        job["cfg"], job["folder"],
                        fallback_host=job["cfg"].get("base_url") or "")
                    res = {"name": job["name"], "folder": job["folder"], "summary": summary}
                except Exception as exc:
                    res = {"name": job["name"], "folder": job["folder"],
                           "summary": {"errors": [f"{type(exc).__name__}: {exc}"]}}
                with _task_lock:
                    _task_state["results"].append(res)
                    _task_state["queue_done"] = i + 1
                    if _task_state.get("stop_requested"):
                        _task_state["progress"] = "已手动终止"
                        break
        finally:
            with _task_lock:
                _task_state["running"] = False
                _task_state["current"] = None
                if _task_state.get("stop_requested"):
                    _task_state["progress"] = "已手动终止"
                    _task_state["stop_requested"] = False
                else:
                    _task_state["progress"] = "全部完成"

    threading.Thread(target=worker, daemon=True).start()
    return True


# ----------------------------------------------------------------------
# 任务接口（每个任务绑定一个目录）
# ----------------------------------------------------------------------

@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    return jsonify({"ok": True, "tasks": load_tasks()})


@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    folder = data.get("folder") or "/"
    folder_name = (data.get("folder_name") or name).strip()
    if not name:
        return jsonify({"ok": False, "error": "任务名不能为空"}), 400
    if not folder:
        return jsonify({"ok": False, "error": "未选择目录"}), 400
    sync_mode = data.get("sync_mode") or "incremental"
    if sync_mode not in ("incremental", "force"):
        sync_mode = "incremental"
    tasks = load_tasks()
    task = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "folder": folder,
        "folder_name": folder_name,
        "enabled": True,
        "sync_mode": sync_mode,
        "delete_orphans": bool(data.get("delete_orphans", sync_mode == "force")),
        "keep_root": bool(data.get("keep_root", True)),
        "root_name": data.get("root_name") or folder_name,
        "recursive": bool(data.get("recursive", True)),
        "min_size_mb": float(data.get("min_size_mb") or 0),
        "media_ext": data.get("media_ext") or None,
        "copy_ext": data.get("copy_ext") or None,
        "created_at": datetime.now().isoformat(),
    }
    tasks.append(task)
    save_tasks(tasks)
    return jsonify({"ok": True, "task": task})


@app.route("/api/tasks/<task_id>", methods=["PUT"])
def api_update_task(task_id):
    data = request.get_json(force=True) or {}
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            for k in ("name", "folder", "folder_name", "enabled", "sync_mode",
                      "delete_orphans", "keep_root", "root_name", "recursive",
                      "min_size_mb", "media_ext", "copy_ext"):
                if k not in data:
                    continue
                v = data[k]
                if k in ("enabled", "delete_orphans", "keep_root", "recursive"):
                    v = bool(v)
                elif k == "sync_mode":
                    v = v if v in ("incremental", "force") else t.get("sync_mode")
                elif k == "min_size_mb":
                    v = float(v or 0)
                elif k in ("media_ext", "copy_ext") and isinstance(v, str):
                    v = [x.strip().lstrip(".") for x in v.split(",") if x.strip()] or None
                t[k] = v
            save_tasks(tasks)
            return jsonify({"ok": True, "task": t})
    return jsonify({"ok": False, "error": "任务不存在"}), 404


@app.route("/api/tasks/<task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    data = request.get_json(force=True, silent=True) or {}
    tasks = load_tasks()
    target = next((x for x in tasks if x["id"] == task_id), None)
    new = [t for t in tasks if t["id"] != task_id]
    if len(new) == len(tasks):
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    save_tasks(new)
    # 可选：同时清理该任务生成的 strm 目录（只删它自己的那一层壳）
    if bool(data.get("clean_output")) and target:
        cfg = load_config()
        out = cfg.get("output_dir") or "/strm"
        shell = sanitize_name(target.get("root_name") or target.get("folder_name") or "")
        if shell:
            import shutil
            d = os.path.join(out, shell)
            try:
                if os.path.isdir(d):
                    shutil.rmtree(d)
            except OSError:
                pass
    return jsonify({"ok": True})


@app.route("/api/tasks/run", methods=["POST"])
def api_run_all_tasks():
    cfg = load_config()
    tasks = [t for t in load_tasks() if t.get("enabled", True)]
    if not tasks:
        return jsonify({"ok": False, "error": "没有已启用的任务"}), 400
    jobs = [_task_to_job(t, cfg) for t in tasks]
    if not _enqueue_jobs(jobs):
        return jsonify({"ok": False, "error": "已有任务正在运行"}), 409
    return jsonify({"ok": True, "message": f"已启动 {len(jobs)} 个任务"})


@app.route("/api/tasks/<task_id>/run", methods=["POST"])
def api_run_one_task(task_id):
    cfg = load_config()
    tasks = load_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    jobs = [_task_to_job(task, cfg)]
    if not _enqueue_jobs(jobs):
        return jsonify({"ok": False, "error": "已有任务正在运行"}), 409
    return jsonify({"ok": True, "message": "已启动任务"})


@app.route("/api/tasks/stop", methods=["POST"])
def api_stop_tasks():
    """强制终止正在运行的任务：置 stop_requested，并立即中断当前生成器。"""
    with _task_lock:
        if not _task_state["running"]:
            return jsonify({"ok": True, "stopped": False, "message": "当前没有运行中的任务"})
        _task_state["stop_requested"] = True
        gen = _current_gen
    if gen is not None:
        gen.cancel()
    return jsonify({"ok": True, "stopped": True, "message": "已发送终止信号，正在停止当前任务"})


@app.route("/api/strm", methods=["POST"])
def api_strm():
    """兼容旧的一次性生成：构建一个临时 job 走队列（不保存为任务）。"""
    data = request.get_json(force=True) or {}
    cfg = load_config()
    if data.get("base_url"):
        cfg["base_url"] = data["base_url"]
    job_cfg = dict(cfg)
    for k in ("sync_mode", "delete_orphans", "keep_root", "root_name",
              "recursive", "min_size_mb", "media_ext", "copy_ext"):
        if k in data:
            v = data[k]
            if k in ("delete_orphans", "keep_root", "recursive"):
                v = bool(v)
            elif k == "min_size_mb":
                v = float(v or 0)
            job_cfg[k] = v
    folder = data.get("folder", "/")
    job = {"name": "手动生成", "folder": folder, "cfg": job_cfg}
    if not _enqueue_jobs([job]):
        return jsonify({"ok": False, "error": "已有任务正在运行"}), 409
    return jsonify({"ok": True, "message": "任务已启动"})


@app.route("/api/strm/status")
def api_strm_status():
    with _task_lock:
        return jsonify({
            "running": _task_state["running"],
            "progress": _task_state["progress"],
            "current": _task_state.get("current"),
            "queue_total": _task_state.get("queue_total", 0),
            "queue_done": _task_state.get("queue_done", 0),
            "results": _task_state.get("results", []),
            "started_at": _task_state["started_at"],
        })


# ----------------------------------------------------------------------
# 302 直链端点（核心）
# ----------------------------------------------------------------------

def _get_link(client, file_id):
    now = time.time()
    with _cache_lock:
        cached = _link_cache.get(file_id)
        if cached and cached[1] > now:
            return cached[0]
    url = client.get_download_url(file_id)
    with _cache_lock:
        _link_cache[file_id] = (url, now + LINK_TTL)
    return url


def _get_cas_link(client, cfg, file_id, cas_name):
    """
    .cas 文件的播放直链：先秒传还原出临时文件，取直链后延迟删除临时文件。

    结果同样进缓存，避免拖动进度条时反复秒传。
    """
    now = time.time()
    key = "cas:" + file_id
    with _cache_lock:
        cached = _link_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

    restorer = get_restorer(cfg, client)
    url, size, temp_id, real_name = restorer.restore_temp(file_id, cas_name)
    # 临时文件延迟删除，给播放器留出开始缓冲的时间
    restorer.schedule_delete(temp_id)
    remember_temp_dir(cfg, restorer.get_temp_dir())

    with _cache_lock:
        _link_cache[key] = (url, now + LINK_TTL)
    app.logger.info("CAS 还原成功 %s -> %s (%d 字节)", cas_name, real_name, size)
    return url


@app.route("/d/<path:file_id>", methods=["GET", "HEAD"])
def direct_link(file_id):
    """
    Emby/播放器请求这个地址时，换取移动云盘直链并 302 跳转。

    视频流直接从移动云盘 CDN 到播放器，本机只做一次跳转，不中转流量。
    带 ?cas=文件名 时走秒传还原流程。
    """
    cfg = load_config()
    if not cfg.get("authorization"):
        return Response("尚未配置移动云盘 Authorization", status=503)

    cas_name = request.args.get("cas") or ""
    try:
        client = build_client(cfg)
        client.init()
        if cas_name and is_cas_name(cas_name) and cfg.get("cas_enabled", True):
            url = _get_cas_link(client, cfg, file_id, cas_name)
        else:
            url = _get_link(client, file_id)
    except (Yun139Error, CASError) as exc:
        return Response(f"获取直链失败: {exc}", status=502)
    except Exception as exc:
        return Response(f"获取直链失败: {type(exc).__name__}: {exc}", status=502)

    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/link/<path:file_id>")
def api_link(file_id):
    """调试用：查看某个文件的直链，不跳转。"""
    cfg = load_config()
    cas_name = request.args.get("cas") or ""
    try:
        client = build_client(cfg)
        client.init()
        if cas_name and is_cas_name(cas_name):
            url = _get_cas_link(client, cfg, file_id, cas_name)
        else:
            url = _get_link(client, file_id)
        return jsonify({"ok": True, "url": url, "cas": bool(cas_name)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ----------------------------------------------------------------------
# 定时同步调度器（仅用标准库，无额外依赖）
# ----------------------------------------------------------------------

def _compute_next_run(cfg, now):
    """计算下一次定时运行的本地时间（无下次则返回 None）。"""
    from datetime import timedelta
    kind = cfg.get("schedule_kind")
    if kind == "daily":
        try:
            h = int(cfg.get("schedule_hour") or 3)
            m = int(cfg.get("schedule_minute") or 0)
        except (TypeError, ValueError):
            h, m = 3, 0
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        return cand
    if kind == "interval":
        try:
            iv = max(1, int(cfg.get("schedule_interval_hours") or 6))
        except (TypeError, ValueError):
            iv = 6
        last = _schedule_meta.get("last_run_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last) if isinstance(last, str) else last
            except (ValueError, TypeError):
                last_dt = None
            if last_dt:
                cand = last_dt + timedelta(hours=iv)
                return cand if cand > now else now
        return now
    return None


def _run_scheduled():
    """触发一次定时生成；统一占用 _task_state 槽位，避免与手动/任务并发。"""
    if not _claim_running():
        return
    try:
        cfg = load_config()
        job_cfg = dict(cfg)
        job_cfg["sync_mode"] = cfg.get("schedule_sync_mode") or "incremental"
        force = job_cfg["sync_mode"] == "force"
        job_cfg["delete_orphans"] = bool(cfg.get("schedule_delete_orphans", force))
        job_cfg["keep_root"] = cfg.get("keep_root", True)
        job_cfg["root_name"] = (cfg.get("schedule_root_name") or None) if job_cfg["keep_root"] else None
        folder = cfg.get("schedule_folder") or "/"
        summary = run_strm_job(job_cfg, folder, force=force,
                               fallback_host=cfg.get("base_url") or "")
        with _task_lock:
            _schedule_meta["last_run_at"] = datetime.now().isoformat()
            _schedule_meta["last_result"] = summary
        app.logger.info("定时任务完成：%s", summary)
    finally:
        with _task_lock:
            _task_state["running"] = False


def _scheduler_loop():
    """后台线程：每 15 秒检查一次是否到了运行时间。"""
    while True:
        try:
            time.sleep(15)
            cfg = load_config()
            if not cfg.get("schedule_enabled") or cfg.get("schedule_kind") == "off":
                _schedule_meta["next_run_at"] = None
                continue
            now = datetime.now()
            nxt = _compute_next_run(cfg, now)
            _schedule_meta["next_run_at"] = nxt.isoformat() if nxt else None
            if nxt and now >= nxt:
                _run_scheduled()
        except Exception as exc:
            app.logger.warning("定时调度器异常: %s", exc)


# ----------------------------------------------------------------------
# CAS 临时文件管理
# ----------------------------------------------------------------------

@app.route("/api/cas/status")
def api_cas_status():
    """查看当前待清理的临时文件数量与配置。"""
    cfg = load_config()
    pending = sum(r.pending_count() for r in _restorers.values())
    return jsonify({
        "ok": True,
        "enabled": bool(cfg.get("cas_enabled", True)),
        "temp_ttl": cfg.get("cas_temp_ttl", 300),
        "allow_all_ext": bool(cfg.get("cas_allow_all_ext", False)),
        "pending_cleanup": pending,
    })


@app.route("/api/cas/purge", methods=["POST"])
def api_cas_purge():
    """立即清空临时目录里的残留文件（用于手工恢复云盘原状）。"""
    cfg = load_config()
    data = request.get_json(force=True, silent=True) or {}
    max_age = data.get("max_age")
    try:
        client = build_client(cfg)
        client.init()
        restorer = get_restorer(cfg, client)
        count = restorer.purge_temp_dir(None if max_age is None else float(max_age))
        return jsonify({"ok": True, "deleted": count})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ----------------------------------------------------------------------
# 定时同步接口
# ----------------------------------------------------------------------

@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    cfg = load_config()
    enabled = bool(cfg.get("schedule_enabled")) and cfg.get("schedule_kind") not in (None, "off")
    nxt = _compute_next_run(cfg, datetime.now()) if enabled else None
    return jsonify({
        "ok": True,
        "enabled": bool(cfg.get("schedule_enabled")),
        "kind": cfg.get("schedule_kind", "off"),
        "hour": cfg.get("schedule_hour", 3),
        "minute": cfg.get("schedule_minute", 0),
        "interval_hours": cfg.get("schedule_interval_hours", 6),
        "folder": cfg.get("schedule_folder", "/"),
        "sync_mode": cfg.get("schedule_sync_mode", "incremental"),
        "keep_root": cfg.get("keep_root", True),
        "schedule_root_name": cfg.get("schedule_root_name", ""),
        "last_run_at": _schedule_meta.get("last_run_at"),
        "last_result": _schedule_meta.get("last_result"),
        "next_run_at": nxt.isoformat() if nxt else None,
        "running": _task_state["running"],
    })


@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    cfg = load_config()
    data = request.get_json(force=True) or {}
    for key in ("schedule_enabled", "schedule_kind", "schedule_hour",
                "schedule_minute", "schedule_interval_hours",
                "schedule_folder", "schedule_sync_mode", "schedule_root_name"):
        if key not in data:
            continue
        val = data[key]
        if key == "schedule_enabled":
            val = bool(val)
        elif key == "schedule_kind":
            val = val if val in ("off", "daily", "interval") else "off"
        elif key == "schedule_sync_mode":
            val = val if val in ("incremental", "force") else "incremental"
        elif key == "schedule_root_name":
            val = str(val or "")
        elif key in ("schedule_hour", "schedule_minute", "schedule_interval_hours"):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        cfg[key] = val
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/schedule/now", methods=["POST"])
def api_run_schedule_now():
    with _task_lock:
        if _task_state["running"]:
            return jsonify({"ok": False, "error": "已有任务正在运行"}), 409
    threading.Thread(target=_run_scheduled, daemon=True).start()
    return jsonify({"ok": True, "message": "定时任务已启动"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8025))
    threading.Thread(target=_scheduler_loop, name="139strm-scheduler",
                     daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True)
