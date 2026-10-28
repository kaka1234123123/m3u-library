#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 jellyfin/wrangler.toml 中摘掉 kv_namespaces 段（用于 KV 不可用时的降级部署）。

场景：Cloudflare API Token 失效或缺 KV 权限时，拿不到命名空间 id，
      但 jellyfin worker 仍应部署（客户端兼容修复不依赖 KV）。
      此时把 [[kv_namespaces]] 整段移除，worker 侧 deltaEnabled()=false
      会自动降级为纯静态，无需改任何业务代码。

用法：
  python scripts/strip_kv_binding.py [path/to/wrangler.toml]
"""

import os
import re
import sys

DEFAULT_PATH = os.path.join("jellyfin", "wrangler.toml")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print("STRIP_KV_ERR: 文件不存在 %s" % path, file=sys.stderr)
        return 1
    with open(path, encoding="utf-8") as f:
        s = f.read()
    # 匹配 [[kv_namespaces]] 段：到下一个 [ 段头之前为止
    new = re.sub(r"\[\[kv_namespaces\]\][^\[]*", "", s)
    if new == s:
        print("[strip_kv] 无 kv_namespaces 段，无需处理")
        return 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    print("[strip_kv] 已移除 kv_namespaces 段，worker 将以纯静态模式运行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
