# -*- coding: utf-8 -*-
"""
CAS（秒传文件）解析与还原播放。

.cas 文件本身只有几百字节，内容是一段 Base64 编码的 JSON，记录着原始文件的
名称、大小、MD5 / SHA256 等特征。真正的数据早已存在云盘服务器里，只是从你的
文件列表里看不到了。

播放 .cas 的原理（对照 OpenList 的 drivers/139/cas.go）：
    1. 下载 .cas 文件内容，Base64 + JSON 解出原始文件信息
    2. 用 SHA256 调用 /file/create 做"秒传"——在临时目录里凭空还原出一个真文件
    3. 取这个临时文件的真实直链，302 给播放器
    4. 延迟删除临时文件，云盘里除了 .cas 之外不留痕迹

全程不消耗真实存储空间，也不产生上传流量。
"""

import base64
import json
import logging
import os
import random
import threading
import time

from yun139.client import Yun139Error

logger = logging.getLogger("139strm.cas")

# 移动云盘接口报这些字眼时，通常是账号「权益」不足（会员等级 / 容量 / 文件大小限制），
# 与 139Strm 本身无关，代码层面无法绕过，只能升级账号或只还原较小的文件。
ENTITLEMENT_KEYWORDS = (
    "权益", "不足", "超限", "容量", "过大", "entitlement", "quota",
)


def _fmt_size(size):
    """把字节数转成人类可读的大小。"""
    if size >= GB:
        return "%.2f GB" % (size / GB)
    if size >= MB:
        return "%.1f MB" % (size / MB)
    return "%d 字节" % size

# 临时还原目录名，放在个人云根目录
TEMP_DIR_NAME = "139STRM_TEMP"

# 还原出来的临时文件保留多久后删除（秒）
# 不能立刻删：播放器拿到 302 之后才会真正发起带 Range 的请求
DEFAULT_TEMP_TTL = 300

# 一个已还原的临时文件最多被复用多久（秒）。
# 超过这个时长后下次播放强制重新秒传还原，避免长时间复用同一份临时文件。
MAX_SESSION_TTL = 12 * 3600

VIDEO_EXTS = (
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m2ts",
    ".wmv", ".rmvb", ".m4v", ".mpg", ".mpeg", ".3gp",
)

MB = 1024 * 1024
GB = 1024 * MB


class CASError(Exception):
    """CAS 解析或还原失败。"""


class CASInfo:
    """.cas 文件里记录的原始文件信息。"""

    __slots__ = ("provider", "name", "size", "md5", "slice_md5",
                 "sha1", "sha256", "pre_id", "create_time")

    def __init__(self, provider="", name="", size=0, md5="", slice_md5="",
                 sha1="", sha256="", pre_id="", create_time=""):
        self.provider = provider
        self.name = name
        self.size = int(size or 0)
        self.md5 = md5
        self.slice_md5 = slice_md5
        self.sha1 = sha1
        self.sha256 = sha256
        self.pre_id = pre_id
        self.create_time = create_time

    def __repr__(self):
        return f"<CASInfo {self.name} ({self.size} bytes, {self.provider})>"


def is_cas_name(name):
    """文件名是否以 .cas 结尾（不区分大小写）。"""
    return (name or "").lower().endswith(".cas")


def is_video_name(name):
    ext = os.path.splitext(name or "")[1].lower()
    return ext in VIDEO_EXTS


