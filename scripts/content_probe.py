#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""低频检查候选微信链接是否可访问并含有正文。"""

import argparse
import csv
import importlib.util
import json
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wechat_crawler.py")
DEFAULT_INPUT = ROOT / "output" / "ima_ready_candidates.csv"
DEFAULT_OUTPUT = ROOT / "output" / "content_probe.json"


def load_module():
    spec = importlib.util.spec_from_file_location("wechat_crawler", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ap = argparse.ArgumentParser(description="检查微信文章正文可访问性")
    ap.add_argument("--input", default=str(DEFAULT_INPUT))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--limit", type=int, default=0, help="只检查前 N 条，0=全部")
    ap.add_argument("--delay", type=float, default=0.8)
    ap.add_argument("--session-file", help="登录态 cURL 文件（默认 config/session.curl）")
    args = ap.parse_args()

    module = load_module()
    if args.session_file:
        module.SESSION_FILE = Path(args.session_file).expanduser().resolve()
    sess = module.load_session(required=True)
    with open(args.input, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[:args.limit]
    results = {}
    if os.path.exists(args.output):
        try:
            with open(args.output, encoding="utf-8") as f:
                results = json.load(f)
        except (OSError, json.JSONDecodeError):
            results = {}

    for index, row in enumerate(rows, 1):
        url = row.get("链接", "")
        if not url or url in results:
            continue
        text = module.fetch_content_text(sess, url)
        results[url] = {
            "公众号": row.get("公众号", ""),
            "标题": row.get("标题", ""),
            "日期": row.get("日期", ""),
            "status": "ok" if text else "unavailable",
            "text_length": len(text),
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"[{index}/{len(rows)}] {results[url]['status']} {len(text):5d} {row.get('公众号')} | {row.get('标题')}", flush=True)
        time.sleep(args.delay)

    ok = sum(item.get("status") == "ok" for item in results.values())
    print(f"正文可访问 {ok}/{len(results)}，结果：{args.output}")


if __name__ == "__main__":
    main()
