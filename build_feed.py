#!/usr/bin/env python3
"""Merge many feeds into one filtered RSS feed (feed.xml). No AI involved.

Usage: python build_feed.py [sources.yml] [output.xml]
"""
import datetime as dt
import hashlib
import html
import re
import sys
import time
from email.utils import format_datetime
from urllib.parse import quote_plus, urlsplit, urlunsplit, parse_qsl, urlencode
from xml.sax.saxutils import escape

import feedparser
import requests
import yaml

UA = "Mozilla/5.0 (compatible; CuratedFeedBot/1.0)"
TIMEOUT = 25
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def log(msg):
    print(msg, flush=True)


def fetch_bytes(url):
    if url.startswith("file://"):
        with open(url[7:], "rb") as f:
            return f.read()
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content


def resolve_youtube(handle):
    """Turn '@handle' into a channel id by reading the channel page."""
    page = fetch_bytes("https://www.youtube.com/" + handle.lstrip("/")).decode("utf-8", "replace")
    for pat in (r'"externalId":"(UC[\w-]{22})"',
                r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"channelId":"(UC[\w-]{22})"'):
        m = re.search(pat, page)
        if m:
            return m.group(1)
    raise ValueError("could not find channel id for " + handle)


def source_url(src):
    if "url" in src:
        return src["url"]
    if "google_news" in src:
        q = quote_plus(src["google_news"] + " when:" + str(src.get("days", 30)) + "d")
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    if "youtube_channel_id" in src:
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + src["youtube_channel_id"]
    if "youtube" in src:
        return "https://www.youtube.com/feeds/videos.xml?channel_id=" + resolve_youtube(src["youtube"])
    if "pinterest" in src:  # "user" or "user/board"
        return "https://www.pinterest.com/" + src["pinterest"].strip("/") + (".rss" if "/" in src["pinterest"] else "/feed.rss")
    raise ValueError("source needs one of: url, google_news, youtube, youtube_channel_id, pinterest")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_link(link):
    parts = urlsplit(link)
    q = [(k, v) for k, v in parse_qsl(parts.query) if k not in TRACKING]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), urlencode(q), ""))


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def compile_kw(words):
    if not words:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(w) for w in words) + r")(?![A-Za-z0-9])", re.I)


def entry_date(e):
    for k in ("published_parsed", "updated_parsed"):
        t = e.get(k)
        if t:
            return dt.datetime(*t[:6], tzinfo=dt.timezone.utc)
    return None


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "sources.yml"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "feed.xml"
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)
    st = cfg.get("settings", {})
    include = compile_kw(st.get("include_keywords", []))
    exclude = compile_kw(st.get("exclude_keywords", []))
    max_items = int(st.get("max_items", 150))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=int(st.get("max_age_days", 21)))

    items, seen_links, seen_titles = [], set(), set()
    total_errors = 0
    for src in cfg.get("sources", []):
        name = src["name"]
        mode = src.get("mode", "keyword")
        require = compile_kw(src.get("require_any", []))
        try:
            url = source_url(src)
            parsed = feedparser.parse(fetch_bytes(url))
            if parsed.bozo and not parsed.entries:
                raise ValueError("feed did not parse")
        except Exception as ex:  # one broken source must not stop the build
            total_errors += 1
            log(f"[ERROR] {name}: {ex}")
            continue
        fetched = kept = 0
        for e in parsed.entries:
            fetched += 1
            title = strip_html(e.get("title", ""))
            link = e.get("link", "")
            when = entry_date(e)
            if not title or not link or not when or when < cutoff:
                continue
            summary = strip_html(e.get("summary", ""))[:400]
            text = f"{title} {summary}"
            if mode == "keyword" and not (include and include.search(text)):
                continue
            if require and not require.search(text):
                continue
            if exclude and exclude.search(title):
                continue
            nl, nt = norm_link(link), norm_title(title)
            if nl in seen_links or nt in seen_titles:
                continue
            seen_links.add(nl)
            seen_titles.add(nt)
            items.append({"source": name, "title": title, "link": link, "when": when, "summary": summary})
            kept += 1
        log(f"[ok] {name}: {fetched} fetched, {kept} kept")

    if not items:
        log("No items produced; leaving existing feed untouched.")
        sys.exit(1)

    items.sort(key=lambda i: i["when"], reverse=True)
    items = items[:max_items]
    newest = items[0]["when"]

    feed_url = st.get("feed_url", "")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(st.get('title', 'Curated Feed'))}</title>",
        f"<link>{escape(st.get('site_url', 'https://github.com'))}</link>",
        f"<description>{escape(st.get('description', ''))}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{format_datetime(newest)}</lastBuildDate>",
    ]
    if feed_url:
        parts.append(f'<atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>')
    for i in items:
        guid = hashlib.sha1(norm_link(i["link"]).encode()).hexdigest()
        desc = f"{i['summary']}\n\nSource: {i['source']}" if i["summary"] else f"Source: {i['source']}"
        parts += [
            "<item>",
            f"<title>{escape('[' + i['source'] + '] ' + i['title'])}</title>",
            f"<link>{escape(i['link'])}</link>",
            f'<guid isPermaLink="false">{guid}</guid>',
            f"<pubDate>{format_datetime(i['when'])}</pubDate>",
            f"<description>{escape(desc)}</description>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
    log(f"Wrote {len(items)} items to {out_path} ({total_errors} source errors).")


if __name__ == "__main__":
    main()
