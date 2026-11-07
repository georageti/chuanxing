#!/usr/bin/env python3
"""川星：每天抓取 GitHub 高 star 项目并按用途分类。

只用 Python 标准库。用法：
    python3 fetch.py              # 正常运行
    python3 fetch.py --reclassify # 不联网，只按 categories.json 重新分类并重新生成输出
    python3 fetch.py --translate  # 不抓 GitHub，只把还没翻译的简介翻成中文，再重新生成输出

环境变量 GITHUB_TOKEN（或 GH_TOKEN）可选；有它限额高很多，没有也能跑，只是慢一点。
"""
import html
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "categories.json")
DB_PATH = os.path.join(ROOT, "data", "repos.json")
DAILY_DIR = os.path.join(ROOT, "daily")
DOCS_DIR = os.path.join(ROOT, "docs")

BEIJING = timezone(timedelta(hours=8))
TODAY = datetime.now(BEIJING).strftime("%Y-%m-%d")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
UA = "ChuanXing-daily-collector"
OTHER = {"id": "other", "name": "其他"}


def _ssl_context():
    """python.org 版的 Python 在 macOS 上默认没有根证书，缺证书时改用系统自带的。"""
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if not (paths.cafile and os.path.exists(paths.cafile)) and os.path.exists("/etc/ssl/cert.pem"):
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


SSL_CTX = _ssl_context()


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 网络请求

class Http:
    def __init__(self):
        self.search_interval = 2.2 if TOKEN else 6.5  # 搜索接口：有 token 30 次/分，没有 10 次/分
        self.last_search = 0.0
        self.calls = 0

    def get(self, url, api=True, tries=4):
        headers = {"User-Agent": UA}
        if api:
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if TOKEN:
                headers["Authorization"] = "Bearer " + TOKEN
        for attempt in range(tries):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
                    self.calls += 1
                    body = resp.read().decode("utf-8", "replace")
                    return json.loads(body) if api else body
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) and attempt < tries - 1:
                    wait = self._rate_wait(e.headers)
                    if wait is None or wait > 900:
                        log(f"  限流且等待时间过长，跳过：{url}")
                        return None
                    log(f"  被限流，等待 {wait:.0f} 秒…")
                    time.sleep(wait)
                    continue
                if e.code >= 500 and attempt < tries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                log(f"  请求失败 HTTP {e.code}：{url}")
                return None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < tries - 1:
                    time.sleep(3 * (attempt + 1))
                    continue
                log(f"  网络错误 {e}：{url}")
                return None
        return None

    @staticmethod
    def _rate_wait(headers):
        if headers.get("Retry-After"):
            return float(headers["Retry-After"]) + 1
        if headers.get("X-RateLimit-Remaining") == "0" and headers.get("X-RateLimit-Reset"):
            return max(1.0, float(headers["X-RateLimit-Reset"]) - time.time() + 2)
        return 60.0

    def search(self, q, per_page=100, sort="stars"):
        gap = self.search_interval - (time.time() - self.last_search)
        if gap > 0:
            time.sleep(gap)
        self.last_search = time.time()
        params = urllib.parse.urlencode({"q": q, "sort": sort, "order": "desc", "per_page": per_page})
        data = self.get("https://api.github.com/search/repositories?" + params)
        return (data or {}).get("items", [])


# ---------------------------------------------------------------- 数据来源

def parse_trending(page):
    """解析 github.com/trending 页面，返回 [(full_name, 本期新增 star)]。"""
    out = []
    for block in page.split('<article class="Box-row">')[1:]:
        m = re.search(r'<h2 class="h3 lh-condensed">.*?href="/([^"/]+/[^"/]+)"', block, re.S)
        if not m:
            continue
        gained = re.search(r"([\d,]+) stars? (?:today|this week|this month)", block)
        out.append((m.group(1), int(gained.group(1).replace(",", "")) if gained else 0))
    return out


def repo_record(item):
    return {
        "name": item["full_name"],
        "url": item["html_url"],
        "desc": (item.get("description") or "").strip(),
        "stars": item.get("stargazers_count", 0),
        "forks": item.get("forks_count", 0),
        "lang": item.get("language") or "",
        "topics": item.get("topics") or [],
        "created": (item.get("created_at") or "")[:10],
        "pushed": (item.get("pushed_at") or "")[:10],
        "archived": bool(item.get("archived")),
    }


