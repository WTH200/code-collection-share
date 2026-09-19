#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========== 企业微信推送配置（可选） ==========
QYWX_TOKEN = __import__("os").getenv("QYWX_TOKEN", "")  # 企业微信机器人 Webhook key（机器人地址 ?key= 后面的值，留空不推送）

# ==========================================================
# 功能说明：code 换 DOSSENSESSIONID（含缓存与自动刷新）
# 机制：本地 code 服务获取微信 code → 东呈 SSO 三步登录 → 缓存会话；
#       下次运行先校验缓存会话，有效则直接签到，失效自动重新登录。
# ==========================================================


"""
东呈酒店微信小程序动态 code 版（每日签到得积分）

功能：
  1. 本地 code 服务获取微信 code
  2. code 走东呈 SSO 三步登录换取 DOSSENSESSIONID
  3. 授权校验后每日签到（/selling/checkin/new），并查询当日签到状态
  4. PushPlus 推送
  5. 品赞代理，业务请求优先代理，失败直连兜底

环境变量：
  code 服务列表：10.30.9.183:8088（CODE_SERVER 可覆盖为单个地址）
  PLUSPLUS_TOKEN    PushPlus token，可选
  QYWX_TOKEN        企业微信机器人 Webhook key，可选（机器人地址 ?key= 后面的值）
  PROXY_API         品赞代理提取 API，可选
  PROXY_TYPE        http / socks5，默认 http
  DCJD_BLACKBOX     同盾设备指纹 blackbox，可选（默认用最近抓包值）

依赖：
  pip install requests
  socks5 代理需：
  pip install requests[socks]

⚠️ blackbox 是设备指纹，若服务端后续强校验或过期，请重新抓包。
"""

import hashlib
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

try:
    import urllib3

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    urllib3.disable_warnings()
except Exception:
    pass


APP_NAME = "东呈酒店微信小程序"
APPID = "wxa4b8c0bda7f71cfc"

SERVERS = [
    "10.30.9.183:8088",
]

if os.getenv("CODE_SERVER"):
    SERVERS = [os.getenv("CODE_SERVER")]

PLUSPLUS_TOKEN = os.getenv("PLUSPLUS_TOKEN", "")
PROXY_API = os.getenv("PROXY_API", "")
PROXY_TYPE = os.getenv("PROXY_TYPE", "http").lower()

PROXY_RETRY_TIMES = 3
PROXY_VALIDATE_URL = "http://httpbin.org/ip"
PROXY_FETCH_INTERVAL = 3
ENABLE_DIRECT_FALLBACK = True
REQUEST_TIMEOUT = 30

SSO_BASE = "https://login.dossen.com"
SSO_LOGIN_URL = f"{SSO_BASE}/sso/login"
SSO_VERIFY_ST_URL = f"{SSO_BASE}/sso/verifySt"
SSO_GET_SESSION_URL = f"{SSO_BASE}/sso/getSessionId"

BASE_URL = "https://campaignapi.dossen.com"
AUTHORIZE_URL = f"{BASE_URL}/auth/authorizate"
CHECKIN_URL = f"{BASE_URL}/selling/checkin/new"

ACTIVITY_QUERY_BASE = "https://selling-activity-query.dossen.com"
WEEKLY_PAGE_URL = f"{ACTIVITY_QUERY_BASE}/welfare/checkin/weeklyPage"

APP_ACCESS_TOKEN = "04F965AD5B494975BCF9764522B961B2"
DEFAULT_BLACKBOX = "jMPHy1789629388u2nUgO7WiJb"
DEFAULT_DISTINCT_ID = "1789629379473-3260631-0ec9963a93117d-10622654"
BLACKBOX = os.getenv("DCJD_BLACKBOX", DEFAULT_BLACKBOX)
DISTINCT_ID = os.getenv("DCJD_DISTINCT_ID", DEFAULT_DISTINCT_ID)
VER = "1.0.7"
CACHE_DIR = os.environ.get("CODE_CACHE_DIR", os.path.join(os.path.expanduser("~"), "Documents", "写代码"))

os.makedirs(CACHE_DIR, exist_ok=True)

SESSION_FILE = os.path.join(CACHE_DIR, "dossen_session.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) "
    "NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) UnifiedP"
)


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def mask(value: Any) -> str:
    value = str(value or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-6:]}"


