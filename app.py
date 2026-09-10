# -*- coding: utf-8 -*-
"""
139Strm —— 移动云盘（139）STRM 生成器与 302 直链服务

设计要点：
  * 只做移动云盘，不依赖 OpenList，不需要境外中转。
  * 生成的 .strm 内容指向本机 /d/<file_id>，
    播放时服务端换取移动云盘直链后 302 跳转，视频流不经过本机。
"""

import collections
import json
import os
import re
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
#
# 关键：这个值必须**远小于**直链寿命（移动云盘实测约 15 分钟）。
# 302 直连下播放器 follow 之后就钉死在拿到的那条链上，服务端无从补救，
# 所以交出去的链必须留足余量。以前设成 15 分钟（≈直链寿命），
# 结果缓存快到期时返回的是一条只剩一两分钟的链 —— 表现就是
# 「播一会儿又卡 / 续播一直加载」。现在固定 4 分钟，
# 保证任何一次请求拿到的链都还有 11 分钟以上可用。
CAS_LINK_MAX_TTL = 4 * 60
# 直链缓存失效后，临时文件再多留一会儿（防止最后一波 Range 请求打空）
CAS_TEMP_GRACE = 120
# 【v2.2.10 已废除】CAS_PROBE_INTERVAL 曾经是「缓存期内最多 60 秒探一次」的
# 节流。实测下来这个节流得不偿失：省下的那点探测开销，代价是这 60 秒窗口里
# 链坏了照样往外发，而 302 直连下播放器一旦拿到链就钉死、服务端事后无法补救。
# 现在改成**每次交链都真探**，一次只要 0.2 秒。留着这个名字只为让老配置和
# 外部脚本不至于报错，代码里不再使用。
CAS_PROBE_INTERVAL = 0
_probe_at = {}
# 「说不清」名单：某条链上一次探测拿到了 200 却没交待任何长度信息。
# 一次可能是 CDN 正在给刚还原的文件回源，连续两次才敢判死。
_probe_suspect = {}
# 直链交到播放器之前先自己拉 1 个字节验一次。
# 注意：这里的语义不只是「验活」，更是**替播放器把 CDN 回源这趟等完** ——
# 秒传还原出来的文件在 CDN 上是冷的，1GB+ 的文件回源常常要好几秒，
# 播放器第一脚踩下去就得干等，等不及就是「一直加载中」；
# 而用户「返回播放」时回源正好完成，于是秒开 —— 这就是那个
# 「时好时坏、什么间隔都可能撞上」的偶发问题。
# 宁可开播慢两三秒，也别把一条还没就绪的链交给播放器。
CAS_LINK_VERIFY_TIMEOUT = 2      # 第一次探测超时（秒）
CAS_LINK_VERIFY_RETRY_TIMEOUT = 1.5  # 复探超时（秒）
CAS_VERIFY_BACKOFF = 1.2         # 探不清时退避多久再探一次

# ----------------------------------------------------------------------
# 探测熔断：防止「探不到 → 判链死刑 → 重建」演变成还原风暴
# ----------------------------------------------------------------------
# 【v2.2.10 关键修复】这里的阈值从 8 降到 3，而且改成数「新链被判坏」。
#
# 事情是这样的：交链前探测直链，是 v2.2.3 才加进来的。v2.2.2 及以前
# 交链前**完全不探测**，而那一版在生产上跑了很久、从没出过问题。
#
# 为什么探测会变成灾难：探测是**服务器自己**发起的，而服务器在甲骨文
# （海外），要跨国际线路去访问移动云盘的国内 CDN。国内家庭宽带/本地
# 虚拟机探一次 0.2 秒，海外服务器却可能很慢、甚至被拒。
#
# 而 v2.2.3 之后把「探测没探成」也当成了「链坏了」，于是：
#   每个播放请求 → 探测失败 → 判定链坏 → 删掉刚还原的文件、重新秒传
#   → 再探测又失败…… 一轮好几秒到十几秒，播放器一直转圈。
# 等用户「返回重播」，状态热了、或者熔断生效了，就又能播 —— 完全对上
# 「续播一直加载中、返回重播才行」。
#
# 铁律：**刚还原出来的新链不可能是坏的**（文件刚建、签名刚签发）。
# 连着几条新链都被判坏，那问题一定在探测方，不在链。
PROBE_BREAKER_THRESHOLD = 3      # 连续这么多次「新链被判坏」就熔断
PROBE_BREAKER_SECONDS = 600      # 熔断持续多久
_probe_fail_streak = 0           # 连续「根本没探成」（超时/网络异常）
_dead_streak = 0                 # 连续「刚还原的新链被判坏」
_probe_fail_until = 0
# 「删掉重还原」的补救多久之内不重复做（秒）。万一 CDN 压根不接受 Range
# 探测（每条链都回 4xx），没有这个冷却就会每次换链都白搭一次秒传，
# 换集直接慢一倍 —— 真遇到这种情况，认赔一次比一直赔划算。
CAS_VERIFY_RETRY_COOLDOWN = 600
_verify_retried = {}

