#!/usr/bin/env python3
"""m3u-library 巡检脚本：一条命令看清「线上站点 + GitHub Actions」的健康状态。

回答四个问题：
  1. 线上站点的数据有多旧？（停更 → 红色）
  2. 线上站点能不能正常访问？
  3. 最近几个 workflow run 成功还是失败？
  4. 有没有卡在 queued 长时间不执行的 run？（并发组饥饿的征兆）

用法：
  python3 scripts/health_check.py
  python3 scripts/health_check.py --repo kaka1234123123/m3u-library
  python3 scripts/health_check.py --json

退出码：
  0 = 一切正常
  1 = 有问题（停更 / 站点不可达 / 有失败或长时间排队的 run）
  2 = 检查本身失败（网络异常等）
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "kaka1234123123/m3u-library"
SITE_URL = "https://kaka-m3u.pages.dev/"
DATA_URL = "https://kaka-m3u.pages.dev/api/all.json"
API = "https://api.github.com"
UA = "m3u-health-check/1.0"
STALE_HOURS = 6.0
QUEUE_STALE_MIN = 60


def http_json(url, token=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def http_head(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def check_site():
    """站点可访问性 + 数据新鲜度。"""
    out = {"reachable": False, "status": None, "data": None, "error": None}
    try:
        out["status"] = http_head(SITE_URL)
        out["reachable"] = out["status"] == 200
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["reachable"] = e.code == 200
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out

    try:
        payload = http_json(DATA_URL)
        updated = payload.get("updated")
        cats = payload.get("cats") or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        dt = datetime.datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = (now - dt).total_seconds() / 3600.0
        out["data"] = {
            "updated": updated,
            "age_hours": round(age, 2),
            "stale": age > STALE_HOURS,
            "count": payload.get("count"),
            "per_cat": {k: (v or {}).get("count") for k, v in cats.items()} if isinstance(cats, dict) else {},
        }
    except Exception as e:
        out["error"] = "数据检查失败 %s: %s" % (type(e).__name__, e)
    return out


def check_runs(repo, token=None, limit=12):
    """最近 workflow runs 的状态 + 长时间排队的检测。"""
    out = {"runs": [], "long_queued": [], "error": None}
    try:
        data = http_json("%s/repos/%s/actions/runs?per_page=%d" % (API, repo, limit), token)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out

    now = datetime.datetime.now(datetime.timezone.utc)
    for r in data.get("workflow_runs", []):
        created = r.get("created_at") or ""
        try:
            dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
        except Exception:
            dt = None
        item = {
            "id": r.get("id"),
            "name": r.get("name"),
            "event": r.get("event"),
            "status": r.get("status"),
            "conclusion": r.get("conclusion"),
            "created_at": created,
            "queued_minutes": round((now - dt).total_seconds() / 60.0) if dt else None,
        }
        out["runs"].append(item)
        if item["status"] in ("queued", "waiting", "pending") and (item["queued_minutes"] or 0) > QUEUE_STALE_MIN:
            out["long_queued"].append(item)
    return out


def main():
    ap = argparse.ArgumentParser(description="m3u-library 巡检")
    ap.add_argument("--repo", default=DEFAULT_REPO, help="owner/repo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--runs", type=int, default=12, help="检查最近多少个 run")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    site = check_site()
    runs = check_runs(args.repo, token, args.runs)

    problems = []
    if not site["reachable"]:
        problems.append("站点不可达（HTTP %s）%s" % (site["status"], site["error"] or ""))
    if site["data"] is None:
        problems.append("读不到线上数据清单")
    elif site["data"]["stale"]:
        problems.append("线上数据已停更 %.1f 小时（阈值 %.0f 小时）" % (site["data"]["age_hours"], STALE_HOURS))

    failed = [r for r in runs["runs"] if r["conclusion"] == "failure"]
    if failed:
        problems.append("最近有 %d 个 run 失败，最新：%s (%s)" % (
            len(failed), failed[0]["name"], failed[0]["created_at"]))
    if runs["long_queued"]:
        problems.append("有 %d 个 run 长时间排队（> %d 分钟），疑似并发组饥饿：%s" % (
            len(runs["long_queued"]), QUEUE_STALE_MIN,
            ", ".join("%s#%s 排队 %d 分钟" % (r["name"], r["id"], r["queued_minutes"]) for r in runs["long_queued"][:3])))

    result = {
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "repo": args.repo,
        "site": site,
        "runs": runs,
        "problems": problems,
        "healthy": not problems,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["healthy"] else 1

    print("=" * 62)
    print("m3u-library 巡检 — %s" % result["checked_at"])
    print("=" * 62)

    print("\n【站点】%s" % SITE_URL)
    print("  可访问: %s (HTTP %s)" % ("是" if site["reachable"] else "否", site["status"]))
    if site["data"]:
        d = site["data"]
        print("  数据更新: %s（距今 %.1f 小时）%s" % (
            d["updated"], d["age_hours"], "← 已停更！" if d["stale"] else "正常"))
        print("  条目总数: %s" % d["count"])
        for cat, n in sorted((d.get("per_cat") or {}).items()):
            print("    %-8s %s" % (cat, n))
    if site["error"]:
        print("  错误: %s" % site["error"])

    print("\n【最近 workflow runs】")
    if runs["error"]:
        print("  查询失败: %s" % runs["error"])
    else:
        for r in runs["runs"][:8]:
            flag = ""
            if r["conclusion"] == "failure":
                flag = "  ✗ 失败"
            elif r["status"] in ("queued", "waiting", "pending") and (r["queued_minutes"] or 0) > QUEUE_STALE_MIN:
                flag = "  ⚠ 排队 %d 分钟" % r["queued_minutes"]
            print("  %-19s %-24s %-11s %-9s%s" % (
                r["created_at"], r["name"][:24], r["status"], r["conclusion"] or "-", flag))

    print("\n【结论】")
    if not problems:
        print("  ✓ 一切正常")
    else:
        for p in problems:
            print("  ✗ %s" % p)
    print()
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("巡检自身异常: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(2)
