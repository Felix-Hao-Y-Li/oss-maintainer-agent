"""维护者的操作里有多少是机器人做的：决定评估集能用多少条"人工标准答案"（评估硬规则第 5 条：只认人工操作）。

用法：uv run python scripts/bot_share.py   （要先在 .env 里填好 GITHUB_TOKEN_SYNC）

执行地图（从上往下就是运行顺序）：
  main()
    → 对每个仓库 collect(repo)
        → dup_closers(repo)     2026 年以来"按重复关闭"的 issue：谁关的、有没有记下原 issue
        → label_actors(repo)    2026 年以来最新的 200 条 issue：每次打标签是谁打的
            → search_issues()   GraphQL 搜索 + 翻页，一次请求同时带回 issue 和它的时间线事件
                → graphql()     发请求；网络出错时退避重试
            → who()             把操作人分成 bot / author / maintainer
    → 打印每个仓库一行，结果存进 data/bot_share.json

名词表：
  时间线事件（timeline item）  issue 上发生过的每件事（打标签、关闭……），每条都记着是谁做的（actor）
  bot          机器人账号，比如 github-actions、自动分类机器人
  author       issue 作者自己。普通用户没有打标签的权限，作者名下的标签基本是提 issue 时模板自带的
  maintainer   其他人工操作，也就是维护者。只有这一类能当"标签"的标准答案
  duplicateOf  按重复关闭时记下的"原 issue"。有它，这一对 issue 就能直接当查重的标准答案
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

REPOS = [
    "langchain-ai/langgraph", "langchain-ai/langchain", "pydantic/pydantic", "pytest-dev/pytest",
    "vllm-project/vllm", "huggingface/transformers", "run-llama/llama_index", "home-assistant/core",
]
SINCE = "2026-01-01"       # 测试期起点（评估硬规则第 1 条）
LABEL_SAMPLE = 200         # 标签只抽最新 200 条：看比例够了，不用全量拉
OUT = Path(__file__).resolve().parents[1] / "data" / "bot_share.json"   # data/ 不进仓库

load_dotenv()
TOKEN = os.environ["GITHUB_TOKEN_SYNC"]

# [核心] 一条 GraphQL 查询同时拿到 issue 和它的时间线事件，省掉"每条 issue 再单独请求一次"。
# $events 决定要哪几类事件：统计重复时只要关闭事件，统计标签时只要打标签事件。
QUERY = """
query($q: String!, $after: String, $events: [IssueTimelineItemsItemType!]) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue {
      number
      author { login }
      timelineItems(itemTypes: $events, first: 30) { nodes {
        ... on LabeledEvent { actor { __typename login } label { name } }
        ... on ClosedEvent { actor { __typename login } stateReason duplicateOf { ... on Issue { number } } }
      } }
    } }
  }
}
"""


def main() -> None:
    rows = [collect(repo) for repo in REPOS]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print(f"已保存：{OUT}", file=sys.stderr)


def collect(repo: str) -> dict:
    """[核心] 一个仓库的两项统计。"""
    print(f"统计 {repo} ...", file=sys.stderr)
    return {"repo": repo, "dup": dup_closers(repo), "labels": label_actors(repo)}


def dup_closers(repo: str) -> dict:
    """[核心] 按重复关闭的 issue：最后一次关闭是谁做的。
    usable = 人工关闭（作者或维护者）并且记下了原 issue，这些才能直接当查重标准答案。"""
    q = f"repo:{repo} is:issue is:closed reason:duplicate created:>={SINCE}"
    c = Counter()
    for issue in search_issues(q, ["CLOSED_EVENT"], limit=1000):
        closes = [e for e in issue["timelineItems"]["nodes"] if e]
        if not closes:
            c["no_event"] += 1
            continue
        last = closes[-1]                          # 可能关了又重开，以最后一次为准
        kind = who(last["actor"], issue["author"])
        c[kind] += 1
        if kind in ("author", "maintainer") and last.get("duplicateOf"):
            c["usable"] += 1
    return dict(c)


def label_actors(repo: str) -> dict:
    """[核心] 最新 LABEL_SAMPLE 条 issue 上的每次打标签：按操作人分类计数。
    另外按类别记下最常见的 5 个标签和机器人账号名：看机器人在做什么，10-29 定评估用哪些标签时参考。"""
    q = f"repo:{repo} is:issue created:>={SINCE} sort:created-desc"
    c, top, bots = Counter(), {}, Counter()
    issues = search_issues(q, ["LABELED_EVENT"], limit=LABEL_SAMPLE)
    for issue in issues:
        for e in issue["timelineItems"]["nodes"]:
            if not e or not e.get("label"):        # 标签被删掉后，事件里的 label 是空的
                continue
            kind = who(e["actor"], issue["author"])
            c[kind] += 1
            top.setdefault(kind, Counter())[e["label"]["name"]] += 1
            if kind == "bot":
                bots[e["actor"]["login"]] += 1
    return {"issues": len(issues), **c, "bots": dict(bots),
            "top_labels": {k: v.most_common(5) for k, v in top.items()}}


def who(actor: dict | None, author: dict | None) -> str:
    """[核心] 操作人分三类（外加账号已注销的 unknown）。
    机器人有两种：GitHub App（类型是 Bot）和普通账号起名叫 xxx-bot 的，两种都算。"""
    if not actor:
        return "unknown"
    login = actor["login"]
    if actor["__typename"] == "Bot" or login.lower().endswith(("bot", "[bot]")):
        return "bot"
    if author and login == author["login"]:
        return "author"
    return "maintainer"


def search_issues(q: str, events: list[str], limit: int) -> list[dict]:
    """[了解] 按 cursor（翻页书签）一页 100 条往后翻，直到够 limit 条或没有下一页。"""
    issues, after = [], None
    while len(issues) < limit:
        page = graphql({"q": q, "after": after, "events": events})["search"]
        issues += [n for n in page["nodes"] if n]  # 非 Issue 的结果会是空对象，过滤掉
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
        time.sleep(1)                              # [脚手架] 翻页之间歇 1 秒，避开 GitHub 的二级限流
    return issues[:limit]


def graphql(variables: dict) -> dict:
    """[脚手架] 发一次 GraphQL 请求。网络错误或 5xx 退避重试（5 秒、10 秒），3 次都失败就停。
    查询本身写错（返回 errors）不重试：重试也不会好，直接报出来。"""
    body = json.dumps({"query": QUERY, "variables": variables}).encode()
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "oss-maintainer-agent"}
    for attempt in range(3):
        try:
            req = urllib.request.Request("https://api.github.com/graphql", data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            if "errors" in data:
                raise RuntimeError(data["errors"][0]["message"])
            return data["data"]
        except (urllib.error.URLError, TimeoutError) as e:   # HTTPError 也是 URLError 的子类
            if attempt == 2:
                raise
            wait = 5 * 2 ** attempt
            print(f"  请求失败（{e}），{wait} 秒后重试", file=sys.stderr)
            time.sleep(wait)


if __name__ == "__main__":
    main()