# 一次 Range 探测的结论
PROBE_ALIVE = "alive"       # 老实给了字节/长度信息
PROBE_DEAD = "dead"         # 明确 4xx —— 这条链已经废了
PROBE_VAGUE = "vague"       # 应答拿到了，可 200 却没交待长度 —— 说不清
PROBE_UNKNOWN = "unknown"   # 探测自己没探成（超时 / 异常 / 5xx / 3xx）

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
    # ---- 播放方式 ----
    # False=302 直连（默认，推荐）：视频流量从移动云盘 CDN 直达播放器，
    #      不经过部署服务器 —— 云服务器（甲骨文等）务必保持这个设置，
    #      否则每个视频都会吃掉服务器的上行流量配额。
    # True=代理转发：视频流经服务器中转，直链过期可随时换，能消灭
    #      「播到一半 / 续播一直加载中」，但流量全部走服务器带宽。
    #      只适合服务器就在家里（局域网播放无带宽顾虑）的场景。
    "play_proxy": False,
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


# 已初始化的云盘 client 按「账号配置」缓存。
#
# 原来 direct_link 每个请求都 build_client + init，而 init 里有一次
# qryRoutePolicy 接口往返，还要新建 TCP/TLS 会话。换集点播这种冷启动
# 请求（init + 秒传 + 取直链 + 清扫）要串行 4~5 次接口，播放器等不到
# 就「点下一集一直加载中，返回重播秒开」（重播时还原缓存已就绪）。
# 缓存 client 后命中请求 0 次 init 往返，连接也可复用。
_clients = {}
_clients_lock = threading.Lock()
_CLIENT_TTL = 3600          # 定期重建，给 refresh_token 留出续期机会


def _client_key(cfg):
    return "|".join(str(cfg.get(k, "")) for k in
                    ("authorization", "cloud_type", "cloud_id", "username"))


def get_client(cfg):
    """取缓存好的 client；没有就用当前配置新建并 init。

    cfg 变化（换号/改配置）会得到不同的 key，自动各建各的；
    init 失败不会缓存，下次请求重新 init。
    """
    key = _client_key(cfg)
    now = time.time()
    with _clients_lock:
        entry = _clients.get(key)
        if entry and now - entry[1] < _CLIENT_TTL:
            return entry[0]
    client = build_client(cfg)
    client.init()
    with _clients_lock:
        _clients[key] = (client, time.time())
        for k in [k for k in _clients if k != key]:   # 只留当前账号
            _clients.pop(k, None)
    return client


def drop_client(cfg):
    """丢掉缓存的 client（凭据失效 / 接口异常时重建用）。"""
    with _clients_lock:
        _clients.pop(_client_key(cfg), None)


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

def _amz_deadline(url):
    """从预签名直链里读出**真正的**过期时刻（unix 秒）；读不到返回 None。

    实测（v2.2.10）：139 给的是对象存储的预签名 URL，形如
        https://<bucket>.eos.<region>.cmecloud.cn/<obj>
            ?X-Amz-Algorithm=AWS4-HMAC-SHA256
            &X-Amz-Date=20260910T012236Z      ← 签发时刻（UTC）
            &X-Amz-Expires=900                 ← 有效期秒数
            &X-Amz-Signature=...
    过期时刻 = X-Amz-Date + X-Amz-Expires，实测稳定 900 秒。
    """
    try:
        from urllib.parse import urlparse, parse_qs
        import calendar
        q = parse_qs(urlparse(url).query)
        date_s = (q.get("X-Amz-Date") or [""])[0]
        exp_s = (q.get("X-Amz-Expires") or [""])[0]
        if date_s and exp_s:
            signed_at = calendar.timegm(
                time.strptime(date_s, "%Y%m%dT%H%M%SZ"))
            return signed_at + int(exp_s)
    except Exception:
        pass
    return None


def _link_expire(url, default_ttl, max_ttl=None):
    """按直链自己的过期时间来决定缓存多久。

    【v2.2.10 更正】以前这里读的是 URL 里的 `t` 参数，注释里写着
    「t 是过期时间戳」。实际上 `t` 恒等于 2 —— 那是个标志位，不是
    时间戳。于是 `now - 86400 < 2 < now + 86400` 永远不成立，判断
    永远落空，**永远走兜底值**。连带后果是诊断页里显示的「直链余命」
    也永远是那个兜底数字（239 秒），等于一直是瞎的。

    现在改成读预签名 URL 里真正的寿命（X-Amz-Date + X-Amz-Expires），
    并保留原来的 t 逻辑作为兼容分支。
    """
    now = time.time()
    fallback = now + default_ttl
    if max_ttl:
        fallback = min(fallback, now + max_ttl)

    # 1) 预签名 URL：读真正的过期时刻
    deadline = _amz_deadline(url)
    if deadline and now - 86400 < deadline < now + 86400:
        expire = deadline - 60          # 留 60 秒余量，不把「快死的链」交出去
        if max_ttl:
            expire = min(expire, now + max_ttl)
        return max(expire, now + 15)

    # 2) 兼容老式带 t= 时间戳的直链
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
                return max(expire, now + 15)
    except Exception:
        pass
    return fallback