def json_preview(data: Any, limit: int = 800) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:limit]
    except Exception:
        return str(data)[:limit]


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def safe_data(resp: Dict[str, Any]) -> Dict[str, Any]:
    """Safely extract 'data' from an API response, handling null/missing."""
    return resp.get("data") or {}


def log_title() -> None:
    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏨 东呈酒店小程序动态 code 版                  ║")
    print(f"║ 🕒 启动时间: {now_text():<32}║")
    print(f"║ 🔢 账号数量: {len(SERVERS):<34}║")
    print("╚" + "═" * 50 + "╝")


def log_account_header(index: int, total: int, server: str) -> None:
    print()
    print("┌" + "─" * 50 + "┐")
    print(f"│ 🧩 账号 {index} / {total:<37}│")
    print(f"│ 🌍 来源 {server:<40}│")
    print("└" + "─" * 50 + "┘")


def direct_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def parse_proxy_response(text: Any) -> Dict[str, Any] | None:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)

    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        proxy_obj = None

        if isinstance(data.get("data"), list) and data["data"]:
            proxy_obj = data["data"][0]
        elif isinstance(data.get("data"), dict):
            proxy_obj = data["data"]
        elif data.get("ip") and data.get("port"):
            proxy_obj = data
        elif isinstance(data.get("result"), dict):
            proxy_obj = data["result"]

        if proxy_obj:
            host = proxy_obj.get("ip") or proxy_obj.get("host")
            port = proxy_obj.get("port")
            if host and port:
                return {
                    "host": str(host),
                    "port": int(port),
                    "username": proxy_obj.get("user") or proxy_obj.get("username") or "",
                    "password": proxy_obj.get("pass") or proxy_obj.get("password") or "",
                }
    except Exception:
        pass

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            return {
                "host": parts[0],
                "port": int(parts[1]),
                "username": parts[2] if len(parts) > 2 else "",
                "password": parts[3] if len(parts) > 3 else "",
            }

    return None


def build_proxy_dict(proxy_info: Dict[str, Any] | None) -> Dict[str, str] | None:
    if not proxy_info:
        return None

    host = proxy_info["host"]
    port = proxy_info["port"]
    username = proxy_info.get("username", "")
    password = proxy_info.get("password", "")

    auth = ""
    if username and password:
        auth = f"{quote(username)}:{quote(password)}@"

    scheme = "socks5" if PROXY_TYPE == "socks5" else "http"
    proxy_url = f"{scheme}://{auth}{host}:{port}"

    print(f"🛠️ [代理] 生成 {scheme.upper()} 代理 {host}:{port}")

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def validate_proxy(proxies: Dict[str, str] | None) -> Tuple[bool, str]:
    if not proxies:
        return False, ""

    try:
        response = requests.get(PROXY_VALIDATE_URL, proxies=proxies, timeout=15)
        if response.status_code == 200:
            try:
                ip = response.json().get("origin", "未知")
            except Exception:
                ip = "未知"
            print(f"✅ [代理] 验证通过，出口 IP: {ip}")
            return True, ip
    except Exception as exc:
        print(f"⚠️ [代理] 验证失败: {exc}")

    return False, ""


def get_valid_proxy(account_name: str) -> Tuple[Dict[str, str] | None, str]:
    if not PROXY_API:
        print(f"⚠️ [代理] {account_name} 未配置 PROXY_API，使用直连")
        return None, ""

    print(f"🌐 [代理] {account_name} 正在获取品赞代理...")

    for index in range(1, PROXY_RETRY_TIMES + 1):
        try:
            response = direct_session().get(PROXY_API, timeout=15)
            proxy_info = parse_proxy_response(response.text)

            if not proxy_info:
                print(f"⚠️ [代理] 第 {index} 次代理解析失败")
                continue

            print(f"✅ [代理] 提取到 {proxy_info['host']}:{proxy_info['port']}")
            proxies = build_proxy_dict(proxy_info)

            ok, ip = validate_proxy(proxies)
            if ok:
                return proxies, ip

            print(f"⚠️ [代理] 第 {index} 次代理不可用")
        except Exception as exc:
            print(f"⚠️ [代理] 第 {index} 次获取代理异常: {exc}")

        if index < PROXY_RETRY_TIMES:
            sleep(2)

    print("⚠️ [代理] 获取失败，使用直连")
    return None, ""


