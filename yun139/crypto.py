# -*- coding: utf-8 -*-
"""
移动云盘（139Yun）协议层：签名与加解密

本模块严格对照 OpenList drivers/139/util.go 实现，逐行对齐 Go 语义，
特别是以下几处容易出错的细节：

1. ``encodeURIComponent``
   Go 用 url.QueryEscape（除 A-Za-z0-9-_.~ 外全部转义，空格转 +），
   随后又把 + 换回 %20，并把 %21 %27 %28 %29 %2A 还原成 ! ' ( ) *。
   Python 的 quote 需要手工补上这套还原规则。

2. ``calSign`` 的字符排序
   Go 的 strings.Split(s, "") 是按 rune（字符）切分，不是按字节。
   Python 的 list(str) 同样是按字符，语义一致；排序结果等价于 UTF-8 字节序。

3. 密钥
   KEY_HEX_1 是 24 字节（AES-192），KEY_HEX_2 是 16 字节（AES-128），
   两者都是十六进制字符串，使用前需 hex 解码。
"""

import base64
import hashlib
import json
import os
import decimal
from urllib.parse import quote

from Crypto.Cipher import AES

# 第一层加解密密钥（AES-192），hex 解码后为 "scB5IPbIS1QSsulsNrS0lg=="
KEY_HEX_1 = "73634235495062495331515373756c734e7253306c673d3d"
# 第二层解密密钥（AES-128），hex 解码后为 "qPqDw263XgFgL3u8"
KEY_HEX_2 = "7150714477323633586746674c337538"


# --------------------------------------------------------------------------
# 编码 / 哈希
# --------------------------------------------------------------------------

def encode_uri_component(s: str) -> str:
    """等价 Go: encodeURIComponent(url.QueryEscape(str) + 手工还原)。"""
    # quote 的 safe 置空，保证 '/' 等全部转义；Python 空格转 %20，与 Go 还原后一致
    r = quote(s, safe="")
    # 对齐 Go 里的 strings.Replace 还原规则
    r = (r.replace("%21", "!")
          .replace("%27", "'")
          .replace("%28", "(")
          .replace("%29", ")")
          .replace("%2A", "*"))
    return r


