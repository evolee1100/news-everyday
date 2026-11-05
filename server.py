#!/usr/bin/env python3
"""地球速报 — 国际 + 马来西亚头条聚合器（零依赖，只用 Python 标准库）

运行：python3 server.py        然后打开 http://localhost:8000
      python3 server.py --port 9000
      python3 server.py --build  只抓一次新闻，输出 data/news.json（可部署成静态网站）
"""
import argparse
import html
import json
import math
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_SECONDS = 10 * 60          # 10 分钟内重复访问直接用缓存
MAX_AGE_HOURS = 36               # 只要最近 36 小时的新闻
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) EarthBrief/1.0"

# region: world / my     lang: en / zh
SOURCES = [
    # —— 国际 ——
    {"name": "BBC News",     "region": "world", "lang": "en", "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera",   "region": "world", "lang": "en", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "The Guardian", "region": "world", "lang": "en", "url": "https://www.theguardian.com/world/rss"},
    {"name": "NPR",          "region": "world", "lang": "en", "url": "https://feeds.npr.org/1004/rss.xml"},
    {"name": "CNA World",    "region": "world", "lang": "en", "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6311"},
    {"name": "CNA Asia",     "region": "world", "lang": "en", "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6511"},
    {"name": "BBC 中文",     "region": "world", "lang": "zh", "url": "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"},
    {"name": "DW 中文",      "region": "world", "lang": "zh", "url": "https://rss.dw.com/rdf/rss-chi-all"},
    {"name": "RFI 中文",     "region": "world", "lang": "zh", "url": "https://www.rfi.fr/cn/rss"},
    # —— 马来西亚 ——
    {"name": "Free Malaysia Today", "region": "my", "lang": "en", "url": "https://www.freemalaysiatoday.com/category/nation/feed/"},
    {"name": "Malay Mail",          "region": "my", "lang": "en", "url": "https://www.malaymail.com/feed/rss/malaysia"},
    {"name": "Malaysiakini",        "region": "my", "lang": "en", "url": "https://www.malaysiakini.com/rss/en/news.rss"},
    {"name": "Bernama",             "region": "my", "lang": "en", "url": "https://www.bernama.com/en/rssfeed.php"},
    {"name": "New Straits Times",   "region": "my", "lang": "en", "url": "https://www.nst.com.my/feed",
     "link_filter": r"/(news/(nation|politics|crime-courts)|business/economy)/"},
    {"name": "The Vibes",           "region": "my", "lang": "en", "url": "https://www.thevibes.com/rss",
     "link_filter": r"/articles/(news|business)/"},
    {"name": "The Sun",             "region": "my", "lang": "en", "url": "https://thesun.my/rss/local"},
    {"name": "MalaysiaNow",         "region": "my", "lang": "en", "url": "https://www.malaysianow.com/feed"},
    {"name": "当今大马",            "region": "my", "lang": "zh", "url": "https://www.malaysiakini.com/rss/zh/news.rss"},
    {"name": "诗华日报",            "region": "my", "lang": "zh", "url": "https://news.seehua.com/feed/",
     "cat_filter": r"西马|沙巴|砂|诗巫|古晋|美里|全国|大马|财经"},
]

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "rss1": "http://purl.org/rss/1.0/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

STOP = set("""a an the of to in on for and or but with at by from as is are was were be been has have had
it its this that these those after before over under into about amid says say said new will would could
can may not no up out off more than his her their they he she we you i us our who what when where why how
s t vs via just also first last two three one year years day days week news live update updates latest""".split())


# ---------------------------------------------------------------- 抓取与解析
def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_text(s, limit=220):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] if " " in s[:limit] else s[:limit]
        s += "…"
    return s


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def find_image(it, desc_raw):
    for tag in ("media:thumbnail", "media:content"):
        for el in it.findall(tag, NS):
            u = el.get("url")
            if u and (el.get("medium") in (None, "image") or re.search(r"\.(jpe?g|png|webp)", u, re.I)):
                return u
    enc = it.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    m = re.search(r'<img[^>]+src="([^"]+)"', desc_raw or "")
    return html.unescape(m.group(1)) if m else None


