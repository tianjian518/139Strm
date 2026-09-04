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

# CAS 直链指向的是「秒传还原出来的临时文件」，缓存不能比直链自己的寿命还长。
# 这里只设上限与兜底，真正的缓存时长取直链 t= 参数（见 _link_expire）；
# 缓存失效时优先复用已还原的临时文件重新取链，只有文件没了才重新秒传。
CAS_LINK_MAX_TTL = 15 * 60
# 直链缓存失效后，临时文件再多留一会儿（防止最后一波 Range 请求打空）
CAS_TEMP_GRACE = 120
# 缓存期内多久探一次直链是否还活着（秒）。设这个是为了接住「用户手动清空了
# 临时目录」这类情况：不探的话缓存没到期，播放器会一直拿到指向已删文件的死链。
# 节流 + 只有明确 4xx 才判失效，所以不会因为 CDN 不支持探测而反复还原。
CAS_PROBE_INTERVAL = 60
_probe_at = {}

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
    # ---- STRM 默认设置（任务未指定时使用） ----
    "media_ext": DEFAULT_MEDIA_EXT,
    "copy_ext": DEFAULT_COPY_EXT,
    "min_size_mb": 0,
    "url_encode": True,        # strm 内 URL 是否编码（兼容中文路径）
    # ---- CAS 秒传还原 ----
    "cas_enabled": True,       # 是否对 .cas 文件做秒传还原播放
    "cas_temp_ttl": 300,       # 还原出的临时文件保留秒数后自动删除
    "cas_allow_all_ext": False,  # False=只还原视频；True=任何后缀都还原
    "cas_temp_dir_id": "",     # 记住临时目录 ID，避免重启后重复创建
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
            if key == "url_encode":
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

def _human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


@app.route("/api/list")
def api_list():
    """
    列目录。folder 传的是移动云盘的「目录 ID」，不传（或传 / 空）表示根目录。
    子目录往下钻时，前端把上一级的 file_id 原样回传即可。
    """
    cfg = load_config()
    folder = (request.args.get("folder") or "/").strip() or "/"
    try:
        client = build_client(cfg)
        client.init()
        is_root = folder in ("", "/") or folder == client.root_folder_id
        items = client.list_files(folder)
        # 目录排前面，同类型按名称排序，方便找
        items.sort(key=lambda x: (not x.is_folder, x.name.lower()))
        out = []
        for it in items:
            d = it.to_dict()
            d["size_human"] = "" if it.is_folder else _human_size(it.size)
            out.append(d)
        return jsonify({
            "ok": True,
            "folder": folder,
            "is_root": is_root,
            "items": out,
        })
    except Yun139Error as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ----------------------------------------------------------------------
# STRM 生成
# ----------------------------------------------------------------------

