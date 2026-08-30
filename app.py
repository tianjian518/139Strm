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
from datetime import datetime

from flask import Flask, jsonify, render_template, request, redirect, Response

from yun139 import crypto
from yun139.client import Yun139Client, Yun139Error, CLOUD_TYPES
from yun139.strm import StrmGenerator, DEFAULT_MEDIA_EXT, DEFAULT_COPY_EXT
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

# 后台生成任务
_task_state = {
    "running": False,
    "progress": "",
    "result": None,
    "started_at": None,
}
_task_lock = threading.Lock()


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

@app.route("/api/strm", methods=["POST"])
def api_strm():
    data = request.get_json(force=True) or {}
    cfg = load_config()

    with _task_lock:
        if _task_state["running"]:
            return jsonify({"ok": False, "error": "已有任务正在运行"}), 409
        _task_state.update({
            "running": True, "progress": "正在初始化",
            "result": None, "started_at": datetime.now().isoformat(),
        })

    folder = data.get("folder", "/")
    output_dir = data.get("output_dir") or cfg.get("output_dir") or "/strm"
    media_ext = data.get("media_ext") or cfg.get("media_ext")
    copy_ext = data.get("copy_ext") or cfg.get("copy_ext")
    min_size = data.get("min_size_mb", cfg.get("min_size_mb", 0))
    recursive = data.get("recursive", cfg.get("recursive", True))
    if data.get("base_url"):
        cfg["base_url"] = data["base_url"]

    # 在主线程取好 host，后台线程取不到 request
    fallback_host = request.host_url.rstrip("/")

    def worker():
        try:
            with _task_lock:
                _task_state["progress"] = "正在连接移动云盘"
            client = build_client(cfg)
            client.init()

            base_url = get_base_url(cfg, fallback_host)
            if not base_url:
                raise ValueError(
                    "无法确定访问地址，请在配置中填写 base_url（如 http://192.168.1.10:8025）"
                )
            gen = StrmGenerator(
                client=client, base_url=base_url, output_dir=output_dir,
                media_ext=media_ext, copy_ext=copy_ext,
                min_size_mb=float(min_size or 0), recursive=bool(recursive),
                include_cas=bool(cfg.get("cas_enabled", True)),
            )
            with _task_lock:
                _task_state["progress"] = "正在扫描目录（可能需要一点时间）"
            gen.generate(folder, "")

            with _task_lock:
                _task_state["result"] = gen.summary()
                _task_state["progress"] = "完成"
        except Exception as exc:
            with _task_lock:
                _task_state["result"] = {"errors": [f"{type(exc).__name__}: {exc}"],
                                         "created": 0, "skipped": 0,
                                         "copied": 0, "logs": []}
                _task_state["progress"] = "失败"
        finally:
            with _task_lock:
                _task_state["running"] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "任务已启动"})


@app.route("/api/strm/status")
def api_strm_status():
    with _task_lock:
        return jsonify({
            "running": _task_state["running"],
            "progress": _task_state["progress"],
            "result": _task_state["result"],
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8025))
    app.run(host="0.0.0.0", port=port, threaded=True)
