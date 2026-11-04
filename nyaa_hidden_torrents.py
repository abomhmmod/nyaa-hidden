#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nyaa_hidden_torrents.py
=======================
عارض التورنتات المخفية في nyaa.si — يفتح صفحة ويب أنيقة تشبه nyaa وتعرض
"المخفية فقط".  A pretty, nyaa-like web view of HIDDEN torrents only.

ماذا يفعل (بكل بساطة):
  1) يتصل بـ nyaa.si ويجلب أحدث المعرّفات.
  2) يبحث في "الفجوات" (المعرّفات غير الظاهرة في القوائم) عن التورنتات المخفية.
  3) يحفظ ما يجده في أرشيف محلي — حتى لا تضيع النتائج القديمة (ترجع للوراء).
  4) يولّد صفحة HTML جميلة (الأحدث أولًا + بحث + صفحات) ويفتحها في Chrome.

شغّله بنقرة مزدوجة على اختصار «التورنتات المخفية»، وكل مرة يعرض آخر المخفية.

ما هو التورنت "المخفي"؟
  على nyaa يستطيع الرافع إخفاء التورنت (Hidden): فلا يظهر في الصفحة الرئيسية
  ولا في البحث ولا في RSS، لكن صفحته المباشرة /view/{id} تعمل للجميع. ومعرّفات
  nyaa أرقام متسلسلة، فنكتشف المخفي عبر فحص الأرقام المفقودة من القوائم:
    * 404            -> محذوف/غير موجود (نتجاهله)
    * 200 + رأس رمادي -> مخفي بثقة عالية (الرأس الرمادي هو علامة nyaa للإخفاء)
    * 200 بلا رمادي   -> موجود لكن غير مؤكد (قد يكون رفعًا جديدًا/مجهولًا)

