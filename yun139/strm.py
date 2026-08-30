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


class StrmGenerator:
    def __init__(self, client, base_url, output_dir,
                 media_ext=None, copy_ext=None, min_size_mb=0,
                 recursive=True, strip_cas=True, include_cas=True):
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

        self.created = 0
        self.skipped = 0
        self.copied = 0
        self.cas_count = 0
        self.errors = []
        self.log_lines = []

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

    def generate(self, folder_id="/", relative_path=""):
        """从指定目录开始递归生成。relative_path 为输出目录下的相对子目录。"""
        try:
            items = self.client.list_files(folder_id)
        except Yun139Error as exc:
            self.errors.append(f"列出目录失败 {relative_path or '/'}: {exc}")
            self.log(f"列出目录失败: {exc}")
            return

        target_dir = os.path.join(self.output_dir, relative_path) if relative_path else self.output_dir
        os.makedirs(target_dir, exist_ok=True)

        for item in items:
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

        if os.path.exists(strm_path):
            self.skipped += 1
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
        if os.path.exists(dest):
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

    def summary(self):
        return {
            "created": self.created,
            "cas": self.cas_count,
            "skipped": self.skipped,
            "copied": self.copied,
            "errors": self.errors,
            "logs": self.log_lines[-200:],
        }