def _note_probe_fail():
    """探测「根本没探成」（超时/网络异常）记一笔；连续太多次就熔断。"""
    global _probe_fail_streak, _probe_fail_until
    _probe_fail_streak += 1
    if _probe_fail_streak >= PROBE_BREAKER_THRESHOLD * 2:
        _probe_fail_until = time.time() + PROBE_BREAKER_SECONDS
        _probe_fail_streak = 0
        app.logger.warning(
            "连续多次探测都没探成，判定为「探测不可用」并熔断 %d 秒 —— "
            "这段时间内不再因为探不到而判链死刑",
            PROBE_BREAKER_SECONDS)


def _note_probe_ok():
    global _probe_fail_streak, _dead_streak
    _probe_fail_streak = 0
    _dead_streak = 0


def _note_fresh_dead(cas_name=""):
    """刚还原出来的新链被判坏了 —— 数一数，连着几次就熔断。

    铁律：刚还原出来的链**不可能是坏的**。连着几条都被判坏，那问题在
    探测方（服务器到 CDN 的路），不在链。这时候还照「判坏 → 删掉重还原」
    办，就是每来一个请求还原一份 —— 播放器一路转圈。

    返回 True 表示熔断刚刚触发（调用方应当放弃这次重建）。
    """
    global _dead_streak, _probe_fail_until
    _dead_streak += 1
    if _dead_streak >= PROBE_BREAKER_THRESHOLD:
        _probe_fail_until = time.time() + PROBE_BREAKER_SECONDS
        _dead_streak = 0
        app.logger.warning(
            "连续 %d 条**刚还原出来的**新链都被判成坏链 —— 这不可能是链的问题，"
            "是这台服务器看不到 CDN。判定探测不可用，熔断 %d 秒，"
            "期间照常把链接交给播放器（最后一条：%s）",
            PROBE_BREAKER_THRESHOLD, PROBE_BREAKER_SECONDS, cas_name)
        return True
    return False


def _probe_usable():
    """探测这件事本身还靠不靠得住（熔断器没跳闸）。"""
    return time.time() >= _probe_fail_until


def _probe_status():
    """给自检接口用：探测功能现在是什么状态。"""
    left = int(_probe_fail_until - time.time())
    return {"usable": _probe_usable(),
            "breaker_left": max(left, 0),
            "fail_streak": _probe_fail_streak,
            "dead_streak": _dead_streak}


def _probe_link(url, timeout=5):
    """
    对直链发一次 Range: bytes=0-0，看它还活不活。

    只取 1 个字节，且用 stream 避免把整个视频拉回来。

    「探测自己没探成」（超时、网络异常）和「应答拿到了却说不清」
    是两回事，必须分开：前者是**探测侧**的问题（服务器到 CDN 的路不通），
    后者才是**链侧**的问题。这个区分是防还原风暴的关键。
    """
    try:
        import requests
        resp = requests.get(url, headers={"Range": "bytes=0-0"},
                            timeout=timeout, stream=True)
    except Exception:
        _note_probe_fail()
        return PROBE_UNKNOWN
    _note_probe_ok()
    code = resp.status_code
    if 400 <= code < 500:
        resp.close()
        return PROBE_DEAD
    if 200 <= code < 300:
        length = (resp.headers.get("Content-Length") or "").strip()
        content_range = (resp.headers.get("Content-Range") or "").strip()
        chunked = "chunked" in (resp.headers.get("Transfer-Encoding")
                                or "").lower()
        resp.close()
        if code == 206:
            return PROBE_ALIVE          # 老老实实给了 1 个字节
        if chunked:
            return PROBE_ALIVE          # chunked 编码没有 Content-Length 是正常的
        if content_range or length not in ("", "0"):
            return PROBE_ALIVE          # 200 但诚实交待了总长度
        # 200 却没有任何长度信息：要么 CDN 正在给刚还原的文件回源（正常），
        # 要么是在对一个已经废掉的文件敷衍（v2.1.13 抓的假活）→ 说不清
        return PROBE_VAGUE
    resp.close()
    return PROBE_UNKNOWN                # 3xx / 5xx：探测没探成