الاستخدام المهذّب: المهلة الافتراضية بين الطلبات تحترم الموقع، مع تراجع تلقائي
عند الحظر (429)، وإيقاف تلقائي عند تتابع الحظر. الأداة لا تُنزّل أي ملف؛ تقرأ
صفحات العرض فقط وتعرض لك رابط المغناطيس.
"""

import argparse
import gzip
import html as html_module
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import webbrowser
import zlib
from http import cookiejar
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# --------------------------------------------------------------------------- #
# إعدادات / Settings
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL = "https://nyaa.si"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# كل شيء بجانب السكربت في نفس المجلد -> محمول، يُنقل كاملًا دون بقايا.
DATA_DIR = SCRIPT_DIR
ARCHIVE_PATH = os.path.join(DATA_DIR, "archive.json")
HTML_PATH = os.path.join(DATA_DIR, "التورنتات المخفية.html")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")   # إعدادات الإشعارات (اختياري)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

ROWS_PER_PAGE = 75            # nyaa shows 75 rows per listing page
LISTING_PAGE_CAP = 100        # nyaa blocks listing pages beyond ~100 (anti-scraping)
MAX_CONSECUTIVE_BLOCKS = 5    # fail-closed after this many blocks in a row
MAX_RANGE_SPAN = 5000         # large explicit ranges need --yes
DISPLAY_PER_PAGE = 30         # hidden torrents shown per page in the HTML
ARCHIVE_CAP = 500             # keep at most this many (newest) hidden torrents
CHECKPOINT_SECONDS = 60       # save progress this often while scanning ids

# جلسة مشتركة بكوكيز (لتجاوز حاجز DDoS-Guard). Shared cookie session.
OPENER = urlrequest.build_opener(
    urlrequest.HTTPCookieProcessor(cookiejar.CookieJar()))


# --------------------------------------------------------------------------- #
# أدوات الإخراج / console helpers
# --------------------------------------------------------------------------- #
def _setup_console():
    """اجعل الطرفية تطبع UTF-8 حتى لا تنهار على العناوين العربية/اليابانية."""
    for name in ("stdout", "stderr"):
        st = getattr(sys, name, None)
        rc = getattr(st, "reconfigure", None)
        if callable(rc):
            try:
                rc(encoding="utf-8", errors="replace")
            except Exception:
                pass


def info(msg):
    print(msg, flush=True)


def warn(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# طبقة الشبكة / HTTP layer
# --------------------------------------------------------------------------- #
def _decode(raw, headers):
    enc = (headers.get("Content-Encoding") or "").lower()
    data = raw
    if "gzip" in enc:
        try:
            data = gzip.decompress(raw)
        except Exception:
            try:
                data = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            except Exception:
                data = raw
    elif "deflate" in enc:
        try:
            data = zlib.decompress(raw)
        except Exception:
            try:
                data = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                data = raw
    return data.decode("utf-8", errors="replace")


def http_get(url, timeout, retries, delay, verbose=False):
    """
    GET مهذّب: 404 تُرجَع كحالة (مفيدة)، 429/5xx/403 تُعاد محاولتها بتراجع أُسّي
    ثم تُرجَع حالتها كي يعمل قاطع الحظر. يُرجِع (status, body).
    """
    attempt = 0
    last = None
    while attempt <= retries:
        attempt += 1
        req = urlrequest.Request(url, headers=HEADERS, method="GET")
        try:
            with OPENER.open(req, timeout=timeout) as resp:
                body = _decode(resp.read(), resp.headers)
                status = getattr(resp, "status", None) or resp.getcode() or 200
                return status, body
        except urlerror.HTTPError as e:
            status = e.code
            if status == 404:
                return 404, ""
            if status == 429 or status == 403 or 500 <= status <= 599:
                last = "HTTP %s" % status
                if attempt > retries:
                    return status, ""
                ra = None
                try:
                    ra = e.headers.get("Retry-After")
                    ra = float(ra) if ra and ra.isdigit() else None
                except Exception:
                    ra = None
                _backoff(attempt, delay, ra, status, verbose)
                continue
            return status, ""
        except (urlerror.URLError, TimeoutError, OSError) as e:
            last = repr(e)
            if attempt > retries:
                break
            _backoff(attempt, delay, None, None, verbose)
            continue
    raise RuntimeError("Could not connect to %s (%s)" % (url, last))


def _backoff(attempt, base, retry_after, status, verbose):
    if retry_after is not None and retry_after >= 0:
        wait = min(300.0, retry_after + 1.0)
    else:
        wait = min(60.0, max(base, 2.0) * (2 ** (attempt - 1))) + random.uniform(0, 1.0)
    if verbose:
        tag = ("HTTP %s" % status) if status else "network error"
        warn("    [backoff] %s -> waiting %.1f seconds" % (tag, wait))
    time.sleep(wait)


def pause(delay):
    if delay > 0:
        time.sleep(delay + random.uniform(0, min(0.4, delay * 0.25)))


def warm_up(base, timeout, verbose):
    """طلب الصفحة الرئيسية أولًا لالتقاط كوكيز الجلسة (DDoS-Guard)."""
    try:
        http_get(base + "/", timeout, 2, 1.0, verbose)
        if verbose:
            warn("[init] Session and site cookies are ready.")
    except RuntimeError as e:
        if verbose:
            warn("[init] warning: %s" % e)


# --------------------------------------------------------------------------- #
# تجميع المعرّفات الظاهرة / collect visible ids
# --------------------------------------------------------------------------- #
_RE_VIEW_ID = re.compile(r'href="/view/(\d+)"')
_RE_RSS_ID = re.compile(r"/view/(\d+)")


def collect_visible(base, lo, hi, timeout, retries, delay, verbose):
    """
    صفحات القوائم (الأحدث أولًا) لجمع المعرّفات الظاهرة داخل [lo, hi].
    يُرجِع (set_in_range, max_id, covered_min).
    """
    in_range, max_id, covered_min, page = set(), None, None, 0
    while page < LISTING_PAGE_CAP:
        page += 1
        url = base + ("/?p=%d" % page)
        if verbose:
            warn("[list] page %d" % page)
        try:
            status, body = http_get(url, timeout, min(retries, 1), delay, verbose)
        except RuntimeError as e:
            warn("[list] stopped: %s" % e)
            break
        if status == 403:
            if verbose:
                warn("[list] nyaa blocks browsing past page %d (site limit)."
                     % (page - 1))
            break
        ids = [int(x) for x in _RE_VIEW_ID.findall(body)]
        if not ids:
            break
        pmax, pmin = max(ids), min(ids)
        if max_id is None or pmax > max_id:
            max_id = pmax
        if covered_min is None or pmin < covered_min:
            covered_min = pmin
        for i in ids:
            if lo <= i <= hi:
                in_range.add(i)
        pause(delay)
        if pmin <= lo:          # غطّينا الحد الأدنى للنطاق
            break
        if pmax < lo:           # تجاوزنا النطاق إلى الأقدم
            break
    return in_range, max_id, covered_min


def rss_max_id(base, timeout, retries, delay, verbose):
    """أكبر معرّف حالي من RSS (أخف نقطة وصول)."""
    try:
        _, body = http_get(base + "/?page=rss", timeout, retries, delay, verbose)
    except RuntimeError:
        return None, set()
    ids = {int(m) for m in _RE_RSS_ID.findall(body)}
    return (max(ids) if ids else None), ids


# --------------------------------------------------------------------------- #
# تحليل صفحة العرض / parse a /view page
# --------------------------------------------------------------------------- #
_RE_TITLE = re.compile(r'<h3[^>]*class="panel-title"[^>]*>\s*(.*?)\s*</h3>', re.S | re.I)
_RE_CATEGORY = re.compile(r'Category:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_CATEGORY_ID = re.compile(r'href="/\?c=(\d+_\d+)"')
_RE_DATE_TS = re.compile(r'Date:\s*</div>\s*<div[^>]*data-timestamp="(\d+)"', re.S | re.I)
_RE_SUBMITTER = re.compile(r'Submitter:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_SEEDERS = re.compile(r'Seeders:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_LEECHERS = re.compile(r'Leechers:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_COMPLETED = re.compile(r'Completed:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_FILESIZE = re.compile(r'File size:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_KBD_HASH = re.compile(r'<kbd>\s*([0-9a-fA-F]{40})\s*</kbd>')
_RE_MAGNET = re.compile(r'href="(magnet:\?xt=urn:btih:[0-9a-fA-F]{40}[^"]*)"', re.I)
_RE_MAGNET_HASH = re.compile(r'urn:btih:([0-9a-fA-F]{40})', re.I)
_RE_PANEL_TITLE = re.compile(r'class="panel-title"')
_RE_HIDDEN_CUE = re.compile(
    r'class="panel-heading"[^>]*style="[^"]*background-color:\s*darkgray', re.I)
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_INFORMATION = re.compile(r'Information:\s*</div>\s*<div[^>]*>(.*?)</div>', re.S | re.I)
_RE_HREF = re.compile(r'href="([^"]+)"', re.I)
_RE_DESCRIPTION = re.compile(r'id="torrent-description"[^>]*>(.*?)</div>', re.S | re.I)
_RE_FILE = re.compile(r'<i class="fa fa-file"></i>(.*?)\s*<span class="file-size">\(([^)]*)\)</span>',
                      re.S | re.I)


def _text(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", html_module.unescape(_RE_TAGS.sub("", s))).strip()


def _clean_markdown(raw):
    """وصف nyaa مكتوب بـ Markdown؛ نحوّله إلى نصّ مقروء بلا رموز."""
    s = html_module.unescape(raw or "")
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)                       # صور
    s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)",
               lambda m: m.group(1).strip() or m.group(2).strip(), s)  # روابط -> نصّها
    s = _RE_TAGS.sub("", s)
    s = re.sub(r"^\s*#{1,6}\s*", "", s, flags=re.M)                   # عناوين
    s = re.sub(r"^\s*(?:[-*_]\s*){3,}$", "", s, flags=re.M)           # خطوط فاصلة
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)                        # اقتباس
    s = re.sub(r"\*\*|__|~~|`", "", s)
    s = re.sub(r"(?<!\w)\*(?!\s)|(?<!\s)\*(?!\w)", "", s)
    lines, blank = [], False
    for ln in s.splitlines():
        ln = ln.strip()
        if not ln:
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        lines.append(ln)
        blank = False
    return "\n".join(lines).strip()


def is_real_page(status, body):
    return status == 200 and bool(_RE_PANEL_TITLE.search(body or ""))


def parse_view(tid, body, base):
    info = {
        "id": tid,
        "view_url": "%s/view/%d" % (base, tid),
        "download_url": "%s/download/%d.torrent" % (base, tid),
        "title": "", "category": "", "category_id": "",
        "submitter": "Anonymous", "ts": 0, "date": "",
        "size": "", "seeders": "0", "leechers": "0", "completed": "0",
        "info_hash": "", "magnet": "",
        "hidden_cue": False, "status": "exists_unconfirmed",
    }
    m = _RE_TITLE.search(body)
    if m:
        info["title"] = _text(m.group(1))
    m = _RE_CATEGORY.search(body)
    if m:
        info["category"] = _text(m.group(1))
    m = _RE_CATEGORY_ID.search(body)
    if m:
        info["category_id"] = m.group(1)
    m = _RE_DATE_TS.search(body)
    if m:
        try:
            info["ts"] = int(m.group(1))
            info["date"] = time.strftime("%Y-%m-%d %H:%M",
                                         time.localtime(info["ts"]))
        except Exception:
            pass
    m = _RE_SUBMITTER.search(body)
    if m and _text(m.group(1)):
        info["submitter"] = _text(m.group(1))
    m = _RE_SEEDERS.search(body)
    if m:
        info["seeders"] = _text(m.group(1)) or "0"
    m = _RE_LEECHERS.search(body)
    if m:
        info["leechers"] = _text(m.group(1)) or "0"
    m = _RE_COMPLETED.search(body)
    if m:
        info["completed"] = _text(m.group(1)) or "0"
    m = _RE_FILESIZE.search(body)
    if m:
        info["size"] = _text(m.group(1))
    m = _RE_KBD_HASH.search(body)
    if m:
        info["info_hash"] = m.group(1).lower()
    info["magnet"] = _pick_magnet(body, info["info_hash"])
    if info["magnet"] and not info["info_hash"]:
        mh = _RE_MAGNET_HASH.search(info["magnet"])
        if mh:
            info["info_hash"] = mh.group(1).lower()
    m = _RE_INFORMATION.search(body)
    if m:
        h = _RE_HREF.search(m.group(1))
        val = html_module.unescape(h.group(1)) if h else _text(m.group(1))
        if val and val.lower() != "no information.":
            info["information"] = val
    m = _RE_DESCRIPTION.search(body)
    if m:
        desc = _clean_markdown(m.group(1))
        if desc and desc.lower() != "no description.":
            info["description"] = desc[:1500]
    files = [(_text(a), _text(b)) for a, b in _RE_FILE.findall(body)]
    if files:
        info["file_count"] = len(files)
        info["files"] = [list(f) for f in files[:10]]
    info["hidden_cue"] = bool(_RE_HIDDEN_CUE.search(body))
    info["status"] = "hidden" if info["hidden_cue"] else "exists_unconfirmed"
    return info


def _pick_magnet(body, info_hash):
    mags = [html_module.unescape(m) for m in _RE_MAGNET.findall(body)]
    if not mags:
        return ""
    if info_hash:
        for mag in mags:
            mh = _RE_MAGNET_HASH.search(mag)
            if mh and mh.group(1).lower() == info_hash:
                return mag
    return mags[0]


# --------------------------------------------------------------------------- #
# الأرشيف / archive (JSON)
# --------------------------------------------------------------------------- #
def load_archive():
    try:
        with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def save_archive(items):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- #
# الإشعارات / notifications (Discord webhook + Telegram bot) — اختياري
# --------------------------------------------------------------------------- #
def load_config():
    # utf-8-sig يتحمّل وجود BOM (كما قد يحفظه Notepad) أو غيابه.
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


_CAT_MAIN_AR = {
    "anime": "أنمي", "audio": "صوتيات", "literature": "كتب",
    "live action": "تمثيل حيّ", "pictures": "صور", "software": "برامج",
}
_CAT_SUB_AR = {
    "anime music video": "مقاطع موسيقية (AMV)",
    "english-translated": "مترجم إلى الإنجليزية",
    "non-english-translated": "مترجم إلى لغة غير الإنجليزية",
    "raw": "خام (Raw)",
    "lossless": "بلا فقد (Lossless)", "lossy": "مضغوط (Lossy)",
    "idol/promotional video": "آيدول ومقاطع ترويجية",
    "graphics": "رسوم", "photos": "صور فوتوغرافية",
    "applications": "تطبيقات", "games": "ألعاب",
}


def _category_ar(cat):
    cat = (cat or "").strip()
    if not cat:
        return "—"
    main, _, sub = cat.partition(" - ")
    m = _CAT_MAIN_AR.get(main.strip().lower())
    sb = _CAT_SUB_AR.get(sub.strip().lower()) if sub else None
    if m and (sb or not sub):
        return m + ((" — " + sb) if sb else "")
    return cat


def _h(s):
    return html_module.escape(str(s if s is not None else ""), quote=False)


def _short_magnet(t):
    ih = (t.get("info_hash") or "").strip()
    if not ih:
        return ""
    return "magnet:?xt=urn:btih:%s&tr=http://nyaa.tracker.wf:7777/announce" % ih


def _notify_message(t):
    """رسالة تيلغرام منسّقة (HTML) لتورنت واحد."""
    title = t.get("title") or ("#" + str(t.get("id")))
    sub = (t.get("submitter") or "").strip()
    head = ["🆕 <b>تورنت مخفيّ جديد على Nyaa</b>", "", "<b>%s</b>" % _h(title), ""]
    head.append("📁 التصنيف: %s" % _h(_category_ar(t.get("category"))))
    head.append("👤 الرافع: %s" % (_h(sub) if sub and sub != "Anonymous" else "مجهول"))
    line = "📦 الحجم: %s" % _h(t.get("size") or "—")
    if t.get("file_count"):
        line += "   •   🗂 الملفّات: %d" % int(t["file_count"])
    head.append(line)
    if t.get("date"):
        head.append("📅 تاريخ الرفع: %s" % _h(t["date"]))
    head.append("🌱 البذور: %s   •   🔻 المحمّلون: %s   •   ✅ اكتمل: %s"
                % (_h(t.get("seeders") or "0"), _h(t.get("leechers") or "0"),
                   _h(t.get("completed") or "0")))
    inf = (t.get("information") or "").strip()
    if inf.startswith(("http://", "https://")):
        shown = re.sub(r"^https?://(www\.)?", "", inf).rstrip("/")
        head.append('ℹ️ معلومات: <a href="%s">%s</a>'
                    % (html_module.escape(inf, quote=True), _h(shown[:60])))
    elif inf:
        head.append("ℹ️ معلومات: %s" % _h(inf[:120]))

    files = t.get("files") or []
    if files:
        head += ["", "📄 <b>الملفّات:</b>"]
        for f in files[:5]:
            name, size = (f + ["", ""])[:2] if isinstance(f, list) else (str(f), "")
            head.append("• %s%s" % (_h(name[:120]), ("  (%s)" % _h(size)) if size else ""))
        extra = int(t.get("file_count") or len(files)) - min(5, len(files))
        if extra > 0:
            head.append("… و%d أخرى (في صفحة التورنت)" % extra)

    tail = []
    mag = _short_magnet(t)
    if mag:
        tail += ["", "🧲 رابط المغناطيس (اضغط عليه لنسخه):", "<code>%s</code>" % _h(mag)]

    desc = (t.get("description") or "").strip()
    body = "\n".join(head)
    rest = "\n".join(tail)
    if desc:
        room = 3800 - len(body) - len(rest) - 80
        if room > 120:
            d = desc if len(desc) <= min(room, 700) else desc[:min(room, 700)].rstrip() + " …"
            body += "\n\n📝 <b>الوصف:</b>\n<blockquote expandable>%s</blockquote>" % _h(d)
    return body + ("\n" + rest if rest else "")


def _telegram_buttons(t):
    row = []
    if t.get("view_url"):
        row.append({"text": "🔗 صفحة التورنت", "url": t["view_url"]})
    if t.get("download_url"):
        row.append({"text": "⬇️ ملف التورنت", "url": t["download_url"]})
    return {"inline_keyboard": [row]} if row else None


def _discord_embed(t):
    """بطاقة Discord أنيقة (Embed) لتورنت واحد."""
    # روابط قصيرة فقط (بلا سلسلة المغناطيس الطويلة): أيقونة مغناطيس قابلة للنقر
    # تفتح التورنت مباشرة في البرنامج، ورابط لتحميل ملف .torrent.
    desc = []
    if t.get("view_url"):
        desc.append("🧲 [مغناطيس](%s/magnet)" % t["view_url"])
    if t.get("download_url"):
        desc.append("⬇️ [‎.torrent](%s)" % t["download_url"])
    embed = {
        "title": (t.get("title") or ("#" + str(t.get("id"))))[:250],
        "color": 0x2F6F4F,
        "fields": [
            {"name": "📁 التصنيف",
             "value": (t.get("category") or "-")[:200], "inline": True},
            {"name": "📦 الحجم",
             "value": (t.get("size") or "-")[:60], "inline": True},
            {"name": "📅 التاريخ",
             "value": (t.get("date") or "-")[:60], "inline": True},
            {"name": "🌱 بذور / 🔻 محمّلون",
             "value": "%s / %s" % (t.get("seeders") or "0",
                                   t.get("leechers") or "0"), "inline": True},
        ],
        "footer": {"text": "Nyaa • #%s" % t.get("id")},
    }
    if t.get("view_url"):
        embed["url"] = t["view_url"]
    if desc:
        embed["description"] = "  ·  ".join(desc)
    return embed


def _post_discord(webhook, content=None, embeds=None):
    payload = {}
    if content:
        payload["content"] = content[:1900]
    if embeds:
        payload["embeds"] = embeds[:10]  # Discord: حتى 10 بطاقات لكل رسالة.
    body = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST")
    with urlrequest.urlopen(req, timeout=20) as r:
        r.read()


def _send_telegram(token, chat_id, text, html=False, buttons=None):
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    fields = {"chat_id": chat_id, "text": text[:4000],
              "disable_web_page_preview": "true"}
    if html:
        fields["parse_mode"] = "HTML"
    if buttons:
        fields["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
    req = urlrequest.Request(url, data=urlparse.urlencode(fields).encode("utf-8"),
                             headers={"User-Agent": USER_AGENT}, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=20) as r:
            r.read()
    except urlerror.HTTPError as e:
        if not (html and e.code == 400):
            raise
        # احتياط: إن رفض تيلغرام التنسيق أُرسلت الرسالة نصًّا عاديًّا.
        plain = html_module.unescape(_RE_TAGS.sub("", text))
        _send_telegram(token, chat_id, plain, False, buttons)


def _send_all(cfg, text, verbose=False):
    """يرسل نصًا لكل القنوات المُعدّة؛ يُرجِع True إن نجحت قناة واحدة على الأقل."""
    ok = False
    wh = (cfg.get("discord_webhook") or "").strip()
    tok = (cfg.get("telegram_token") or "").strip()
    chat = str(cfg.get("telegram_chat_id") or "").strip()
    if wh:
        try:
            _post_discord(wh, text)
            ok = True
        except Exception as e:
            if verbose:
                warn("  [discord] send failed: %r" % e)
    if tok and chat:
        try:
            _send_telegram(tok, chat, text)
            ok = True
        except Exception as e:
            if verbose:
                warn("  [telegram] send failed: %r" % e)
    return ok


def send_torrents(items, header=None, verbose=False):
    """يرسل قائمة تورنتات منسّقة: بطاقات Embeds لديسكورد (5 لكل رسالة لاحترام
    حدود ديسكورد) ونصّ لتيلغرام. يُرجِع عدد التورنتات المُرسلة (0 إن لا قناة)."""
    cfg = load_config()
    wh = (cfg.get("discord_webhook") or "").strip()
    tok = (cfg.get("telegram_token") or "").strip()
    chat = str(cfg.get("telegram_chat_id") or "").strip()
    if not items or (not wh and not (tok and chat)):
        return 0
    if wh:
        first = True
        for i in range(0, len(items), 10):
            chunk = items[i:i + 10]
            embeds = [_discord_embed(t) for t in chunk]
            try:
                _post_discord(wh, content=(header if first else None),
                              embeds=embeds)
            except Exception as e:
                if verbose:
                    warn("  [discord] send failed: %r" % e)
            first = False
            time.sleep(0.7)
    if tok and chat:
        if header and len(items) > 1:
            try:
                _send_telegram(tok, chat, re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>",
                                                 _h(header)), True)
            except Exception:
                pass
        for t in items:
            try:
                _send_telegram(tok, chat, _notify_message(t), True, _telegram_buttons(t))
            except Exception as e:
                if verbose:
                    warn("  [telegram] send failed: %r" % e)
            time.sleep(0.4)
    return len(items)


def notify_new(new_items, verbose=False):
    """يرسل إشعارًا أنيقًا بكل تورنت مخفي جديد إن كانت الإشعارات مُعدّة."""
    if not new_items:
        return
    items = sorted(new_items, key=lambda x: int(x["id"]), reverse=True)
    capped = items[:15]
    header = "🆕 **تورنتات مخفية جديدة على Nyaa** — %d" % len(capped)
    n = send_torrents(capped, header, verbose)
    if n and len(items) > 15:
        cfg = load_config()
        _send_all(cfg, "➕ و %d تورنت مخفي جديد إضافي — افتح القائمة."
                  % (len(items) - 15), verbose)
    if n:
        info("Sent %d notification(s) about new items." % n)


def merge_archive(old, found, max_keep=ARCHIVE_CAP):
    by_id = {int(t["id"]): t for t in old}
    new_count = 0
    now = int(time.time())
    for t in found:
        tid = int(t["id"])
        if tid in by_id:
            t.setdefault("first_seen", by_id[tid].get("first_seen", now))
        else:
            t["first_seen"] = now
            new_count += 1
        by_id[tid] = t
    merged = sorted(by_id.values(), key=lambda x: int(x["id"]), reverse=True)
    if max_keep and len(merged) > max_keep:
        merged = merged[:max_keep]      # نحتفظ بأحدث max_keep تورنت مخفي
    return merged, new_count


# --------------------------------------------------------------------------- #
# فحص المعرّفات المفقودة / probe candidates
# --------------------------------------------------------------------------- #
def probe_candidates(base, candidates, args, checkpoint=None):
    """يفحص قائمة المعرّفات ويُرجِع (found, deleted, blocks, processed).
    processed = عدد المعرّفات التي اكتمل فحصها (بالترتيب).
    checkpoint(found_batch, failed_batch, lowest_done): يُستدعى كل دقيقة وفي النهاية
    ليحفظ التقدّم أولًا بأول، فلا يضيع شيء إن أُغلق البرنامج أو انتهى وقته.
    --max-minutes يحدّد مدة الفحص؛ عند انتهائها يتوقف ويُكمل في المرة التالية."""
    v = args.verbose
    found, deleted, blocks, consec, hidden_n = [], 0, 0, 0, 0
    n = len(candidates)
    processed = 0
    budget = getattr(args, "max_minutes", 0) or 0
    deadline = (time.time() + budget * 60) if budget > 0 else None
    last_cp = time.time()
    pend_found, pend_failed = [], []

    def flush():
        nonlocal last_cp, pend_found, pend_failed
        if checkpoint:
            checkpoint(pend_found, pend_failed,
                       candidates[processed - 1] if processed else None)
        pend_found, pend_failed = [], []
        last_cp = time.time()

    try:
        for i, tid in enumerate(candidates, 1):
            if deadline is not None and time.time() >= deadline:
                warn("\n[time limit] %d of %d ids left — the next run continues from here."
                     % (n - processed, n))
                break
            if not v:
                print("\r  Scanning: %d/%d   (hidden: %d, deleted: %d)   "
                      % (i, n, hidden_n, deleted), end="", flush=True)
            stop = False
            try:
                status, body = http_get("%s/view/%d" % (base, tid),
                                        args.timeout, args.retries, args.delay, v)
            except RuntimeError as e:
                status, body = None, ""
                if v:
                    warn("  [error] %s" % e)

            if status is None or status in (403, 429) or 500 <= status <= 599:
                # لم يُفحص فعلًا (حظر/خطأ شبكة): يُعاد إلى قائمة الانتظار.
                blocks += 1
                consec += 1
                pend_failed.append(tid)
                if consec >= MAX_CONSECUTIVE_BLOCKS:
                    warn("\n[stopped] Consecutive blocks (%s) — stopped to protect the site "
                         "and your account." % ("HTTP %s" % status if status else "network"))
                    stop = True
            elif status == 404:
                deleted += 1
                consec = 0
            elif is_real_page(status, body):
                consec = 0
                rec = parse_view(tid, body, base)
                if rec["status"] == "hidden":
                    hidden_n += 1
                if rec["status"] == "hidden" or args.include_unconfirmed:
                    found.append(rec)
                    pend_found.append(rec)
                    if v:
                        warn("  ✓ %s [%d] %s"
                             % ("HIDDEN" if rec["status"] == "hidden"
                                else "unconfirmed", tid, rec["title"][:60]))
            else:
                consec = 0
            processed = i
            if stop:
                break
            if checkpoint and time.time() - last_cp >= CHECKPOINT_SECONDS:
                flush()
            pause(args.delay)
    except KeyboardInterrupt:
        warn("\n[interrupted] Whatever has been found so far will be saved.")
    if not v:
        print()
    if checkpoint:
        flush()
    return found, deleted, blocks, processed


# --------------------------------------------------------------------------- #
# فحص كل تصنيف على حدة / per-category deep scan
# --------------------------------------------------------------------------- #
# التصنيفات الورقية (مرتّبة بالأكبر أولًا حتى يُضبط العمق مبكرًا).
LEAF_CATS = ["1_2", "1_3", "1_4", "1_1",
             "4_1", "4_3", "4_4", "4_2",
             "2_1", "2_2", "3_1", "3_2", "3_3",
             "5_1", "5_2", "6_1", "6_2"]


def page_category(base, cat, hi, stop_lo, args, visible):
    """صفحات تصنيف واحد؛ يضيف المعرّفات الظاهرة (<=hi) إلى visible ويُرجِع أصغر معرّف."""
    v = args.verbose
    cmin = None
    p = 0
    while p < LISTING_PAGE_CAP:
        p += 1
        url = base + ("/?c=%s&p=%d" % (cat, p))
        try:
            status, body = http_get(url, args.timeout, min(args.retries, 1),
                                    args.delay, v)
        except RuntimeError:
            break
        if status != 200:
            break  # 404 = تجاوزنا آخر صفحة لهذا التصنيف، 403 = حدّ الموقع
        ids = [int(x) for x in _RE_VIEW_ID.findall(body)]
        if not ids:
            break
        for i in ids:
            if i <= hi:
                visible.add(i)
        pmin = min(ids)
        if cmin is None or pmin < cmin:
            cmin = pmin
        pause(args.delay)
        if stop_lo is not None and pmin <= stop_lo:
            break
    return cmin


def scan_by_category(args):
    """يبني خريطة الظاهرة من كل تصنيف على حدة للوصول لعمق أكبر، ثم يفحص الفجوات."""
    base = args.base_url.rstrip("/")
    v = args.verbose
    warm_up(base, args.timeout, v)
    info("Fetching the latest id ...")
    max_id, rss_ids = rss_max_id(base, args.timeout, args.retries, args.delay, v)
    if max_id is None:
        raise RuntimeError("Could not determine the latest id — check your connection.")
    hi = max_id
    visible = set(i for i in rss_ids if i <= hi)
    covered = []
    target_lo = None
    info("Scanning %d categories to build the visible-id map (may take several minutes)..."
         % len(LEAF_CATS))
    for idx, cat in enumerate(LEAF_CATS, 1):
        info("  [%d/%d] category %s ..." % (idx, len(LEAF_CATS), cat))
        cmin = page_category(base, cat, hi, target_lo, args, visible)
        if cmin is not None:
            covered.append(cmin)
            target_lo = max(covered)
    if not covered:
        raise RuntimeError("Could not build the category map (the site may be blocking temporarily).")
    lo = max(covered)
    candidates = [i for i in range(lo, hi + 1) if i not in visible]
    info("Covered range [%d .. %d] (depth %d) — visible: %d — missing to scan: %d"
         % (lo, hi, hi - lo, len(visible), len(candidates)))
    found, deleted, blocks, _ = probe_candidates(base, candidates, args)
    stats = {"probed": len(candidates), "deleted": deleted, "blocks": blocks,
             "found": len(found), "max_id": max_id, "lo": lo, "hi": hi}
    return found, stats


# --------------------------------------------------------------------------- #
# بحث أعمق بالمعرّفات / resumable ID backfill (FartCloud's idea)
# --------------------------------------------------------------------------- #
def scan_backfill(args):
    """يفحص معرّفات أقدم واحدًا واحدًا (تحت منطقة القوائم) ويُكمل من حيث وقف.
    لا حدّ للعمق لأن /view/{id} غير محدود بالصفحات؛ المخفي نادر (~0.6%)."""
    base = args.base_url.rstrip("/")
    v = args.verbose
    warm_up(base, args.timeout, v)
    info("Fetching the latest id ...")
    max_id, _ = rss_max_id(base, args.timeout, args.retries, args.delay, v)

    st = load_state()
    frontier = st.get("frontier")
    if frontier is None:
        ids = [int(t["id"]) for t in load_archive()]
        frontier = min(ids) if ids else (max_id or 1)
        info("No saved checkpoint — starting from the bottom of what we collected: %d" % frontier)

    chunk = max(1, args.backfill)
    hi_b = frontier - 1
    lo_b = max(1, frontier - chunk)
    if hi_b < 1:
        info("Reached the beginning of nyaa — nothing older than this.")
        return [], {"probed": 0, "deleted": 0, "blocks": 0, "found": 0,
                    "max_id": max_id, "lo": frontier, "hi": frontier}

    candidates = list(range(hi_b, lo_b - 1, -1))   # تنازليًا: الأحدث أولًا
    info("Deep id backfill: from %d down to %d (%d ids). "
         "Hidden torrents are rare, so only a few may show per batch." % (hi_b, lo_b, len(candidates)))
    found, deleted, blocks, processed = probe_candidates(base, candidates, args)
    lowest = candidates[processed - 1] if processed > 0 else frontier
    stats = {"probed": processed, "deleted": deleted, "blocks": blocks,
             "found": len(found), "max_id": max_id, "lo": lowest, "hi": hi_b}
    return found, stats


# --------------------------------------------------------------------------- #
# قائمة الانتظار / pending id ranges (resumable scanning)
# --------------------------------------------------------------------------- #
# state.json يحفظ "gaps": نطاقات معرّفات لم تُفحص بعد. كل تشغيل يضيف إليها الجديد
# منذ آخر مرة، ثم يفحصها من الأحدث إلى الأقدم ويحذف منها ما أنهاه كل دقيقة؛ فإن
# توقف (انتهاء الوقت، إطفاء الجهاز، حظر) أكمل التشغيل التالي من حيث وقف.
def _norm_gaps(gaps):
    rows = []
    for r in gaps or []:
        try:
            a, b = int(r[0]), int(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if a > b:
            a, b = b, a
        if b >= 1:
            rows.append([max(1, a), b])
    rows.sort()
    out = []
    for a, b in rows:
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _clip_gaps_below(gaps, x):
    """يُبقي من النطاقات ما هو أصغر من x فقط."""
    return [[a, min(b, x - 1)] for a, b in gaps if a <= x - 1]


def _gap_size(gaps):
    return sum(b - a + 1 for a, b in gaps)


def _scan_pending(args, base, max_id, rss_ids):
    v = args.verbose
    hi = max_id
    st = load_state()
    last_top = st.get("top")
    gaps = _norm_gaps(st.get("gaps"))
    new_lo = (last_top + 1) if last_top is not None else max(1, hi - args.window + 1)
    if new_lo <= hi:
        gaps = _norm_gaps(gaps + [[new_lo, hi]])
    # سجّل العمل المطلوب قبل البدء، حتى لا يضيع إن توقف الفحص في منتصفه.
    st["top"] = max(hi, last_top or 0)
    st["gaps"] = gaps
    if gaps:
        st["frontier"] = (gaps[0][0] if st.get("frontier") is None
                          else min(st["frontier"], gaps[0][0]))
    save_state(st)

    stats = {"probed": 0, "deleted": 0, "blocks": 0, "found": 0,
             "max_id": max_id, "lo": gaps[0][0] if gaps else None, "hi": hi,
             "committed": True, "new": 0}
    if not gaps:
        info("Latest id: %s — nothing new since the last scan." % max_id)
        return [], stats
    info("Latest id: %s — ids waiting to be scanned: %d"
         % (max_id, _gap_size(gaps)))

    # القوائم تغطي أحدث ~7500 معرّف فقط؛ تُجلب إن كان في الانتظار شيء ضمنها.
    list_lo = max(gaps[0][0], hi - LISTING_PAGE_CAP * ROWS_PER_PAGE)
    visible = set()
    if gaps[-1][1] >= list_lo:
        visible, _, _ = collect_visible(base, list_lo, hi, args.timeout,
                                        args.retries, args.delay, v)
    visible |= set(rss_ids)
    candidates = [i for a, b in reversed(gaps) for i in range(b, a - 1, -1)
                  if i not in visible]
    info("Missing ids to scan: %d (newest first; progress is saved every minute)"
         % len(candidates))

    hold = {"low": None, "failed": []}

    def current_gaps():
        base_g = gaps if hold["low"] is None else _clip_gaps_below(gaps, hold["low"])
        return _norm_gaps(base_g + [[f, f] for f in hold["failed"]])

    def save_gaps():
        s2 = load_state()
        s2["gaps"] = current_gaps()
        save_state(s2)

    def checkpoint(found_batch, failed_batch, lowest_done):
        if lowest_done is not None:
            hold["low"] = lowest_done
        hold["failed"].extend(failed_batch)
        save_gaps()
        if found_batch:
            stats["new"] += _commit_found(args, found_batch)
            build_html(load_archive(), stats["new"], args.per_page)

    found, deleted, blocks, processed = probe_candidates(base, candidates, args, checkpoint)
    if processed == len(candidates):
        hold["low"] = gaps[0][0]   # اكتمل كل شيء (ما بقي ظاهر في القوائم)
        save_gaps()
    left = _gap_size(current_gaps())
    if left:
        info("Still waiting: %d ids — the next run continues from here." % left)
    stats.update({"probed": processed, "deleted": deleted, "blocks": blocks,
                  "found": len(found)})
    return found, stats


# --------------------------------------------------------------------------- #
# الفحص / scan
# --------------------------------------------------------------------------- #
def scan(args):
    base = args.base_url.rstrip("/")
    v = args.verbose
    warm_up(base, args.timeout, v)

    info("Fetching the latest ids from nyaa.si ...")
    max_id, rss_ids = rss_max_id(base, args.timeout, args.retries, args.delay, v)

    if args.range:
        lo, hi = sorted(args.range)
    else:
        if max_id is None:
            # احتياط: جرّب الصفحة الأولى مباشرة
            _, m, _ = collect_visible(base, 0, 10**12, args.timeout,
                                      args.retries, args.delay, v)
            max_id = m
        if max_id is None:
            raise RuntimeError("Could not determine the latest id — check your connection.")
        hi = max_id
        if not args.deep:
            # الوضع المعتاد: كل الجديد منذ آخر مرة مهما طالت المدة، الأحدث أولًا،
            # مع حفظ التقدّم أولًا بأول (انظر _scan_pending).
            return _scan_pending(args, base, max_id, rss_ids)
        lo = 1  # عمق كبير؛ سيُقصّ تلقائيًا إلى أقصى ما تسمح به القوائم.

    info("Latest id: %s — scanning range [%d .. %d]" % (max_id, lo, hi))

    visible, lst_max, covered_min = collect_visible(
        base, lo, hi, args.timeout, args.retries, args.delay, v)
    visible |= {i for i in rss_ids if lo <= i <= hi}
    if lst_max and (max_id is None or lst_max > max_id):
        max_id = lst_max
    # البحث العميق/النطاق يلتزم بما غطّته القوائم. أما الوضع المعتاد فيُكمل تحت
    # منطقة القوائم بالمعرّفات لجلب كل ما تراكم أثناء غيابك (العلامة الرمادية
    # تمنع أي نتيجة خاطئة)، مع تنبيه إن طالت الغيبة.
    if covered_min is not None and covered_min > lo:
        lo = covered_min

    candidates = [i for i in range(lo, hi + 1) if i not in visible]
    info("Visible ids in range: %d — missing ids to scan: %d"
         % ((hi - lo + 1) - len(candidates), len(candidates)))

    found, deleted, blocks, _ = probe_candidates(base, candidates, args)
    stats = {"probed": len(candidates), "deleted": deleted, "blocks": blocks,
             "found": len(found), "max_id": max_id, "lo": lo, "hi": hi}
    return found, stats


# --------------------------------------------------------------------------- #
# توليد صفحة HTML / build the page
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>التورنتات المخفية — Nyaa</title>
<style>
*{box-sizing:border-box}
body{font-family:"Segoe UI",Tahoma,Arial,sans-serif;margin:0;background:#eef0f2;color:#222}
.topbar{background:#2f6f4f;color:#fff;padding:12px 18px;display:flex;flex-wrap:wrap;align-items:center;gap:14px}
.topbar .logo{font-size:20px;font-weight:bold;display:flex;align-items:center;gap:8px}
.topbar .stats{font-size:13px;opacity:.95}
.topbar .stats b{font-size:15px}
.spacer{flex:1}
#search{padding:8px 12px;border:none;border-radius:6px;min-width:220px;font-size:14px}
.note{background:#fff7e0;border-bottom:1px solid #e8d98a;padding:8px 18px;font-size:13px;color:#6b5900}
.wrap{padding:14px 18px}
table.t{width:100%;border-collapse:collapse;background:#fff;font-size:13px;direction:ltr;box-shadow:0 1px 3px rgba(0,0,0,.12)}
table.t th,table.t td{border:1px solid #e6e6e6;padding:7px 9px;vertical-align:middle}
table.t th{background:#fafafa;color:#444;font-weight:600;white-space:nowrap}
table.t tr:nth-child(even) td{background:#fcfcfc}
table.t tr:hover td{background:#eef7f1}
td.name a{color:#1c6ea4;text-decoration:none;font-weight:500}
td.name a:hover{text-decoration:underline}
.b-h{background:#777;color:#fff;border-radius:3px;padding:1px 6px;font-size:11px;margin-inline-start:6px}
.b-u{background:#b8860b;color:#fff;border-radius:3px;padding:1px 6px;font-size:11px;margin-inline-start:6px}
td.links{white-space:nowrap;font-size:16px;text-align:center}
td.links a{text-decoration:none;margin:0 3px}
td.size,td.date{white-space:nowrap;color:#555;text-align:center}
td.cat{white-space:nowrap;color:#444}
td.seed{color:#1a7f37;font-weight:bold;text-align:center}
td.leech{color:#cf222e;font-weight:bold;text-align:center}
td.done{color:#555;text-align:center}
td.empty{text-align:center;padding:48px;color:#888;font-size:15px}
.pager{display:flex;gap:8px;align-items:center;justify-content:center;padding:16px;flex-wrap:wrap}
.pager button{padding:7px 14px;border:1px solid #ccc;background:#fff;border-radius:6px;cursor:pointer;font-size:14px}
.pager button:disabled{opacity:.4;cursor:default}
.pager button:not(:disabled):hover{background:#2f6f4f;color:#fff;border-color:#2f6f4f}
.pager .info{font-size:14px;color:#444;min-width:130px;text-align:center}
footer{text-align:center;color:#999;font-size:12px;padding:18px}
</style>
</head>
<body>
<div class="topbar">
  <div class="logo">🐱 التورنتات المخفية — Nyaa</div>
  <div class="stats">المجموع: <b id="total">0</b> &nbsp;|&nbsp; المعروض: <b id="count">0</b> &nbsp;|&nbsp; جديد هذه المرة: <b id="newcount">0</b></div>
  <div class="spacer"></div>
  <input id="search" type="search" placeholder="🔍 ابحث بالاسم أو التصنيف...">
</div>
<div class="note">آخر تحديث: <b id="generated"></b> — يُعرض <b id="perpage">30</b> في كل صفحة، وتنتقل للوراء بالأزرار أسفل الجدول. لتحديث القائمة: شغّل اختصار «التورنتات المخفية»، ولجمع أكبر عدد ممكن دفعة واحدة شغّل «بحث عميق».</div>
<div class="wrap">
  <table class="t">
    <thead><tr>
      <th>التصنيف</th><th>الاسم</th><th>روابط</th><th>الحجم</th>
      <th>التاريخ</th><th>🌱 بذور</th><th>🔻 محمّلون</th><th>✅ مكتمل</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="pager">
    <button id="first">⏮ الأحدث</button>
    <button id="prev">◀ السابق</button>
    <span class="info" id="pageinfo"></span>
    <button id="next">التالي ▶</button>
    <button id="last">الأقدم ⏭</button>
  </div>
</div>
<footer>أداة محلية لاكتشاف التورنتات المخفية في nyaa.si — للاستخدام الشخصي والتعليمي.</footer>
<script>
const DATA = __DATA__;
const META = __META__;
const PER = __PER__;
let filtered = DATA, page = 0;
const $ = s => document.querySelector(s);
function esc(s){const d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML;}
function pages(){return Math.max(1, Math.ceil(filtered.length / PER));}
function render(){
  const tb=$('#rows'), start=page*PER, rows=filtered.slice(start,start+PER);
  if(!filtered.length){
    tb.innerHTML='<tr><td class="empty" colspan="8">لا توجد تورنتات مخفية في الأرشيف بعد.<br>شغّل الاختصار مرة أخرى لاحقًا لفحص أحدث المعرّفات.</td></tr>';
  } else {
    tb.innerHTML=rows.map(function(t){
      const badge=t.status==='hidden'?'<span class="b-h">مخفي</span>':'<span class="b-u">غير مؤكد</span>';
      const mag=t.magnet?'<a title="مغناطيس" href="'+esc(t.magnet)+'">🧲</a>':'';
      const dl=t.download_url?'<a title="ملف .torrent" target="_blank" rel="noopener" href="'+esc(t.download_url)+'">⬇️</a>':'';
      const vu='<a title="فتح في nyaa" target="_blank" rel="noopener" href="'+esc(t.view_url)+'">🔗</a>';
      return '<tr>'+
        '<td class="cat">'+esc(t.category||t.category_id||'')+'</td>'+
        '<td class="name"><a target="_blank" rel="noopener" href="'+esc(t.view_url)+'">'+esc(t.title||('#'+t.id))+'</a>'+badge+'</td>'+
        '<td class="links">'+mag+dl+vu+'</td>'+
        '<td class="size">'+esc(t.size||'')+'</td>'+
        '<td class="date">'+esc(t.date||'')+'</td>'+
        '<td class="seed">'+esc(t.seeders||'0')+'</td>'+
        '<td class="leech">'+esc(t.leechers||'0')+'</td>'+
        '<td class="done">'+esc(t.completed||'0')+'</td>'+
      '</tr>';
    }).join('');
  }
  $('#pageinfo').textContent='صفحة '+(page+1)+' من '+pages();
  $('#prev').disabled=$('#first').disabled=(page<=0);
  $('#next').disabled=$('#last').disabled=(page>=pages()-1);
  $('#count').textContent=filtered.length;
}
function go(p){page=Math.min(Math.max(0,p),pages()-1);render();window.scrollTo(0,0);}
$('#first').onclick=function(){go(0);};
$('#prev').onclick=function(){go(page-1);};
$('#next').onclick=function(){go(page+1);};
$('#last').onclick=function(){go(pages()-1);};
$('#search').oninput=function(e){
  const q=e.target.value.trim().toLowerCase();
  filtered=!q?DATA:DATA.filter(function(t){return ((t.title||'')+' '+(t.category||'')+' '+t.id).toLowerCase().includes(q);});
  page=0;render();
};
$('#total').textContent=DATA.length;
$('#generated').textContent=META.generated||'';
$('#newcount').textContent=(META.new_count!=null?META.new_count:0);
$('#perpage').textContent=PER;
render();
</script>
</body>
</html>
"""


