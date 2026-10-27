"""检查 .env 里的 GitHub token 能不能用：只打印限流额度，不打印 token 本身。

用法：uv run python scripts/check_token.py
看到 core 上限 5000，说明 token 生效了；不登录时这个数是 60。
"""

import json
import os
import urllib.error
import urllib.request

from dotenv import load_dotenv  # [脚手架] 把 .env 里的"键=值"读进环境变量

load_dotenv()
token = os.getenv("GITHUB_TOKEN_SYNC", "")
if not token:
    raise SystemExit("没读到 GITHUB_TOKEN_SYNC：先把 .env.example 复制成 .env，再在等号后面填上 token")

# [核心] /rate_limit 这个接口本身不消耗额度，适合拿来测 token。
# 请求头带上 token，GitHub 就按"已登录"给额度。
req = urllib.request.Request("https://api.github.com/rate_limit", headers={
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "oss-maintainer-agent",   # GitHub 要求必须带 User-Agent
})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        resources = json.loads(resp.read())["resources"]
except urllib.error.HTTPError as e:
    # 401 = token 填错了、过期了，或者复制时多带了空格
    raise SystemExit(f"请求失败（HTTP {e.code}）：检查 .env 里的 token 有没有复制完整")

# [了解] core = 普通 REST 接口；search = 搜索接口；graphql = 10-28 同步要用的 GraphQL 接口
for name in ("core", "search", "graphql"):
    r = resources.get(name, {})
    print(f"{name:8} 上限 {r.get('limit', '?'):>5}  剩余 {r.get('remaining', '?'):>5}")
