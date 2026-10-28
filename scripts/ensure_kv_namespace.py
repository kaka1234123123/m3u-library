#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KV 命名空间供给脚本（幂等）：按标题确保 Cloudflare KV 命名空间存在。

背景：
  jellyfin/wrangler.toml 需要一个 KV 命名空间 id（binding = "KV_DELTA"）。
  但把 id 硬编码进 toml 会带来两个问题：
    1. 每个 fork / 每台机器 / 每个 CF 账号的 id 都不同，硬编码必然对不上；
    2. 硬编码的 id 一旦指向别人的命名空间，增量数据会写进对方账号。

做法：
  按「标题」而非 id 定位命名空间。脚本幂等执行：
    - 标题已存在 → 直接输出其 id
    - 标题不存在 → 调 CF API 创建，再取回 id
  调用方（CI）把输出的 id 注入 wrangler.toml 的 kv_namespaces.id，
  从而全程不需要把 id 当成仓库 Secret 硬编码。

用法：
  python scripts/ensure_kv_namespace.py
  python scripts/ensure_kv_namespace.py --title m3u-delta --output-id-file /tmp/ns_id
  python scripts/ensure_kv_namespace.py --title m3u-delta --dry-run   # 只查不建

环境变量：
  CLOUDFLARE_API_TOKEN   必需，CF API Token
  CLOUDFLARE_ACCOUNT_ID  必需，无内置兜底（内置他人账号 id 会误写别人命名空间）

退出码：
  0 成功（拿到 id，id 打印在 stdout 的 NS_ID= 之后）
  3 参数 / 凭据缺失
  4 CF API 报错
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"

# 命名空间标题：与 scripts/sync_delta_kv.py 的 DELTA_NS_TITLE 保持一致，
# 两边按同一个标题定位同一个命名空间。
DEFAULT_TITLE = "m3u-delta"


def _req(url, token, method="GET", payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    # ⚠️ Content-Type 只在真正带 body 时才加。
    #    带在 GET 请求上会让 Cloudflare 判定为「声明了 JSON body」，对
    #    某些 token 直接返回 HTTP 401 Authentication error —— 而同一个
    #    token 不带这个头时列表接口完全正常（scripts/sync_delta_kv.py
    #    的 resolve_ns_id 就是这么写的）。
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # 与 scripts/sync_delta_kv.py 保持一致：用默认 opener，
    # 让 CI/本机的代理设置按需生效。
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise RuntimeError("CF API %s %s -> HTTP %s: %s" % (method, url, e.code, body))
    except urllib.error.URLError as e:
        # 网络层失败（连接重置 / 超时 / DNS）也要转成 RuntimeError，
        # 否则会抛出未捕获的 traceback，CI 里表现为步骤崩溃而非可读错误。
        raise RuntimeError("CF API %s %s -> 网络错误: %s" % (method, url, e))
    except (TimeoutError, OSError) as e:
        raise RuntimeError("CF API %s %s -> 连接失败: %s" % (method, url, e))


def list_namespaces(token, account_id):
    url = "%s/accounts/%s/storage/kv/namespaces" % (API, account_id)
    body = _req(url, token)
    if not body.get("success"):
        raise RuntimeError("CF API 列命名空间失败: %s" % json.dumps(body)[:400])
    return body.get("result", [])


def find_by_title(token, account_id, title):
    for ns in list_namespaces(token, account_id):
        if ns.get("title") == title:
            return ns.get("id")
    return None


def create_namespace(token, account_id, title):
    url = "%s/accounts/%s/storage/kv/namespaces" % (API, account_id)
    body = _req(url, token, method="POST", payload={"title": title})
    if not body.get("success"):
        raise RuntimeError("CF API 创建命名空间失败: %s" % json.dumps(body)[:400])
    return (body.get("result") or {}).get("id")


def main():
    ap = argparse.ArgumentParser(description="确保 Cloudflare KV 命名空间存在（按标题，幂等）")
    ap.add_argument("--title", default=DEFAULT_TITLE, help="命名空间标题（默认 %s）" % DEFAULT_TITLE)
    ap.add_argument("--account-id", default=None, help="CF 账号 ID（缺省取环境变量 CLOUDFLARE_ACCOUNT_ID）")
    ap.add_argument("--output-id-file", default=None, help="把 id 写入此文件（供 CI 注入 toml）")
    ap.add_argument("--dry-run", action="store_true", help="只查找，不创建")
    args = ap.parse_args()

    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        print("ENSURE_KV_ERR: 缺少 CLOUDFLARE_API_TOKEN", file=sys.stderr)
        return 3
    # 账号 ID 必须显式提供：不同 fork / 不同 CF 账号各不相同，
    # 这里绝不内置任何兜底值（内置他人账号 id 会导致误写别人命名空间）。
    account_id = args.account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if not account_id:
        print("ENSURE_KV_ERR: 缺少 CLOUDFLARE_ACCOUNT_ID（或 --account-id）", file=sys.stderr)
        return 3

    try:
        ns_id = find_by_title(token, account_id, args.title)
        if ns_id:
            print("[ensure_kv] 命名空间 '%s' 已存在，id=%s" % (args.title, ns_id))
        else:
            if args.dry_run:
                print("[ensure_kv] 命名空间 '%s' 不存在（dry-run，未创建）" % args.title)
                print("NS_ID=")
                return 0
            ns_id = create_namespace(token, account_id, args.title)
            if not ns_id:
                print("ENSURE_KV_ERR: 创建成功但未取回 id", file=sys.stderr)
                return 4
            print("[ensure_kv] 已创建命名空间 '%s'，id=%s" % (args.title, ns_id))
    except RuntimeError as e:
        msg = str(e)
        # 区分「token 无效/过期」与「token 有效但缺 KV 权限」——
        # 两者修法完全不同：前者要重新生成 token，后者要在 token 上补权限。
        if "HTTP 401" in msg:
            print("ENSURE_KV_ERR: %s" % msg, file=sys.stderr)
            print("ENSURE_KV_HINT: CLOUDFLARE_API_TOKEN 无效或已过期。"
                  "请在 Cloudflare 控制台重新生成 API Token 并更新仓库 Secret；"
                  "若确认 token 有效，则它缺少 KV Storage 读写权限。", file=sys.stderr)
        elif "HTTP 403" in msg:
            print("ENSURE_KV_ERR: %s" % msg, file=sys.stderr)
            print("ENSURE_KV_HINT: CLOUDFLARE_API_TOKEN 有效但权限不足。"
                  "请在 Cloudflare 控制台为该 Token 补上「Workers KV Storage:Edit」"
                  "（以及 Account 级别读写），再更新 Secret。", file=sys.stderr)
        else:
            print("ENSURE_KV_ERR: %s" % msg, file=sys.stderr)
        return 4

    # 输出给 CI 捕获：NS_ID=<id>
    print("NS_ID=%s" % ns_id)
    if args.output_id_file:
        with open(args.output_id_file, "w", encoding="utf-8") as f:
            f.write(ns_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