def md5_hex(s: str) -> str:
    """小写 32 位 md5，等价 Go utils.GetMD5EncodeStr。"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def cal_sign(body: str, ts: str, rand_str: str) -> str:
    """
    等价 Go: calSign(body, ts, randStr)
      body  -> encodeURIComponent -> 字符排序拼接 -> base64
      res   = md5(base64) + md5(ts + ":" + randStr)
      return md5(res).upper()
    """
    body = encode_uri_component(body)
    # Go 按 rune 排序；Python list(str) 按字符，语义一致
    ordered = "".join(sorted(body))
    encoded = base64.b64encode(ordered.encode("utf-8")).decode("utf-8")
    res = md5_hex(encoded) + md5_hex(ts + ":" + rand_str)
    return md5_hex(res).upper()


# --------------------------------------------------------------------------
# AES
# --------------------------------------------------------------------------

def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padding = block_size - len(data) % block_size
    return data + bytes([padding]) * padding


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("pkcs7: data is empty")
    unpadding = data[-1]
    if unpadding > len(data):
        raise ValueError("pkcs7: invalid padding")
    return data[:-unpadding]


def aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pkcs7_pad(plaintext, len(key) if len(key) == 16 else 16))


def aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return pkcs7_unpad(cipher.decrypt(ciphertext))


def aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """等价 Go aes_ecb_decrypt：逐块 ECB 解密后去 PKCS7 填充。"""
    cipher = AES.new(key, AES.MODE_ECB)
    return pkcs7_unpad(cipher.decrypt(ciphertext))


# --------------------------------------------------------------------------
# 登录用的两层加密请求体
# --------------------------------------------------------------------------

def go_json_string(s: str) -> str:
    """
    对齐 Go encoding/json 的字符串编码。

    Go 默认开启 HTML escaping，会把 & < > 分别转成 \\u0026 \\u003c \\u003e，
    而 Python 的 json.dumps(ensure_ascii=False) 不会，必须手工补上，
    否则加密请求体与官方实现不一致，服务端会解不开。
    """
    out = ['"']
    for ch in s:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "<":
            out.append("\\u003c")
        elif ch == ">":
            out.append("\\u003e")
        elif ch == "&":
            out.append("\\u0026")
        elif code == 0x2028:
            out.append("\\u2028")
        elif code == 0x2029:
            out.append("\\u2029")
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def go_float_str(f: float) -> str:
    """
    对齐 Go fmt.Sprintf("%v", float64)，即 %g + shortest：

    记有效数字位数为 nd、十进制指数为 e（value = 0.digits * 10^(e+1)），
    当 e < -4 或 e >= 6 时使用科学计数法，形如 1.3800138e+10、1e-05。

    这个细节很致命：官方实现里 msisdn "13800138000" 会被序列化成
    1.3800138e+10 而不是 13800138000，写错了登录就会失败。
    """
    import math

    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "+Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0"

    neg = f < 0
    af = abs(f)

    d = decimal.Decimal(repr(af)).normalize()
    sign, digits, exp = d.as_tuple()
    digits_str = "".join(str(x) for x in digits).rstrip("0")
    if not digits_str:
        return "0"
    nd = len(digits_str)
    dp = nd + exp          # value = 0.digits * 10^dp
    e = dp - 1

    prefix = "-" if neg else ""
    if e < -4 or e >= 6:
        mant = digits_str[0] + ("." + digits_str[1:] if nd > 1 else "")
        return "%s%se%s%02d" % (prefix, mant, "+" if e >= 0 else "-", abs(e))
    if dp <= 0:
        return "%s0.%s%s" % (prefix, "0" * (-dp), digits_str)
    if dp >= nd:
        return "%s%s%s" % (prefix, digits_str, "0" * (dp - nd))
    return "%s%s.%s" % (prefix, digits_str[:dp], digits_str[dp:])


def sorted_json_stringify(obj, _from_json: bool = False) -> str:
    """
    等价 Go sortedJsonStringify：JSON 按键名升序、紧凑输出（无空格）。

    格式形如 {"k":v,"k2":v2}，冒号与逗号后均无空格。

    ``_from_json`` 标记该值是否由 JSON 字符串解析而来。Go 里 json.Unmarshal
    到 interface{} 的数字一律是 float64，所以解析出来的数字必须走
    go_float_str；而直接写在 map 里的 Go int 用 %v 输出为普通整数。
    """
    if obj is None:
        return "null"
    if isinstance(obj, str):
        if not _from_json:
            # Go 的逻辑：先尝试当作 JSON 解析，成功则递归，否则按普通字符串输出
            try:
                parsed = json.loads(obj)
            except (ValueError, TypeError):
                return go_json_string(obj)
            return sorted_json_stringify(parsed, _from_json=True)
        return go_json_string(obj)
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)):
        if _from_json or isinstance(obj, float):
            return go_float_str(float(obj))
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(sorted_json_stringify(i, _from_json) for i in obj) + "]"
    if isinstance(obj, dict):
        pairs = []
        for key in sorted(obj.keys()):
            pairs.append(
                go_json_string(str(key)) + ":" + sorted_json_stringify(obj[key], _from_json)
            )
        return "{" + ",".join(pairs) + "}"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def encrypt_body(body: dict, aes_key_hex: str = KEY_HEX_1) -> str:
    """
    构造加密请求体，等价 Go yun139EncryptedRequest 的第 2~3 步：
      payload = base64(iv(16B) + aes_cbc(sorted_json, key))
    返回可直接 POST 出去的字符串。
    """
    aes_key = bytes.fromhex(aes_key_hex)
    sorted_json = sorted_json_stringify(body)
    iv = os.urandom(16)
    encrypted = aes_cbc_encrypt(sorted_json.encode("utf-8"), aes_key, iv)
    return base64.b64encode(iv + encrypted).decode("utf-8")


def decrypt_response(resp_body: str, aes_key_hex: str = KEY_HEX_1) -> bytes:
    """解密响应：base64 解开后前 16 字节是 IV，其后是 AES-CBC 密文。"""
    aes_key = bytes.fromhex(aes_key_hex)
    decoded = base64.b64decode(resp_body)
    if len(decoded) < 16:
        raise ValueError("响应过短，不是合法的加密体")
    return aes_cbc_decrypt(decoded[16:], aes_key, decoded[:16])


def random_str(n: int = 16) -> str:
    """生成随机字符串，对应 Go random.String(16)。"""
    import random
    import string
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
