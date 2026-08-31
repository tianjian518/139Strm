# -*- coding: utf-8 -*-
"""STRM 文件生成器：递归遍历移动云盘目录，为每个媒体文件生成 .strm。"""

import os
import re
from datetime import datetime
from urllib.parse import quote

from .client import Yun139Error

# 常见的媒体后缀
DEFAULT_MEDIA_EXT = [
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v",
    "ts", "rmvb", "rm", "mpg", "mpeg", "3gp", "iso",
]

# 常见字幕/附属文件后缀（可选择直接下载到本地）
DEFAULT_COPY_EXT = ["srt", "ass", "ssa", "sub", "vtt", "nfo", "jpg", "png"]

ILLEGAL_CHARS = r'[\\/:*?"<>|]'


def sanitize_name(name: str) -> str:
    """清理非法文件名字符，兼容 Windows/Emby。"""
    return re.sub(ILLEGAL_CHARS, "_", name).strip().rstrip(".")


class CancelError(Exception):
    """生成任务被手动终止时抛出，用于及时中断递归扫描。"""
    pass


class StrmGenerator:
    def __init__(self, client, base_url, output_dir,
                 media_ext=None, copy_ext=None, min_size_mb=0,
                 recursive=True, strip_cas=True, include_cas=True,
                 force=False, delete_orphans=False):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.output_dir = output_dir
        self.media_ext = [e.lower().lstrip(".") for e in (media_ext or DEFAULT_MEDIA_EXT)]
        self.copy_ext = [e.lower().lstrip(".") for e in (copy_ext or DEFAULT_COPY_EXT)]
        self.min_size = int(min_size_mb * 1024 * 1024)
        self.recursive = recursive
        # 去掉 .cas 尾巴，让 Emby 看到的是 xxx.mp4 而不是 xxx.mp4.cas
        self.strip_cas = strip_cas
        # 是否把 .cas 秒传文件也生成 strm（靠 302 时秒传还原来播放）
        self.include_cas = include_cas
        # 强同步：True 时覆盖已存在的 strm / 附属文件；False 时（增量）跳过已存在
        self.force = force
        # 删除孤儿：扫描结束后，删除本次同步范围内、源头已不存在的 .strm
        # （仅清理 .strm，绝不删字幕/图片等附属文件，避免误删用户手动放置的文件）
        self.delete_orphans = delete_orphans
        # 终止标志：被 cancel() 置 True 后，下一个检查点抛出 CancelError
        self.cancelled = False

        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.copied = 0
        self.deleted = 0
        self.cas_count = 0
        self.errors = []
        self.log_lines = []
        # 本次扫描实际产出/保留的 strm 绝对路径（用于删除孤儿时比对）
        self._produced = set()
        # 本次递归触及的输出目录（删除孤儿时只在这些目录内清理，避免误删其他来源）
        self._touched_dirs = set()

    def log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.log_lines.append(line)
        return line

    def is_media(self, name):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext == "cas" and self.include_cas:
            return True
        return ext in self.media_ext

    def is_copyable(self, name):
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return ext in self.copy_ext

    def cancel(self):
        """请求终止当前生成。会在下一个检查点抛出 CancelError 中断递归。"""
        self.cancelled = True

    def generate(self, folder_id="/", relative_path="", force=None, root_name=None):
        """从指定目录开始递归生成。relative_path 为输出目录下的相对子目录。

        force 为 True 时覆盖已存在的 strm（强同步）；为 None 时沿用实例自身设置。
        root_name 非空时，把"选定目录"的名称作为输出目录的第一层（建一层壳），
        让目录结构更清晰（如选 /电影 → 输出 /strm/电影/...）。
        """
        if force is not None:
            self.force = force
        if self.cancelled:
            raise CancelError("生成已被手动终止")
        # 仅顶层生效：把选定目录名套成输出第一层；递归调用时 relative_path 已非空、root_name 为 None
        if root_name and relative_path == "":
            relative_path = sanitize_name(root_name)
        try:
            items = self.client.list_files(folder_id)
        except Yun139Error as exc:
            self.errors.append(f"列出目录失败 {relative_path or '/'}: {exc}")
            self.log(f"列出目录失败: {exc}")
            return

        target_dir = os.path.join(self.output_dir, relative_path) if relative_path else self.output_dir
        os.makedirs(target_dir, exist_ok=True)
        self._touched_dirs.add(os.path.abspath(target_dir))

        for item in items:
            if self.cancelled:
                raise CancelError("生成已被手动终止")
            if item.is_folder:
                if self.recursive:
                    sub = os.path.join(relative_path, sanitize_name(item.name)) if relative_path \
                        else sanitize_name(item.name)
                    self.generate(item.file_id, sub)
                continue

            if self.is_media(item.name):
                self._make_strm(item, target_dir)
            elif self.is_copyable(item.name):
                self._copy_file(item, target_dir)
            else:
                self.skipped += 1

    def _make_strm(self, item, target_dir):
        if 0 < self.min_size and item.size < self.min_size:
            self.skipped += 1
            return

        raw_name = item.name
        is_cas = raw_name.lower().endswith(".cas")
        name = raw_name[:-4] if (self.strip_cas and is_cas) else raw_name
        safe = sanitize_name(name)
        strm_path = os.path.join(target_dir, safe + ".strm")

        exists = os.path.exists(strm_path)
        # 增量模式（默认）：同名 strm 已存在就跳过（源头仍在，记录为已产出）
        if exists and not self.force:
            self.skipped += 1
            self._produced.add(os.path.abspath(strm_path))
            return

        # 关键：strm 内容指向我们自己的 /d/<file_id>，
        # 播放时由服务端 302 到移动云盘直链，本机不中转流量
        url = f"{self.base_url}/d/{item.file_id}"
        if is_cas:
            # 带上原始文件名，302 时才知道要做秒传还原
            url += "?cas=" + quote(raw_name, safe="")
        try:
            with open(strm_path, "w", encoding="utf-8") as fp:
                fp.write(url)
            self._produced.add(os.path.abspath(strm_path))
            if exists:
                self.updated += 1
                self.log(f"更新 {safe}.strm" + ("（CAS 秒传）" if is_cas else ""))
            else:
                self.created += 1
                if is_cas:
                    self.cas_count += 1
                self.log(f"生成 {safe}.strm" + ("（CAS 秒传）" if is_cas else ""))
        except OSError as exc:
            self.errors.append(f"写入失败 {safe}: {exc}")

    def _copy_file(self, item, target_dir):
        """把字幕等小文件真正下载到本地（体积不大，可接受）。"""
        safe = sanitize_name(item.name)
        dest = os.path.join(target_dir, safe)
        if os.path.exists(dest) and not self.force:
            return
        try:
            url = self.client.get_download_url(item.file_id)
            import requests
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            with open(dest, "wb") as fp:
                fp.write(resp.content)
            self.copied += 1
            self.log(f"下载附属文件 {safe}")
        except Exception as exc:
            self.errors.append(f"下载失败 {safe}: {exc}")

    def clean_orphans(self):
        """扫描结束后，删除本次同步范围内、源头已不存在的 .strm（仅限 .strm）。

        删除范围严格限定在 self._touched_dirs（本次递归触及的输出目录），
        不会动其他来源的 strm，也不会删字幕/图片等附属文件。
        仅当 delete_orphans=True 时调用。
        """
        if not self.delete_orphans:
            return
        for d in sorted(self._touched_dirs):
            try:
                entries = os.listdir(d)
            except OSError as exc:
                self.errors.append(f"清理孤儿时无法读取目录 {d}: {exc}")
                continue
            for fn in entries:
                if not fn.lower().endswith(".strm"):
                    continue
                full = os.path.abspath(os.path.join(d, fn))
                if full in self._produced:
                    continue
                try:
                    os.remove(full)
                    self.deleted += 1
                    self.log(f"删除孤儿 {fn}（源头已不存在）")
                except OSError as exc:
                    self.errors.append(f"删除失败 {fn}: {exc}")

    def summary(self):
        return {
            "cancelled": self.cancelled,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "cas": self.cas_count,
            "skipped": self.skipped,
            "copied": self.copied,
            "errors": self.errors,
            "logs": self.log_lines[-200:],
        }
