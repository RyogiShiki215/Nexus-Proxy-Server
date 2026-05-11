"""
Nexus Proxy Server - 中转代理服务器 (纯标准库版本)
无需任何第三方依赖，仅使用 Python 内置模块。

功能：
1. 身份池管理（deviceId + proxy + UA 原子绑定）
2. 复刻签名逻辑调用上游
3. 对下游暴露 OpenAI 兼容 API
4. 管理后台 API（密钥管理、身份池、设定）
5. 内置静态文件服务（管理面板）
"""

import hashlib
import json
import atexit
import os
import random
import secrets
import string
import time
import threading
import ssl
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen, ProxyHandler as UrllibProxyHandler, build_opener
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse, unquote
from pathlib import Path
from io import BytesIO

# ============================================================
# 配置
# ============================================================

HOST = os.getenv("NEXUS_HOST", "0.0.0.0")
PORT = int(os.getenv("NEXUS_PORT", os.getenv("PORT", "9800")))
MAX_BODY_SIZE = 512 * 1024  # 512KB 请求体上限

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATIC_DIR = BASE_DIR / "static"

UPSTREAM_URL = os.getenv("NEXUS_UPSTREAM_URL", "https://example.com/api/optimize")
WC_SECRET = os.getenv("NEXUS_WC_SECRET", "change-me")

# 加载 UA 列表
UA_FILE = BASE_DIR / "user-agents.txt"
USER_AGENTS = []
if UA_FILE.exists():
    USER_AGENTS = [line.strip() for line in UA_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]

# ============================================================
# 安全：密码哈希 + IP 限速
# ============================================================

def _hash_password(pwd):
    """SHA-256 哈希密码"""
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()

def _verify_password(pwd, pwd_hash):
    """验证密码是否匹配哈希"""
    return _hash_password(pwd) == pwd_hash


class RateLimiter:
    """IP 限速器：防暴力破解"""
    def __init__(self, max_requests=20, window_seconds=60):
        self._max = max_requests
        self._window = window_seconds
        self._records = {}  # ip -> [timestamps]
        self._lock = threading.Lock()
        self._banned = {}   # ip -> ban_until_ts

    def is_allowed(self, ip):
        now = time.time()
        with self._lock:
            # 检查是否被封禁
            if ip in self._banned:
                if now < self._banned[ip]:
                    return False
                else:
                    del self._banned[ip]

            # 清理过期记录
            if ip in self._records:
                self._records[ip] = [t for t in self._records[ip] if now - t < self._window]
            else:
                self._records[ip] = []

            if len(self._records[ip]) >= self._max:
                # 超限，封禁 5 分钟
                self._banned[ip] = now + 300
                print(f"  [安全] IP {ip} 请求过于频繁，封禁 5 分钟")
                return False

            self._records[ip].append(now)
            return True

    def record_fail(self, ip):
        """记录认证失败，连续 5 次失败封禁 10 分钟"""
        now = time.time()
        with self._lock:
            key = f"fail_{ip}"
            if key not in self._records:
                self._records[key] = []
            self._records[key] = [t for t in self._records[key] if now - t < 600]
            self._records[key].append(now)
            if len(self._records[key]) >= 5:
                self._banned[ip] = now + 600
                print(f"  [安全] IP {ip} 认证失败过多，封禁 10 分钟")


rate_limiter = RateLimiter(max_requests=30, window_seconds=60)


class ApiRpmLimiter:
    """API Key 每分钟请求数限制器"""
    def __init__(self):
        self._records = {}  # key -> [timestamps]
        self._lock = threading.Lock()

    def is_allowed(self, key, rpm_limit):
        """检查该 key 是否在 RPM 限制内"""
        now = time.time()
        with self._lock:
            if key not in self._records:
                self._records[key] = []
            # 清理 60 秒前的记录
            self._records[key] = [t for t in self._records[key] if now - t < 60]
            if len(self._records[key]) >= rpm_limit:
                return False
            self._records[key].append(now)
            return True


_api_rpm_limiter = ApiRpmLimiter()

# ============================================================
# 数据持久化（线程安全）
# ============================================================

_data_lock = threading.Lock()

