#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并、去重并按 IMA 筛选标准给公众号文章评分。"""

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from account_loader import category_map, load_accounts as load_account_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "output"
ACCOUNTS = PROJECT_ROOT / "accounts.xlsx"

MARKETING = re.compile(
    r"招聘|招募|报名|抽奖|红包|奖金|福利|优惠|折扣|课程|训练营|直播|活动报名|扫码|加微信|入群|会员|广告|推广|招商|合作咨询|限时|赠书|免费领取|免费下载|诚邀体验|实习|实习生|论坛议程|开放产品权限|乔迁|荣获.*奖|高新技术企业|闭门会议|出席源创会|路演纪要|重要公告|估值表|赎回费|披露政策"
)
TOPIC_TERMS = {
    "量化交易": r"量化|策略|回测|因子|alpha|交易|投资组合|组合优化|高频|做市|信号|择时|收益|风险模型|净值",
    "金融工程": r"金融工程|衍生品|期权|期货|债券|固收|信用|波动率|定价|曲线|利率|金工",
    "宏观与市场": r"宏观|通胀|利率|流动性|美联储|经济|财政|货币|周期|供应链|地缘|市场|财报|估值",
    "AI与智能体": r"AI|人工智能|大模型|LLM|Agent|智能体|深度学习|机器学习|NLP|自然语言|Transformer|论文|Paper|RAG",
    "研究工具": r"Python|GitHub|Qlib|代码|编程|数据|机器学习|算法|开源|框架|工程|API",
}
SPECIFIC = re.compile(
    r"量化|策略|因子|回测|组合|期权|期货|债券|宏观|通胀|利率|流动性|财报|估值|AI|Agent|大模型|论文|Paper|NLP|Python|GitHub|Qlib|深度学习|机器学习|算法|风险|波动率|收益|交易"
)
TRUSTED = re.compile(r"华泰|中信|广发|东吴|中金|弘则|德邦|开源|券商|证券|研究|资产|基金|期权|债市|金融工程|投研|量化公司")


def clean(value, is_url=False):
    value = str(value or "")
    if is_url:
        # Decode only the entity used by the HTML attribute. A broad
        # html.unescape() turns the URL query key "&timestamp" into "×tamp".
        return value.replace("&amp;", "&").replace("×tamp", "&timestamp").strip()
    value = html.unescape(value)
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value).strip()


def norm_title(value):
    value = clean(value).casefold()
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def safe_name(value):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_") or "unknown"


def base_category(value):
    return re.split(r"[（(]", value or "")[0].strip() or "未分类"


def tags_for(text):
    tags = [tag for tag, pattern in TOPIC_TERMS.items() if re.search(pattern, text, re.I)]
    return "、".join(tags) or "待人工判断"


def score_row(row, as_of, probe=None):
    title = clean(row["标题"])
    digest = clean(row.get("摘要", ""))
    text = f"{title} {digest} {row.get('分类', '')}"

    topic = 1 if row.get("分类") else 0
    hits = sum(1 for pattern in TOPIC_TERMS.values() if re.search(pattern, text, re.I))
    if hits >= 2:
        topic = 3
    elif hits == 1:
        topic = max(topic, 2)

    source = 2 if TRUSTED.search(f"{row['公众号']} {row.get('分类', '')}") else 1

    recency = 0
    try:
        published = dt.date.fromisoformat(row.get("日期", ""))
        age = (as_of - published).days
        if age <= 30:
            recency = 2
        elif age <= 90:
            recency = 1
    except ValueError:
        age = None

    title_quality = 0
    if len(title) >= 8 and not re.search(r"干货|必看|重磅|速看|新鲜出炉", title):
        title_quality = 1
    if digest or SPECIFIC.search(title):
        title_quality = min(2, title_quality + 1)

    independent = 1 if SPECIFIC.search(text) and not MARKETING.search(text) else 0
    total = topic + source + recency + title_quality + independent
    marketing = bool(MARKETING.search(text))
    if not row.get("链接", "").startswith("https://mp.weixin.qq.com/"):
        status = "reject-invalid-url"
    elif marketing:
        status = "reject-marketing"
    elif probe and probe.get("status") != "ok":
        status = "reject-unavailable"
    elif total >= 8:
        status = "ready"
    elif total >= 6:
        status = "candidate"
    else:
        status = "reject"

    reasons = [f"主题{topic}/3", f"来源{source}/2", f"时效{recency}/2",
               f"标题摘要{title_quality}/2", f"独立研究{independent}/1"]
    if marketing:
        reasons.append("命中营销/活动词")
    if probe:
        row["正文检查"] = probe.get("status", "")
        row["正文长度"] = probe.get("text_length", 0)
        if probe.get("status") != "ok":
            reasons.append("正文当前不可访问")
    if age is not None and age > 180:
        reasons.append("超过180天，仅作历史参考")
    row.update({
        "标题": title,
        "摘要": digest,
        "主题标签": tags_for(text),
        "质量分": total,
        "入库状态": status,
        "评分理由": "；".join(reasons),
    })
    return row