def build_html(archive, new_count, per_page=DISPLAY_PER_PAGE):
    meta = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "new_count": new_count,
    }
    data_json = json.dumps(archive, ensure_ascii=False).replace("</", "<\\/")
    meta_json = json.dumps(meta, ensure_ascii=False).replace("</", "<\\/")
    page = (HTML_TEMPLATE.replace("__DATA__", data_json)
            .replace("__META__", meta_json)
            .replace("__PER__", str(int(per_page))))
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    return HTML_PATH


def open_in_browser(path):
    url = "file:///" + os.path.abspath(path).replace("\\", "/")
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     r"Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, url])
                return True
            except Exception:
                pass
    try:
        webbrowser.open(url)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# واجهة الأوامر / CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        description="Show hidden torrents on nyaa.si in a clean web page.")
    p.add_argument("--window", type=int, default=3000,
                   help="Number of latest ids to scan for new items (default 3000).")
    p.add_argument("--rebuild", action="store_true",
                   help="Rebuild the page from the archive only, no internet.")
    p.add_argument("--deep", action="store_true",
                   help="Deep scan: scans the full depth nyaa allows (~7700 ids).")
    p.add_argument("--by-category", dest="by_category", action="store_true",
                   help="Very deep scan: sweeps each category separately to reach greater depth (~2x).")
    p.add_argument("--backfill", type=int, default=0, metavar="N",
                   help="Deep id backfill: scans N older ids (e.g. 3000) "
                        "and resumes from where it stopped each time (bypasses the page limit entirely).")
    p.add_argument("--watch", type=int, default=0, metavar="MIN",
                   help="Auto-watch: scans every MIN minutes continuously and sends alerts "
                        "(no scheduled task — just leave the window open).")
    p.add_argument("--max-minutes", dest="max_minutes", type=int, default=0, metavar="MIN",
                   help="Stop scanning after MIN minutes and continue from there next run "
                        "(progress is saved every minute anyway). 0 = no limit.")
    p.add_argument("--range", nargs=2, type=int, metavar=("START", "END"),
                   help="Scan an explicit id range instead of --window.")
    p.add_argument("--yes", "--force", dest="yes", action="store_true",
                   help="Confirm scanning a large range (over %d)." % MAX_RANGE_SPAN)
    p.add_argument("--per-page", dest="per_page", type=int, default=DISPLAY_PER_PAGE,
                   help="Number of torrents shown per page (default %d)."
                        % DISPLAY_PER_PAGE)
    p.add_argument("--max-keep", dest="max_keep", type=int, default=ARCHIVE_CAP,
                   help="Maximum number kept in the archive (default %d)." % ARCHIVE_CAP)
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between requests in seconds (default 1.0).")
    p.add_argument("--timeout", type=float, default=15.0,
                   help="Timeout per request (default 15).")
    p.add_argument("--retries", type=int, default=2,
                   help="Number of retries on block/error (default 2).")
    p.add_argument("--include-unconfirmed", action="store_true",
                   help="Also include existing-but-unconfirmed ones (200 with no gray cue).")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help="Base URL (default %s)." % DEFAULT_BASE_URL)
    p.add_argument("--no-open", action="store_true",
                   help="Do not open the browser (for testing).")
    p.add_argument("--probe", type=int, metavar="ID",
                   help="Scan a single id and print its data (diagnostic).")
    p.add_argument("--test-notify", dest="test_notify", action="store_true",
                   help="Send a test message to Discord/Telegram to verify the setup.")
    p.add_argument("--send-latest", dest="send_latest", type=int, default=0,
                   metavar="N",
                   help="Send the latest N hidden torrents from the archive, formatted, to Discord/Telegram.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose log.")
    return p