def parse_feed(raw, src):
    root = ET.fromstring(raw.lstrip())  # 有些 feed 开头多一个空行
    items = root.findall(".//item") or root.findall(".//rss1:item", NS)
    out = []
    for pos, it in enumerate(items):
        def g(*tags):
            for t in tags:
                el = it.find(t, NS)
                if el is not None and (el.text or "").strip():
                    return el.text.strip()
            return ""
        title = re.sub(r"^[A-Za-z]+ : ", "", clean_text(g("title", "rss1:title"), 200))  # Bernama 的 "General : " 前缀
        link = g("link", "rss1:link") or (it.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about") or "")
        if not title or not link:
            continue
        desc_raw = g("description", "rss1:description", "content:encoded")
        date = parse_date(g("pubDate", "dc:date", "rss1:date"))
        if date and date.timestamp() > time.time() + 600:
            date = datetime.now(timezone.utc)  # 有些网站把本地时间标成 UTC，会变成“未来”
        out.append({
            "title": title,
            "link": link.strip(),
            "summary": clean_text(desc_raw),
            "image": find_image(it, desc_raw),
            "time": date.isoformat() if date else None,
            "_ts": date.timestamp() if date else None,
            "_pos": pos,
            "_cats": " ".join((c.text or "") for c in it.findall("category")),
            "source": src["name"],
            "region": src["region"],
            "lang": src["lang"],
        })
    return out


def fetch_source(src):
    try:
        items = parse_feed(fetch(src["url"]), src)
        if src.get("link_filter"):  # 综合 feed 只留本地新闻栏目
            items = [i for i in items if re.search(src["link_filter"], i["link"])]
        if src.get("cat_filter"):   # 按 RSS 分类只留本地新闻
            items = [i for i in items if re.search(src["cat_filter"], i["_cats"])]
        return src, items, None
    except Exception as e:  # 单个来源失败不影响整体
        return src, [], f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 聚类与排序
def tokens(item):
    t = item["title"].lower()
    if item["lang"] == "zh":
        chars = re.sub(r"[^一-鿿]", "", t)
        return {chars[i:i + 2] for i in range(len(chars) - 1)}
    words = re.findall(r"[a-z0-9]+", t)
    return {w[:6] for w in words if w not in STOP and len(w) > 2}  # 粗略词干：前 6 字母


def similar(a, b):
    if a["lang"] != b["lang"]:
        return False
    ta, tb = a["_tok"], b["_tok"]
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    need = 3 if a["lang"] == "zh" else 2
    return inter >= need and inter / min(len(ta), len(tb)) >= 0.45


def item_score(it, now):
    # 编辑在 RSS 里排在越前面越重要；越新越重要（半衰期 10 小时）
    pos_w = 1.0 / (1 + 0.12 * it["_pos"])
    age_h = max(0.0, (now - it["_ts"]) / 3600) if it["_ts"] else 12
    fresh = math.exp(-age_h * math.log(2) / 10)
    return pos_w * (0.35 + 0.65 * fresh)


def rank(items, now, limit=30):
    for it in items:
        it["_tok"] = tokens(it)
        it["_score"] = item_score(it, now)
    items.sort(key=lambda x: -x["_score"])
    clusters = []
    for it in items:
        for c in clusters:
            if any(similar(it, m) for m in c):
                c.append(it)
                break
        else:
            clusters.append([it])

    result = []
    for c in clusters:
        best_by_src = {}
        for m in c:
            if m["source"] not in best_by_src or m["_score"] > best_by_src[m["source"]]["_score"]:
                best_by_src[m["source"]] = m
        members = sorted(best_by_src.values(), key=lambda x: -x["_score"])
        n = len(members)
        # 多家媒体同时报道 = 大新闻
        score = members[0]["_score"] + 0.55 * sum(m["_score"] for m in members[1:]) + 0.25 * (n - 1)
        lead = members[0]
        with_img = next((m for m in members if m.get("image")), None)
        if not lead.get("image") and with_img and with_img["_score"] > lead["_score"] - 0.15:
            lead = with_img  # 分数差不多时优先用有配图的版本
        result.append({
            **{k: v for k, v in lead.items() if not k.startswith("_")},
            "score": round(score, 3),
            "sources": n,
            "related": [{"source": m["source"], "title": m["title"], "link": m["link"]}
                        for m in members if m is not lead][:4],
        })
    result.sort(key=lambda x: -x["score"])
    return result[:limit]


def build_news():
    now = time.time()
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        results = list(ex.map(fetch_source, SOURCES))
    status, pool = [], {"world": [], "my": []}
    for src, items, err in results:
        fresh = [i for i in items if i["_ts"] is None or now - i["_ts"] < MAX_AGE_HOURS * 3600]
        status.append({"source": src["name"], "region": src["region"], "ok": err is None,
                       "count": len(fresh), "error": err})
        pool[src["region"]].extend(fresh)
    world = rank(pool["world"], now)
    # 马来西亚媒体也会报道国际大事（如美国大选）——国际版已有的就从大马版拿掉
    world_items = [{"lang": w["lang"], "_tok": tokens({"lang": w["lang"], "title": t})}
                   for w in pool["world"] for t in [w["title"]]]
    my = [c for c in rank(pool["my"], now, limit=60)
          if not any(similar({"lang": c["lang"], "_tok": tokens(c)}, w) for w in world_items)][:40]
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "world": world,
        "my": my,
        "status": status,
    }


# ---------------------------------------------------------------- 服务器
_cache = {"at": 0, "data": None}
_lock = threading.Lock()


def get_news(force=False):
    with _lock:
        if force or not _cache["data"] or time.time() - _cache["at"] > CACHE_SECONDS:
            _cache["data"] = build_news()
            _cache["at"] = time.time()
        return _cache["data"]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def do_GET(self):
        if self.path.startswith("/api/news"):
            data = json.dumps(get_news(force="refresh=1" in self.path), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--build", action="store_true", help="抓一次新闻写入 data/news.json 后退出")
    a = ap.parse_args()
    if a.build:
        (ROOT / "data").mkdir(exist_ok=True)
        data = build_news()
        (ROOT / "data" / "news.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        ok = sum(s["ok"] for s in data["status"])
        print(f"已写入 data/news.json：国际 {len(data['world'])} 条，马来西亚 {len(data['my'])} 条，来源 {ok}/{len(SOURCES)} 成功")
        return
    print(f"地球速报已启动 → http://localhost:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