def collect(http, cfg, db):
    """跑一遍所有来源，返回 {full_name: {"rec": 记录, "src": set(来源)}} 和 trending 信息。"""
    found = {}
    trending = {}

    def add(item, src):
        rec = repo_record(item)
        slot = found.setdefault(rec["name"], {"rec": rec, "src": set()})
        slot["rec"] = rec
        slot["src"].add(src)

    # 1) Trending
    for since in cfg.get("trending", ["daily"]):
        log(f"抓取 Trending（{since}）…")
        page = http.get(f"https://github.com/trending?since={since}", api=False)
        if not page:
            continue
        for name, gained in parse_trending(page):
            info = trending.setdefault(name, {})
            info[since] = gained
    log(f"  Trending 共 {len(trending)} 个项目，补全详情…")
    # 用搜索接口一次查多个仓库（repo:a/b repo:c/d …），查询语句最长 256 个字符，
    # 这样不占普通接口每小时 60 次的额度
    names, batch = list(trending), []
    def flush(batch):
        if not batch:
            return
        got = set()
        for item in http.search(" ".join("repo:" + n for n in batch), sort="stars"):
            add(item, "trending")
            got.add(item["full_name"].lower())
        for n in batch:
            if n.lower() not in got:
                log(f"  没查到 {n}（可能改了名或被设为私有）")
    for name in names:
        if len(" ".join("repo:" + n for n in batch + [name])) > 250:
            flush(batch)
            batch = []
        batch.append(name)
    flush(batch)
    # 搜索结果里的名字大小写可能和 Trending 页面不同，统一成搜索结果的写法
    canon = {k.lower(): k for k in found}
    trending = {canon.get(n.lower(), n): v for n, v in trending.items()}

    # 2) 近期新星
    rising = cfg.get("rising", {})
    since_day = (datetime.now(timezone.utc) - timedelta(days=rising.get("days", 30))).strftime("%Y-%m-%d")
    q = f"created:>{since_day} stars:>={rising.get('min_stars', 300)} archived:false"
    log(f"搜索近期新星：{q}")
    for item in http.search(q, per_page=min(100, rising.get("limit", 50))):
        add(item, "rising")

    # 3) 各分类的高 star 项目
    min_stars = cfg.get("min_stars", 1000)
    for cat in cfg["categories"]:
        for query in cat.get("queries", []):
            q = f"{query} stars:>={min_stars} archived:false"
            log(f"搜索 [{cat['name']}]：{q}")
            for item in http.search(q):
                add(item, "q:" + cat["id"])
    return found, trending


# ---------------------------------------------------------------- 分类

_ASCII = re.compile(r"^[\x00-\x7f]+$")


def norm(text):
    text = text.lower().replace("_", " ").replace("-", " ").replace("/", " ")
    return re.sub(r"\s+", " ", text)


def compile_rules(cfg):
    rules = []
    for cat in cfg["categories"]:
        pats = []
        for kw in cat.get("keywords", []):
            k = norm(kw).strip()
            if not k:
                continue
            if _ASCII.match(k):
                pats.append(re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?:s|es)?(?![a-z0-9])"))
            else:
                pats.append(re.compile(re.escape(k)))
        rules.append({"cat": cat, "topics": set(t.lower() for t in cat.get("topics", [])), "pats": pats})
    return rules


def score(rule, rec, sources):
    s = 3 * len(rule["topics"].intersection(t.lower() for t in rec.get("topics", [])))
    name = norm(rec["name"].split("/", 1)[-1])
    desc = norm(rec.get("desc", ""))
    for p in rule["pats"]:
        if p.search(name):
            s += 2
        elif p.search(desc):
            s += 1
    if ("q:" + rule["cat"]["id"]) in sources:
        s += 2
    return s


def classify(rec, sources, rules, cfg):
    threshold = cfg.get("threshold", 3)
    scores = [(score(r, rec, sources), r["cat"]["id"]) for r in rules]
    tags = [cid for sc, cid in scores if sc >= threshold]
    override = cfg.get("overrides", {}).get(rec["name"])
    if override:
        primary = override
    elif tags:
        # 达标的分类里取加权分最高的；同分时 categories 里排前面的优先（max 遇到相等取第一个）
        weighted = [(sc * r["cat"].get("weight", 1), cid) for (sc, cid), r in zip(scores, rules) if sc >= threshold]
        primary = max(weighted, key=lambda x: x[0])[1]
    else:
        best = max(scores, default=(0, OTHER["id"]))
        primary = best[1] if best[0] >= 2 else OTHER["id"]
    if primary not in tags:
        tags.insert(0, primary)
    return primary, tags


# ---------------------------------------------------------------- 翻译

CJK = re.compile(r"[\u4e00-\u9fff]")
SHORTCODE = re.compile(r":[a-z0-9_+-]+:")
GTX_URL = "https://translate.googleapis.com/translate_a/single"


def clean_desc(text):
    """去掉 GitHub 的 :emoji: 短代码和多余空白，作为翻译的原文。"""
    return re.sub(r"\s+", " ", SHORTCODE.sub("", text or "")).strip()


def is_chinese(text):
    return len(CJK.findall(text)) >= max(2, len(text) * 0.15)