def _cas_link_dead(key, url):
    """
    缓存里的这条直链**现在**还能不能真的取到数据。

    【v2.2.10 重要更正】以前这里有两个要命的设计：
      1. 60 秒节流：同一个片子最多 60 秒探一次。省下的那点时间，
         代价是这 60 秒窗口里链坏了照样往外发 —— 302 直连下播放器
         一旦拿到链就钉死，服务端事后无法补救。
      2. 「探不清就判活」：探测超时/5xx/3xx 一律当活链交出去。
         可「探不清」恰恰就是播放器**一直转圈**的那种状态 ——
         链是挂死的，不是干脆报错的。把它当活链交出去，等于把
         播放器钉在一条死链上，用户只能退出重播。
    现在改成：每次交链都真探，且**必须探到「活着」才算活**。
    实测一次探测只要 0.2 秒，代价远小于让用户对着转圈等。
    """
    _probe_at[key] = time.time()
    if _link_ready(url):
        _probe_suspect.pop(key, None)
        return False
    return True


def _link_ready(url, tries=1):
    """这条链现在能不能真的取到数据 —— 必须探到「活着」才算数。

    默认只探一次。以前默认探两次是为了防 CDN 抖动误判，但两次超时叠起来
    最坏能到 8 秒，全加在用户的开播等待上 —— 这本身就是「一直加载中」的
    一个成因。现在单次超时也压到 2 秒，最坏 2 秒。

    熔断期内一律返回 True：那是「我们探不到」，不是「链坏了」，
    绝不能拿它去判死刑（否则就是还原风暴）。
    """
    if not _probe_usable():
        return True
    for i in range(max(1, tries)):
        t = (CAS_LINK_VERIFY_TIMEOUT if i == 0
             else CAS_LINK_VERIFY_RETRY_TIMEOUT)
        if _probe_link(url, t) == PROBE_ALIVE:
            return True
    return False


def _fresh_link_broken(key, url, cas_name=""):
    """
    刚签发的直链是不是根本用不了。

    【v2.2.10 更正 —— 这是整场排查的落点】
    这里以前是无条件相信探测结果：探不到就删掉重还原。在海外服务器上
    这是个灾难 —— 服务器跨国际线路去看国内 CDN，探测经常探不到，
    于是一个播放请求就删文件、重还原一份，播放器一路转圈。
    现在加一道闸：**连着几条刚还原的新链都被判坏，就认定是探测方的
    问题**（新链不可能是坏的），熔断探测，照常把链接交出去。

    为什么非要在交出去之前验一次：播放器 follow 302 之后就**钉死在这条
    URL 上**了，服务端后面再怎么重建、换链它都不知道。
    """
    _probe_at[key] = time.time()        # 刚探过，记一笔
    if not _probe_usable():
        return False
    if _link_ready(url):
        _note_probe_ok()
        return False
    # 探不到 / 判坏：先记一笔「新链被判坏」，够数就熔断，熔断后不再重建
    return not _note_fresh_dead(cas_name)


def _verify_retry_allowed(key):
    """补救机会还有没有（见 CAS_VERIFY_RETRY_COOLDOWN）。"""
    now = time.time()
    if now - _verify_retried.get(key, 0) < CAS_VERIFY_RETRY_COOLDOWN:
        return False
    if len(_verify_retried) > 500:
        _verify_retried.clear()
    _verify_retried[key] = now
    return True


def _cache_put(key, value):
    """写入直链缓存，顺手清掉已过期的条目（缓存表不能无限长）。"""
    with _cache_lock:
        _link_cache[key] = value
        if len(_link_cache) > 500:
            now = time.time()
            for k in [k for k, v in _link_cache.items() if v[1] <= now]:
                _link_cache.pop(k, None)


# ----------------------------------------------------------------------
# 续播识别：把「接着上次进度回来」当成全新播放处理
# ----------------------------------------------------------------------
#
# 用户实测出来的规律：**全新播放一部新片子从来不卡，只有续播才卡。**
# 两者的差别只有一处 —— 续播时播放器带着一个非零起点的 Range 回来，
# 而服务端这边还留着这部片子的旧直链、旧还原会话、旧临时文件。
# 新片子那边一切都是空的，所以从不复现。
#
# 结论：与其去猜旧状态哪里坏了，不如**续播时把旧状态全部丢掉，
# 走一条和全新播放一模一样的路**。状态空间直接坍缩，玄学无处藏身。
RESUME_IDLE_GAP = 180      # 距上次来要链超过这么久，又带非零 Range → 判为续播
_last_served = {}          # file_id -> 上次成功交链的时刻
_last_served_lock = threading.Lock()
# 每个片子**每一次**来请求的时刻（不管成没成）。用来算「距上次请求隔了多久」——
# 这是判断「这次是续播还是播放中的 seek」最直接的证据，也是排查
# 「隔夜续播一直加载中」时最关键的一个数字。
_last_seen = {}
_last_seen_lock = threading.Lock()