def request_with_proxy(
    method: str,
    url: str,
    *,
    proxies: Dict[str, str] | None = None,
    server: str = "",
    **kwargs,
) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    if proxies:
        try:
            return requests.request(method, url, proxies=proxies, **kwargs)
        except Exception as exc:
            print(f"⚠️ [代理] {server} 代理请求失败: {exc}")
            if not ENABLE_DIRECT_FALLBACK:
                raise
            print("🔁 [兜底] 切换直连重试")

    session = direct_session()
    return session.request(method, url, **kwargs)



def send_qywx(title, content):
    """企业微信机器人推送（Webhook）。未配置 QYWX_TOKEN 时自动跳过。"""
    if not QYWX_TOKEN:
        print("[企业微信] 未配置 QYWX_TOKEN，跳过推送")
        return False
    key = QYWX_TOKEN.split("key=")[-1].strip()
    import json as _qywx_json, urllib.request as _qywx_urllib
    try:
        text = "%s\n%s" % (title, content)
        if len(text.encode("utf-8")) > 2000:
            text = text.encode("utf-8")[:2000].decode("utf-8", "ignore")
        payload = _qywx_json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
        req = _qywx_urllib.Request("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + key,
                                   data=payload, headers={"Content-Type": "application/json"})
        res = _qywx_json.loads(_qywx_urllib.urlopen(req, timeout=10).read().decode("utf-8"))
        ok = res.get("errcode") == 0
        print("[企业微信] 推送%s errcode=%s errmsg=%s" % ("成功 ✓" if ok else "失败 ✗", res.get("errcode"), res.get("errmsg", "")))
        return ok
    except Exception as _exc:
        print("[企业微信] 推送异常:", _exc)
        return False
def send_pushplus(title: str, content: str) -> None:
    send_qywx(title, content)  # 企业微信推送（QYWX_TOKEN 未配置时自动跳过）
    if not PLUSPLUS_TOKEN:
        print("⚠️ [PushPlus] 未配置 PLUSPLUS_TOKEN，跳过推送")
        return

    try:
        requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PLUSPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": "txt",
            },
            timeout=10,
        )
        print("✅ [PushPlus] 推送成功")
    except Exception as exc:
        print(f"❌ [PushPlus] 推送失败: {exc}")


def get_code(server: str) -> str | None:
    url = f"http://{server}/login"
    print(f"🔐 [授权] 请求本地 code 服务: {url}")

    try:
        response = direct_session().get(
            url,
            params={"appId": APPID},
            timeout=20,
        )
        data = response.json()

        if data.get("err") != 0 or not data.get("code"):
            print(f"❌ [授权] code 获取失败: {json_preview(data)}")
            return None

        print("✅ [授权] code 获取成功")
        return data["code"]
    except Exception as exc:
        print(f"❌ [授权] code 获取异常: {exc}")
        return None


def common_headers(session_id: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Dossen-Platform": "WxMiniApp",
        "firstchannel": "DOSSEN",
        "secondchannel": "DOSSEN_MINIPROGRAM",
        "DOSSENSESSIONID": session_id,
        "DOSSENUT": "",
        "ver": VER,
        "access_token": APP_ACCESS_TOKEN,
        "blackbox": BLACKBOX,
        "Referer": f"https://servicewechat.com/{APPID}/517/page-frame.html",
    }
    return headers