def _commit_found(args, items):
    """يدمج ما وُجد في الأرشيف ويحفظه ويرسل إشعارًا بالجديد منه؛ يُرجِع عدد الجديد."""
    archive = load_archive()
    existing_ids = {int(t["id"]) for t in archive}
    new_items = [t for t in items if int(t.get("id", -1)) not in existing_ids]
    merged, new_count = merge_archive(archive, items, args.max_keep)
    save_archive(merged)
    try:
        notify_new(new_items, args.verbose)
    except Exception as e:
        if args.verbose:
            warn("[notify] error: %r" % e)
    return new_count


def _finalize(args, found, stats):
    """يدمج النتائج، يحفظ الأرشيف والحالة، يرسل الإشعارات، ويبني الصفحة (دون فتحها).
    يُرجِع (new_count, total, html_path). الفحص المعتاد يحفظ أولًا بأول بنفسه."""
    if stats.get("committed"):
        new_count = stats.get("new", 0)
    else:
        new_count = _commit_found(args, found)
        if not args.range:
            st = load_state()
            if stats.get("lo"):
                prev = st.get("frontier")
                st["frontier"] = stats["lo"] if prev is None else min(prev, stats["lo"])
            if not (args.backfill and args.backfill > 0) and stats.get("max_id"):
                st["top"] = max(stats["max_id"], st.get("top") or 0)
            save_state(st)
    archive = load_archive()
    path = build_html(archive, new_count, args.per_page)
    return new_count, len(archive), path