def gtx(lines, to):
    """谷歌翻译的免费接口。用 curl 发请求：这个接口会拒绝 Python 自带 urllib 的请求（429）。"""
    cmd = ["curl", "-sS", "--fail", "-m", "30", "-G", GTX_URL, "-A", "Mozilla/5.0",
           "--data-urlencode", "client=gtx", "--data-urlencode", "sl=auto",
           "--data-urlencode", "tl=" + to, "--data-urlencode", "dt=t",
           "--data-urlencode", "q=" + "\n".join(lines)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "curl 失败").strip()[:200])
    data = json.loads(p.stdout)
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).split("\n")


def translate_db(db, cfg):
    """把简介翻成中文，结果存在 desc_zh 里；desc_zh_src 记下翻译时的原文，简介没变就不再翻。"""
    tc = cfg.get("translate", {})
    if not tc.get("enabled", True):
        return
    to, glossary = tc.get("to", "zh-CN"), tc.get("glossary", {})
    todo = []
    for name, r in db.items():
        src = clean_desc(r.get("desc", ""))
        if not src:
            r.pop("desc_zh", None), r.pop("desc_zh_src", None)
        elif r.get("desc_zh_src") == src and r.get("desc_zh"):
            continue
        elif is_chinese(src):
            r["desc_zh"], r["desc_zh_src"] = src, src
        else:
            todo.append((name, src))
    todo = todo[: tc.get("max_per_run", 3000)]
    if not todo:
        return
    if not shutil.which("curl"):
        log("没有 curl，跳过翻译。")
        return
    log(f"翻译 {len(todo)} 条简介…")

    def fix(text):
        text = text.strip()
        for wrong, right in glossary.items():
            text = text.replace(wrong, right)
        return text

    batches, cur, size = [], [], 0
    for item in todo:
        if cur and size + len(item[1]) + 1 > tc.get("batch_chars", 3000):
            batches.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += len(item[1]) + 1
    if cur:
        batches.append(cur)

    done, fails = 0, 0
    for batch in batches:
        try:
            out = gtx([src for _, src in batch], to)
            if len(out) != len(batch):  # 行数对不上就逐条翻，避免错位
                out = []
                for _, src in batch:
                    out.append(" ".join(gtx([src], to)))
                    time.sleep(0.3)
        except Exception as e:
            fails += 1
            log(f"  翻译失败：{e}")
            if fails >= 3:
                log("  翻译接口连续失败，剩下的下次运行再翻。")
                break
            time.sleep(3)
            continue
        for (name, src), zh in zip(batch, out):
            db[name]["desc_zh"], db[name]["desc_zh_src"] = fix(zh), src
        done += len(batch)
        time.sleep(1)
    log(f"  翻译完成 {done} 条。")


def desc_of(r):
    return r.get("desc_zh") or r.get("desc", "")


# ---------------------------------------------------------------- 输出

def fmt_stars(n):
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def md_escape(text):
    return text.replace("|", "\\|").replace("\n", " ")