def extract_token(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None

    candidates = [
        data.get("access_token"),
        data.get("accessToken"),
        data.get("token"),
    ]

    inner = data.get("data")
    if isinstance(inner, dict):
        candidates.extend([
            inner.get("access_token"),
            inner.get("accessToken"),
            inner.get("token"),
        ])

        user = inner.get("user")
        if isinstance(user, dict):
            candidates.extend([
                user.get("access_token"),
                user.get("accessToken"),
                user.get("token"),
            ])

    for item in candidates:
        if item and item != "null":
            return str(item)

    return None


def extract_session_id(response: requests.Response, data: Any) -> str:
    session_id = ""
    try:
        session_id = response.cookies.get("DOSSENSESSIONID", "")
    except Exception:
        session_id = ""

    if not session_id and isinstance(data, dict):
        candidates = [data.get("sessionId"), data.get("DOSSENSESSIONID")]
        inner = data.get("data")
        if isinstance(inner, dict):
            candidates.extend([inner.get("sessionId"), inner.get("DOSSENSESSIONID")])
        for item in candidates:
            if item and item != "null":
                session_id = str(item)
                break

    return session_id


def login_by_code(server: str, code: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    try:
        print("🔐 [登录] 使用 code 换 ST")
        response = request_with_proxy(
            "POST",
            SSO_LOGIN_URL,
            headers=common_headers(),
            json={
                "accountType": "3",
                "loginType": "WECHAT_SILENCE",
                "title": "登录",
                "distinctId": DISTINCT_ID,
                "password": code,
            },
            proxies=proxies,
            server=server,
            verify=False,
        )

        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:800]}

        if not isinstance(data, dict) or data.get("code") != 0:
            print(f"❌ [登录] SSO 登录失败: {json_preview(data)}")
            return None, data
        st = str(data.get("results") or "")
        if not st:
            print(f"❌ [登录] 未返回 ST: {json_preview(data)}")
            return None, data

        verify_resp = request_with_proxy(
            "GET",
            SSO_VERIFY_ST_URL,
            headers=common_headers(),
            params={"st": st},
            proxies=proxies,
            server=server,
            verify=False,
        )
        verify_data = verify_resp.json()
        ut = str(verify_data.get("results") or "") if verify_data.get("code") == 0 else ""
        if not ut:
            print(f"❌ [登录] ST 校验失败: {json_preview(verify_data)}")
            return None, verify_data

        session_resp = request_with_proxy(
            "GET",
            SSO_GET_SESSION_URL,
            headers=common_headers(),
            params={"ut": ut},
            proxies=proxies,
            server=server,
            verify=False,
        )
        session_data = session_resp.json()
        session_id = str(session_data.get("results") or "") if session_data.get("code") == 0 else ""
        if not session_id:
            print(f"❌ [登录] 会话获取失败: {json_preview(session_data)}")
            return None, session_data

        print(f"✅ [登录] DOSSENSESSIONID 获取成功: {mask(session_id)}")
        return session_id, session_data
    except Exception as exc:
        print(f"❌ [登录] 请求异常: {exc}")
        return None, None


