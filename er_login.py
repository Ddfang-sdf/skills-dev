"""
ER 登录器 — 复刻 SshNBBox tools/util/ErBspSessionReq.py 的 BspSessionBuilder 流程。
5 步登录链路最终拿到 bspsession (cookie) + roarand (csrf token) 塞进 header。

用法:
    python er_login.py <ip> <user> <pwd>      # CLI 模式，打印 cookie/roarand
    from er_login import login                 # 库模式，供 rest_run.py import

返回: (client, headers, bsp, csrf)
  - client:   已建立会话的 requests.Session，可直接发后续请求
  - headers:  含 Cookie/roarand/X-Uni-Crsf-Token 的完整 header dict
  - bsp:      bspsession 字符串
  - csrf:     roarand/csrfToken 字符串

依赖: requests, urllib3
"""
import json
import sys
import urllib3
import requests
from requests.utils import dict_from_cookiejar

urllib3.disable_warnings()

ER_PORT = 31943
LOCATION = "location"


def login(ip, user, pwd, verbose=True):
    """登录 NCE ER (31943)，返回 (client, headers, bsp, csrf)。

    Raises:
        Exception: 任何一步失败（状态码不对/关键字段为空）都抛异常，message 含定位信息。
    """
    base_url = f"https://{ip}:{ER_PORT}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
    }
    client = requests.session()

    def _log(msg):
        if verbose:
            print(msg)

    # 1. 用户校验 -> 拿 redirectURL
    token_url = base_url + "/unisso/v2/validateUser.action"
    params = {
        "service": "/unisess/v1/auth?service=%2Fncecommonwebsite%2Fv1%2Fnewportal%2Fportal%2Floading%2Floading.html"
    }
    body = {"organizationName": "", "username": user, "password": pwd}
    r = client.post(token_url, params=params, headers=headers,
                    data=json.dumps(body), verify=False)
    _log(f"[1/5] validateUser status={r.status_code}")
    if r.status_code != 200:
        raise Exception(f"ER_AUTH_FAILED: validateUser status={r.status_code}, body={r.text[:500]}")
    redirect_url = json.loads(r.text).get("redirectURL", "")
    if not redirect_url:
        raise Exception(f"ER_AUTH_FAILED: redirectURL 为空, body={r.text[:500]}")
    redirect_url = base_url + redirect_url

    # 2. 跟随重定向拿 bspsession
    r = client.get(redirect_url, headers=headers, verify=False, allow_redirects=False)
    _log(f"[2/5] redirect status={r.status_code}")
    bsp = dict_from_cookiejar(r.cookies).get("bspsession", "")
    if not bsp:
        raise Exception("ER_AUTH_FAILED: bspsession 为空 (登录可能被拒，检查账号密码)")
    headers["Cookie"] = f"locale=zh-cn; bspsession={bsp}"

    # 3. 跟随 license 页面重定向直到 200
    while r.status_code != 200:
        loc = r.headers.get(LOCATION, "")
        if not loc:
            break
        url = base_url + loc if base_url not in loc else loc
        r = client.get(url, headers=headers, verify=False, allow_redirects=False)
    _log("[3/5] license redirect done")

    # 4. 拿 csrfToken
    r = requests.get(base_url + "/unisess/v1/auth/session",
                     headers=headers, verify=False, allow_redirects=True)
    _log(f"[4/5] session status={r.status_code}")
    try:
        csrf = r.json().get("csrfToken", "")
    except Exception:
        raise Exception(f"ER_AUTH_FAILED: session 响应非 JSON, status={r.status_code}, body={r.text[:500]}")
    if not csrf:
        raise Exception(f"ER_AUTH_FAILED: csrfToken 为空, body={r.text[:500]}")
    headers["roarand"] = csrf
    headers["X-Uni-Crsf-Token"] = csrf

    # 5. NCE 登录跳转（部分接口需要）
    login_url = (base_url + "/plat/licapp/v1/licensedirectlogin?"
                 "service=%2Funisess%2Fv1%2Fauth%3Fservice%3D"
                 f"https%253A%252F%252F{ip}%253A{ER_PORT}%252F"
                 "ncecommonwebsite%252Fv1%252Fnewportal%252F"
                 "index.html%253Frefr-flags%253De")
    r = client.get(login_url, headers=headers, verify=False, allow_redirects=False)
    while r.status_code != 200:
        loc = r.headers.get(LOCATION, "")
        if not loc:
            break
        url = base_url + loc if base_url not in loc else loc
        r = client.get(url, headers=headers, verify=False, allow_redirects=False)
    _log("[5/5] nce login done")

    return client, headers, bsp, csrf


def is_authorized(response_status, response_body):
    """判断响应是否为 401 未授权（rest_run.py 用于触发自动重登）"""
    return response_status == 401


if __name__ == "__main__":
    ip = sys.argv[1] if len(sys.argv) > 1 else "7.222.36.7"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pwd = sys.argv[3] if len(sys.argv) > 3 else "Changeme_123"
    try:
        client, headers, bsp, csrf = login(ip, user, pwd)
        print("\n=== 登录成功 ===")
        print("bspsession:", bsp)
        print("roarand   :", csrf)
        print("cookie    :", headers["Cookie"])
    except Exception as e:
        print(f"\n!!! 登录失败: {e}", file=sys.stderr)
        sys.exit(1)
