"""候选仓库统计 第二轮（不登录版）：扩大候选范围，看"多仓库合起来"测试样本够不够。

执行地图：
  main() → 对每个仓库 collect(repo)
             → languages(repo)   Python 代码占比（代码切块要用 Python 的 ast 模块）
             → count(...)        两个测试期起点各数一遍：2025-07-01（宽）和 2026-01-01（严）
         → 打印每个仓库一行 + 所有仓库合计

和第一轮的区别：去掉按年分布（省请求），加上 2025-07 起的宽口径和语言占比。
复用第一轮 repo_stats.py 里的 get()/count()：同一个目录下直接 import。
"""

import json
import sys
import urllib.request

from repo_stats import count, get  # [脚手架] 复用第一轮的请求和计数函数（含限流等待）

REPOS = [
    "langchain-ai/langgraph", "pytest-dev/pytest",                       # 第一轮已有，补宽口径
    "langchain-ai/langchain", "run-llama/llama_index", "vllm-project/vllm",
    "huggingface/transformers", "pydantic/pydantic", "home-assistant/core",
    "hiyouga/LLaMA-Factory", "PaddlePaddle/PaddleOCR", "chatchat-space/Langchain-Chatchat",  # 中文 issue 多
]
WIDE, STRICT = "2025-07-01", "2026-01-01"   # 宽 / 严两个测试期起点


def python_share(repo: str) -> float | str:
    """[了解] 仓库里 Python 代码按字节数占多少百分比。"""
    data = get(f"https://api.github.com/repos/{repo}/languages")
    if "error" in data or not data:
        return "?"
    return round(data.get("Python", 0) * 100 / sum(data.values()), 1)


def collect(repo: str) -> dict:
    """[核心] 一个仓库：两个口径下的 issue 数、按重复关闭数、关联 PR 数。"""
    print(f"统计 {repo} ...", file=sys.stderr)
    r = {"repo": repo, "python%": python_share(repo)}
    for tag, start in (("wide", WIDE), ("strict", STRICT)):
        r[f"issues_{tag}"] = count(repo, f"created:>={start}")
        r[f"dup_{tag}"] = count(repo, f"is:closed reason:duplicate created:>={start}")
        r[f"pr_{tag}"] = count(repo, f"is:closed linked:pr created:>={start}")
    return r


def main() -> None:
    rows = [collect(repo) for repo in REPOS]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    # [核心] 合计：只加能数出来的整数（查询失败的格子是字符串，跳过）
    keys = [k for k in rows[0] if k not in ("repo", "python%")]
    total = {k: sum(r[k] for r in rows if isinstance(r[k], int)) for k in keys}
    print("合计", json.dumps(total, ensure_ascii=False))


if __name__ == "__main__":
    main()
