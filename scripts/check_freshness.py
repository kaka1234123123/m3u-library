#!/usr/bin/env python3
"""检查线上站点数据的新鲜度，判断是否需要重新部署。

背景：
  站点（Cloudflare Pages）上的数据带一个 `updated` 字段（生成时刻，见
  generate_movies_json.py / generator/jellyfin_data.py），是判断「线上停更了多久」
  的唯一可靠依据 —— 仓库里的 DATA_VERSION 是入库版本、_worker.js 里的
  ServerName 是硬编码的旧名，两者都不能用来判断部署新旧。

用法：
  python3 scripts/check_freshness.py                       # 默认线上地址，阈值 6 小时
  python3 scripts/check_freshness.py --max-age-hours 8     # 自定义阈值
  python3 scripts/check_freshness.py --url https://xxx/api/all.json
  python3 scripts/check_freshness.py --json                # 输出机器可读的 JSON
  python3 scripts/check_freshness.py --github-output       # 追加 stale= 到 $GITHUB_OUTPUT
  python3 scripts/check_freshness.py --fail-if-stale       # 过期时退出码 1（本地/CI 断言用）

退出码：
  0 = 检查成功（是否过期见 --json 输出 / --github-output 写入的 stale=）
  2 = 检查本身失败（网络/解析异常）—— 调用方应保守处理，宁可多部署一次

零第三方依赖：只用标准库，CI runner 上直接可用。
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://kaka-m3u.pages.dev/api/all.json"
USER_AGENT = "m3u-freshness-check/1.0"


def _parse_updated(raw):
    """把 all.json 里的 updated 字符串解析成 aware datetime。

    生成端用的是 `datetime.datetime.now().isoformat(timespec="seconds")`，
    没有时区信息。CI runner 的 TZ 是 UTC，所以按 UTC 解释。
    """
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check(url, max_age_hours):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = fetch(url)
    if not isinstance(payload, dict):
        raise ValueError("all.json 顶层不是 JSON 对象")
    updated = _parse_updated(payload.get("updated"))
    if updated is None:
        raise ValueError("all.json 缺少可解析的 updated 字段")

    age_hours = (now - updated).total_seconds() / 3600.0
    cats = payload.get("cats") or {}
    per_cat = {}
    if isinstance(cats, dict):
        per_cat = {k: (v or {}).get("count") for k, v in cats.items()}

    return {
        "url": url,
        "updated": updated.isoformat(),
        "age_hours": round(age_hours, 2),
        "max_age_hours": max_age_hours,
        "stale": age_hours > max_age_hours,
        "count": payload.get("count"),
        "per_cat": per_cat,
        "checked_at": now.isoformat(),
    }


def main():
    ap = argparse.ArgumentParser(description="检查 m3u 线上数据新鲜度")
    ap.add_argument("--url", default=DEFAULT_URL, help="all.json 地址")
    ap.add_argument("--max-age-hours", type=float, default=6.0, help="超过多少小时算过期")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--github-output", action="store_true", help="追加 stale= 到 $GITHUB_OUTPUT")
    ap.add_argument("--fail-if-stale", action="store_true", help="过期时退出码 1")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP 超时秒数")
    args = ap.parse_args()

    try:
        result = check(args.url, args.max_age_hours)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        # 检查失败 → 返回 2。调用方（deploy.yml）应保守处理：查不到就当过期，部署一次。
        print("FRESHNESS_CHECK_ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        if args.json:
            print(json.dumps({"error": str(exc), "stale": True}, ensure_ascii=False))
        if args.github_output:
            with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as fh:
                fh.write("stale=true\n")
                fh.write("check_failed=true\n")
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        state = "过期，需要部署" if result["stale"] else "新鲜，无需部署"
        print("线上数据 updated = %s" % result["updated"])
        print("距今 %.1f 小时（阈值 %.1f 小时）→ %s" % (
            result["age_hours"], result["max_age_hours"], state))
        if result.get("count") is not None:
            print("库内条目: %d" % result["count"])
        for cat, n in sorted((result.get("per_cat") or {}).items()):
            print("  %-8s %s" % (cat, n))

    if args.github_output:
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as fh:
            fh.write("stale=%s\n" % ("true" if result["stale"] else "false"))
            fh.write("age_hours=%s\n" % result["age_hours"])
            fh.write("check_failed=false\n")

    if args.fail_if_stale and result["stale"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