def run_strm_job(cfg, folder, target_subdir="", force=None, fallback_host=""):
    """
    真正执行一次 STRM 生成（手动 / 任务 / 调度都会走到这里）。

    所有生成选项都从 cfg 读取（调用方负责把全局配置与任务选项合并好）。
    target_subdir：输出子目录名（任务名 sanitize 后的壳目录），顶层调用传
    "" 时 strs 直接落在 output_dir 下；非空时 strs 落在 output_dir/<target_subdir>/ 下。
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
        gen = StrmGenerator(
            client=client, base_url=base_url,
            output_dir=cfg.get("output_dir") or "/strm",
            media_ext=cfg.get("media_ext"), copy_ext=cfg.get("copy_ext"),
            min_size_mb=float(cfg.get("min_size_mb", 0) or 0),
            include_cas=bool(cfg.get("cas_enabled", True)),
            force=bool(force),
            delete_orphans=delete_orphans,
        )
        global _current_gen
        with _task_lock:
            _current_gen = gen
        try:
            gen.generate(folder, target_subdir=target_subdir)
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
    """把一个任务合并进全局配置，得到一次生成用的 job。"""
    job_cfg = dict(cfg)
    # 任务级字段：未提供则沿用全局配置
    for k, cast in (
        ("sync_mode", lambda v: v if v in ("incremental", "force") else None),
        ("delete_orphans", lambda v: bool(v)),
        ("media_ext", lambda v: v if v else None),
        ("copy_ext", lambda v: v if v else None),
        ("min_size_mb", lambda v: float(v) if v not in (None, "") else None),
    ):
        v = task.get(k)
        if v is not None and v != "":
            cv = cast(v) if k != "sync_mode" else v
            if cv is not None:
                job_cfg[k] = cv
    # 输出子目录 = 任务名 sanitize（套壳天然开启；想要去掉套壳就把 task.cron_empty=False 也行
    # 但与 Smart 心智一致，强制套壳，零配置）
    sub = sanitize_name(task.get("name") or "")
    return {"name": task.get("name", "任务"), "folder": task.get("folder", "/"),
            "subdir": sub, "cfg": job_cfg}


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
                        target_subdir=job.get("subdir", ""),
                        fallback_host=job["cfg"].get("base_url") or "")
                    res = {"name": job["name"], "folder": job["folder"],
                           "task_id": job.get("task_id"), "summary": summary}
                except Exception as exc:
                    res = {"name": job["name"], "folder": job["folder"],
                           "task_id": job.get("task_id"),
                           "summary": {"errors": [f"{type(exc).__name__}: {exc}"]}}
                with _task_lock:
                    _task_state["results"].append(res)
                    _task_state["queue_done"] = i + 1
                    if _task_state.get("stop_requested"):
                        _task_state["progress"] = "已手动终止"
                        break
                # 把这次运行结果写回对应任务记录（last_run_at / last_summary / 下次运行）
                if job.get("task_id"):
                    try:
                        ts = load_tasks()
                        now = datetime.now()
                        for tt in ts:
                            if tt["id"] == job["task_id"]:
                                tt["last_run_at"] = now.isoformat()
                                tt["last_summary"] = res["summary"]
                                if tt.get("cron"):
                                    nxt = _next_run_from_cron(tt["cron"], now)
                                    tt["next_run_at"] = nxt.isoformat() if nxt else None
                                break
                        save_tasks(ts)
                    except Exception:
                        pass
        finally:
            with _task_lock:
                _task_state["running"] = False
                _task_state["current"] = None
                if _task_state.get("stop_requested"):
                    _task_state["progress"] = "已手动终止"
                    _task_state["stop_requested"] = False
                else:
                    failed = sum(1 for r in _task_state["results"]
                                 if (r.get("summary") or {}).get("errors"))
                    if failed:
                        _task_state["progress"] = (
                            f"跑完 {len(_task_state['results'])} 个任务，"
                            f"其中 {failed} 个报错（点 📋 看日志）")
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
    if not name:
        return jsonify({"ok": False, "error": "任务名不能为空"}), 400
    if not folder:
        return jsonify({"ok": False, "error": "未选择目录"}), 400
    sync_mode = data.get("sync_mode") or "incremental"
    if sync_mode not in ("incremental", "force"):
        sync_mode = "incremental"
    cron = (data.get("cron") or "").strip()
    if cron and not _is_valid_cron(cron):
        return jsonify({"ok": False, "error": f"无效的 crontab 表达式: {cron}"}), 400
    tasks = load_tasks()
    task = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        # folder 存的是移动云盘的「目录 ID」（list_files 拿它当 parentFileId 用），
        # folder_path 只是给人看的路径文字，不参与列目录。
        "folder": folder,
        "folder_path": (data.get("folder_path") or "").strip(),
        "enabled": True if data.get("enabled") is None else bool(data.get("enabled")),
        "cron": cron,
        "sync_mode": sync_mode,
        "delete_orphans": bool(data.get("delete_orphans", sync_mode == "force")),
        "media_ext": data.get("media_ext") or None,
        "copy_ext": data.get("copy_ext") or None,
        "min_size_mb": (float(data.get("min_size_mb")) if data.get("min_size_mb") not in (None, "") else None),
        "last_run_at": None,
        "last_summary": None,
        "next_run_at": _next_run_from_cron(cron, datetime.now()).isoformat() if cron else None,
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
            for k in ("name", "folder", "folder_path", "enabled", "cron", "sync_mode",
                      "delete_orphans", "media_ext", "copy_ext", "min_size_mb"):
                if k not in data:
                    continue
                v = data[k]
                if k == "cron":
                    v = str(v or "").strip()
                    if v and not _is_valid_cron(v):
                        return jsonify({"ok": False, "error": f"无效的 crontab 表达式: {v}"}), 400
                    t["cron"] = v
                    t["next_run_at"] = _next_run_from_cron(v, datetime.now()).isoformat() if v else None
                elif k == "enabled":
                    t["enabled"] = bool(v)
                elif k == "delete_orphans":
                    t["delete_orphans"] = bool(v)
                elif k == "sync_mode":
                    if v in ("incremental", "force"):
                        t["sync_mode"] = v
                elif k in ("media_ext", "copy_ext"):
                    if isinstance(v, str):
                        v = [x.strip().lstrip(".") for x in v.split(",") if x.strip()] or None
                    t[k] = v or None
                elif k == "min_size_mb":
                    t["min_size_mb"] = (float(v) if v not in (None, "") else None)
                else:
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
        shell = sanitize_name(target.get("name") or "")
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
    jobs = []
    for t in tasks:
        job = _task_to_job(t, cfg)
        job["task_id"] = t["id"]
        jobs.append(job)
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
    job = _task_to_job(task, cfg)
    job["task_id"] = task["id"]
    if not _enqueue_jobs([job]):
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
    """兼容旧的一次性生成：构造一个临时 job 走队列（不保存为任务）。
    v2.1 起建议直接用 /api/tasks/<id>/run。"""
    data = request.get_json(force=True) or {}
    cfg = load_config()
    if data.get("base_url"):
        cfg["base_url"] = data["base_url"]
    job_cfg = dict(cfg)
    for k in ("sync_mode", "delete_orphans", "media_ext", "copy_ext", "min_size_mb"):
        if k in data:
            v = data[k]
            if k == "delete_orphans":
                v = bool(v)
            elif k == "min_size_mb":
                v = float(v) if v not in (None, "") else None
            elif k in ("media_ext", "copy_ext") and isinstance(v, str):
                v = [x.strip().lstrip(".") for x in v.split(",") if x.strip()] or None
            job_cfg[k] = v
    folder = data.get("folder", "/")
    # 一次性生成也用「子目录壳」保持一致性：取 folder 末段 sanitize
    sub = sanitize_name(folder.rstrip("/").split("/")[-1] or "manual")
    job = {"name": "手动生成", "folder": folder, "subdir": sub, "cfg": job_cfg}
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

def _link_expire(url, default_ttl, max_ttl=None):
    """按直链自己的过期时间来决定缓存多久。

    移动云盘的直链带 t= 过期时间戳，实测只有约 15 分钟。
    以前一律缓存 2 小时，结果直链早过期了缓存还有效 —— 表现就是
    「播过的视频，过一会儿再播就一直加载」。
    这里直接读 t，留 60 秒余量；读不到才退回默认时长。

    t 有可能是毫秒时间戳（那会被当成 5 万年后的过期时间，
    缓存永不失效），所以要先做量级校验，超出合理区间就退回默认值。
    """
    now = time.time()
    fallback = now + default_ttl
    if max_ttl:
        fallback = min(fallback, now + max_ttl)
    try:
        from urllib.parse import urlparse, parse_qs
        t = int(parse_qs(urlparse(url).query).get("t", ["0"])[0])
        if t > 0:
            if t > 1e11:          # 量级明显是毫秒
                t = t / 1000.0
            # 只接受「一天之内」的过期时间，超出说明这个 t 不是我们想的那个 t
            if now - 86400 < t < now + 86400:
                expire = t - 60
                if max_ttl:
                    expire = min(expire, now + max_ttl)
                # 直链已经（快）过期时也要至少缓存十几秒，否则每次请求都会
                # 重新换链，播放器一密集请求就把接口打爆了
                return max(expire, now + 15)
    except Exception:
        pass
    return fallback


def _cas_link_dead(key, url):
    """
    缓存里的直链是不是已经废了（节流探测，同一个片子最多 60 秒探一次）。

    只在**明确拿到 4xx** 时才判死 —— 网络异常、超时、5xx 一律当作还活着，
    绝不能让探测抖动变成「不停重新秒传还原」。
    """
    now = time.time()
    if now - _probe_at.get(key, 0) < CAS_PROBE_INTERVAL:
        return False
    _probe_at[key] = now
    try:
        import requests
        # 只取 1 个字节，且用 stream 避免把整个视频拉回来
        resp = requests.get(url, headers={"Range": "bytes=0-0"},
                            timeout=5, stream=True)
        code = resp.status_code
        resp.close()
    except Exception:
        return False          # 探不到就不敢断定，继续用
    return 400 <= code < 500


def _cache_put(key, value):
    """写入直链缓存，顺手清掉已过期的条目（缓存表不能无限长）。"""
    with _cache_lock:
        _link_cache[key] = value
        if len(_link_cache) > 500:
            now = time.time()
            for k in [k for k, v in _link_cache.items() if v[1] <= now]:
                _link_cache.pop(k, None)


def _get_link(client, file_id):
    now = time.time()
    with _cache_lock:
        cached = _link_cache.get(file_id)
        if cached and cached[1] > now:
            return cached[0]
    url = client.get_download_url(file_id)
    _cache_put(file_id, (url, _link_expire(url, LINK_TTL)))
    return url


def _get_cas_link(client, cfg, file_id, cas_name):
    """
    .cas 文件的播放直链：秒传还原出临时文件，再把临时文件的直链 302 给播放器。

    这里最容易踩的坑是「反复还原」：播放过程中播放器会不断回来换直链，
    如果每次换链都重新秒传还原一个新文件，而旧文件要等 TTL 到点才删，
    同一部电影就会在临时目录里同时躺着好几份 —— 一部 4K 片子几十分钟就能
    堆出几十 GB。所以规则是：

      * 缓存期内：直接复用上一条直链，只给临时文件续期；
      * 缓存过期：优先复用**已还原的那一个**临时文件，重新取一条直链
        （不产生新文件）；只有临时文件确实没了才重新秒传还原，
        而且还原前会先清掉这部片子的旧副本；
      * 临时文件的删除时间跟着缓存走（缓存失效后再留 CAS_TEMP_GRACE 秒），
        既不会中途被删导致播放中断，也不会长期滞留。
    """
    now = time.time()
    key = "cas:" + file_id
    restorer = get_restorer(cfg, client)

    with _cache_lock:
        cached = _link_cache.get(key)
    if cached and len(cached) >= 3 and cached[1] > now:
        url, expire, temp_id = cached[0], cached[1], cached[2]
        if not _cas_link_dead(key, url):
            # 还在有效期内 → 直接复用，并把临时文件的寿命续到缓存失效之后
            restorer.schedule_delete(
                temp_id, delay=max(int(restorer.temp_ttl or 0),
                                   int(expire - now + CAS_TEMP_GRACE)))
            return url
        # 直链已经废了（多半是用户在云盘里手动删了临时文件）→ 重建
        with _cache_lock:
            _link_cache.pop(key, None)
        restorer.forget_session(file_id)
        app.logger.info("缓存的直链已失效（%s），重新还原", cas_name)

    url, size, temp_id, real_name, restored = restorer.fetch_link(file_id, cas_name)
    expire = _link_expire(url, LINK_TTL, CAS_LINK_MAX_TTL)
    # 临时文件必须活过缓存：缓存失效后再宽限 CAS_TEMP_GRACE 秒。
    # 不能只看 cas_temp_ttl —— 直链 15 分钟后才过期，这期间播放器拿着旧直链
    # 继续拉流，文件提前删了就会播到一半断掉。
    restorer.schedule_delete(
        temp_id, delay=max(int(restorer.temp_ttl or 0),
                           int(expire - now + CAS_TEMP_GRACE)))
    if restored:
        remember_temp_dir(cfg, restorer.get_temp_dir())
        app.logger.info("CAS 还原成功 %s -> %s (%d 字节)", cas_name, real_name, size)
    _cache_put(key, (url, expire, temp_id))
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
# Crontab 解析（仅支持 5 字段标准格式：分 时 日 月 周）
# ----------------------------------------------------------------------

def _expand_cron_field(field, lo, hi):
    """把 crontab 的单个字段展开成允许的整数集合。支持 *, a-b, a,b,c, /step。"""
    result = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = max(1, int(step_s))
            except ValueError:
                continue
        else:
            base = part
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            try:
                start = int(a); end = int(b)
            except ValueError:
                continue
        else:
            try:
                v = int(base)
            except ValueError:
                continue
            if step == 1 and "/" not in part:
                result.add(v)
                continue
            start, end = (v, hi) if "-" not in base else (start, end)
        for x in range(int(start), int(end) + 1, step):
            result.add(x)
    return {x for x in result if lo <= x <= hi}


def _is_valid_cron(expr):
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    for i, (lo, hi) in enumerate([(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]):
        if not _expand_cron_field(parts[i], lo, hi):
            return False
    return True


def _parse_cron(expr):
    """解析 5 段 crontab，返回各字段允许值集合；表达式空/无效返回 None。

    星期按标准 crontab 语义：0 和 7 都表示周日。
    """
    if not expr:
        return None
    try:
        parts = expr.strip().split()
        if len(parts) != 5:
            return None
        mins = _expand_cron_field(parts[0], 0, 59)
        hours = _expand_cron_field(parts[1], 0, 23)
        doms = _expand_cron_field(parts[2], 1, 31)
        months = _expand_cron_field(parts[3], 1, 12)
        dows = _expand_cron_field(parts[4], 0, 7)
    except Exception:
        return None
    if not (mins and hours and doms and months and dows):
        return None
    if 7 in dows:
        dows.discard(7)
        dows.add(0)
    return mins, hours, doms, months, dows


def _cron_dow(dt):
    """换算成标准 crontab 的星期值：0=周日, 1=周一 … 6=周六。"""
    return dt.isoweekday() % 7


def _cron_day_match(doms, dows, dt):
    """判断某天是否满足「日 / 星期」两个字段（标准 crontab 语义）。

    - 两个字段都是 *（未限制）→ 每天都算；
    - 只有「日」被限定 → 以「日」为准；
    - 只有「星期」被限定 → 以「星期」为准（否则写「周一」会变成每天都跑）；
    - 两个都被限定 → OR，任一满足即跑。
    """
    dom_restricted = len(doms) < 31
    dow_restricted = len(dows) < 7
    if dom_restricted and dow_restricted:
        return (dt.day in doms) or (_cron_dow(dt) in dows)
    if dom_restricted:
        return dt.day in doms
    if dow_restricted:
        return _cron_dow(dt) in dows
    return True


def _cron_matches(expr, dt):
    """dt（精确到分钟）是否命中 cron 表达式——调度器靠它判断「到点没」。"""
    parsed = _parse_cron(expr)
    if not parsed:
        return False
    mins, hours, doms, months, dows = parsed
    # 月份/星期/日 都要满足，日期部分按标准 crontab 语义交给 _cron_day_match
    return (dt.month in months
            and _cron_day_match(doms, dows, dt)
            and dt.hour in hours
            and dt.minute in mins)


def _next_run_from_cron(expr, now):
    """返回 expr 在 now 之后最近一次触发的 datetime；表达式空/无效返回 None。

    注意：返回的一定是「now 之后」的点，永远大于 now，
    不能拿它跟 now 比大小来判断是否到点（那会导致任务永不触发）。
    """
    parsed = _parse_cron(expr)
    if not parsed:
        return None
    mins, hours, doms, months, dows = parsed
    from datetime import timedelta
    cand = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 366):  # 最多往后扫一年
        if (cand.month in months
                and _cron_day_match(doms, dows, cand)
                and cand.hour in hours
                and cand.minute in mins):
            return cand
        cand += timedelta(minutes=1)
    return None


# ----------------------------------------------------------------------
# 任务调度循环（每 30 秒扫一次，到时间的任务串行入队执行）
# ----------------------------------------------------------------------

_SCHEDULER_TICK_SEC = 30
_last_trigger_key = {}  # task_id -> 上次触发的 cron 时间，避免同分钟内重复触发


def _task_scheduler_loop():
    while True:
        try:
            time.sleep(_SCHEDULER_TICK_SEC)
            cfg = load_config()
            tasks = load_tasks()
            now = datetime.now()
            # 关键：拿「当前这一分钟」去匹配 cron，而不是比较 _next_run_from_cron()。
            # 后者返回的是 now「之后」的下一个触发点，永远大于 now，
            # 于是 now >= nxt 永远不成立 —— 表现为界面显示着下次时间、却到点不运行。
            tick = now.replace(second=0, microsecond=0)
            due = []
            for t in tasks:
                if not t.get("enabled", True):
                    continue
                cron = (t.get("cron") or "").strip()
                if not cron:
                    continue
                # 30 秒一个 tick，同一分钟会扫到两次，用这一分钟做去重键
                if _last_trigger_key.get(t["id"]) == tick:
                    continue
                if _cron_matches(cron, tick):
                    due.append(t)
                    _last_trigger_key[t["id"]] = tick
            if not due:
                continue
            # 串行入队（受 _claim_running 约束）
            jobs = [_task_to_job(t, cfg) for t in due]
            _enqueue_jobs(jobs)
        except Exception as exc:
            app.logger.warning("任务调度器异常: %s", exc)


# ----------------------------------------------------------------------
# CAS 临时文件管理
# ----------------------------------------------------------------------

@app.route("/api/cas/status")
def api_cas_status():
    """查看当前待清理的临时文件数量与配置。"""
    cfg = load_config()
    pending = sum(r.pending_count() for r in _restorers.values())
    sessions = sum(r.session_count() for r in _restorers.values())
    result = {
        "ok": True,
        "enabled": bool(cfg.get("cas_enabled", True)),
        "temp_ttl": cfg.get("cas_temp_ttl", 300),
        "allow_all_ext": bool(cfg.get("cas_allow_all_ext", False)),
        "pending_cleanup": pending,
        "active_sessions": sessions,
    }
    # 顺便看一眼临时目录里到底堆了多少东西（查不到就算了，不影响主流程）
    if cfg.get("authorization"):
        try:
            client = build_client(cfg)
            client.init()
            restorer = get_restorer(cfg, client)
            temp_dir = restorer.ensure_temp_dir()
            items = client.personal_list(temp_dir)
            result["temp_files"] = len(items)
            result["temp_bytes"] = sum(i.size for i in items)
        except Exception as exc:
            result["temp_dir_error"] = str(exc)
    return jsonify(result)


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
# （v2.1 已移除全局定时配置，改为每个任务自带 crontab 字段。历史接口保留为
#  兼容旧版 Web UI 的 410 Gone 提示，避免旧前端误调。）


def _dir_is_mount(path):
    """判断目录是否是独立挂载点（挂了卷才会是）。读不到 /proc/mounts 返回 None（未知）。"""
    if not path:
        return None
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    try:
        with open("/proc/mounts", encoding="utf-8") as fp:
            for line in fp:
                cols = line.split()
                if len(cols) >= 2:
                    try:
                        if os.path.realpath(cols[1]) == target:
                            return True
                    except OSError:
                        continue
    except OSError:
        return None
    return False


@app.route("/api/storage")
def api_storage():
    """数据落在哪、有没有挂载卷。

    没挂载卷时，配置 / 任务 / strm 全写在容器自己的可写层里，
    容器一删除重建（比如升级镜像）就全没了，所以这里给出显式提示。
    """
    cfg = load_config()
    cfg_dir = os.path.dirname(CONFIG_PATH) or "."
    out_dir = cfg.get("output_dir") or ""
    cfg_mounted = _dir_is_mount(cfg_dir)
    out_mounted = _dir_is_mount(out_dir)
    # 配置目录必须确认挂了；输出目录没配置时按"不扣分"处理
    persisted = bool(cfg_mounted) and (out_mounted is not False)
    return jsonify({
        "ok": True,
        "config_dir": cfg_dir,
        "config_path": CONFIG_PATH,
        "tasks_path": get_tasks_path(),
        "output_dir": out_dir,
        "config_mounted": cfg_mounted,     # True / False / None(未知)
        "output_mounted": out_mounted,
        "persisted": persisted,
        "config_exists": os.path.exists(CONFIG_PATH),
        "tasks_exists": os.path.exists(get_tasks_path()),
    })


@app.route("/api/schedule", methods=["GET", "POST"])
def api_schedule_removed():
    return jsonify({
        "ok": False,
        "error": "全局定时配置已移除（v2.1），请在「任务管理」里给单个任务设置 crontab",
    }), 410


@app.route("/api/schedule/now", methods=["POST"])
def api_schedule_now_removed():
    return jsonify({
        "ok": False,
        "error": "全局定时配置已移除（v2.1），请直接「运行」某个任务",
    }), 410


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8025))
    threading.Thread(target=_task_scheduler_loop, name="139strm-task-scheduler",
                     daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True)
