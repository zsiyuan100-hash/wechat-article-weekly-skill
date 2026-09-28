#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号文章爬虫（双通道）
=============================================
- 通道1 sogou（免登录，默认）：sogou 微信文章搜索 -> 解析跳转得到真实 mp.weixin.qq.com 直达链接
- 通道2 session（微信公众平台登录态，需有公众号后台权限）：searchbiz 解析账号 -> profile_ext getmsg
- 输入：accounts.json / accounts.txt（每行一个公众号名）
- 输出：output/index.csv + output/<公众号名>.md + output/links.txt + output/_log.jsonl
- 依赖：仅标准库

用法：
  python3 scripts/wechat_crawler.py --depth 50                 # 全量抓取，每个号最近50篇
  python3 scripts/wechat_crawler.py --filter 中金点睛          # 只抓指定号
  python3 scripts/wechat_crawler.py --channel sogou            # 强制 sogou 免登录通道（默认自动选择）
  python3 scripts/wechat_crawler.py --validate                 # 验证通道 + 打印诊断
  python3 scripts/wechat_crawler.py --with-content             # 额外抓全文存本地
  python3 scripts/wechat_crawler.py --supplement google_urls.csv  # 合并 Google 补充链接
"""

import argparse
import csv
import html
import http.cookiejar
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from account_loader import category_map, load_accounts as load_account_records

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = ROOT / "output"
SESSION_FILE = Path(os.environ.get("WECHAT_SESSION_FILE", CONFIG_DIR / "session.curl"))
DEFAULT_ACCOUNTS = ROOT / "accounts.xlsx"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# 会话解析：从 "Copy as cURL" 行里提取 Cookie / token / pass_ticket 等
# --------------------------------------------------------------------------- #
def parse_session(curl_text):
    """从 cURL 命令行里解析出 {cookie, token, pass_ticket, uin, appmsg_token}."""
    s = curl_text.strip()
    # 提取首个 URL（curl '...'）
    m = re.search(r"curl\s+['\"]([^'\"]+)['\"]", s)
    url = m.group(1) if m else ""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query) if url else {}
    token = q.get("token", [""])[0]
    pass_ticket = q.get("pass_ticket", [""])[0]

    # 提取所有 -H 'Header: value'（兼容单双引号）
    headers = {}
    for h in re.findall(r"-H\s+['\"]([^'\"]+)['\"]", s):
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    cookie = headers.get("cookie", "")

    def _cookie_val(name):
        m = re.search(r"(?:^|;\s*)" + name + r"=([^;\s]+)", cookie)
        return m.group(1) if m else ""

    key = q.get("key", [""])[0] or _cookie_val("key")
    if not pass_ticket:
        pass_ticket = _cookie_val("pass_ticket")
    return {
        "cookie": cookie,
        "token": token,
        "pass_ticket": pass_ticket,
        "uin": _cookie_val("wxuin") or q.get("uin", [""])[0],
        "appmsg_token": _cookie_val("appmsg_token") or q.get("appmsg_token", [""])[0],
        "key": key,
        "raw": s,
    }


def load_session(required=True):
    if not os.path.exists(SESSION_FILE):
        if required:
            sys.exit(
                "[!] 缺少登录态：请先按 SKILL.md 的「获取登录态」把 cURL 粘贴到 "
                f"{SESSION_FILE}\n    然后重试。"
            )
        return None
    with open(SESSION_FILE, encoding="utf-8") as f:
        text = f.read()
    sess = parse_session(text)
    if required and not sess["cookie"]:
        sys.exit("[!] 未从 cURL 里解析到 Cookie。请确认粘贴的是 mp.weixin.qq.com 请求的 cURL。")
    return sess


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url, sess, referer="https://mp.weixin.qq.com/", retries=3, timeout=20):
    last_err = None
    for i in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Cookie": sess["cookie"],
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (i + 1))
    return 0, ""

def http_get_html(url, sess, retries=2):
    """正文页直接抓，登录态失效也能抓（公开页面），失败返回空串。"""
    status, text = http_get(url, sess, referer="https://mp.weixin.qq.com/s", retries=retries)
    if status != 200:
        return ""
    return text


# --------------------------------------------------------------------------- #
# 微信公众平台内部 API
# --------------------------------------------------------------------------- #
def api_search_biz(sess, name):
    """公众号搜索：返回 fakeid(即 __biz)。"""
    rand = random.random()
    url = ("https://mp.weixin.qq.com/cgi-bin/searchbiz?action=search_biz"
           f"&token={urllib.parse.quote(sess['token'])}&lang=zh_CN&f=json&ajax=1"
           f"&random={rand}&query={urllib.parse.quote(name)}&begin=0&count=5")
    status, text = http_get(url, sess)
    if status != 200 or not text:
        return None, "request-failed"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "bad-json"
    base = data.get("base_resp", {})
    if base.get("ret") != 0:
        return None, f"ret={base.get('ret')}"
    lst = data.get("list") or []
    if not lst:
        return None, "no-result"
    # 优先精确昵称匹配
    for item in lst:
        if item.get("nickname") == name:
            return item.get("fakeid"), "ok"
    return lst[0].get("fakeid"), "fuzzy"


def api_get_msg(sess, biz, offset, count=5):
    """后台 appmsg list_ex 接口拉一页文章。返回 (articles, next_offset, ok, msg)。
    只需 cookie + token（公众号后台运营者登录态），begin 按篇数翻页。"""
    url = ("https://mp.weixin.qq.com/cgi-bin/appmsg?action=list_ex"
           f"&begin={offset}&count={count}&fakeid={urllib.parse.quote(biz)}"
           f"&type=9&query=&token={urllib.parse.quote(sess['token'])}"
           "&lang=zh_CN&f=json&ajax=1")
    status, text = http_get(
        url, sess,
        referer="https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1",
    )
    if status != 200 or not text:
        return [], offset, False, "request-failed"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], offset, False, "bad-json"
    base = data.get("base_resp", {})
    if base.get("ret") != 0:
        return [], offset, False, f"ret={base.get('ret')}"
    articles = []
    for item in data.get("app_msg_list", []) or []:
        link = html.unescape(item.get("link", ""))
        if link.startswith("http://"):
            link = "https://" + link[len("http://"):]
        ts = item.get("create_time", 0)
        articles.append({
            "title": (item.get("title") or "").strip(),
            "url": link,
            "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "",
            "digest": (item.get("digest") or "").strip(),
        })
    if len(articles) < count:
        return articles, -1, True, "ok"   # 没有更多了
    return articles, offset + count, True, "ok"


# --------------------------------------------------------------------------- #
# 正文提取（可选）
# --------------------------------------------------------------------------- #
def fetch_content_text(sess, url):
    h = http_get_html(url, sess)
    if not h:
        return ""
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>', h, re.S)
    body = m.group(1) if m else ""
    body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
    body = re.sub(r"<[^>]+>", "\n", body)
    body = html.unescape(body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body[:20000]


# --------------------------------------------------------------------------- #
# sogou 免登录通道：微信文章搜索 -> /link 跳转 -> 真实 mp.weixin.qq.com 链接
# --------------------------------------------------------------------------- #
class SogouCrawler:
    """带 cookiejar 的 sogou 微信文章搜索爬虫。"""
    SEARCH_URL = "https://weixin.sogou.com/weixin?type=2&query={q}&page={page}"
    MOBILE_SEARCH_URL = ("https://weixin.sogou.com/weixinwap?type=2&s_from=input&query={q}"
                         "&ie=utf8&page={page}&_sug_=n&_sug_type_=")
    BLOCKED = "antispider"

    def __init__(self, delay=2.0, mobile=False):
        self.delay = delay
        self.mobile = mobile
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("User-Agent", ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 Mobile/15E148") if mobile else UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
        ]

    def _get(self, url, referer=None):
        headers = {"Referer": referer} if referer else {}
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=20) as resp:
            final = resp.geturl()
            body = resp.read().decode("utf-8", "replace")
        return final, body

    def search_page(self, name, page):
        """搜索一页，返回 [{title, account, date, link}]（link 为 sogou /link 相对地址）。"""
        url_template = self.MOBILE_SEARCH_URL if self.mobile else self.SEARCH_URL
        url = url_template.format(q=urllib.parse.quote(name), page=page)
        final, body = self._get(url, referer="https://weixin.sogou.com/")
        if self.BLOCKED in final or "antispider" in body[:2000]:
            raise RuntimeError("sogou-antispider")
        if self.mobile:
            return self._parse_mobile_page(body)
        items = []
        # 每条结果: <h3><a ... href="/link?url=...">标题</a></h3> ... <a class="account">号名</a> ... 日期
        for m in re.finditer(
            r'<h3>\s*<a[^>]*href="(/link\?[^"]+)"[^>]*>(.*?)</a>\s*</h3>(.*?)(?=<h3>|<div id="pagebar"|$)',
            body, re.S,
        ):
            # Do not run html.unescape() over the full URL. In query strings,
            # "&timestamp" can be interpreted as the HTML entity "&times" and
            # silently become "×tamp", which makes otherwise valid links fail.
            link = m.group(1).replace("&amp;", "&")
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            tail = m.group(3)
            summary_match = re.search(r'class="txt-info"[^>]*>(.*?)</p>', tail, re.S)
            summary = ""
            if summary_match:
                summary = re.sub(r"<[^>]+>", "", summary_match.group(1))
                summary = html.unescape(summary).strip()
            acc = (re.search(r'class="account"[^>]*>(.*?)</a>', tail, re.S)
                   or re.search(r'class="all-time-y2"[^>]*>(.*?)</span>', tail, re.S))
            account = re.sub(r"<[^>]+>|<!--.*?-->", "", acc.group(1)).strip() if acc else ""
            date = self._parse_date(tail)
            if title and link:
                items.append({"title": title, "account": account, "date": date,
                              "digest": summary, "link": link})
        return items

    def _parse_mobile_page(self, body):
        """解析移动版 /weixinwap 的逐条 li 结构。"""
        items = []
        for block in re.findall(
            r'<li[^>]+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>', body, re.S
        ):
            anchor = re.search(
                r'<a[^>]+href="(/link\?[^"]+)"[^>]+data-uigs="article_title_\d+"[^>]*>(.*?)</a>',
                block, re.S,
            )
            if not anchor:
                continue
            link = anchor.group(1).replace("&amp;", "&")
            title = re.sub(r"<[^>]+>|<!--.*?-->", "", anchor.group(2))
            title = html.unescape(title).strip()
            summary_match = re.search(r'data-type="article_summary"[^>]*>(.*?)</p>', block, re.S)
            summary = ""
            if summary_match:
                summary = re.sub(r"<[^>]+>|<!--.*?-->", "", summary_match.group(1))
                summary = html.unescape(summary).strip()
            account_match = re.search(r'data-sourcename="([^"]+)"', block, re.S)
            account = html.unescape(account_match.group(1)).strip() if account_match else ""
            date_match = re.search(r'class="s3"[^>]+data-lastModified="(\d+)"[^>]*>(.*?)</span>', block, re.S)
            if date_match:
                date = datetime.fromtimestamp(int(date_match.group(1))).strftime("%Y-%m-%d")
            else:
                date = self._parse_date(block)
            if title and link:
                items.append({"title": title, "account": account, "date": date,
                              "digest": summary, "link": link})
        return items

    @staticmethod
    def _parse_date(tail):
        ts = re.search(r"timeConvert\('(\d{10})'\)", tail)
        if ts:
            return datetime.fromtimestamp(int(ts.group(1))).strftime("%Y-%m-%d")
        d = re.search(r'(\d{4}-\d{2}-\d{2})', tail)
        if d:
            return d.group(1)
        d = re.search(r'(\d{1,2})月(\d{1,2})日', tail)
        if d:
            return datetime.now().strftime("%Y-") + f"{int(d.group(1)):02d}-{int(d.group(2)):02d}"
        if "昨天" in tail:
            return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if "前天" in tail:
            return (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        d = re.search(r'(\d{1,2})天前', tail)
        if d:
            return (datetime.now() - timedelta(days=int(d.group(1)))).strftime("%Y-%m-%d")
        return datetime.now().strftime("%Y-%m-%d")

    def resolve_link(self, sogou_link, query):
        """把 sogou /link 跳转解析成真实 mp.weixin.qq.com 文章 URL。"""
        url = "https://weixin.sogou.com" + sogou_link
        entry = "weixinwap" if self.mobile else "weixin"
        final, body = self._get(url, referer="https://weixin.sogou.com/" + entry + "?type=2&query=" + urllib.parse.quote(query))
        if self.BLOCKED in final:
            raise RuntimeError("sogou-antispider")
        # 302 已跟随: final 即真实地址
        if "mp.weixin.qq.com" in final:
            return final
        # 新版中间页: url += 'https://mp.'; url += 'weixin.qq.c'; ... 一行行拼接
        frags = re.findall(r"url\s*\+=\s*'([^']*)'", body)
        if frags and "".join(frags).startswith("http"):
            return "".join(frags).replace("@", "")
        # 旧版中间页: window.location.replace("http://mp.weixin.qq.com/s?...")
        m = re.search(r'window\.location\.replace\("([^"]+)"\)', body) \
            or re.search(r"window\.location\.href\s*=\s*'([^']+)'", body) \
            or re.search(r'var\s+url\s*=\s*[\'"]([^\'"]+mp\.weixin\.qq\.com[^\'"]*)[\'"]', body)
        if m:
            return m.group(1)
        return ""


def crawl_one_account_sogou(crawler, name, depth):
    articles, seen = [], set()
    try:
        page = 1
        while len(articles) < depth and page <= 10:
            items = crawler.search_page(name, page)
            if not items:
                break
            matched = [it for it in items if it["account"] == name] or items
            got = 0
            for it in matched:
                if len(articles) >= depth:
                    break
                if it["title"] in seen:
                    continue
                try:
                    real = crawler.resolve_link(it["link"], name)
                except (urllib.error.URLError, TimeoutError, OSError):
                    # A single expired or throttled Sogou redirect must not
                    # discard the other articles found for this account.
                    real = ""
                time.sleep(crawler.delay)
                if not real:
                    continue
                seen.add(it["title"])
                articles.append({"title": it["title"], "url": real,
                                 "date": it["date"], "digest": it.get("digest", "")})
                got += 1
            if got == 0 and page >= 2:
                break
            page += 1
            time.sleep(crawler.delay)
        return {"account": name, "status": "ok" if articles else "no-result",
                "articles": articles, "error": "" if articles else "sogou 无该号结果"}
    except RuntimeError as e:
        return {"account": name, "status": "blocked", "articles": articles,
                "error": str(e)}



def crawl_one_account(sess, name, depth, delay, with_content=False, out_dir=None):
    biz, status = api_search_biz(sess, name)
    if not biz:
        return {"account": name, "status": status, "articles": [], "error": f"searchbiz: {status}"}

    articles, seen = [], set()
    offset = 0
    pages = 0
    while len(articles) < depth and pages < 200:
        batch, next_offset, ok, msg = api_get_msg(sess, biz, offset)
        pages += 1
        if not ok:
            return {"account": name, "status": "getmsg-failed", "articles": articles,
                    "error": f"getmsg: {msg} @offset={offset}"}
        for a in batch:
            key = a["url"] or (a["title"] + a["date"])
            if key not in seen:
                seen.add(key)
                articles.append(a)
        if next_offset <= offset:
            break
        offset = next_offset
        time.sleep(delay)

    if with_content and articles:
        d = os.path.join(out_dir or OUTPUT_DIR, "articles", safe_name(name))
        os.makedirs(d, exist_ok=True)
        for a in articles[:depth]:
            if a["url"]:
                txt = fetch_content_text(sess, a["url"])
                if txt:
                    fn = f"{a['date'] or 'nodate'}-{safe_name(a['title'])[:40]}.md"
                    with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                        f.write(f"# {a['title']}\n\n> {a['date']}  |  {a['url']}\n\n{txt}\n")
                time.sleep(delay)

    return {"account": name, "status": "ok", "articles": articles[:depth], "error": ""}


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "unknown"


# --------------------------------------------------------------------------- #
# 输出渲染
# --------------------------------------------------------------------------- #
def render(results, categories, supplement=None, out_dir=None):
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)

    all_rows, links = [], []
    supplement = supplement or {}

    for r in results:
        name = r["account"]
        cat = categories.get(name, "")
        rows = [(name, cat, a["title"], a["url"], a["date"], a["digest"])
                for a in r["articles"]]
        # 合并 Google 补充链接（去重）
        for s in supplement.get(name, []):
            if s["url"] and s["url"] not in {x[3] for x in rows}:
                rows.append((name, cat, s["title"], s["url"], s.get("date", ""), s.get("digest", "")))
        rows.sort(key=lambda x: x[4], reverse=True)
        all_rows.extend(rows)
        links.extend(x[3] for x in rows if x[3])

        fn = os.path.join(out_dir, safe_name(name) + ".md")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\n")
            f.write(f"分类：{cat} ｜ 共 {len(rows)} 篇\n\n")
            f.write("| 日期 | 标题 | 摘要 |\n| --- | --- | --- |\n")
            for _, _, title, url, date, digest in rows:
                digest_s = (digest or "").replace("|", "\\|").replace("\n", " ")[:80]
                f.write(f"| {date} | [{title}]({url}) | {digest_s} |\n")

    # index.csv
    with open(os.path.join(out_dir, "index.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["公众号", "分类", "标题", "链接", "日期", "摘要"])
        w.writerows(all_rows)

    with open(os.path.join(out_dir, "links.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(dict.fromkeys(links)) + "\n")

    # 日志
    with open(os.path.join(out_dir, "_log.jsonl"), "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({
                "account": r["account"], "status": r["status"],
                "count": len(r["articles"]), "error": r.get("error", ""),
            }, ensure_ascii=False) + "\n")

    return all_rows


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def load_accounts(path=None):
    account_path = Path(path) if path else DEFAULT_ACCOUNTS
    if not account_path.exists():
        sys.exit(f"[!] 找不到公众号名单：{account_path}")
    try:
        return [item["name"] for item in load_account_records(account_path)]
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"[!] 无法读取公众号名单 {account_path}: {exc}")


def load_categories(path=None):
    account_path = Path(path) if path else DEFAULT_ACCOUNTS
    if not account_path.exists():
        return {}
    return category_map(load_account_records(account_path))


def load_supplement(path):
    """Google 补充 CSV：列 = 公众号,标题,链接,日期,摘要"""
    out = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("公众号") or row.get("account") or "").strip()
            if not name:
                continue
            out.setdefault(name, []).append({
                "title": (row.get("标题") or "").strip(),
                "url": (row.get("链接") or "").strip(),
                "date": (row.get("日期") or "").strip(),
                "digest": (row.get("摘要") or "").strip(),
            })
    return out


def main():
    ap = argparse.ArgumentParser(description="微信公众号文章爬虫")
    ap.add_argument("--accounts", help="公众号名单文件（xlsx/json/txt），默认用项目 accounts.xlsx")
    ap.add_argument("--filter", help="只抓包含该关键字的号（逗号分隔多个）")
    ap.add_argument("--depth", type=int, default=50, help="每个号抓最近多少篇（默认50）")
    ap.add_argument("--delay", type=float, default=1.2, help="请求间隔秒（默认1.2）")
    ap.add_argument("--with-content", action="store_true", help="额外抓全文存本地")
    ap.add_argument("--supplement", help="Google 补充链接 CSV（公众号,标题,链接,日期,摘要）")
    ap.add_argument("--out", help="输出目录（默认 output/）")
    ap.add_argument("--session-file", help="登录态 cURL 文件（默认 config/session.curl）")
    ap.add_argument("--validate", action="store_true", help="验证登录态并打印诊断")
    ap.add_argument("--no-resume", action="store_true", help="忽略断点，从头重抓全部公众号")
    ap.add_argument("--channel", choices=["auto", "sogou", "session"], default="auto",
                    help="抓取通道：auto=有 session.curl 用 session 否则 sogou（默认）")
    args = ap.parse_args()

    global SESSION_FILE
    if args.session_file:
        SESSION_FILE = Path(args.session_file).expanduser().resolve()

    if args.channel == "auto":
        args.channel = "session" if os.path.exists(SESSION_FILE) else "sogou"
    sess = load_session(required=(args.channel == "session"))

    if args.validate and args.channel == "sogou":
        sc = SogouCrawler(delay=args.delay)
        try:
            items = sc.search_page("量化投资", 1)
            print(f"[validate] sogou 搜索到 {len(items)} 条")
            for it in items[:3]:
                print("   -", it["date"], it["account"], it["title"], "|", it["link"][:60])
            real = sc.resolve_link(items[0]["link"], "量化投资")
            print(f"[validate] link 解析 -> {real[:100] or '失败'}")
        except RuntimeError as e:
            print(f"[validate] 被反爬拦截：{e}，稍后再试或降低频率。")
        return
    if args.validate:
        name = "中金点睛"
        biz, status = api_search_biz(sess, name)
        print(f"[validate] searchbiz({name}): status={status}, biz={biz}")
        if biz:
            arts, nxt, ok, msg = api_get_msg(sess, biz, 0)
            print(f"[validate] getmsg offset=0: ok={ok} ({msg}), articles={len(arts)}, next_offset={nxt}")
            for a in arts[:3]:
                print("   -", a["date"], a["title"], "|", a["url"][:80])
        print("[validate] 若 status=ret=-3 或 ok=False，请刷新 config/session.curl 重新登录。")
        return

    accounts = load_accounts(args.accounts)
    if args.filter:
        keys = [k.strip() for k in args.filter.split(",") if k.strip()]
        accounts = [a for a in accounts if any(k in a for k in keys)]
    if not accounts:
        sys.exit("[!] 过滤后没有公众号。")

    categories = load_categories(args.accounts)
    supplement = load_supplement(args.supplement)
    out_dir = args.out or OUTPUT_DIR

    print(f"共 {len(accounts)} 个公众号，每个抓最近 {args.depth} 篇，通道={args.channel} ...")
    sc = SogouCrawler(delay=max(args.delay, 2.0)) if args.channel == "sogou" else None

    # 断点续跑：已成功的号直接复用
    ckpt_path = os.path.join(out_dir, "_checkpoint.json")
    done = {}
    if sc is not None and os.path.exists(ckpt_path) and not args.no_resume:
        try:
            with open(ckpt_path, encoding="utf-8") as f:
                done = {k: v for k, v in json.load(f).items() if v.get("status") == "ok"}
            print(f"[resume] 复用已完成的 {len(done)} 个号（output/_checkpoint.json）")
        except Exception:
            done = {}

    def save_ckpt(res_list):
        os.makedirs(out_dir, exist_ok=True)
        with open(ckpt_path, "w", encoding="utf-8") as f:
            json.dump({r["account"]: r for r in res_list if r["status"] == "ok"}, f,
                      ensure_ascii=False)

    results, blocked_streak = [], 0
    queue = [n for n in accounts if n not in done]
    results.extend(done[n] for n in accounts if n in done)
    for i, name in enumerate(queue, 1):
        t0 = time.time()
        while True:
            if sc is not None:
                r = crawl_one_account_sogou(sc, name, args.depth)
            else:
                r = crawl_one_account(sess, name, args.depth, args.delay,
                                      with_content=args.with_content, out_dir=out_dir)
            if r["status"] == "blocked" and blocked_streak < 5:
                blocked_streak += 1
                wait = min(300 * blocked_streak, 1800)  # 5/10/15/20/25 分钟递增冷却
                print(f"[{i}/{len(queue)}] {name} 被反爬拦截，冷却 {wait//60} 分钟后重试"
                      f"（第 {blocked_streak}/5 次）...")
                time.sleep(wait)
                continue
            break
        blocked_streak = 0 if r["status"] != "blocked" else blocked_streak
        dt = time.time() - t0
        tag = "OK " if r["status"] == "ok" else "ERR"
        print(f"[{i}/{len(queue)}] {tag} {name} -> {len(r['articles'])} 篇 "
              f"({dt:.0f}s){' | ' + r['error'] if r['error'] else ''}")
        results.append(r)
        save_ckpt(results + list(done.values()))

    all_rows = render(results, categories, supplement, out_dir)
    ok = sum(1 for r in results if r["status"] == "ok")
    total_articles = len(all_rows)
    print(f"\n完成：{ok}/{len(results)} 个号成功，共 {total_articles} 篇文章。")
    print(f"输出：\n  {os.path.join(out_dir, 'index.csv')}\n  {os.path.join(out_dir, 'links.txt')}\n  {out_dir}/<公众号名>.md")
    fails = [r["account"] for r in results if r["status"] != "ok"]
    if fails:
        print(f"失败 {len(fails)} 个：{', '.join(fails)}（详见 output/_log.jsonl）")


if __name__ == "__main__":
    main()