def _range_start():
    """播放器这次要的是从第几个字节开始。没带 Range、或从头开始 → 0。"""
    rng = request.headers.get("Range") or ""
    m = re.match(r"\s*bytes\s*=\s*(\d+)", rng, re.I)
    return int(m.group(1)) if m else 0


def _mark_served(file_id):
    """记一笔：刚刚给这个片子交过链。"""
    with _last_served_lock:
        _last_served[file_id] = time.time()
        if len(_last_served) > 2000:
            # 表不能无限长；清空比逐个淘汰省事，误判代价也只是多走一次全新流程
            _last_served.clear()
            _last_served[file_id] = time.time()


def _is_resume(file_id):
    """这次请求是不是「播到一半退出去、过了一阵子回来接着播」。

    判据两条，缺一不可：
      1. 播放器带了一个**非零起点**的 Range —— 说明它要接着上次的进度；
      2. 这个片子已经有一阵子没人来要链了 —— 说明不是播放过程中的 seek。

    第 2 条很关键：播放中拖动进度条也会带非零 Range，但那是「几秒前
    刚来要过链」的连续播放，绝不能当成续播去重新秒传 —— 那正是
    v2.1.x「一分钟还原七八次」的老病根。
    """
    if _range_start() <= 0:
        return False
    with _last_served_lock:
        last = _last_served.get(file_id, 0)
    return (time.time() - last) > RESUME_IDLE_GAP


def _get_link(client, file_id, resume=False):
    """普通视频（非 .cas）的播放直链。

    resume=True（续播）时把这条片子的旧链彻底丢掉，当全新播放处理。
    """
    if resume:
        with _cache_lock:
            _link_cache.pop(file_id, None)
        app.logger.info("续播：普通直链按全新播放处理 %s", file_id[:12])

    now = time.time()
    with _cache_lock:
        cached = _link_cache.get(file_id)
        if cached and cached[1] > now:
            # 缓存命中也要现场验一次：坏了就重取，绝不把死链交出去
            if _link_ready(cached[0]):

                return cached[0]
            app.logger.info("缓存的普通直链已失效，重新获取: %s", file_id[:12])
    url = client.get_download_url(file_id)
    # 普通视频的直链同样只有约 15 分钟寿命，缓存上限必须跟着收紧，
    # 否则缓存末期交出去的是一条马上要过期的链（续播就一直加载）
    _cache_put(file_id, (url, _link_expire(url, LINK_TTL, CAS_LINK_MAX_TTL)))
    return url


