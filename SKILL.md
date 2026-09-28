---
name: wechat-article-weekly
description: "Weekly refresh of Monica's curated WeChat Official Account article library: crawl accounts.xlsx targets, quality-screen and deduplicate newly discovered articles, and import real mp.weixin.qq.com links into classified IMA folders. Use for 公众号周更、每周摘取文章、更新量化交易知识库或复核公众号抓取队列. Do not use for uploading Markdown article indexes."
---

# WeChat Article Weekly

Refresh this repository and the shared IMA knowledge base path `量化交易知识库 > 投研策略 > 公众号精选·量化交易`.

This public repository is a handoff package. It includes the current account list
and import-state snapshot, but never commit `config/session.curl`: that file is a
live login credential. Copy `config/session.example.curl` locally and fill it
only when the authenticated content-probe channel is needed.

## Required Outcome

- Read the maintained targets and categories from `accounts.xlsx` / `accounts.json`.
- Discover recent articles without mixing similarly named accounts.
- Keep only quality-screened, accessible, real `https://mp.weixin.qq.com/` article URLs.
- Submit only URLs not recorded in `output/ima_imported_urls.json`.
- Import through IMA's `网页链接` action, never by uploading Markdown, CSV, or link-index files.
- Preserve the ten IMA category folders and report submitted, parsed, queued, rejected, and blocked counts.

## Modules

- `scripts/weekly_refresh.py`: checkpointed weekly orchestration and submitted-state tracking.
- `scripts/account_loader.py`: standard-library loader for `accounts.xlsx`, JSON, or TXT lists.
- `scripts/public_index_batch.py`: exact-account public-index crawl with resumable checkpoints.
- `scripts/quality_filter.py`: deduplication, scoring, accessibility filtering, and batch generation.
- `scripts/content_probe.py`: low-frequency article-body accessibility checks.
- `scripts/wechat_crawler.py`: Sogou and optional authenticated WeChat crawler implementation.
- `references/ima-import.md`: manual IMA web-link import and verification procedure.
- `accounts.xlsx`: maintained account/category input.
- `output/ima_imported_urls.json`: no-repeat state; preserve it across runs.

## Run

From a clone of the repository, prepare the weekly queue:

```bash
python3 scripts/weekly_refresh.py prepare --workspace .
```

The command writes the dated queue and `output/weekly/latest_queue.json`. It is checkpointed and safe to rerun. If the crawler is blocked, retain the checkpoint and stop instead of replacing prior output with an empty result.

Read [references/ima-import.md](references/ima-import.md) before importing a non-empty queue. Use the Computer Use skill for IMA UI actions. Do not upload the Markdown, CSV, or JSON files to IMA; submit only the real article URLs through `网页链接`.

After every queued batch has been submitted and the IMA folder counts have increased accordingly, mark the queue submitted:

```bash
python3 scripts/weekly_refresh.py mark-imported \
  --workspace . --queue output/weekly/latest_queue.json
```

Do not run `mark-imported` for failed or unsubmitted batches. If only part of a queue succeeds, create a JSON file containing only the confirmed articles and mark that partial queue.

## Operational Rules

- Treat `config/session.curl` as a credential. Never print, quote, upload, or commit it.
- Keep `config/session.curl` local and verify `git status` before every push.
- Prefer the public-index checkpoint workflow. The WeChat backend cross-account list may return `ret=200013`; do not loop aggressively or refresh credentials to bypass it.
- Accept a public-index result only when its account name exactly matches the requested account after normalization.
- Keep request delays at or above script defaults. Stop on anti-crawl pages and leave the checkpoint for the next run.
- Deduplicate by canonical URL and normalized title. The submitted-state file is the final no-repeat gate.
- IMA accepts at most ten newline-separated links per import. Wait for the folder item count to increase before submitting the next batch when IMA is congested.
- `等待解析` is submitted but not yet parsed. Report it separately; do not resubmit it in the same run.
- Never delete existing IMA content during a weekly refresh.

## Completion Report

Report the date, target-account count, discovered and unique counts, new ready count, per-folder submitted counts, IMA parsing backlog, candidate/rejected counts, unresolved accounts, and paths to the queue and CSV outputs. If there are no new qualified links, say so and do not open IMA.