def api_get(server: str, url: str, session_id: str, proxies: Dict[str, str] | None) -> Dict[str, Any]:
    response = request_with_proxy(
        "GET",
        url,
        headers=common_headers(session_id),
        proxies=proxies,
        server=server,
        verify=False,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


def api_post(server: str, url: str, session_id: str, proxies: Dict[str, str] | None, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = request_with_proxy(
        "POST",
        url,
        headers=common_headers(session_id),
        json=payload,
        proxies=proxies,
        server=server,
        verify=False,
    )
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "msg": f"JSON解析失败: {response.text[:300]}",
        }


# ====================== Token缓存管理 ======================
# ====================== 会话缓存管理 ======================
def load_session_cache() -> Dict[str, Any]:
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r", encoding="utf-8") as file:
                return json.load(file)
    except Exception as exc:
        print(f"⚠️ [缓存] 读取失败: {exc}")
    return {}


def save_session_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as file:
            json.dump(cache, file, ensure_ascii=False, indent=2)
        print("✅ [缓存] 会话保存成功")
    except Exception as exc:
        print(f"❌ [缓存] 保存失败: {exc}")


def get_cached_session(server: str) -> str | None:
    cache = load_session_cache()
    data = cache.get(server) or {}
    session_id = str(data.get("sessionId", "") or "")
    if session_id:
        print(f"✅ [缓存] 使用 {server} 会话")
        return session_id
    return None


def set_cached_session(server: str, session_id: str) -> None:
    cache = load_session_cache()
    cache[server] = {
        "sessionId": session_id,
        "updateTime": datetime.now().isoformat(),
    }
    save_session_cache(cache)


def login_with_cache(server: str, proxies: Dict[str, str] | None) -> Tuple[str | None, Dict[str, Any] | None]:
    session_id = get_cached_session(server)
    if session_id:
        auth = api_post(server, AUTHORIZE_URL, session_id, proxies, {})
        if auth.get("code") == 0:
            return session_id, auth
        print(f"⚠️ [缓存] 会话失效: {json_preview(auth)}")

    code = get_code(server)
    if not code:
        return None, None

    session_id, raw_login = login_by_code(server, code, proxies)
    if not session_id:
        return None, raw_login

    set_cached_session(server, session_id)
    auth = api_post(server, AUTHORIZE_URL, session_id, proxies, {})
    if auth.get("code") != 0:
        print(f"❌ [授权] 用户信息获取失败: {json_preview(auth)}")
        return session_id, auth
    return session_id, auth


def do_checkin(server: str, session_id: str, proxies: Dict[str, str] | None) -> Tuple[str, str]:
    weekly = api_get(server, WEEKLY_PAGE_URL, session_id, proxies)
    weekly_results = weekly.get("results") or {}
    if weekly.get("code") == 0 and weekly_results.get("hasCheckinToday") is True:
        msg = "今日已签到，无需重复操作"
        print(f"✅ [签到] {msg}")
        return msg, "-"

    checkin_url = f"{CHECKIN_URL}?blackbox={quote(BLACKBOX)}"
    resp = api_get(server, checkin_url, session_id, proxies)
    if resp.get("code") == 0:
        results = resp.get("results") or {}
        points = results.get("point")
        msg = f"签到成功，获得 {points} 积分"
        print(f"✅ [签到] {msg}")
        return msg, str(points if points is not None else "-")

    msg = f"签到失败: {json_preview(resp)}"
    print(f"❌ [签到] {msg}")
    return msg, "-"


def run_account(index: int, total: int, server: str) -> Dict[str, Any]:
    result = {
        "server": server,
        "success": False,
        "proxyStatus": "未使用代理",
        "proxyIp": "-",
        "session": "-",
        "signMsg": "-",
        "points": "-",
        "error": "",
    }

    log_account_header(index, total, server)

    proxies, proxy_ip = get_valid_proxy(server)
    result["proxyStatus"] = "使用专属代理" if proxies else "使用直连"
    result["proxyIp"] = proxy_ip or "-"

    sleep(PROXY_FETCH_INTERVAL)

    delay = random.randint(2, 6)
    print(f"⏳ [延迟] 启动延迟 {delay}s")
    sleep(delay)

    if not BLACKBOX:
        print("⚠️ [签到] 未配置 DCJD_BLACKBOX（同盾设备指纹），若签到失败请自行抓包补充")

    session_id, auth = login_with_cache(server, proxies)
    if not session_id:
        result["error"] = f"登录失败: {json_preview(auth)}"
        return result

    result["session"] = mask(session_id)
    member = (auth.get("results") or {}) if isinstance(auth, dict) else {}
    card_no = member.get("cardNO")
    if card_no:
        print(f"✅ [授权] 会员卡号: {mask(card_no)}")

    try:
        sign_msg, points = do_checkin(server, session_id, proxies)
        result["signMsg"] = sign_msg
        result["points"] = points

        result["success"] = True
        return result

    except Exception as exc:
        result["error"] = traceback.format_exc().strip()
        print(f"❌ [账号] 执行失败: {exc}")
        return result


def build_notify(results: List[Dict[str, Any]]) -> str:
    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    content = f"""🏨 东呈酒店小程序任务结果

━━━━━━━━━━━━━━━━━━━━
🏁 总结：{success_count} 成功 / {fail_count} 失败
🕒 时间：{now_text()}
━━━━━━━━━━━━━━━━━━━━
"""

    for idx, res in enumerate(results, 1):
        icon = "✅" if res["success"] else "❌"

        content += f"""
🧩 账号 {idx}
📝 签到：{res["signMsg"]}
💰 积分：{res["points"]}
{icon} 结果：{"成功" if res["success"] else "失败"}
"""

        if not res["success"]:
            content += f"❌ 原因：{res['error']}\n"

        content += "━━━━━━━━━━━━━━━━━━━━\n"

    return content


def main() -> None:
    log_title()

    results: List[Dict[str, Any]] = []

    for index, server in enumerate(SERVERS, 1):
        try:
            result = run_account(index, len(SERVERS), server)
            results.append(result)
        except Exception as exc:
            print(f"❌ [主程序] {server} 执行异常: {exc}")
            results.append({
                "server": server,
                "success": False,
                "proxyStatus": "-",
                "proxyIp": "-",
                "token": "-",
                "signMsg": "-",
                "points": "-",
                "error": traceback.format_exc().strip(),
            })

        if index < len(SERVERS):
            print("⏳ [间隔] 等待 2s 后处理下一个账号")
            sleep(2)

    success_count = sum(1 for item in results if item["success"])
    fail_count = len(results) - success_count

    print()
    print("╔" + "═" * 50 + "╗")
    print("║ 🏁 东呈酒店任务执行完成                      ║")
    print(f"║ ✅ 成功: {success_count:<39}║")
    print(f"║ ❌ 失败: {fail_count:<39}║")
    print(f"║ 🕒 结束时间: {now_text():<32}║")
    print("╚" + "═" * 50 + "╝")

    send_pushplus("🏨 东呈酒店任务完成", build_notify(results))


if __name__ == "__main__":
    main()