def write_daily(db, cats, trending, new_names, first_run):
    os.makedirs(DAILY_DIR, exist_ok=True)
    cat_name = {c["id"]: c["name"] for c in cats}
    lines = [f"# 川星日报 {TODAY}", ""]
    if first_run:
        lines.append(f"> 首次运行，建立基线：共收录 {len(new_names)} 个项目。下面每个分类只列前 30 个，完整列表请看网页。")
    else:
        lines.append(f"> 今日新收录 {len(new_names)} 个项目，Trending 上榜 {len(trending)} 个。")
    lines.append("")

    if trending:
        lines += ["## 今日 Trending", "", "| 项目 | 分类 | 今日新增 | 本周新增 | Star | 简介 |", "|---|---|---|---|---|---|"]
        rows = sorted(trending.items(), key=lambda kv: -kv[1].get("daily", 0))
        for name, info in rows:
            r = db.get(name)
            if not r:
                continue
            lines.append(
                f"| [{name}]({r['url']}) | {cat_name.get(r['category'], '其他')} | "
                f"{('+' + str(info['daily'])) if info.get('daily') else '-'} | "
                f"{('+' + str(info['weekly'])) if info.get('weekly') else '-'} | "
                f"{fmt_stars(r['stars'])} | {md_escape(desc_of(r)[:80])} |"
            )
        lines.append("")

    lines += ["## 新收录（按分类）", ""]
    by_cat = {}
    for name in new_names:
        by_cat.setdefault(db[name]["category"], []).append(db[name])
    if not by_cat:
        lines += ["今天没有新收录的项目。", ""]
    for c in cats:
        items = sorted(by_cat.get(c["id"], []), key=lambda r: -r["stars"])
        if not items:
            continue
        lines += [f"### {c['name']}（{len(items)}）", ""]
        for r in items[:30]:
            lang = f" · {r['lang']}" if r["lang"] else ""
            lines.append(f"- [{r['name']}]({r['url']}) ⭐ {fmt_stars(r['stars'])}{lang} — {md_escape(desc_of(r)[:120])}")
        if len(items) > 30:
            lines.append(f"- ……还有 {len(items) - 30} 个，见网页")
        lines.append("")

    with open(os.path.join(DAILY_DIR, f"{TODAY}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    days = sorted((fn[:-3] for fn in os.listdir(DAILY_DIR) if re.match(r"\d{4}-\d{2}-\d{2}\.md$", fn)), reverse=True)
    index = ["# 川星日报归档", ""] + [f"- [{d}]({d}.md)" for d in days]
    with open(os.path.join(DAILY_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index) + "\n")


def write_site(db, cats, trending):
    os.makedirs(DOCS_DIR, exist_ok=True)
    counts = {}
    for r in db.values():
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    repos = []
    for r in sorted(db.values(), key=lambda r: -r["stars"]):
        t = trending.get(r["name"], {})
        repos.append({
            "n": r["name"], "u": r["url"], "d": r["desc"], "dz": r.get("desc_zh", ""), "s": r["stars"], "l": r["lang"],
            "tp": r["topics"][:8], "c": r["category"], "tg": r["tags"], "fs": r["first_seen"],
            "ls": r["last_seen"], "cr": r["created"], "td": t.get("daily", 0), "tw": t.get("weekly", 0),
            "g": r["stars"] - r.get("stars_first", r["stars"]),
        })
    payload = {
        "generated": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"),
        "today": TODAY,
        "categories": [{"id": c["id"], "name": c["name"], "count": counts.get(c["id"], 0)} for c in cats],
        "repos": repos,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(DOCS_DIR, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.CX_DATA=" + text + ";\n")


# ---------------------------------------------------------------- 主流程

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def main():
    translate_only = "--translate" in sys.argv
    reclassify_only = "--reclassify" in sys.argv or translate_only
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("找不到 categories.json")
    db = load_json(DB_PATH, {})
    first_run = not db
    rules = compile_rules(cfg)
    cats = [c for c in cfg["categories"]] + [OTHER]

    trending = {}
    new_names = []
    if not reclassify_only:
        if not TOKEN:
            log("提示：没有设置 GITHUB_TOKEN，会按未登录的限额慢慢跑。")
        http = Http()
        found, trending = collect(http, cfg, db)
        for name, slot in found.items():
            rec = slot["rec"]
            old = db.get(name)
            if old is None:
                rec.update(first_seen=TODAY, stars_first=rec["stars"], sources=sorted(slot["src"]))
                new_names.append(name)
            else:
                rec.update(first_seen=old["first_seen"], stars_first=old.get("stars_first", rec["stars"]),
                           sources=sorted(set(old.get("sources", [])) | slot["src"]))
                for k in ("desc_zh", "desc_zh_src"):  # 沿用已有的翻译，简介变了 translate_db 会重翻
                    if k in old:
                        rec[k] = old[k]
            rec["last_seen"] = TODAY
            db[name] = rec
        log(f"本次请求 {http.calls} 次，发现 {len(found)} 个项目，其中新收录 {len(new_names)} 个。")
        # 同一天重复运行时，把今天早些时候新收录的也算进日报
        new_names = sorted(set(new_names) | {n for n, r in db.items() if r.get("first_seen") == TODAY})
    else:
        new_names = [n for n, r in db.items() if r.get("first_seen") == TODAY]

    if translate_only or not reclassify_only:
        translate_db(db, cfg)

    for name, rec in db.items():
        rec["category"], rec["tags"] = classify(rec, set(rec.get("sources", [])), rules, cfg)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(db.items())), f, ensure_ascii=False, indent=1)

    # 重新分类模式下没有 trending 数据，就沿用今天日报里的；为简单起见只在联网时重写日报
    if not reclassify_only:
        write_daily(db, cats, trending, new_names, first_run)
        with open(os.path.join(ROOT, "data", "trending.json"), "w", encoding="utf-8") as f:
            json.dump({"date": TODAY, "repos": trending}, f, ensure_ascii=False, indent=1)
    else:
        saved = load_json(os.path.join(ROOT, "data", "trending.json"), {})
        if saved.get("date") == TODAY:
            trending = saved.get("repos", {})
        write_daily(db, cats, trending, new_names, first_run=False)
    write_site(db, cats, trending)

    by_cat = {}
    for r in db.values():
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    log("分类统计：" + "，".join(f"{c['name']} {by_cat.get(c['id'], 0)}" for c in cats))
    log(f"完成：daily/{TODAY}.md，docs/data.js")


if __name__ == "__main__":
    main()