def load_rows(checkpoint_path, accounts_path=ACCOUNTS):
    categories = category_map(load_account_records(accounts_path))

    rows = []
    index_path = OUTPUT / "index.csv"
    if index_path.exists():
        with index_path.open(encoding="utf-8-sig", newline="") as f:
            rows.extend(dict(row, 来源="existing-index") for row in csv.DictReader(f))

    with open(checkpoint_path, encoding="utf-8") as f:
        checkpoint = json.load(f)
    for account, result in checkpoint.items():
        for article in result.get("articles", []):
            rows.append({
                "公众号": account,
                "分类": categories.get(account, ""),
                "标题": article.get("title", ""),
                "链接": article.get("url", ""),
                "日期": article.get("date", ""),
                "摘要": article.get("digest", ""),
                "来源": "public-index",
            })
    return rows


def dedupe(rows):
    unique = {}
    for row in rows:
        row["链接"] = clean(row.get("链接", ""), is_url=True)
        row["标题"] = clean(row.get("标题", ""))
        if not row["标题"] or not row["链接"]:
            continue
        key = row["链接"] or norm_title(row["标题"])
        if key not in unique:
            unique[key] = row
    # Exact title duplicates are usually syndicated copies. Keep the first
    # row, then score it consistently; the source list is already curated.
    by_title = {}
    result = []
    for row in unique.values():
        key = norm_title(row["标题"])
        if key in by_title:
            continue
        by_title[key] = row
        result.append(row)
    return result


def write_csv(path, rows, fields):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_batches(rows, batch_dir, as_of):
    os.makedirs(batch_dir, exist_ok=True)
    grouped = defaultdict(list)
    for row in rows:
        grouped[base_category(row.get("分类", ""))].append(row)
    for category, items in sorted(grouped.items()):
        items.sort(key=lambda item: (item.get("日期", ""), item.get("质量分", 0)), reverse=True)
        path = os.path.join(batch_dir, safe_name(category) + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {category} · 公众号精选\n\n")
            f.write(f"> 抓取日期：{as_of.isoformat()}；按质量分降序和日期筛选。\n\n")
            current = None
            for item in items:
                if item["公众号"] != current:
                    current = item["公众号"]
                    f.write(f"## {current}\n\n")
                digest = item.get("摘要", "").replace("|", " ")[:180]
                f.write(f"- [{item['标题']}]({item['链接']})  \n")
                f.write(f"  {item.get('日期', '')} · {item.get('主题标签', '')} · {item.get('质量分', '')}/10")
                if digest:
                    f.write(f" · {digest}")
                f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="IMA 公众号文章质量筛选")
    ap.add_argument("--checkpoint", default=str(OUTPUT / "public_index_checkpoint.json"))
    ap.add_argument("--accounts", default=str(ACCOUNTS),
                    help="公众号名单（.xlsx/.json/.txt），默认 accounts.xlsx")
    ap.add_argument("--as-of", default=dt.date.today().isoformat())
    ap.add_argument("--probe", default=str(OUTPUT / "content_probe.json"))
    args = ap.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)

    probe = {}
    if os.path.exists(args.probe):
        with open(args.probe, encoding="utf-8") as f:
            probe = json.load(f)
    rows = []
    for row in dedupe(load_rows(args.checkpoint, args.accounts)):
        rows.append(score_row(row, as_of, probe.get(row.get("链接", ""))))
    rows.sort(key=lambda row: (row["入库状态"] != "ready", -int(row["质量分"]), row.get("日期", "")), reverse=False)
    fields = ["公众号", "分类", "标题", "链接", "日期", "摘要", "主题标签", "质量分", "入库状态", "评分理由", "正文检查", "正文长度", "来源"]
    write_csv(OUTPUT / "all_articles_scored.csv", rows, fields)
    write_csv(OUTPUT / "ima_ready_candidates.csv",
              [row for row in rows if row["入库状态"] == "ready"], fields)
    write_csv(OUTPUT / "ima_review_candidates.csv",
              [row for row in rows if row["入库状态"] == "candidate"], fields)
    write_csv(OUTPUT / "ima_rejected.csv",
              [row for row in rows if row["入库状态"].startswith("reject")], fields)
    write_batches([row for row in rows if row["入库状态"] == "ready"],
                  OUTPUT / "ima_batches", as_of)
    write_batches([row for row in rows if row["入库状态"] == "candidate"],
                  OUTPUT / "ima_review_batches", as_of)

    counts = defaultdict(int)
    for row in rows:
        counts[row["入库状态"]] += 1
    print(f"去重后 {len(rows)} 篇：" + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"ready: {OUTPUT / 'ima_ready_candidates.csv'}")
    print(f"batches: {OUTPUT / 'ima_batches'}")


if __name__ == "__main__":
    main()
