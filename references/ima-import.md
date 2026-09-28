# IMA Web-Link Import

Use this procedure only after `python3 scripts/weekly_refresh.py prepare --workspace .`
produces a non-empty queue.

The repository is public, but IMA is a separate authenticated application. Never
upload the repository files to IMA. Only submit the `url` values from the queue.

## Destination

Shared knowledge base: `量化交易知识库`

Folder path: `投研策略 > 公众号精选·量化交易 > <folder>`

Canonical folders:

- `量化投研`
- `金工卖方`
- `宏观研究`
- `债券研究`
- `基金投资`
- `投资交易`
- `量化IT`
- `AI技术`
- `量化公司`
- `尽调与市场`

Create a missing canonical folder. Do not rename or merge existing user folders without an explicit request.

## Queue

Read `output/weekly/latest_queue.json`. Each item in `groups` contains a destination `folder` and `batches`; each batch has no more than ten article objects.

For every non-empty batch:

1. Enter its destination folder.
2. Choose the toolbar add action, then `网页链接`.
3. Paste only the batch's `url` values, one URL per line.
4. Confirm the form says it supports public pages and at most ten links, then choose `导入`.
5. Verify the folder item count increased by the batch size. Titles may appear gradually while IMA shows `等待解析` or `解析中`.

Valid items should resolve to article titles with source `公众号`. A raw URL in `等待解析` still counts as submitted, but record the backlog in the completion report.

## Failure Handling

- If a batch does not increase the item count, retry it once after checking the current folder and form contents.
- If only some items are added, identify the confirmed URLs when possible and retain the others in an unmarked queue.
- Do not use `本地文件`, `本地文件夹`, Markdown, CSV, or note import as a substitute.
- Do not delete parse failures automatically. Leave them for review and do not mark them submitted unless their IMA item exists.
- Mark imported state only after the corresponding IMA items exist:

  ```bash
  python3 scripts/weekly_refresh.py mark-imported \
    --workspace . --queue output/weekly/latest_queue.json
  ```