def decode(data):
    """Base64 + JSON 解出 CAS 内容。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    data = data.strip()
    try:
        raw = base64.b64decode(data)
    except Exception as exc:
        raise CASError(f"CAS 内容不是合法 Base64: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CASError(f"CAS 内容不是合法 JSON: {exc}") from exc

    if not payload.get("name") or payload.get("size") is None:
        raise CASError("CAS 内容缺少 name / size 字段")
    if not (payload.get("md5") or payload.get("sha256") or payload.get("sha1")):
        raise CASError("CAS 内容缺少任何一种文件校验值")

    provider = (payload.get("provider") or "").lower()
    if provider == "115" and not (payload.get("sha1") and payload.get("preID")):
        raise CASError("115 的 CAS 内容缺少 sha1 / preID")

    return CASInfo(
        provider=provider,
        name=payload.get("name") or "",
        size=int(payload.get("size") or 0),
        md5=payload.get("md5") or "",
        slice_md5=payload.get("sliceMd5") or payload.get("md5") or "",
        sha1=payload.get("sha1") or "",
        sha256=payload.get("sha256") or "",
        pre_id=payload.get("preID") or "",
        create_time=str(payload.get("create_time") or ""),
    )


def derive_restore_name(cas_name, original_name):
    """
    由 .cas 文件名和原始文件名推出还原后的文件名。

    例：cas_name="电影.mp4.cas", original_name="电影.mp4" -> "电影.mp4"
        cas_name="电影.cas",     original_name="电影.mp4" -> "电影.mp4"
    """
    base_name = os.path.splitext(cas_name)[0]
    base_name = os.path.splitext(base_name)[0]
    ext = os.path.splitext(original_name)[1]
    if not base_name:
        base_name = os.path.splitext(original_name)[0]
    return base_name + ext


def resolve_restore_name(cas_name, info):
    """校验并得出还原文件名。"""
    if info is None:
        raise CASError("缺少 CAS 内容")
    if not is_cas_name(cas_name):
        raise CASError(f"文件名 {cas_name!r} 不是 .cas 结尾")
    if not os.path.splitext(cas_name)[0].strip():
        raise CASError(f".cas 文件名 {cas_name!r} 去掉后缀后为空")
    restore_name = derive_restore_name(cas_name, info.name).strip()
    if not restore_name:
        raise CASError(f".cas 文件名 {cas_name!r} 推不出原始文件名")
    if "/" in restore_name or "\\" in restore_name:
        raise CASError(f"还原文件名 {restore_name!r} 含有路径分隔符")
    return restore_name


def _part_size(size):
    """分片大小：超过 30GB 用 512MB，否则 100MB。"""
    if size // GB > 30:
        return 512 * MB
    return 100 * MB


def build_part_infos(size):
    part_size = _part_size(size)
    part = 1
    if size > part_size:
        part = (size + part_size - 1) // part_size
    return [
        {
            "partNumber": i + 1,
            "partSize": min(size - i * part_size, part_size),
            "parallelHashCtx": {"partOffset": i * part_size},
        }
        for i in range(part)
    ]


class CASRestorer:
    """在移动云盘上把 .cas 秒传还原成可播放的真文件。"""

    def __init__(self, client, temp_ttl=DEFAULT_TEMP_TTL,
                 temp_dir_name=TEMP_DIR_NAME, allow_all_ext=False):
        self.client = client
        self.temp_ttl = temp_ttl
        self.temp_dir_name = temp_dir_name
        # True 时任何后缀都还原；False 时只还原视频后缀
        self.allow_all_ext = allow_all_ext
        self._temp_dir_id = None
        self._lock = threading.Lock()
        self._pending = {}
        # 临时文件到期时间表：用独立锁保护，避免和 _lock 互相等待
        self._pending_lock = threading.Lock()
        self._reaper = None
        # 已还原会话：cas 文件 ID -> {"temp_id","name","size","created_at"}
        # 播放中反复请求直链时，只复用这里的临时文件重新取一次直链，
        # 绝不重新秒传还原 —— 否则一部电影会被反复还原出几十 GB。
        self._sessions = {}
        # 每个 cas 文件的还原单飞锁，避免并发请求各还原一份
        self._flight = {}
        self._state_lock = threading.Lock()
        # 删除失败的临时文件重试次数
        self._delete_failures = {}
        self.max_session_ttl = MAX_SESSION_TTL

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, file_id, cas_name):
        """下载 .cas 文件内容并解析出原始文件信息。"""
        url = self.client.get_download_url(file_id)
        resp = self.client._session.get(url, timeout=self.client.timeout)
        resp.raise_for_status()
        return decode(resp.content)

    # ------------------------------------------------------------------
    # 临时目录
    # ------------------------------------------------------------------

    def _scan_temp_dir(self):
        """在根目录里查找临时文件夹；查询失败会抛异常（区别于「确实没有」）。"""
        for item in self.client.personal_list(self.client.root_folder_id):
            if item.name == self.temp_dir_name and item.is_folder:
                return item.file_id
        return None

    def find_temp_dir(self):
        """在根目录里查找已存在的临时文件夹，找不到或查询失败都返回 None。"""
        try:
            return self._scan_temp_dir()
        except Exception as exc:
            logger.warning("列出根目录失败: %s", exc)
            return None

    def ensure_temp_dir(self):
        """取得（必要时创建）根目录里的临时文件夹。

        每次都会重新确认目录 ID：临时目录可能被用户在云盘里删掉，
        或从回收站恢复后 ID 变了。复用一个失效的 ID 会导致所有 CAS
        播放全部失败，所以必须拿到当前真实存在的目录 ID。
        """
        with self._lock:
            try:
                found = self._scan_temp_dir()
            except Exception as exc:
                # 只是查不到（网络抖动等），手上有记住的 ID 就先沿用，避免误建新目录
                if self._temp_dir_id:
                    logger.warning("列出根目录失败，沿用已记住的临时目录: %s", exc)
                    return self._temp_dir_id
                raise CASError(f"无法列出云盘根目录: {exc}")

            if found:
                if self._temp_dir_id and found != self._temp_dir_id:
                    logger.warning("临时目录 ID 已变化 %s -> %s，已自动更新",
                                   self._temp_dir_id, found)
                self._temp_dir_id = found
                return found

            root = self.client.root_folder_id
            resp = self.client.personal_create({
                "parentFileId": root,
                "name": self.temp_dir_name,
                "description": "",
                "type": "folder",
                "fileRenameMode": "force_rename",
            })
            data = resp.get("data") or {}
            file_id = data.get("fileId") or ""
            if not file_id:
                raise CASError(f"创建临时目录失败: {resp}")

            # force_rename 下若已同名，服务端会改名，需要重新查一次拿正确 ID
            if (data.get("fileName") or self.temp_dir_name) != self.temp_dir_name:
                found = self.find_temp_dir()
                if found:
                    self._temp_dir_id = found
                    return found
                raise CASError(
                    f"已存在同名目录 {data.get('fileName')}，且无法定位，"
                    f"请手动删除后重试"
                )

            self._temp_dir_id = file_id
            logger.info("已创建临时目录 %s (%s)", self.temp_dir_name, file_id)
            return file_id

    def set_temp_dir(self, dir_id):
        """调用方持久化的临时目录 ID，避免服务重启后重复创建。"""
        with self._lock:
            self._temp_dir_id = dir_id or None

    def get_temp_dir(self):
        with self._lock:
            return self._temp_dir_id

    # ------------------------------------------------------------------
    # 秒传还原
    # ------------------------------------------------------------------

    def _create_by_sha256(self, dir_id, name, size, sha256):
        if len(sha256) != 64:
            raise CASError(f"SHA256 长度不正确: {len(sha256)}")
        payload = {
            "contentHash": sha256,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": build_part_infos(size)[:100],
            "size": size,
            "parentFileId": dir_id,
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        }
        try:
            resp = self.client.personal_create(payload, use_pc_headers=True)
        except Yun139Error as exc:
            msg = str(exc)
            if any(k in msg for k in ENTITLEMENT_KEYWORDS):
                # 移动云盘账号权益（会员等级 / 文件大小上限）不足，代码无法绕过
                raise CASError(
                    "账号秒传权益不足，无法还原大小约 %s 的文件（%s）。"
                    "这是移动云盘账号等级限制，与 139Strm 无关；"
                    "请升级移动云盘会员，或只播放较小的 CAS 文件。" % (_fmt_size(size), msg)
                ) from exc
            raise
        data = resp.get("data") or {}
        if not data.get("exist") and not data.get("rapidUpload") \
                and data.get("partInfos") is not None:
            raise CASError(
                "秒传失败：云端已不存在该文件的源数据，.cas 已失效无法还原"
            )
        file_id = data.get("fileId") or ""
        if not file_id:
            raise CASError(f"秒传还原未返回文件 ID: {resp}")
        return file_id, (data.get("fileName") or name)

    def restore_temp(self, cas_file_id, cas_name):
        """
        把 .cas 还原成临时目录里的一个真文件。

        返回 (直链, 原始大小, 临时文件ID, 云端实际文件名, 原始片名)
        """
        info = self.parse(cas_file_id, cas_name)
        preview_name = resolve_restore_name(cas_name, info)

        if not self.allow_all_ext and not is_video_name(preview_name):
            raise CASError(
                f"{preview_name!r} 不是视频文件，已跳过还原"
                "（可在配置里开启「还原所有类型」）"
            )
        if not info.sha256:
            raise CASError("CAS 内容缺少 sha256，无法秒传还原")

        temp_dir = self.ensure_temp_dir()
        info.name = preview_name
        temp_name = "TEMP_%d_%05d_%s" % (
            int(time.time() * 1000), random.randint(0, 99999), preview_name
        )
        temp_id, real_name = self._create_by_sha256(
            temp_dir, temp_name, info.size, info.sha256
        )
        logger.info("已秒传还原 %s -> %s (%s)", cas_name, real_name, temp_id)

        link = self.client.personal_get_link(temp_id)
        if not link:
            self.delete_quietly(temp_id)
            raise CASError("还原成功但未能取得直链")
        # real_name 是云端实际文件名（带 TEMP_ 前缀，可能被服务端改号），
        # preview_name 是原始片名，清扫旧副本时按它来认人
        return link, info.size, temp_id, real_name, preview_name

    # ------------------------------------------------------------------
    # 取直链（复用优先）
    # ------------------------------------------------------------------

    def _flight_lock(self, cas_file_id):
        """取得某个 .cas 的还原单飞锁。"""
        with self._state_lock:
            lock = self._flight.get(cas_file_id)
            if lock is None:
                lock = threading.Lock()
                self._flight[cas_file_id] = lock
            return lock

    def _refresh_link(self, temp_id):
        """对已存在的临时文件重新取一条直链；文件没了返回空串。"""
        try:
            return self.client.personal_get_link(temp_id) or ""
        except Exception as exc:
            logger.info("复用临时文件 %s 取直链失败，将重新还原: %s", temp_id, exc)
            return ""

    def _session_valid(self, cas_file_id):
        """有没有还能用的已还原会话。"""
        with self._state_lock:
            sess = self._sessions.get(cas_file_id)
        if not sess:
            return None
        if time.time() - sess["created_at"] > self.max_session_ttl:
            with self._state_lock:
                self._sessions.pop(cas_file_id, None)
            return None
        return sess

    def _drop_session(self, cas_file_id):
        with self._state_lock:
            return self._sessions.pop(cas_file_id, None)

    def _active_temp_ids(self):
        """所有正在被复用（不该被清扫）的临时文件 ID。"""
        with self._state_lock:
            return {s["temp_id"] for s in self._sessions.values()}

    def _purge_leftovers(self, restore_name):
        """
        清掉这部片子残留在临时目录里的其它副本。

        服务重启、进程被杀、上一次删除失败都会留下残骸；
        不清理的话，每播一次就多一份，几部片子就能堆出几十 GB。
        正在使用的会话（_sessions 里登记着的）一律跳过。
        """
        if not restore_name:
            return 0
        try:
            temp_dir = self.ensure_temp_dir()
            items = self.client.personal_list(temp_dir)
        except Exception as exc:
            logger.warning("清扫临时目录失败（忽略）: %s", exc)
            return 0
        keep = self._active_temp_ids()
        removed = 0
        for item in items:
            if item.is_folder or item.file_id in keep:
                continue
            # 临时文件名形如 TEMP_<毫秒>_<随机>_<片名>，按片名认人
            if not (item.name == restore_name
                    or item.name.endswith("_" + restore_name)):
                continue
            self.cancel_delete(item.file_id)
            if self.delete_quietly(item.file_id):
                removed += 1
        if removed:
            logger.info("清扫临时目录：删除 %d 个 %s 的旧副本", removed, restore_name)
        return removed

    def fetch_link(self, cas_file_id, cas_name):
        """
        取得 .cas 的播放直链：**能复用就复用**。

        返回 (直链, 大小, 临时文件ID, 还原文件名, 是否重新秒传)。

        播放过程中播放器会反复来换直链，如果每次都重新秒传还原，
        临时目录里就会同时躺着好几份完整电影（旧的要等 TTL 才删），
        一部片子就能堆出几十 GB。这里改成：
          1. 已有还原好的临时文件 → 换一条新直链即可，零新增文件；
          2. 临时文件确实没了 → 才重新秒传，且先清掉这部片子的旧副本。
        """
        sess = self._session_valid(cas_file_id)
        if sess:
            link = self._refresh_link(sess["temp_id"])
            if link:
                return link, sess["size"], sess["temp_id"], sess["name"], False
            self._drop_session(cas_file_id)

        # 单飞：并发请求只让第一个去还原，其余的等它出结果后直接复用
        with self._flight_lock(cas_file_id):
            sess = self._session_valid(cas_file_id)
            if sess:
                link = self._refresh_link(sess["temp_id"])
                if link:
                    return link, sess["size"], sess["temp_id"], sess["name"], False
                self._drop_session(cas_file_id)

            link, size, temp_id, real_name, base_name = self.restore_temp(
                cas_file_id, cas_name
            )
            # 先登记会话再清扫：这样新文件会被列入保护名单，不会被自己清掉
            with self._state_lock:
                self._sessions[cas_file_id] = {
                    "temp_id": temp_id,
                    "name": real_name,
                    "base_name": base_name,
                    "size": size,
                    "created_at": time.time(),
                }
            self._purge_leftovers(base_name)
            logger.info("已秒传还原 %s -> %s（临时文件 %s）",
                        cas_name, real_name, temp_id)
            return link, size, temp_id, real_name, True

    def session_count(self):
        """当前保持着的还原会话数量。"""
        with self._state_lock:
            return len(self._sessions)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def delete_quietly(self, file_id):
        """先移入回收站、再彻底删除，失败只记日志不抛错。

        只要文件离开了临时目录就算成功（进了回收站也不再堆在临时目录里）。
        """
        trashed = False
        try:
            self.client.personal_trash([file_id])
            trashed = True
        except Exception as exc:
            logger.warning("移入回收站失败 %s: %s", file_id, exc)
        try:
            self.client.personal_delete([file_id])
            logger.info("已清理临时文件 %s", file_id)
            with self._state_lock:
                self._delete_failures.pop(file_id, None)
            return True
        except Exception as exc:
            if trashed:
                logger.info("临时文件 %s 已进回收站，彻底删除失败（可忽略）: %s",
                            file_id, exc)
                with self._state_lock:
                    self._delete_failures.pop(file_id, None)
                return True
            logger.warning("彻底删除失败 %s: %s", file_id, exc)
            return False

    def cancel_delete(self, file_id):
        """把文件从待删表里摘掉（准备手动删除 / 会话重建时用）。"""
        with self._pending_lock:
            self._pending.pop(file_id, None)
        with self._state_lock:
            self._delete_failures.pop(file_id, None)

    def schedule_delete(self, file_id, delay=None):
        """登记临时文件的延迟删除时间（到点由后台清理线程删掉）。

        对同一个文件重复调用 = **续期**：只推迟删除时间，不会再起新线程。
        旧实现每次调用都新起一个线程，导致续期之后旧线程仍会按时把文件删掉，
        表现就是「播过的视频，过一会儿再播就一直加载」。
        """
        delay = self.temp_ttl if delay is None else delay
        with self._pending_lock:
            self._pending[file_id] = time.time() + delay
        self._ensure_reaper()
        return delay

    def _ensure_reaper(self):
        """保证后台清理线程只起一次。"""
        with self._pending_lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop,
                                            name="cas-reaper", daemon=True)
            self._reaper.start()

    def _reap_loop(self):
        """每 10 秒检查一次，删掉到期的临时文件。

        删除失败的文件会退避重试（最多 6 次）。以前失败一次就永久遗忘，
        接口抖一下就会在云盘里留下一份永远清不掉的大文件。
        """
        while True:
            try:
                time.sleep(10)
                now = time.time()
                with self._pending_lock:
                    due = [fid for fid, exp in list(self._pending.items())
                           if exp <= now]
                    for fid in due:
                        self._pending.pop(fid, None)
                for fid in due:
                    if self.delete_quietly(fid):
                        continue
                    with self._state_lock:
                        tries = self._delete_failures.get(fid, 0) + 1
                        if tries > 6:
                            self._delete_failures.pop(fid, None)
                            logger.error("临时文件 %s 连续 %d 次删除失败，放弃",
                                         fid, tries)
                            continue
                        self._delete_failures[fid] = tries
                    with self._pending_lock:
                        self._pending[fid] = now + 60 * tries
            except Exception as exc:
                logger.warning("临时文件清理线程异常: %s", exc)

    def purge_temp_dir(self, max_age=None):
        """
        清空临时目录里所有残留文件。

        max_age 为 None 时无条件清空；否则只清理创建超过指定秒数的。
        返回清理数量。
        """
        try:
            temp_dir = self.ensure_temp_dir()
            items = self.client.personal_list(temp_dir)
        except Exception as exc:
            raise CASError(f"读取临时目录失败: {exc}") from exc
        count = 0
        for item in items:
            if max_age is not None:
                if item.modified is None:
                    continue
                age = time.time() - item.modified.timestamp()
                if age < max_age:
                    continue
            if self.delete_quietly(item.file_id):
                count += 1
        return count

    def pending_count(self):
        """当前等待延迟删除的临时文件数量。"""
        now = time.time()
        with self._pending_lock:
            snapshot = list(self._pending.values())
        return sum(1 for exp in snapshot if exp > now)