def _get_cas_link(client, cfg, file_id, cas_name, resume=False):
    """
    .cas 文件的播放直链：秒传还原出临时文件，再把临时文件的直链 302 给播放器。

    这里最容易踩的坑是「反复还原」：播放过程中播放器会不断回来换直链，
    如果每次换链都重新秒传还原一个新文件，而旧文件要等 TTL 到点才删，
    同一部电影就会在临时目录里同时躺着好几份。所以规则是：

      * 缓存期内：直接复用上一条直链，只给临时文件续期；
      * 缓存过期（换链）：v2.2.0 起**一律全新秒传**（对齐 OpenList
        魔改版 139cas 被生产验证顺畅的模式）—— 旧实体交给延迟删除
        和还原时的同名清扫，任何坏状态最多活到下一次全新秒传为止；
        还原前先清掉这部片子的旧副本，临时目录不会堆副本；
      * 复用只发生在 90 秒幂等窗口内（探测后播放、并发请求、
        直链假活后的换链重建），见 cas.SESSION_TTL；
      * 临时文件的删除时间跟着缓存走（缓存失效后再留 CAS_TEMP_GRACE 秒），
        文件永远活过直链，不依赖「删除不影响已签发直链」这类云盘行为；
      * 直链交给播放器之前会自己先拉 1 个字节验一次，拿不到字节就删掉重来
        （只重来一次）—— 播放器 follow 302 之后就钉死在那条 URL 上，
        这是唯一的补救窗口，错过就只能等用户「返回重播」。

    resume=True（续播）：**把这部片子的旧状态全部丢掉，当全新播放处理** ——
    旧直链、旧还原会话、探测节流、补救冷却，一个都不留。理由见
    上面「续播识别」那一段的注释。
    """
    now = time.time()
    key = "cas:" + file_id
    restorer = get_restorer(cfg, client)

    if resume:
        # 续播 = 全新播放：旧状态一个都不信任
        with _cache_lock:
            _link_cache.pop(key, None)
        restorer.forget_session(file_id)
        _probe_at.pop(key, None)
        _probe_suspect.pop(key, None)
        # 补救冷却也清掉：这次的链是刚还原出来的，属于新的一轮，
        # 不该被上一次播放的补救记录拦住（否则续播时探到坏链也不补救）
        _verify_retried.pop(key, None)
        app.logger.info("续播：.cas 按全新播放处理，丢弃旧直链与旧会话 %s", cas_name)

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
        # 探测说这条链废了（多半是用户在云盘里手动删了临时文件）→ 想重建。
        # 但重建必须受冷却约束：万一「探不到」其实是我们自己到 CDN 的路
        # 不通（链对播放器是好的），没有冷却就会变成「来一个请求还原一份」
        # 的还原风暴（v2.1.8 的血泪，一分钟七八次、堆几十 GB）。
        # 宁可偶尔交一条存疑的链，也绝不重演还原风暴。
        if _verify_retry_allowed(key):
            with _cache_lock:
                _link_cache.pop(key, None)
            restorer.forget_session(file_id)
            app.logger.info("缓存的直链已失效（%s），重新还原", cas_name)
        else:
            app.logger.warning(
                "直链探测判定失效，但重建冷却中，先复用旧链: %s", cas_name)
            restorer.schedule_delete(
                temp_id, delay=max(int(restorer.temp_ttl or 0),
                                   int(expire - now + CAS_TEMP_GRACE)))
            return url

    url, size, temp_id, real_name, restored = restorer.fetch_link(file_id, cas_name)
    # 注意 and 的短路：只有确实探到坏链、且熔断器没跳闸，才会走补救
    if _fresh_link_broken(key, url, cas_name) and _verify_retry_allowed(key):
        # 这条链根本用不了：新还原的文件在 CDN 侧还没同步，或者复用的实体
        # 其实已经不在了（取直链接口对不存在的文件照样签发 URL，不探一下
        # 就会把死链发给播放器 —— 表现是「一直加载中，返回重播才正常」）。
        # 播放器 follow 302 后就钉在这条 URL 上了，所以必须现在就补救。
        app.logger.warning("刚取到的直链不可用（%s），删掉重还原一次", cas_name)
        restorer.delete_quietly(temp_id)
        restorer.forget_session(file_id)
        url, size, temp_id, real_name, restored = restorer.fetch_link(
            file_id, cas_name)
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


_CAS_FATAL_MARKS = ("权益不足", "不是视频", "已失效无法还原", "SHA256",
                    "秒传失败", "请手动删除")


def _cas_error_is_final(exc):
    """这类 CAS 业务错误重试也不会成功（账号权益 / 文件本身的问题）。"""
    if not isinstance(exc, CASError):
        return False
    msg = str(exc)
    return any(m in msg for m in _CAS_FATAL_MARKS)


def _resolve_play_url(cfg, file_id, cas_name, use_cas, resume=False):
    """换取一条可用直链。成功返回 (url, None)，失败返回 (None, Response)。"""
    url, last_exc = None, None
    t0 = time.time()
    for attempt in (1, 2):
        try:
            if attempt == 1:
                client = get_client(cfg)
            else:
                # 第一次失败：缓存的 client 可能 token/接入地址已失效，
                # 丢掉缓存全新 init 再试一次；云盘接口偶发抖动也靠这一搏。
                drop_client(cfg)
                client = build_client(cfg)
                client.init()
            url = (_get_cas_link(client, cfg, file_id, cas_name, resume)
                   if use_cas else _get_link(client, file_id, resume))
            break
        except (Yun139Error, CASError) as exc:
            last_exc = exc
            if attempt == 2 or _cas_error_is_final(exc):
                break
        except Exception as exc:
            last_exc = exc
            if attempt == 2:
                break
    if url is None:
        app.logger.info("直链获取失败 cas=%s file=%s 耗时=%.1fs: %s",
                        cas_name, file_id, time.time() - t0, last_exc)
        return None, Response(f"获取直链失败: {last_exc}", status=502)
    took = time.time() - t0
    if took > 2:
        app.logger.info("直链获取较慢 cas=%s file=%s 耗时=%.1fs",
                        cas_name, file_id, took)
    return url, None


def _invalidate_link(file_id, use_cas, cfg=None):
    """丢掉缓存的直链，逼下一次请求换一条新的。"""
    key = ("cas:" + file_id) if use_cas else file_id
    with _cache_lock:
        _link_cache.pop(key, None)
    if use_cas and cfg is not None:
        try:
            get_restorer(cfg, get_client(cfg)).forget_session(file_id)
        except Exception:
            pass


