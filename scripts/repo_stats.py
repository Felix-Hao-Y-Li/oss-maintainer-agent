"""候选仓库统计（不登录版）：数一数每个候选仓库有多少"能当标准答案"的 issue，用来选目标仓库。

执行地图（从上往下就是运行顺序）：
  main()
    → collect(repo)          对一个仓库跑完所有统计
        → repo_info(repo)    仓库基本信息：创建时间、主要语言、有没有 docs/ 目录
        → count(query)       调搜索接口，只要"总条数"，不拉具体内容
        → dup_labels(repo)   在标签清单里找名字带 duplicate 的标签
    → 打印汇总表，并把原始结果存成 JSON

名词表：
  搜索接口   GET /search/issues，返回 total_count（命中多少条）。不登录时每分钟最多 10 次
  core 接口  其他 REST 接口（仓库信息、标签、目录）。不登录时每小时最多 60 次
  reason:duplicate  搜索限定词：按"重复"关闭的 issue（GitHub 2024-12 起支持按重复关闭）
  linked:pr         搜索限定词：关联了 PR 的 issue（代码定位的标准答案来源）

为什么不登录：token 还没建好，先把只需要"计数"的部分跑出来；
"标签有多少是机器人打的"要逐条读事件记录，必须登录，等 token 建好再补。
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# [核心] 三个候选仓库：你最熟的 / RAG 方向很活跃的 / 历史最长、SWE-bench 收录的
REPOS = ["langchain-ai/langgraph", "infiniflow/ragflow", "pytest-dev/pytest"]
YEARS = range(2019, 2027)          # 按年分布：2019 ~ 2026
TEST_START = "2026-01-01"          # 测试期起点：晚于模型知识截止日（评估硬规则第 1 条）
RECENT_START = "2026-07-27"        # 最近 3 个月的起点，看活跃度
SEARCH_GAP = 6.5                   # [脚手架] 两次搜索之间等 6.5 秒，保证每分钟不超过 10 次
OUT = Path(__file__).resolve().parents[1] / "data" / "repo_stats.json"   # data/ 不进仓库


def get(url: str) -> dict:
    """[脚手架] 发一个 GET 请求，返回解析好的 JSON。
    出错时不抛异常，而是返回 {"error": 状态码, "message": ...}，让统计表里能看到"这一项查不了"。"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "repo-stats-script",   # GitHub 要求必须带 User-Agent
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 422 = 查询语法不被支持（比如某个限定词不存在）；403 = 触发限流
        return {"error": e.code, "message": e.read().decode("utf-8", "replace")[:200]}


def count(repo: str, extra: str) -> int | str:
    """[核心] 搜索"repo:仓库 is:issue + 额外条件"，只取命中总数。
    per_page=1：我们只要 total_count，不需要具体 issue，省流量。"""
    q = f"repo:{repo} is:issue {extra}".strip()
    url = "https://api.github.com/search/issues?" + urllib.parse.urlencode({"q": q, "per_page": 1})
    data = get(url)
    time.sleep(SEARCH_GAP)
    if "error" in data:
        if data["error"] == 403:                      # 被限流：等一分钟再试一次
            time.sleep(60)
            data = get(url)
            time.sleep(SEARCH_GAP)
        if "error" in data:
            return f"查询失败({data['error']})"
    return data.get("total_count", "无字段")


def repo_info(repo: str) -> dict:
    """[了解] 仓库基本信息 + 有没有 docs/ 目录（文档放在仓库里才方便做 search_docs 工具）。"""
    info = get(f"https://api.github.com/repos/{repo}")
    docs = get(f"https://api.github.com/repos/{repo}/contents/docs")
    return {
        "created_at": info.get("created_at", "?")[:10],
        "language": info.get("language"),
        "has_docs_dir": "error" not in docs,
    }


def dup_labels(repo: str) -> list[str]:
    """[了解] 翻标签清单（最多 3 页），找名字里带 duplicate 的标签。
    有的仓库不用"按重复关闭"，而是打一个 duplicate 标签，两种都要数。"""
    names = []
    for page in (1, 2, 3):
        data = get(f"https://api.github.com/repos/{repo}/labels?per_page=100&page={page}")
        if not isinstance(data, list) or not data:
            break
        names += [lab["name"] for lab in data]
    return [n for n in names if "duplicate" in n.lower()]


def collect(repo: str) -> dict:
    """[核心] 一个仓库的全部统计。since_2026 这一组决定测试集够不够大。"""
    print(f"统计 {repo} ...", file=sys.stderr)
    r = {"repo": repo, **repo_info(repo)}
    r["total"] = count(repo, "")
    r["by_year"] = {y: count(repo, f"created:{y}-01-01..{y}-12-31") for y in YEARS}
    r["since_2026"] = count(repo, f"created:>={TEST_START}")
    r["recent_3m"] = count(repo, f"created:>={RECENT_START}")
    r["dup_reason"] = count(repo, "is:closed reason:duplicate")
    r["dup_reason_2026"] = count(repo, f"is:closed reason:duplicate created:>={TEST_START}")
    r["linked_pr"] = count(repo, "is:closed linked:pr")
    r["linked_pr_2026"] = count(repo, f"is:closed linked:pr created:>={TEST_START}")
    r["dup_labels"] = {}
    for name in dup_labels(repo):
        r["dup_labels"][name] = {
            "all": count(repo, f'label:"{name}"'),
            "since_2026": count(repo, f'label:"{name}" created:>={TEST_START}'),
        }
    return r


def main() -> None:
    results = [collect(repo) for repo in REPOS]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    print(f"已保存：{OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
