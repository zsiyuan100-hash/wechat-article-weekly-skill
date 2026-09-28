#!/usr/bin/env python3
"""Prepare and track incremental weekly WeChat article imports for IMA."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from account_loader import load_accounts as load_account_records

DEFAULT_WORKSPACE = Path(__file__).resolve().parents[1]
CRAWLER_SCRIPTS = Path("scripts")
DEFAULT_ACCOUNTS = Path("accounts.xlsx")

CATEGORY_FOLDERS = {
    "量化投研": "量化投研",
    "金工卖方": "金工卖方",
    "宏观研究": "宏观研究",
    "债券研究": "债券研究",
    "基金投资": "基金投资",
    "投资交易": "投资交易",
    "量化IT": "量化IT",
    "AI技术": "AI技术",
    "量化公司": "量化公司",
    "尽调与市场": "尽调与市场",
}


def atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def load_json(path: Path, default: object) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def run(command: list[str], cwd: Path) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def canonical_folder(category: str) -> str:
    base = category.split("（", 1)[0].split("(", 1)[0].strip()
    return CATEGORY_FOLDERS.get(base, base or "尽调与市场")


def state_path(workspace: Path) -> Path:
    return workspace / "output" / "ima_imported_urls.json"


def load_state(workspace: Path) -> dict:
    state = load_json(state_path(workspace), {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    state.setdefault("urls", {})
    return state


def articles_from_ready(workspace: Path) -> list[dict[str, str]]:
    rows = read_csv(workspace / "output" / "ima_ready_candidates.csv")
    articles = []
    for row in rows:
        url = row.get("链接", "").replace("×tamp", "&timestamp").strip()
        if not url.startswith("https://mp.weixin.qq.com/"):
            continue
        articles.append({
            "account": row.get("公众号", ""),
            "folder": canonical_folder(row.get("分类", "")),
            "category": row.get("分类", ""),
            "title": row.get("标题", ""),
            "url": url,
            "published_at": row.get("日期", ""),
            "score": row.get("质量分", ""),
            "content_status": row.get("正文检查", ""),
        })
    return articles


def count_csv(path: Path) -> int:
    return len(read_csv(path)) if path.exists() else 0


def prepare(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    scripts = workspace / args.scripts_dir
    accounts_path = args.accounts
    if not accounts_path.is_absolute():
        accounts_path = workspace / accounts_path
    output = workspace / "output"
    as_of = dt.date.fromisoformat(args.as_of)
    week_dir = output / "weekly" / as_of.isoformat()
    checkpoint = week_dir / "public_index_checkpoint.json"
    queue_path = week_dir / "ima_import_queue.json"
    latest_path = output / "weekly" / "latest_queue.json"

    required = [accounts_path, scripts / "public_index_batch.py",
                scripts / "quality_filter.py", scripts / "content_probe.py"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Missing required files: " + ", ".join(missing))

    if not args.skip_crawl:
        run([
            sys.executable, str(scripts / "public_index_batch.py"),
            "--accounts", str(accounts_path),
            "--out", str(checkpoint),
            "--depth", str(args.depth),
            "--pages", str(args.pages),
            "--delay", str(args.delay),
            "--retry-no-result",
        ], workspace)

    if not checkpoint.exists():
        raise SystemExit(f"Checkpoint not found: {checkpoint}")

    checkpoint_data = load_json(checkpoint, {})
    if not isinstance(checkpoint_data, dict):
        raise SystemExit(f"Invalid checkpoint: {checkpoint}")
    blocked = [account for account, result in checkpoint_data.items()
               if isinstance(result, dict) and result.get("status") == "blocked"]
    if blocked:
        raise SystemExit(
            "Public index was blocked; checkpoint retained for a later retry: "
            + ", ".join(blocked)
        )

    quality_command = [
        sys.executable, str(scripts / "quality_filter.py"),
        "--checkpoint", str(checkpoint),
        "--accounts", str(accounts_path),
        "--as-of", as_of.isoformat(),
        "--probe", str(output / "content_probe.json"),
    ]
    run(quality_command, workspace)
    run([
        sys.executable, str(scripts / "content_probe.py"),
        "--input", str(output / "ima_ready_candidates.csv"),
        "--output", str(output / "content_probe.json"),
        "--delay", str(args.probe_delay),
        *( ["--session-file", str(args.session_file)] if args.session_file else [] ),
    ], workspace)
    run(quality_command, workspace)

    state = load_state(workspace)
    submitted = state["urls"]
    ready = articles_from_ready(workspace)
    new_articles = [article for article in ready if article["url"] not in submitted]
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for article in new_articles:
        grouped[article["folder"]].append(article)

    groups = []
    for folder in CATEGORY_FOLDERS.values():
        items = grouped.pop(folder, [])
        if items:
            groups.append({
                "folder": folder,
                "count": len(items),
                "batches": [items[index:index + 10] for index in range(0, len(items), 10)],
            })
    for folder in sorted(grouped):
        items = grouped[folder]
        groups.append({
            "folder": folder,
            "count": len(items),
            "batches": [items[index:index + 10] for index in range(0, len(items), 10)],
        })

    unresolved_accounts = []
    discovered = 0
    for account, result in checkpoint_data.items():
        if not isinstance(result, dict):
            continue
        discovered += len(result.get("articles", []))
        if result.get("status") != "ok":
            unresolved_accounts.append({
                "account": account,
                "status": result.get("status", "unknown"),
                "error": result.get("error", ""),
            })

    target_accounts = len(load_account_records(accounts_path))

    queue = {
        "version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "workspace": str(workspace),
        "checkpoint": str(checkpoint),
        "submitted_state": str(state_path(workspace)),
        "target_accounts": target_accounts,
        "discovered_articles": discovered,
        "ready_total": len(ready),
        "already_submitted": len(ready) - len(new_articles),
        "new_count": len(new_articles),
        "candidate_count": count_csv(output / "ima_review_candidates.csv"),
        "rejected_count": count_csv(output / "ima_rejected.csv"),
        "unresolved_accounts": unresolved_accounts,
        "groups": groups,
    }
    atomic_json(queue_path, queue)
    atomic_json(latest_path, queue)
    state["last_refresh_at"] = queue["generated_at"]
    state["last_queue"] = str(queue_path)
    atomic_json(state_path(workspace), state)

    print(json.dumps({
        "queue": str(queue_path),
        "new_count": queue["new_count"],
        "groups": {group["folder"]: group["count"] for group in groups},
        "candidate_count": queue["candidate_count"],
        "rejected_count": queue["rejected_count"],
        "unresolved_accounts": len(unresolved_accounts),
    }, ensure_ascii=False, indent=2))
    return 0


def seed(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    state = load_state(workspace)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for article in articles_from_ready(workspace):
        if article["url"] in state["urls"]:
            continue
        state["urls"][article["url"]] = {
            **article,
            "submitted_at": now,
            "source": "baseline-seed",
        }
        added += 1
    state["seeded_at"] = now
    atomic_json(state_path(workspace), state)
    print(f"Seeded {added} URLs; total submitted state: {len(state['urls'])}")
    return 0


def mark_imported(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    queue_path = args.queue
    if not queue_path.is_absolute():
        queue_path = workspace / queue_path
    queue = load_json(queue_path, {})
    if not isinstance(queue, dict) or not isinstance(queue.get("groups"), list):
        raise SystemExit(f"Invalid queue: {queue_path}")
    state = load_state(workspace)
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    added = 0
    for group in queue["groups"]:
        for batch in group.get("batches", []):
            for article in batch:
                url = article.get("url", "")
                if not url or url in state["urls"]:
                    continue
                state["urls"][url] = {
                    **article,
                    "submitted_at": now,
                    "source": "weekly-ima-import",
                    "queue": str(queue_path),
                }
                added += 1
    state["last_import_at"] = now
    state["last_import_queue"] = str(queue_path)
    atomic_json(state_path(workspace), state)
    print(f"Marked {added} URLs submitted; total submitted state: {len(state['urls'])}")
    return 0


def status(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    state = load_state(workspace)
    latest = load_json(workspace / "output" / "weekly" / "latest_queue.json", {})
    print(json.dumps({
        "submitted_urls": len(state["urls"]),
        "last_refresh_at": state.get("last_refresh_at"),
        "last_import_at": state.get("last_import_at"),
        "latest_queue": latest,
    }, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("prepare", "seed", "mark-imported", "status"))
    ap.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    ap.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS,
                    help="公众号名单（.xlsx/.json/.txt），默认 accounts.xlsx")
    ap.add_argument("--scripts-dir", type=Path, default=CRAWLER_SCRIPTS,
                    help="抓取辅助脚本目录，默认 scripts/")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--probe-delay", type=float, default=0.8)
    ap.add_argument("--session-file", type=Path,
                    help="登录态 cURL 文件，默认 config/session.curl")
    ap.add_argument("--skip-crawl", action="store_true")
    ap.add_argument("--queue", type=Path, default=Path("output/weekly/latest_queue.json"))
    return ap


def main() -> int:
    args = parser().parse_args()
    return {
        "prepare": prepare,
        "seed": seed,
        "mark-imported": mark_imported,
        "status": status,
    }[args.action](args)


if __name__ == "__main__":
    raise SystemExit(main())