def _proxy_stream(file_id, cfg, cas_name, use_cas):
    """
    代理转发模式：视频流经本机中转（默认）。

    为什么必须有它 —— 302 直连有个结构性死结：播放器 follow 302 之后就
    **钉死在那条 URL 上**，而移动云盘直链实测只有约 15 分钟寿命。于是：

      * 长视频播过 15 分钟 → 旧链过期 → 一直加载中；
      * 暂停一阵子再续播 → 旧链过期、临时文件已被清理 → 一直加载中。

    这两种情况下播放器都**不会**再回来问服务端要新链，服务端连补救窗口
    都没有，只能等用户「退出重播」。

    代理模式下播放器始终连本机地址，每一个 Range 请求都由服务端当场取一条
    有效的直链转发，于是：
      * 直链过期 → 下一次 Range 请求自动换新链，播放不中断；
      * 续播时临时文件已删 → 当场重新秒传还原（幂等，1~2 秒），不必退出重播。

    代价是流量经过本机（内网播放无感；外网播放会占用本机上行带宽），
    所以系统设置里可以切回 302 直连。
    """
    import requests as _rq

    resume = _is_resume(file_id)
    url, err = _resolve_play_url(cfg, file_id, cas_name, use_cas, resume)
    if url is None:
        return err
    _mark_served(file_id)

    def _open(u, rng):
        headers = {"Range": rng} if rng else {}
        return _rq.get(u, headers=headers, stream=True, timeout=(15, 60))

    # HEAD 不带上 Range：让上游返回 200 + 完整 Content-Length 给播放器探测用
    rng = request.headers.get("Range") if request.method != "HEAD" else None
    up = _open(url, rng)

    if up.status_code >= 400:
        # 上游拒绝（直链过期 / 临时文件已被清）——这正是代理模式的价值所在：
        # 现在就能换一条新链，而不是让用户「返回重播」。
        app.logger.info("代理转发上游返回 %d，换新链重试 cas=%s file=%s",
                        up.status_code, cas_name, file_id)
        up.close()
        _invalidate_link(file_id, use_cas, cfg)
        url2, err2 = _resolve_play_url(cfg, file_id, cas_name, use_cas, True)
        if url2 is None:
            return err2
        up = _open(url2, rng)

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": up.headers.get("Content-Type") or "video/mp4",
        "Cache-Control": "no-store",
    }
    for h in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        if h in up.headers:
            headers[h] = up.headers[h]
    status = up.status_code

    if request.method == "HEAD":
        up.close()
        return Response(status=status, headers=headers)

    def _gen():
        try:
            for chunk in up.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return Response(_gen(), status=status, headers=headers)


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
    use_cas = (bool(cas_name) and is_cas_name(cas_name)
               and cfg.get("cas_enabled", True))
    # 默认 302 直连：视频流从云盘 CDN 直达播放器，不消耗服务器带宽。
    # ?proxy=1 可临时开启代理转发（服务器在家里才适合）。
    if cfg.get("play_proxy", False) or request.args.get("proxy"):
        return _proxy_stream(file_id, cfg, cas_name, use_cas)

    t0 = time.time()
    # ---- 把「播放器到底发了什么」原样记下来 ----
    # 服务端能查的环节都查过了：隔夜续播和全新播放走的路径完全一样，
    # 交出去的链也实测是好的（任意偏移 Range 均 206、0.2 秒）。
    # 那就只剩「播放器 302 之后发生了什么」这一片盲区 —— 唯一的办法
    # 是把它发过来的原始请求头留证。这样用户卡住时不用翻日志，
    # 服务端自己就记着。
    rng_raw = request.headers.get("Range") or ""
    ua_raw = request.headers.get("User-Agent") or ""
    with _last_seen_lock:
        gap = int(time.time() - _last_seen.get(file_id, 0))
        _last_seen[file_id] = time.time()
        if len(_last_seen) > 2000:
            _last_seen.clear()
    resume = _is_resume(file_id)
    url, err = _resolve_play_url(cfg, file_id, cas_name, use_cas, resume)
    if url is None:
        _diag_add(ok=False, name=cas_name or file_id[:12], cas=use_cas,
                  ms=int((time.time() - t0) * 1000), resume=resume,
                  rng=rng_raw, ua=ua_raw[:40], gap=gap,
                  err=(err.get_data(as_text=True) or "")[:160])
        return err

    _mark_served(file_id)
    # 记下这条链还剩多久可用 —— 偶发「一直加载中」时，这是判断服务端
    # 有没有交出「快过期的链」的第一手证据
    life = int(_link_expire(url, LINK_TTL, CAS_LINK_MAX_TTL) - time.time())
    _diag_add(ok=True, name=cas_name or file_id[:12], cas=use_cas,
              ms=int((time.time() - t0) * 1000), life=max(life, 0),
              resume=resume, rng=rng_raw, ua=ua_raw[:40], gap=gap)
    resp = redirect(url, code=302)
    # 防缓存头给全：任何一层（播放器自己的 HTTP 栈、中间反代）只要缓存了
    # 这个 302，之后就会一直用那条会过期的云盘直链 —— 表现就是
    # 「隔夜续播一直加载中，返回重播才好」。
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