SETTINGS_DEFAULTS = {
    "mode": "random",
    "admin_password_hash": _hash_password("admin123"),
    "upstream_url": UPSTREAM_URL,
    "wc_secret": WC_SECRET,
    "max_timeout": 320,
    "auto_register": False,
    "auto_register_target": 20,
    "auto_register_interval": 60,
    "auto_register_max_per_hour": 5,
    "auto_clean_exhausted": True,
    "max_body_size": 512,
    "api_rpm_limit": 0,
    "api_prepend_enabled": False,
    "api_prepend_prompt": "",
    "timezone": "UTC",
}

# 常用时区及其 UTC 偏移（小时）
TIMEZONE_OFFSETS = {
    "UTC": 0,
    "Europe/London": 0,
    "Europe/Paris": 1,
    "Europe/Berlin": 1,
    "Europe/Moscow": 3,
    "Asia/Dubai": 4,
    "Asia/Kolkata": 5.5,
    "Asia/Bangkok": 7,
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Australia/Sydney": 10,
    "Pacific/Auckland": 12,
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "America/Los_Angeles": -8,
}


def _get_timezone_offset_seconds(settings=None):
    """获取当前设定时区的 UTC 偏移（秒）"""
    settings = settings or get_settings()
    tz_name = settings.get("timezone", "UTC")
    offset_hours = TIMEZONE_OFFSETS.get(tz_name, 0)
    return int(offset_hours * 3600)

_settings_lock = threading.Lock()

def _load_json(filename, default=None):
    fp = DATA_DIR / filename
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default if default is not None else {}

def _save_json(filename, data):
    fp = DATA_DIR / filename
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ============================================================
# 身份池管理
# ============================================================

