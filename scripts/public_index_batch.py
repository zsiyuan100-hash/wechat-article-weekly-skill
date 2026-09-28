#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""带断点的 Sogou 微信公开索引批处理。

微信公众平台跨公众号文章列表接口受限时，使用公开索引获取可见文章，
并且只有在搜索结果中的发布账号与目标账号一致时才接受结果。
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from account_loader import account_names, load_accounts as load_account_records


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ACCOUNTS = ROOT / "accounts.xlsx"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_crawler.py")


def load_crawler():
    spec = importlib.util.spec_from_file_location("wechat_crawler", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def norm(value):
    return "".join((value or "").casefold().split()).replace("·", "")


def load_accounts(path):
    return account_names(load_account_records(path))


def load_checkpoint(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_checkpoint(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Sogou 微信公开索引批处理")
    ap.add_argument("--accounts", default=str(DEFAULT_ACCOUNTS),
                    help="公众号名单（.xlsx/.json/.txt），默认 accounts.xlsx")
    ap.add_argument("--filter", help="只处理逗号分隔的公众号")
    ap.add_argument("--out", required=True, help="断点输出 JSON")
    ap.add_argument("--depth", type=int, default=10, help="每个公众号最多保留多少篇")
    ap.add_argument("--pages", type=int, default=1, help="每个公众号最多查询多少页")
    ap.add_argument("--delay", type=float, default=3.0, help="Sogou 请求间隔")
    ap.add_argument("--desktop", action="store_true", help="使用桌面版入口；默认使用移动版")
    ap.add_argument("--retry-no-result", action="store_true",
                    help="重试之前的 no-result 账号，用于补解析或查询更多页")
    ap.add_argument("--start", type=int, default=0, help="账号起始下标")
    ap.add_argument("--stop", type=int, help="账号结束下标（不含）")
    args = ap.parse_args()

    crawler = load_crawler()
    accounts = load_accounts(args.accounts)
    if args.filter:
        wanted = {norm(x) for x in args.filter.split(",") if x.strip()}
        accounts = [name for name in accounts if norm(name) in wanted]
    accounts = accounts[args.start:args.stop]
    checkpoint = load_checkpoint(args.out)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    sc = crawler.SogouCrawler(delay=max(args.delay, 2.0), mobile=not args.desktop)

    print(f"目标账号 {len(accounts)} 个，公开索引每号最多 {args.depth} 篇，查询 {args.pages} 页")
    for index, name in enumerate(accounts, 1):
        # Successful rows are stable checkpoints. no-result rows can be
        # retried explicitly after a cool-down or with deeper pagination.
        if name in checkpoint and checkpoint[name].get("status") == "ok":
            print(f"[{index}/{len(accounts)}] SKIP {name}（已有断点）", flush=True)
            continue
        if (name in checkpoint and checkpoint[name].get("status") == "no-result"
                and not args.retry_no_result):
            print(f"[{index}/{len(accounts)}] SKIP {name}（已有 no-result 断点）", flush=True)
            continue
        started = time.time()
        result = {"account": name, "status": "no-result", "articles": [], "error": ""}
        try:
            seen = set()
            for page in range(1, max(args.pages, 1) + 1):
                items = sc.search_page(name, page)
                if not items:
                    break
                # 不使用“搜索不到精确账号时接受整页”的旧逻辑，防止串号。
                matched = [item for item in items if norm(item.get("account")) == norm(name)]
                for item in matched:
                    key = item.get("title", "") + "|" + item.get("date", "")
                    if key in seen or len(result["articles"]) >= args.depth:
                        continue
                    seen.add(key)
                    try:
                        real = sc.resolve_link(item["link"], name)
                    except Exception as exc:  # noqa: BLE001
                        result["error"] = f"部分跳转失败: {type(exc).__name__}"
                        real = ""
                    if real and "mp.weixin.qq.com/s" in real:
                        result["articles"].append({
                            "title": item["title"],
                            "url": real,
                            "sogou_link": item["link"],
                            "date": item.get("date", ""),
                            "digest": item.get("digest", ""),
                        })
                    elif real == "":
                        # Keep the public redirect for a later low-frequency
                        # resolution pass instead of losing a matched article.
                        result.setdefault("unresolved", []).append({
                            "title": item["title"], "sogou_link": item["link"],
                            "date": item.get("date", ""), "digest": item.get("digest", ""),
                        })
                    time.sleep(args.delay)
                if len(result["articles"]) >= args.depth:
                    break
                time.sleep(args.delay)
            if result["articles"]:
                result["status"] = "ok"
            elif not result["error"]:
                result["error"] = "公开索引当前页没有精确匹配"
        except RuntimeError as exc:
            result["status"] = "blocked"
            result["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["error"] = f"{type(exc).__name__}: {exc}"
        checkpoint[name] = result
        save_checkpoint(args.out, checkpoint)
        elapsed = time.time() - started
        print(f"[{index}/{len(accounts)}] {result['status'].upper():9} {name} -> "
              f"{len(result['articles'])} 篇 ({elapsed:.1f}s) {result['error']}", flush=True)
        if result["status"] == "blocked":
            print("Sogou 返回反爬页面，已保存断点并停止；稍后可用同一命令继续。", flush=True)
            break

    ok = sum(1 for item in checkpoint.values() if item.get("status") == "ok")
    count = sum(len(item.get("articles", [])) for item in checkpoint.values())
    print(f"完成断点：{ok} 个账号，{count} 篇文章，更新时间 {datetime.now():%Y-%m-%d %H:%M:%S}")


if __name__ == "__main__":
    main()