# ----------------------------------------------------------------------
# 播放诊断（最近若干次播放请求留痕，用于定位偶发的「一直加载中」）
# ----------------------------------------------------------------------

_DIAG_MAX = 40
_diag = collections.deque(maxlen=_DIAG_MAX)
_diag_lock = threading.Lock()


def _diag_add(**kw):
    kw["t"] = time.strftime("%m-%d %H:%M:%S")
    with _diag_lock:
        _diag.append(kw)


@app.route("/api/diag")
def api_diag():
    """最近 40 次播放请求：成功/失败、耗时、交出去的直链还剩多久。"""
    with _diag_lock:
        items = list(_diag)[::-1]
    return jsonify({"ok": True, "items": items})


@app.route("/api/diag/clear", methods=["POST"])
def api_diag_clear():
    with _diag_lock:
        _diag.clear()
    return jsonify({"ok": True})


@app.route("/api/selftest")
def api_selftest():
    """网络自检：这台服务器**自己**能不能顺利用上云盘 CDN，以及有多快。

    为什么要单独测这个：交链前的探测是**服务器自己**发起的，而服务器
    可能在海外（甲骨文），要跨国际线路去看移动云盘的国内 CDN。
    国内本地部署探一次 0.2 秒，海外服务器却可能很慢甚至被拒 —— 而
    探测一旦被当成「链坏了」，就会演变成「来一个请求还原一份」，
    播放器一路转圈。

    这个接口把事实量出来：到 CDN 的 TCP 连接耗时、探测结论、探测耗时、
    以及服务器自己的出口 IP。部署在哪、线路好不好，一看便知。
    """
    import socket
    from urllib.parse import urlparse

    out = {"ok": True, "probe": _probe_status(), "egress_ip": None,
           "cdn": None, "note": ""}

    # 服务器出口 IP（几个公共服务依次试，都拿不到就算了，不影响结论）
    for u in ("https://api.ipify.org", "https://ifconfig.me/ip",
              "https://ipinfo.io/ip"):
        try:
            import requests
            r = requests.get(u, timeout=6)
            if r.status_code == 200 and r.text.strip():
                out["egress_ip"] = r.text.strip()[:64]
                break
        except Exception:
            continue

    # 拿一条最近用过的直链来测
    url = ""
    with _cache_lock:
        for v in _link_cache.values():
            if v and isinstance(v[0], str) and v[0].startswith("http"):
                url = v[0]
                break
    if not url:
        out["note"] = ("还没有可测的直链（先随便播一个视频，再回来点自检）。"
                       "出口 IP 和探测状态仍然有效。")
        return jsonify(out)

    host = urlparse(url).netloc.split(":")[0]
    cdn = {"host": host}
    # 1) 纯 TCP 连接耗时：区分「线路慢」和「链坏」
    t0 = time.time()
    try:
        s = socket.create_connection((host, 443), timeout=6)
        s.close()
        cdn["tcp_ms"] = int((time.time() - t0) * 1000)
    except Exception as exc:
        cdn["tcp_ms"] = None
        cdn["tcp_error"] = f"{type(exc).__name__}: {exc}"[:120]
    # 2) 真正探一次这条链
    t0 = time.time()
    verdict = _probe_link(url, CAS_LINK_VERIFY_TIMEOUT)
    cdn["probe_ms"] = int((time.time() - t0) * 1000)
    cdn["probe_verdict"] = verdict
    cdn["life_s"] = int(_link_expire(url, LINK_TTL, CAS_LINK_MAX_TTL) - time.time())
    out["cdn"] = cdn

    # 3) 给一句人话结论
    if verdict == PROBE_ALIVE and (cdn.get("tcp_ms") or 9999) < 1500:
        out["note"] = "这台服务器到云盘 CDN 的线路正常，交链前探测是可靠的。"
    elif verdict == PROBE_ALIVE:
        out["note"] = (f"探测能成功，但到 CDN 的 TCP 连接要 "
                       f"{cdn.get('tcp_ms')} 毫秒，偏慢 —— 每次播放都会多等这么久。")
    else:
        out["note"] = ("⚠️ 这台服务器**探不到**云盘 CDN。这说明服务器所在网络"
                       "访问国内 CDN 有问题（海外服务器很常见）。此时绝不能"
                       "把探测失败当成「链坏了」，否则会不断删文件重还原 ——"
                       "熔断器会自动接管，照常把链接交给播放器。")
    return jsonify(out)


@app.route("/api/link/<path:file_id>")
def api_link(file_id):
    """调试用：查看某个文件的直链，不跳转。"""
    cfg = load_config()
    cas_name = request.args.get("cas") or ""
    try:
        client = get_client(cfg)
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