class IdentityPool:
    def __init__(self):
        self._pool = _load_json("identity_pool.json", [])
        self._lock = threading.Lock()

    def save(self):
        _save_json("identity_pool.json", self._pool)

    def _gen_device_id(self):
        chars = string.ascii_lowercase + string.digits
        return "device-" + ''.join(random.choices(chars, k=16))

    def add(self, proxy="", ua="", device_id="", max_per_hour=5, fake_ip=""):
        identity = {
            "deviceId": device_id or self._gen_device_id(),
            "ua": ua or (random.choice(USER_AGENTS) if USER_AGENTS else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
            "proxy": proxy,
            "fake_ip": fake_ip or _random_ip(),
            "used_count": 0,
            "max_per_hour": max_per_hour,
            "last_reset_ts": int(time.time()),
            "enabled": True,
        }
        with self._lock:
            self._pool.append(identity)
            self.save()
        return identity

    def remove(self, device_id):
        with self._lock:
            self._pool = [i for i in self._pool if i["deviceId"] != device_id]
            self.save()

    def get_all(self):
        with self._lock:
            return list(self._pool)

    def _reset_if_needed(self, ident):
        now = int(time.time())
        if now - ident.get("last_reset_ts", 0) >= 3600:
            ident["used_count"] = 0
            ident["last_reset_ts"] = now

    def pick(self, mode="random", exclude_ids=None):
        """
        选取一个可用身份。
        exclude_ids: 本次请求中已经尝试过但失败的 deviceId 列表，避免重复选取。
        """
        with self._lock:
            exclude = set(exclude_ids or [])
            available = []
            for ident in self._pool:
                if not ident.get("enabled", True):
                    continue
                if ident["deviceId"] in exclude:
                    continue
                self._reset_if_needed(ident)
                if ident["used_count"] < ident["max_per_hour"]:
                    available.append(ident)

            if not available:
                return None

            if mode == "exhaust":
                available.sort(key=lambda x: x["used_count"], reverse=True)
                chosen = available[0]
            else:
                chosen = random.choice(available)

            chosen["used_count"] += 1
            self.save()
            return dict(chosen)

    def mark_exhausted(self, device_id):
        """收到 429 时，将该身份的 used_count 设为上限，本小时内不再使用"""
        with self._lock:
            for ident in self._pool:
                if ident["deviceId"] == device_id:
                    ident["used_count"] = ident["max_per_hour"]
                    self.save()
                    break

    def batch_add(self, count, proxies=None, max_per_hour=5):
        results = []
        for i in range(count):
            proxy = proxies[i] if proxies and i < len(proxies) else ""
            ua = random.choice(USER_AGENTS) if USER_AGENTS else "Mozilla/5.0"
            results.append(self.add(proxy=proxy, ua=ua, max_per_hour=max_per_hour, fake_ip=""))
        return results

    def remove_exhausted(self):
        """删除所有本小时已耗尽（used_count >= max_per_hour）的身份"""
        with self._lock:
            now = int(time.time())
            removed = 0
            new_pool = []
            for ident in self._pool:
                # 先检查是否需要重置
                if now - ident.get("last_reset_ts", 0) >= 3600:
                    new_pool.append(ident)  # 已重置，保留
                elif ident["used_count"] >= ident["max_per_hour"]:
                    removed += 1  # 耗尽，删除
                else:
                    new_pool.append(ident)
            self._pool = new_pool
            self.save()
            return removed

    def remove_all(self):
        """删除所有身份"""
        with self._lock:
            count = len(self._pool)
            self._pool = []
            self.save()
            return count

    def count(self):
        with self._lock:
            return len(self._pool)


pool = IdentityPool()

# ============================================================
# API Key 管理
# ============================================================

class KeyManager:
    USAGE_FLUSH_INTERVAL = 5

    def __init__(self):
        self._keys = _load_json("api_keys.json", {})
        self._lock = threading.RLock()
        self._usage_dirty = False
        self._last_usage_flush = time.time()

    def save(self):
        _save_json("api_keys.json", self._keys)
        self._usage_dirty = False
        self._last_usage_flush = time.time()

    def create(self, name="", rpm_limit=0, max_usage=0, expires_at=0):
        key = "sk-wc-" + secrets.token_hex(24)
        with self._lock:
            self._keys[key] = {
                "name": name,
                "created": int(time.time()),
                "enabled": True,
                "usage": 0,
                "rpm_limit": rpm_limit,
                "max_usage": max_usage,
                "expires_at": expires_at,
            }
            self.save()
        return key

    def validate(self, key):
        info = self.get_info(key)
        return bool(info and info.get("enabled", False))

    def get_info(self, key):
        with self._lock:
            info = self._keys.get(key)
            return dict(info) if info else None

    def record_usage(self, key):
        with self._lock:
            if key in self._keys:
                self._keys[key]["usage"] = self._keys[key].get("usage", 0) + 1
                self._usage_dirty = True
                now = time.time()
                if now - self._last_usage_flush >= self.USAGE_FLUSH_INTERVAL:
                    self.save()

    def flush_usage(self):
        with self._lock:
            if self._usage_dirty:
                self.save()

    def list_all(self):
        with self._lock:
            return {key: dict(info) for key, info in self._keys.items()}

    def revoke(self, key):
        with self._lock:
            if key in self._keys:
                self._keys[key]["enabled"] = False
                self.save()

    def delete(self, key):
        with self._lock:
            if key in self._keys:
                del self._keys[key]
                self.save()


keys = KeyManager()
atexit.register(keys.flush_usage)


# ============================================================
# 使用量统计（用于仪表盘曲线图）
# ============================================================

class UsageTracker:
    """记录每次 API 调用的时间戳和 key，用于绘制使用量曲线"""
    MAX_RECORDS = 100000  # 最多保留 10 万条记录

    def __init__(self):
        raw = _load_json("usage_log.json", [])
        # 兼容旧格式：如果是纯时间戳列表，转换为 {time, key} 格式
        self._records = []
        for item in raw:
            if isinstance(item, dict):
                self._records.append(item)
            else:
                self._records.append({"time": item, "key": ""})
        self._lock = threading.Lock()
        self._dirty = False
        self._last_flush = time.time()

    def record(self, key_name=""):
        now = int(time.time())
        with self._lock:
            self._records.append({"time": now, "key": key_name})
            # 清理超过 30 天的记录
            cutoff = now - 30 * 86400
            if self._records and self._records[0]["time"] < cutoff:
                self._records = [r for r in self._records if r["time"] >= cutoff]
            # 限制总量
            if len(self._records) > self.MAX_RECORDS:
                self._records = self._records[-self.MAX_RECORDS:]
            self._dirty = True
            if time.time() - self._last_flush >= 10:
                self._flush()

    def _flush(self):
        _save_json("usage_log.json", self._records)
        self._dirty = False
        self._last_flush = time.time()

    def flush(self):
        with self._lock:
            if self._dirty:
                self._flush()

    def get_stats(self, granularity, count, tz_offset=0):
        """
        获取使用量统计，按固定粒度分桶。
        granularity: 每根柱子代表的秒数 (3600=1小时, 86400=1天, 604800=1周, 2592000=1月)
        count: 返回多少根柱子
        tz_offset: 时区偏移秒数，用于对齐日边界到本地时间
        返回每个桶的总数和每个 key 的明细。
        """
        now = int(time.time())
        # 将 now 对齐到粒度边界（考虑时区偏移）
        if granularity >= 86400:
            # 加上时区偏移后对齐到当地午夜，再减回偏移得到 UTC 时间戳
            local_now = now + tz_offset
            aligned_now = ((local_now // 86400) + 1) * 86400 - tz_offset
        else:
            aligned_now = (now // 3600 + 1) * 3600

        cutoff = aligned_now - granularity * count
        with self._lock:
            recent = [r for r in self._records if r["time"] >= cutoff]

        result = []
        for i in range(count):
            bucket_start = cutoff + i * granularity
            bucket_end = bucket_start + granularity
            bucket_records = [r for r in recent if bucket_start <= r["time"] < bucket_end]
            # 按 key 分组统计
            key_counts = {}
            for r in bucket_records:
                k = r.get("key") or "管理面板"
                key_counts[k] = key_counts.get(k, 0) + 1
            result.append({
                "time": int(bucket_start),
                "count": len(bucket_records),
                "keys": key_counts,
            })
        return result


usage_tracker = UsageTracker()
atexit.register(usage_tracker.flush)

# ============================================================
# 设定
# ============================================================

def _init_settings():
    """初始化设定，每次启动生成新的随机管理密码"""
    settings = _load_json("settings.json", None)
    # 每次启动都生成新的随机密码
    raw_pwd = secrets.token_urlsafe(12)
    if settings is None:
        settings = dict(SETTINGS_DEFAULTS)
    else:
        # 清理旧版明文字段
        settings.pop("admin_password", None)
        for key, value in SETTINGS_DEFAULTS.items():
            settings.setdefault(key, value)
    # 每次启动覆盖密码哈希
    settings["admin_password_hash"] = _hash_password(raw_pwd)
    _save_json("settings.json", settings)
    return settings, raw_pwd


# 启动时初始化
_current_settings, _startup_password = _init_settings()


def get_settings():
    with _settings_lock:
        return dict(_current_settings)

def save_settings(s):
    global _current_settings
    with _settings_lock:
        _current_settings = dict(s)
        _save_json("settings.json", _current_settings)

# ============================================================
# 签名逻辑
# ============================================================

def sign_body(body_str, settings=None):
    settings = settings or get_settings()
    secret = settings.get("wc_secret", WC_SECRET)
    ts = str(int(time.time()))
    sig = hashlib.sha256((ts + body_str + secret).encode("utf-8")).hexdigest()
    return ts, sig

# ============================================================
# 上游请求
# ============================================================

def _random_ip():
    """生成一个随机的公网 IP 地址"""
    while True:
        ip = f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
        # 排除私有/保留地址段
        first = int(ip.split('.')[0])
        if first in (10, 127) or ip.startswith("192.168.") or ip.startswith("172.16."):
            continue
        return ip


def call_upstream(messages, identity):
    settings = get_settings()
    upstream_url = settings.get("upstream_url", UPSTREAM_URL)
    timeout = settings.get("max_timeout", 320)

    body_obj = {"messages": messages, "deviceId": identity["deviceId"]}
    body_str = json.dumps(body_obj, ensure_ascii=False, separators=(',', ':'))
    ts, sig = sign_body(body_str, settings=settings)

    fake_ip = identity.get("fake_ip") or _random_ip()
    headers = {
        "Content-Type": "application/json",
        "X-WC-Ts": ts,
        "X-WC-Sig": sig,
        "User-Agent": identity["ua"],
        "X-Forwarded-For": fake_ip,
        "X-Real-IP": fake_ip,
        "CF-Connecting-IP": fake_ip,
        "True-Client-IP": fake_ip,
    }

    body_bytes = body_str.encode("utf-8")
    req = Request(upstream_url, data=body_bytes, headers=headers, method="POST")

    # 代理设置
    proxy_url = identity.get("proxy", "")
    # 忽略 SSL 验证（兼容自签名或临时上游地址）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        import urllib.request
        https_handler = urllib.request.HTTPSHandler(context=ctx)
        if proxy_url:
            phandler2 = UrllibProxyHandler({"http": proxy_url, "https": proxy_url})
            full_opener = build_opener(https_handler, phandler2)
        else:
            full_opener = build_opener(https_handler)

        print(f"  [上游] 正在请求 {upstream_url} (deviceId={identity['deviceId'][:12]}...)")
        resp = full_opener.open(req, timeout=timeout)
        raw = resp.read().decode("utf-8")
        print(f"  [上游] 收到响应: {raw[:200]}")
        data = json.loads(raw)
        return data
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        print(f"  [上游] HTTP错误 {e.code}: {body[:200]}")
        error_msg = ""
        try:
            error_msg = json.loads(body).get("error", "")
        except Exception:
            error_msg = body[:200]
        return {"success": False, "error": error_msg or f"HTTP {e.code}", "status": e.code}
    except Exception as e:
        print(f"  [上游] 异常: {type(e).__name__}: {e}")
        return {"success": False, "error": str(e), "status": 502}


def run_upstream_with_retry(messages, mode, max_retries=3):
    tried_ids = []
    result = None
    identity = None

    for attempt in range(max_retries):
        identity = pool.pick(mode=mode, exclude_ids=tried_ids)
        if not identity:
            return None, None

        result = call_upstream(messages, identity)

        if result.get("status") == 429:
            pool.mark_exhausted(identity["deviceId"])
            tried_ids.append(identity["deviceId"])
            continue

        break

    return result, identity

# ============================================================
# HTTP 请求处理器
# ============================================================

class AppHandler(BaseHTTPRequestHandler):
    """处理所有 HTTP 请求"""
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {self.client_address[0]} {format % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True

    def _send_error(self, status, msg):
        self._send_json({"detail": msg}, status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        settings = get_settings()
        max_size = settings.get("max_body_size", 512) * 1024  # KB -> bytes
        if length > max_size:
            raise ValueError(f"Request body too large: {length} > {max_size}")
        if length > 0:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        return {}

    def _check_admin(self):
        ip = self.client_address[0]
        # 先检查是否被封禁（仅针对认证失败的封禁）
        if ip in rate_limiter._banned:
            now = time.time()
            if now < rate_limiter._banned[ip]:
                self._send_error(429, "Too many requests, try again later")
                return False
            else:
                with rate_limiter._lock:
                    rate_limiter._banned.pop(ip, None)
        settings = get_settings()
        pwd = self.headers.get("X-Admin-Password", "")
        pwd_hash = settings.get("admin_password_hash", "")
        if not pwd or not _verify_password(pwd, pwd_hash):
            rate_limiter.record_fail(ip)
            self._send_error(403, "Admin password incorrect")
            return False
        return True

    def _check_api_key(self):
        auth = self.headers.get("Authorization", "")
        key = auth.replace("Bearer ", "").strip()
        key_info = keys.get_info(key)
        if not key_info or not key_info.get("enabled", False):
            self._send_error(401, "Invalid API key")
            return None
        # 过期时间检查
        expires_at = key_info.get("expires_at", 0)
        if expires_at and expires_at > 0 and int(time.time()) > expires_at:
            self._send_error(403, "API key has expired")
            return None
        # 最大使用次数检查
        max_usage = key_info.get("max_usage", 0)
        if max_usage and max_usage > 0 and key_info.get("usage", 0) >= max_usage:
            self._send_error(403, f"API key usage limit reached: {max_usage}")
            return None
        # RPM 限制：优先用 Key 自身设定，否则用全局默认
        rpm_limit = key_info.get("rpm_limit", 0)
        if not rpm_limit:
            settings = get_settings()
            rpm_limit = settings.get("api_rpm_limit", 0)
        if rpm_limit and rpm_limit > 0:
            if not _api_rpm_limiter.is_allowed(key, rpm_limit):
                self._send_error(429, f"API rate limit exceeded: {rpm_limit} RPM")
                return None
        return key

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        # 管理 API
        if path == "/api/admin/status":
            if not self._check_admin(): return
            self._handle_status()
        elif path == "/api/admin/identities":
            if not self._check_admin(): return
            self._send_json(pool.get_all())
        elif path == "/api/admin/keys":
            if not self._check_admin(): return
            self._send_json(keys.list_all())
        elif path == "/api/admin/settings":
            if not self._check_admin(): return
            self._send_json(get_settings())
        elif path.startswith("/api/admin/usage-stats"):
            if not self._check_admin(): return
            self._handle_usage_stats()
        # OpenAI 兼容 - 模型列表
        elif path == "/v1/models":
            self._handle_models()
        # 静态文件
        elif path == "/" or path == "":
            self._serve_static("index.html")
        elif path.startswith("/static/"):
            filename = path[len("/static/"):]
            self._serve_static(filename)
        else:
            self._send_error(404, "Not found")

    def do_POST(self):
        path = self.path.split("?")[0]

        # OpenAI 兼容端点
        if path == "/v1/chat/completions":
            self._handle_chat_completions()

        # 直接使用
        elif path == "/api/direct-optimize":
            if not self._check_admin(): return
            self._handle_direct_optimize()

        # 管理 - 身份池
        elif path == "/api/admin/identities":
            if not self._check_admin(): return
            self._handle_add_identity()
        elif path == "/api/admin/identities/batch":
            if not self._check_admin(): return
            self._handle_batch_add()
        elif path == "/api/admin/identities/remove-exhausted":
            if not self._check_admin(): return
            removed = pool.remove_exhausted()
            self._send_json({"removed": removed})
        elif path == "/api/admin/identities/remove-all":
            if not self._check_admin(): return
            removed = pool.remove_all()
            self._send_json({"removed": removed})

        # 管理 - 密钥
        elif path == "/api/admin/keys":
            if not self._check_admin(): return
            self._handle_create_key()
        elif path.startswith("/api/admin/keys/") and path.endswith("/revoke"):
            if not self._check_admin(): return
            key = unquote(path[len("/api/admin/keys/"):-len("/revoke")])
            keys.revoke(key)
            self._send_json({"ok": True})

        # 管理 - 设定
        elif path == "/api/admin/settings":
            if not self._check_admin(): return
            self._handle_update_settings()

        else:
            self._send_error(404, "Not found")

    def do_DELETE(self):
        path = self.path.split("?")[0]

        if path.startswith("/api/admin/identities/"):
            if not self._check_admin(): return
            device_id = unquote(path[len("/api/admin/identities/"):])
            pool.remove(device_id)
            self._send_json({"ok": True})

        elif path.startswith("/api/admin/keys/"):
            if not self._check_admin(): return
            key = unquote(path[len("/api/admin/keys/"):])
            keys.delete(key)
            self._send_json({"ok": True})

        else:
            self._send_error(404, "Not found")

    # --- 业务处理 ---

    def _handle_chat_completions(self):
        api_key = self._check_api_key()
        if not api_key:
            return

        try:
            body = self._read_body()
        except Exception:
            self._send_error(400, "Invalid JSON body")
            return

        messages = body.get("messages", [])
        if not messages:
            self._send_error(400, "messages is empty")
            return

        is_stream = body.get("stream", False)

        settings = get_settings()
        mode = settings.get("mode", "random")
        msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]
        prepend_prompt = settings.get("api_prepend_prompt", "").strip()
        if settings.get("api_prepend_enabled") and prepend_prompt:
            msgs.insert(0, {"role": "system", "content": prepend_prompt})

        # 只要发送了请求就计数（不管是否成功）
        keys.record_usage(api_key)
        # 获取 key 的备注名用于图表显示
        _key_info = keys.get_info(api_key)
        _key_label = (_key_info.get("name") if _key_info else "") or api_key[:12]
        usage_tracker.record(_key_label)

        # 429 自动重试：最多尝试 3 个不同身份
        result, identity = run_upstream_with_retry(msgs, mode)

        if not identity:
            self._send_error(503, "No available identity in pool")
            return

        # call_upstream 只在出错时设置 "status" 字段
        is_error = not result or result.get("status")
        if is_error:
            status = result.get("status", 502) if result else 502
            error = result.get("error", "upstream error") if result else "no identity available"
            print(f"  [API] 返回错误给调用方: {status} {error}")
            self._send_error(status, error)
            return

        content = result.get("content", "") or ""
        model = result.get("model", "wc-optimizer")
        cmpl_id = f"chatcmpl-{secrets.token_hex(12)}"
        print(f"  [API] 成功，返回内容长度: {len(content)}, stream={is_stream}")

        if is_stream:
            # SSE 流式返回 (标准 OpenAI 格式)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "*")
            self.end_headers()

            created = int(time.time())

            # chunk 1: role
            c1 = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(c1, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

            # chunk 2: content
            c2 = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(c2, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

            # chunk 3: stop
            c3 = {"id": cmpl_id, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(("data: " + json.dumps(c3, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

            # done
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        else:
            # 非流式返回
            response = {
                "id": cmpl_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
                "_upstream_remaining": result.get("remaining"),
            }
            self._send_json(response)

    def _handle_direct_optimize(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_error(400, "Invalid JSON")
            return

        prompt = body.get("prompt", "").strip()
        if not prompt:
            self._send_error(400, "prompt is empty")
            return

        settings = get_settings()
        mode = body.get("mode") or settings.get("mode", "random")
        messages = [{"role": "user", "content": prompt}]

        # 只要发送了请求就计数（在上游请求之前记录，确保即时可见）
        usage_tracker.record()

        # 429 自动重试
        result, identity = run_upstream_with_retry(messages, mode)

        if not identity:
            self._send_error(503, "身份池耗尽")
            return

        # 判断是否出错：call_upstream 只在出错时设置 "status" 字段
        is_error = not result or result.get("status")
        if is_error:
            error = result.get("error", "上游错误") if result else "无可用身份"
            self._send_error(502, error)
            return

        self._send_json({
            "success": True,
            "content": result.get("content", ""),
            "model": result.get("model"),
            "remaining": result.get("remaining"),
            "identity_used": identity["deviceId"],
        })

    def _handle_add_identity(self):
        try:
            body = self._read_body()
        except Exception:
            body = {}
        ident = pool.add(
            proxy=body.get("proxy", ""),
            ua=body.get("ua", ""),
            device_id=body.get("device_id", ""),
            max_per_hour=body.get("max_per_hour", 5),
            fake_ip=body.get("fake_ip", ""),
        )
        self._send_json(ident)

    def _handle_batch_add(self):
        try:
            body = self._read_body()
        except Exception:
            body = {}
        count = body.get("count", 10)
        proxies = body.get("proxies", [])
        max_per_hour = body.get("max_per_hour", 5)
        results = pool.batch_add(count, proxies, max_per_hour)
        self._send_json({"added": len(results)})

    def _handle_create_key(self):
        try:
            body = self._read_body()
        except Exception:
            body = {}
        name = body.get("name", "")
        rpm_limit = int(body.get("rpm_limit", 0))
        max_usage = int(body.get("max_usage", 0))
        expires_at = int(body.get("expires_at", 0))
        key = keys.create(name=name, rpm_limit=rpm_limit, max_usage=max_usage, expires_at=expires_at)
        self._send_json({"key": key})

    def _handle_update_settings(self):
        try:
            body = self._read_body()
        except Exception:
            body = {}
        settings = get_settings()
        if body.get("mode"):
            settings["mode"] = body["mode"]
        if body.get("admin_password"):
            settings["admin_password_hash"] = _hash_password(body["admin_password"])
        if body.get("upstream_url"):
            settings["upstream_url"] = body["upstream_url"]
        if body.get("wc_secret"):
            settings["wc_secret"] = body["wc_secret"]
        if body.get("max_timeout") and body["max_timeout"] > 0:
            settings["max_timeout"] = body["max_timeout"]
        if "auto_register" in body:
            settings["auto_register"] = bool(body["auto_register"])
        if "auto_register_target" in body:
            settings["auto_register_target"] = max(1, int(body["auto_register_target"]))
        if "auto_register_interval" in body:
            settings["auto_register_interval"] = max(10, int(body["auto_register_interval"]))
        if "auto_clean_exhausted" in body:
            settings["auto_clean_exhausted"] = bool(body["auto_clean_exhausted"])
        if "auto_register_max_per_hour" in body:
            settings["auto_register_max_per_hour"] = max(1, int(body["auto_register_max_per_hour"]))
        if "max_body_size" in body:
            settings["max_body_size"] = max(64, int(body["max_body_size"]))
        if "api_rpm_limit" in body:
            settings["api_rpm_limit"] = max(0, int(body["api_rpm_limit"]))
        if "api_prepend_enabled" in body:
            settings["api_prepend_enabled"] = bool(body["api_prepend_enabled"])
        if "api_prepend_prompt" in body:
            settings["api_prepend_prompt"] = str(body["api_prepend_prompt"]).strip()
        if "timezone" in body:
            tz = str(body["timezone"]).strip()
            if tz in TIMEZONE_OFFSETS:
                settings["timezone"] = tz
        save_settings(settings)
        self._send_json(settings)

    def _handle_models(self):
        """OpenAI 兼容 /v1/models 端点"""
        # 验证 API Key（和 OpenAI 行为一致）
        auth = self.headers.get("Authorization", "")
        key = auth.replace("Bearer ", "").strip()
        if key and not keys.validate(key):
            self._send_error(401, "Invalid API key")
            return
        self._send_json({
            "object": "list",
            "data": [
                {
                    "id": "gpt-4o-wc",
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "wc-proxy",
                },
                {
                    "id": "wc-optimizer",
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": "wc-proxy",
                },
            ]
        })

    def _handle_usage_stats(self):
        """返回使用量统计数据，用于仪表盘柱形图"""
        # 从 URL 参数获取粒度
        query = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
        params = {}
        for part in query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        # granularity: 每根柱子代表的秒数
        granularity = int(params.get("granularity", 3600))
        # count: 显示多少根柱子
        count = int(params.get("count", 24))
        # 安全限制
        granularity = max(60, min(granularity, 2592000))
        count = max(1, min(count, 60))
        # 获取时区偏移用于日边界对齐
        settings = get_settings()
        tz_offset = _get_timezone_offset_seconds(settings)
        stats = usage_tracker.get_stats(granularity, count, tz_offset=tz_offset)
        self._send_json({
            "granularity": granularity,
            "count": count,
            "data": stats,
            "tz_offset": tz_offset,
            "timezone": settings.get("timezone", "UTC"),
        })

    def _handle_status(self):
        all_ids = pool.get_all()
        now = int(time.time())
        available = 0
        for ident in all_ids:
            if not ident.get("enabled", True):
                continue
            if now - ident.get("last_reset_ts", 0) >= 3600:
                available += 1
            elif ident["used_count"] < ident["max_per_hour"]:
                available += 1
        all_keys = keys.list_all()
        self._send_json({
            "total_identities": len(all_ids),
            "available_identities": available,
            "total_keys": len(all_keys),
            "active_keys": sum(1 for v in all_keys.values() if v.get("enabled")),
        })

    # --- 静态文件 ---

    def _serve_static(self, filename):
        filepath = STATIC_DIR / filename
        if not filepath.exists() or not filepath.is_file():
            self._send_error(404, "File not found")
            return

        # MIME 类型
        ext = filepath.suffix.lower()
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        content_type = mime_map.get(ext, "application/octet-stream")

        content = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)


# ============================================================
# 多线程 HTTP 服务器
# ============================================================

class ThreadedHTTPServer(HTTPServer):
    """支持多线程处理请求"""
    allow_reuse_address = True

    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


# ============================================================
# 自动注册定时任务
# ============================================================

class AutoRegisterDaemon:
    """后台定时检查身份池数量，不足时自动补充"""
    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print("  [自动注册] 守护线程已启动")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                settings = get_settings()

                # 自动清理耗尽身份
                if settings.get("auto_clean_exhausted", True):
                    removed = pool.remove_exhausted()
                    if removed > 0:
                        print(f"  [自动清理] 删除了 {removed} 个耗尽身份")

                # 自动补充身份
                if settings.get("auto_register", False):
                    target = settings.get("auto_register_target", 20)
                    max_ph = settings.get("auto_register_max_per_hour", 5)
                    current = pool.count()
                    if current < target:
                        need = target - current
                        pool.batch_add(need, max_per_hour=max_ph)
                        print(f"  [自动注册] 补充了 {need} 个身份 (max_per_hour={max_ph}, 当前: {current} → {current + need})")

                interval = settings.get("auto_register_interval", 60)
                time.sleep(max(interval, 10))
            except Exception as e:
                print(f"  [守护线程] 错误: {e}")
                time.sleep(30)


auto_daemon = AutoRegisterDaemon()

# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  Nexus Proxy Server")
    print("=" * 50)
    print(f"  管理面板: http://localhost:{PORT}")
    print(f"  API 端点: http://localhost:{PORT}/v1/chat/completions")
    print(f"  管理密码: {_startup_password}")
    print(f"  (每次启动自动生成新密码，仅此处可见)")
    print(f"  IP限速: 30次/分钟, 认证失败5次封禁10分钟")
    print("=" * 50)
    print()

    auto_daemon.start()

    server = ThreadedHTTPServer((HOST, PORT), AppHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务器已停止")
        auto_daemon.stop()
        server.shutdown()
