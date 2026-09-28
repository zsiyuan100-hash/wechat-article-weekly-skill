# WeChat Article Weekly Skill

可交接的微信公众号周更抓取与 IMA 入库工具包。

仓库地址：<https://github.com/zsiyuan100-hash/wechat-article-weekly-skill>

## What It Does

1. 从 `accounts.xlsx` 读取公众号名称和分类。
2. 通过公开索引按账号精确匹配文章，并保存可续跑检查点。
3. 对文章去重、评分，并检查正文是否可访问。
4. 生成按 IMA 文件夹分类的 URL 队列。
5. 通过 IMA 的 `网页链接` 动作人工分批提交，每批最多 10 条。
6. 只有确认 IMA 条目已经出现后，才更新 `output/ima_imported_urls.json`。

## Repository Layout

```text
SKILL.md
accounts.xlsx
config/
  session.example.curl
references/
  ima-import.md
scripts/
  account_loader.py
  content_probe.py
  public_index_batch.py
  quality_filter.py
  wechat_crawler.py
  weekly_refresh.py
output/
  ima_imported_urls.json
```

## Quick Start

```bash
git clone https://github.com/zsiyuan100-hash/wechat-article-weekly-skill.git
cd wechat-article-weekly-skill
python3 scripts/weekly_refresh.py prepare --workspace .
```

The default input is `accounts.xlsx`. The loader also accepts JSON or TXT files:

```bash
python3 scripts/weekly_refresh.py prepare \
  --workspace . --accounts accounts.json
```

The crawler uses the public Sogou index by default. It may be rate-limited or
blocked. The checkpoint is intentionally retained so a later run can continue.

## Optional Authenticated Probe

The final quality pass can use a WeChat public-platform session to check article
body accessibility. If that channel is available, create the local credential
file from the example:

```bash
cp config/session.example.curl config/session.curl
# Replace the placeholders locally. Do not commit this file.
python3 scripts/wechat_crawler.py --channel session --validate
```

`config/session.curl` contains a live login credential and is intentionally not
included in the repository. Do not paste it into issues, pull requests, chat, or
other shared locations.

## IMA Import

Read [references/ima-import.md](references/ima-import.md). Use only IMA's
`网页链接` action and paste the article URLs from
`output/weekly/latest_queue.json`. Do not upload Markdown, CSV, or JSON files as
a substitute. After each batch, verify the destination folder count increased.

When all batches are confirmed:

```bash
python3 scripts/weekly_refresh.py mark-imported \
  --workspace . --queue output/weekly/latest_queue.json
```

## Data Files

- `accounts.xlsx` is the current account/category snapshot used by the workflow.
- `output/ima_imported_urls.json` is the current no-repeat state. Keep it when
  handing the repository to another operator.
- Generated checkpoints, CSVs, probe results, and batch files are ignored by Git.

## Safety Rules

- Never commit `config/session.curl`.
- Never delete existing IMA content during a refresh.
- Never mark a URL imported before its IMA item is visible.
- Stop on anti-crawl, expired-session, or failed-batch errors and keep the
  checkpoint and unmarked queue.