def main(argv=None):
    _setup_console()
    args = build_parser().parse_args(argv)
    base = args.base_url.rstrip("/")

    # وضع التشخيص: فحص معرّف واحد
    if args.probe is not None:
        warm_up(base, args.timeout, args.verbose)
        status, body = http_get("%s/view/%d" % (base, args.probe),
                                args.timeout, args.retries, args.delay, args.verbose)
        info("status=%s real_page=%s" % (status, is_real_page(status, body)))
        if is_real_page(status, body):
            print(json.dumps(parse_view(args.probe, body, base),
                             ensure_ascii=False, indent=2))
        return 0

    # وضع إعادة البناء: يبني الصفحة من الأرشيف المحفوظ دون أي اتصال بالإنترنت.
    if args.rebuild:
        archive = load_archive()
        path = build_html(archive, 0, args.per_page)
        info("Built the page from the archive (%d hidden torrents)." % len(archive))
        if not args.no_open:
            if open_in_browser(path):
                info("Opened the page in the browser. ✅")
            else:
                warn("Open the file manually:\n%s" % path)
        return 0

    # وضع اختبار الإشعارات: يرسل رسالة تجريبية للتأكد من صحة الإعداد.
    if args.test_notify:
        cfg = load_config()
        if _send_all(cfg, "✅ اختبار: إشعارات «التورنتات المخفية» تعمل بنجاح!", True):
            info("Test message sent — check Discord/Telegram.")
        else:
            warn("Not sent. Check config.json (discord_webhook or "
                 "telegram_token + telegram_chat_id).")
        return 0

    # وضع إرسال آخر N: يرسل أحدث N تورنت مخفي من الأرشيف بشكل منسّق.
    if args.send_latest and args.send_latest > 0:
        items = sorted(load_archive(), key=lambda x: int(x["id"]),
                       reverse=True)[:args.send_latest]
        if not items:
            warn("The archive is empty — nothing to send.")
            return 0
        header = "📥 **آخر %d تورنت مخفي على Nyaa**" % len(items)
        n = send_torrents(items, header, True)
        if n:
            info("Sent %d torrents, formatted, to Discord/Telegram." % n)
        else:
            warn("Not sent — check config.json.")
        return 0

    if args.range:
        lo, hi = sorted(args.range)
        if lo < 1:
            build_parser().error("--range ids must be >= 1")
        if (hi - lo + 1) > MAX_RANGE_SPAN and not args.yes:
            build_parser().error(
                "Large range (%d ids) — add --yes to confirm." % (hi - lo + 1))

    # وضع المراقبة التلقائية: حلقة تبقى تعمل ما دامت النافذة مفتوحة (ليست مهمة مجدولة).
    if args.watch and args.watch > 0:
        info("Auto-watch mode: scanning every %d minutes. Leave this window open "
             "and alerts will reach you automatically (no scheduled task). "
             "To stop: close the window or press Ctrl+C." % args.watch)
        _cfg = load_config()
        _has = bool((_cfg.get("discord_webhook") or "").strip() or
                    ((_cfg.get("telegram_token") or "").strip() and
                     str(_cfg.get("telegram_chat_id") or "").strip()))
        if not _has:
            warn("[notice] Notifications are not configured in config.json yet — fill in "
                 "discord_webhook or telegram_token+chat_id to receive messages.")
        cycle = 0
        while True:
            cycle += 1
            try:
                found, stats = scan(args)
                nc, total, _ = _finalize(args, found, stats)
                info("[scan %d @ %s] new: %d — total: %d — next scan in "
                     "%d minutes" % (cycle, time.strftime("%H:%M:%S"), nc, total,
                                   args.watch))
            except KeyboardInterrupt:
                info("\nWatch stopped.")
                return 0
            except Exception as e:
                warn("[scan %d] failed: %r — will retry next cycle." % (cycle, e))
            try:
                time.sleep(args.watch * 60)
            except KeyboardInterrupt:
                info("\nWatch stopped.")
                return 0

    try:
        if args.backfill and args.backfill > 0:
            found, stats = scan_backfill(args)
        elif args.by_category:
            found, stats = scan_by_category(args)
        else:
            found, stats = scan(args)
    except RuntimeError as e:
        warn("[error] %s" % e)
        return 2
    except KeyboardInterrupt:
        warn("\n[stopped]")
        return 130

    new_count, total, path = _finalize(args, found, stats)
    info("")
    info("Result: found %d in this scan (%d new) — "
         "archive total: %d — deleted/404: %d"
         % (stats["found"], new_count, total, stats["deleted"]))
    info("Page built: %s" % path)
    if not args.no_open:
        if open_in_browser(path):
            info("Opened the page in the browser. ✅")
        else:
            warn("Could not open the browser automatically — open the file manually:\n%s" % path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        warn("\n[stopped]")
        sys.exit(130)
