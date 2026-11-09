#!/usr/bin/env python3
"""News monitor for trader-news. Standard library only.

Watches headlines and SEC filings for the stocks you watch or hold and
prints only what is new since the last check, newest first, with how far each
stock has moved today and a flag on anything that touches your tickers.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers TSLA NVDA AMD
    python3 news.py --tickers TSLA --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-sec                         # headlines only
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per ticker:
  - Yahoo Finance headline RSS:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<TICKER>&region=US&lang=en-US
  - SEC EDGAR filings Atom feed for the company (8-K by default: earnings,
    executive changes, deals, restatements):
    https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<CIK>&type=8-K&count=10&output=atom
    SEC needs a User-Agent with your name and email (sec_user_agent in
    trader/config.json or SEC_USER_AGENT). Without one the SEC part is skipped
    and the tool says so; headlines still run.

  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<TICKER>?range=1d&interval=5m

Times print in New York time (ET). State: trader/state/news.json remembers
what you have seen (30 days), so an item shows once. Offline: --offline DIR
reads yahoo-<TICKER>.xml, chart-<TICKER>.json, sec-<FORM>-<CIK>.xml and
company_tickers.json from DIR. The tests use it.

India ("market": "IN" in trader/config.json, or --market IN): SEC is off and
NSE's own public RSS feeds take its place, fetched once per check for all
tickers: corporate announcements (results, board outcomes, orders, resignations,
fund raising) and board meetings (the date a company will consider its
results). They name the company, not the symbol, so items are matched by the
symbol in the filing's link or by the name in trader/universe-in.txt
(trader-scanner). The feeds hold only the last several hours: check through the
day. Yahoo headlines use the .NS symbol and are often empty for NSE stocks.

Indian business media (India mode; --no-media skips them):
  - The papers' own RSS feeds, once per check: Mint (livemint.com/rss/markets,
    /rss/companies), The Economic Times (markets, stocks) and The Hindu
    BusinessLine (markets, companies). Business Standard refuses tools (403).
  - Moneycontrol and Mint stories per ticker from Google News's public RSS
    search, limited to moneycontrol.com and livemint.com (Moneycontrol's own
    RSS feeds stopped updating in April 2024). Links are Google News links
    that open the story.
  A media story is kept only when its headline or summary names the company
  (or the symbol in capitals): a search hit alone is not enough. Quote,
  listing and "trending stocks" pages are dropped. Symbols that are also market
  words (BSE, NSE, MCX, IDEA...) count only as "BSE Ltd", "BSE shares" or
  "NSE: BSE". Yahoo's .NS headlines are kept only when they name the company too.
  An NSE feed that does not answer gets one slower retry; a refusal never does.

Times print in IST. Offline names: nse-announcements.xml, nse-board-meetings.xml,
mint-markets.xml, mint-companies.xml, et-markets.xml, et-stocks.xml,
bl-markets.xml, bl-companies.xml, gnews-SYMBOL.xml.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%s&type=%s"
            "&dateb=&owner=include&count=%d&output=atom")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
PLACEHOLDER = "you@example.com"
BROWSER_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
NSE_UA = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"  # plain, no browser name
NSE_FEEDS = [  # NSE's public RSS feeds (nseindia.com, read 2026-10-30)
    ("announcement", "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
     "nse-announcements.xml"),
    ("board meeting", "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
     "nse-board-meetings.xml"),
]
MEDIA_FEEDS = [  # the papers' own RSS feeds (read 2026-10-30; Business Standard answers 403 to tools)
    ("Mint", "https://www.livemint.com/rss/markets", "mint-markets.xml"),
    ("Mint", "https://www.livemint.com/rss/companies", "mint-companies.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "et-markets.xml"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms", "et-stocks.xml"),
    ("BusinessLine", "https://www.thehindubusinessline.com/markets/feeder/default.rss", "bl-markets.xml"),
    ("BusinessLine", "https://www.thehindubusinessline.com/companies/feeder/default.rss", "bl-companies.xml"),
]
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
MEDIA_SITES = ("moneycontrol.com", "livemint.com")
MEDIA_NAMES = {"moneycontrol": "Moneycontrol", "livemint": "Mint", "mint": "Mint",
               "economictimes": "Economic Times", "economic times": "Economic Times",
               "businessline": "BusinessLine"}
# NSE subjects that can move a stock the way an important 8-K item does
NSE_IMPORTANT = re.compile(r"financial result|outcome of board meeting|resignation|fraud|default|insolvency|"
                           r"suspension|delisting|credit rating|search|penalt|show cause|litigation|"
                           r"acquisition|scheme of arrangement|buy ?back|fund raising", re.I)
NSE_SKIP = re.compile(r"declaration of NAV", re.I)   # mutual fund prices, not company news
# NSE symbols that are also everyday market words: "BSE" in a headline is nearly always the
# exchange. For these only "BSE Ltd", "BSE shares", "NSE: BSE" and the like name the stock.
AMBIGUOUS_SYMBOLS = {"BSE", "NSE", "MCX", "IDEA", "GOLD", "SILVER", "POWER", "STEEL", "BANK",
                     "INDIA", "HOME", "ONE"}
STOCK_WORDS = r"(?i:Ltd\b|Limited\b|shares?\b|stock\b|share price\b|Q[1-4]\b)"
# Media items that are quote, listing or index pages, not stories (seen live on 2026-10-30)
MEDIA_JUNK = re.compile(r"live (?:nse|bse)\s*/\s*(?:nse|bse)|listing details|f&o quote|bids offers|"
                        r"(?:most visited|most followed|volatile|trending) stocks today|"
                        r"^share/stock market news|corporate relationship department|"
                        r"share price (?:highlights|live updates)|stock price history", re.I)
QUOTE_PAGE = re.compile(r"^(?:\S+\s+){0,5}(?:Ltd|Limited)\.?$")   # "Trent Ltd.", "COFORGE Ltd."
MARKETS = {
    "US": {"code": "US", "exchange": "NYSE", "zone": "ET", "utc_offset": None, "suffix": "",
           "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"},
    "IN": {"code": "IN", "exchange": "NSE", "zone": "IST", "utc_offset": 5.5, "suffix": ".NS",
           "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=IN&lang=en-IN"},
}
MARKET = MARKETS["US"]
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

ITEMS_8K = {
    "1.01": "material agreement", "1.02": "agreement ended", "1.03": "bankruptcy",
    "1.05": "cybersecurity incident", "2.01": "acquisition or sale of assets",
    "2.02": "earnings results", "2.03": "new debt", "2.04": "debt acceleration",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "3.02": "unregistered share sale", "3.03": "shareholder rights change",
    "4.01": "auditor change", "4.02": "past financials no longer reliable",
    "5.01": "change in control", "5.02": "director or officer change",
    "5.03": "bylaws or fiscal year change", "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
IMPORTANT_ITEMS = {"1.03", "1.05", "2.02", "2.04", "2.06", "3.01", "4.02", "5.01", "5.02"}
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEC charges\b|\bantitrust\b|\bjudge\b|\bcourt\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bstock split\b|\boffering\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b"),
    ("halt", r"\bhalt(?:s|ed)?\b|\btrading (?:pause|suspen)"),
    ("regulatory", r"\bFDA\b|\bapproval\b|\bapproves\b|\bclinical\b|\bFTC\b|\bDOJ\b"),
    ("macro", r"\bFed\b|\bFOMC\b|\bCPI\b|\binflation\b|\bjobs report\b|\bpayrolls\b|\btariffs?\b|\bfutures\b"),
]


class NewsError(Exception):
    pass


def use_market(code):
    """Switch the clock, the symbols and the sources: "US" (default) or "IN" (NSE)."""
    global MARKET
    key = (code or "US").strip().upper()
    if key not in MARKETS:
        raise NewsError("market wants US or IN, not %r" % code)
    MARKET = MARKETS[key]
    return MARKET


def market_from(path):
    """The "market" in a JSON config, or None."""
    try:
        doc = load_json(path)
    except ValueError:
        return None
    return doc.get("market") if isinstance(doc, dict) else None


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + _dt.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7, 0))   # 2:00 EST
    end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    """Aware or naive UTC -> naive exchange wall-clock time (New York, or India for IN)."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    off = MARKET["utc_offset"]
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=eastern_offset(utc) if off is None else off)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def sec_user_agent(cli=None, config="trader/config.json"):
    ua = (cli or os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        cfg = load_json(config) or {}
        ua = str(cfg.get("sec_user_agent") or "").strip()
    if not ua or "@" not in ua or PLACEHOLDER in ua:
        return None
    return ua


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, sec_ua=None, offline=None, pause=0.15):
        self.sec_ua = sec_ua
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, sec=False, ua=None, timeout=30):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        if sec and not self.sec_ua:
            raise NewsError("no SEC User-Agent set")
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": self.sec_ua if sec else (ua or BROWSER_UA), "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))
        except OSError as exc:   # a read timeout or a dropped connection
            raise NewsError("no answer from %s (%s)" % (url.split("/")[2], exc.__class__.__name__))


def yahoo_symbol(ticker):
    """TSLA -> TSLA, BRK.B -> BRK-B; in India RELIANCE -> RELIANCE.NS, M&M -> M%26M.NS (URL-safe)."""
    if MARKET["suffix"]:
        return urllib.parse.quote(ticker.upper() + MARKET["suffix"], safe=".-")
    return ticker.upper().replace(".", "-")


def cik_map(fetcher, state):
    """ticker -> 10-digit CIK, from SEC's company_tickers.json (cached in state)."""
    cached = state.setdefault("ciks", {})
    if cached:
        return cached
    data = json.loads(fetcher.get(TICKERS_URL, "company_tickers.json", sec=True).decode("utf-8"))
    for v in data.values():
        cached.setdefault(v["ticker"].upper().replace("-", "."), str(v["cik_str"]).zfill(10))
    return cached


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_sec_atom(data, ticker):
    """EDGAR company filings Atom -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("SEC feed for %s is not valid XML: %s" % (ticker, exc))
    company = ""
    for el in root.iter():
        if _local(el.tag) == "conformed-name":
            company = (el.text or "").strip()
            break
    out = []
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        form = _text(entry, "filing-type")
        acc = _text(entry, "accession-number")
        items = re.findall(r"\d+\.\d+", _text(entry, "items-desc"))
        labels = [ITEMS_8K.get(i, "item " + i) for i in items if i != "9.01"]
        link = ""
        for c in entry.iter():
            if _local(c.tag) == "link":
                link = c.get("href", "")
        title = "%s filed a %s" % (company or ticker, form)
        if labels:
            title += ": " + ", ".join(labels)
        out.append({"id": "sec:" + acc, "source": "SEC EDGAR", "kind": "filing", "form": form,
                    "ticker": ticker, "title": title, "summary": _text(entry, "form-name"),
                    "items": items, "important": bool(set(items) & IMPORTANT_ITEMS),
                    "link": link, "time": parse_time(_text(entry, "updated"))})
    return out


def norm_name(name):
    """"Mahindra & Mahindra Limited" -> "mahindra and mahindra": for matching NSE feed titles."""
    n = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and ")).strip()
    return re.sub(r"\s*\b(limited|ltd)$", "", n).strip()


def nse_names(path="trader/universe-in.txt"):
    """{normalised company name: SYMBOL} from trader-scanner's NSE universe (SYMBOL|Name lines)."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "|" in line:
                    sym, name = line.rstrip("\n").split("|", 1)
                    out.setdefault(norm_name(name), sym.strip().upper())
    return out


def parse_nse_time(value):
    """"30-Oct-2026 14:13:50" (India time) -> aware UTC, or None."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            local = _dt.datetime.strptime((value or "").strip(), fmt)
        except ValueError:
            continue
        return (local - _dt.timedelta(hours=5.5)).replace(tzinfo=_dt.timezone.utc)
    return parse_time(value)


def parse_nse_rss(data, kind, watch, names):
    """One NSE RSS feed -> items for the watched tickers only.

    The title is the company's name; the symbol comes from the filing link
    (.../corporate/ANANTRAJ_30102026141327_...pdf) or else from the name."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE %s feed is not valid XML: %s" % (kind, exc))
    for t, n in watch.items():
        if n:
            names.setdefault(norm_name(n), t)
    out = []
    for it in root.iter("item"):
        company, link, desc = _text(it, "title"), _text(it, "link"), _text(it, "description")
        if NSE_SKIP.search(desc):
            continue
        m = re.search(r"/corporate/([A-Z0-9&\-]+)_\d{8,}", link)
        sym = m.group(1) if m and m.group(1) in watch else names.get(norm_name(company))
        if sym not in watch:
            continue
        body, _, subject = desc.rpartition("|")
        subject = re.sub(r"^\s*SUBJECT:\s*", "", subject).strip()
        body = (body or desc).strip()
        if kind == "board meeting":
            title = "%s: board meeting%s" % (company, (" (" + subject + ")") if subject else "")
        else:
            title = "%s: %s" % (company, subject or "announcement")
        out.append({"id": "nse:" + (link or company + desc), "source": "NSE", "kind": kind,
                    "ticker": sym, "title": title, "summary": body, "link": link,
                    "important": bool(NSE_IMPORTANT.search(subject + " " + body)),
                    "time": parse_nse_time(_text(it, "pubDate"))})
    return out


def short_name(name):
    """"Bajaj Finance Limited" -> "Bajaj Finance": the way headlines name a company."""
    n = re.sub(r"\s+", " ", (name or "")).strip()
    return re.sub(r"(?i)[\s.,]*\b(limited|ltd\.?)$", "", n).strip()


def nse_display_names(path="trader/universe-in.txt"):
    """{SYMBOL: short company name} from trader-scanner's NSE universe."""
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "|" in line:
                    sym, name = line.rstrip("\n").split("|", 1)
                    out[sym.strip().upper()] = short_name(name)
    return out


def media_source(text):
    """"Moneycontrol.com", "Livemint", "https://www.moneycontrol.com" -> "Moneycontrol" / "Mint", else None."""
    t = (text or "").lower()
    for key, label in MEDIA_NAMES.items():
        if key in t:
            return label
    return None


def parse_media_rss(data, watch, source=None, ticker=None):
    """Mint's RSS, or a Google News search -> items that name one of your companies.

    source  "Mint" for Mint's own feed; None = read it per item (Google News <source>)
    ticker  the ticker a Google News search was for (it still has to be named)"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("media RSS is not valid XML: %s" % exc)
    pats = mention_patterns(watch)
    out = []
    for it in root.iter("item"):
        title, link = _text(it, "title"), _text(it, "link")
        src = source
        if src is None:
            el = next((c for c in it.iter() if _local(c.tag) == "source"), None)
            src = media_source((el.text or "") + " " + (el.get("url", "") if el is not None else "")) \
                if el is not None else None
            title = re.sub(r"\s+-\s+[^-]*$", "", title).strip()   # Google adds " - Livemint"
        if not src or not title or is_quote_page(title):
            continue
        summary = re.sub(r"<[^>]+>", " ", _text(it, "description"))
        summary = "" if source is None else re.sub(r"\s+", " ", summary).strip()
        hay = title + " " + summary
        hits = [t for t in sorted(watch) if pats[t].search(hay)]
        if not hits:
            continue
        first = ticker if ticker in hits else hits[0]
        out.append({"id": "media:" + (_text(it, "guid") or link), "source": src, "kind": "headline",
                    "ticker": first, "title": title, "summary": summary, "link": link,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def is_quote_page(title):
    """A quote, listing or "trending stocks" page rather than a story."""
    return bool(MEDIA_JUNK.search(title) or QUOTE_PAGE.match(title.strip()))


def ambiguous(ticker, name=None):
    """An NSE symbol (and name) that is also a market word, like BSE."""
    return (MARKET["code"] == "IN" and ticker in AMBIGUOUS_SYMBOLS
            and (not name or name.upper() == ticker))


def gnews_url(name, ticker, hours):
    days = max(1, int(-(-hours // 24)))
    who = '"%s"' % (name or ticker)
    if ambiguous(ticker, name):
        who = '("%s Ltd" OR "%s shares")' % (ticker, ticker)
    q = "%s (%s) when:%dd" % (who, " OR ".join("site:" + s for s in MEDIA_SITES), days)
    return GNEWS % urllib.parse.quote(q)


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\$%s\b" % re.escape(t),
                r"\b(?:NYSE|NASDAQ|Nasdaq|NSE|BSE)\s*:\s*%s\b" % re.escape(t)]
        if ambiguous(t, name):   # "BSE" alone is the exchange: want "BSE Ltd", "BSE shares"
            alts.append(r"(?<![\w&-])%s\s+%s" % (re.escape(t), STOCK_WORDS))
        else:
            if name and len(name) >= 4:
                alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
            if MARKET["code"] == "IN" and len(t) >= 3:
                alts.append(r"(?<![\w&-])%s(?![\w&-])" % re.escape(t))   # headlines use symbols: "NALCO shares"
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and "2.02" in it.get("items", []) and "earnings" not in it["tags"]:
            it["tags"].insert(0, "earnings")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, sec=True, forms=("8-K",), count=10, errors=None, names=None,
            media=True, hours=48):
    errors = [] if errors is None else errors
    items = []
    india = MARKET["code"] == "IN"
    pats = mention_patterns(watch) if india else {}
    for t in sorted(watch):
        try:
            got = parse_rss(fetcher.get(MARKET["yahoo"] % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
        except NewsError as exc:
            if not (india and fetcher.offline and "no file" in str(exc)):
                errors.append("headlines for %s: %s" % (t, exc))
            continue
        if india:   # Yahoo's .NS feeds carry loosely related stories: keep the ones that name it
            got = [i for i in got if pats[t].search("%s %s" % (i["title"], i["summary"]))]
        items += got
    if india:
        names = dict(names or {})
        for kind, url, fname in NSE_FEEDS:
            try:
                items += parse_nse_rss(fetch_nse(fetcher, url, fname), kind, watch, names)
            except NewsError as exc:
                errors.append("NSE %ss: %s. Open %s in your browser to read them" % (kind, exc, url))
        if media:
            items += collect_media(fetcher, watch, errors, hours)
        return items
    if sec:
        try:
            ciks = cik_map(fetcher, state)
        except NewsError as exc:
            errors.append("SEC ticker list: %s" % exc)
            ciks = {}
        for t in sorted(watch):
            cik = ciks.get(t)
            if not cik:
                if ciks:
                    errors.append("no SEC CIK for %s" % t)
                continue
            for form in forms:
                try:
                    data = fetcher.get(SEC_FEED % (cik, form, count),
                                       "sec-%s-%s.xml" % (form, cik), sec=True)
                    items += parse_sec_atom(data, t)
                except NewsError as exc:
                    errors.append("SEC %s for %s: %s" % (form, t, exc))
    return items


def fetch_nse(fetcher, url, fname, wait=5):
    """An NSE feed, with one slower retry when NSE does not answer (it is often slow).
    A refusal (an HTTP error) is never retried."""
    try:
        return fetcher.get(url, fname, ua=NSE_UA)
    except NewsError as exc:
        if fetcher.offline or not re.match(r"(no answer from|could not reach)", str(exc)):
            raise
    time.sleep(wait)
    return fetcher.get(url, fname, ua=NSE_UA, timeout=60)


def collect_media(fetcher, watch, errors, hours=48):
    """Indian business media: the papers' own feeds once (Mint, Economic Times, BusinessLine),
    then one Google News search per ticker for Moneycontrol and Mint."""
    items = []
    for label, url, fname in MEDIA_FEEDS:
        try:
            items += parse_media_rss(fetcher.get(url, fname, ua=NSE_UA), watch, source=label)
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("%s feed %s: %s" % (label, url, exc))
    for t in sorted(watch):
        try:
            data = fetcher.get(gnews_url(watch.get(t), t, hours), "gnews-%s.xml" % t, ua=NSE_UA)
            items += parse_media_rss(data, watch, ticker=t)
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("Moneycontrol / Mint search for %s: %s" % (t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s %s" % (to_eastern(m["time"]).strftime("%H:%M"), MARKET["zone"])) if m.get("time") else ""
        out.append("%-*s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            6 if MARKET["code"] == "US" else 10,
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = (to_eastern(it["time"]).strftime("%Y-%m-%d %H:%M ") + MARKET["zone"]) if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-*s %s" % (mark, when, who, 13 if MARKET["code"] == "US" else 14,
                                            it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % (to_eastern(now).strftime("%Y-%m-%d %H:%M ") + MARKET["zone"]))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--forms", default="8-K", help="SEC forms to watch, comma separated")
    ap.add_argument("--no-sec", action="store_true", help="headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--no-media", action="store_true", help="India: skip Moneycontrol and Mint")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--user-agent", help="SEC User-Agent: \"Your Name you@example.com\"")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--market", help="US (default) or IN (NSE); default: \"market\" in the config")
    ap.add_argument("--universe", default="trader/universe-in.txt",
                    help="India: SYMBOL|Name lines to match NSE feed titles (trader-scanner universe --market IN)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        use_market(args.market or market_from(args.config))
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2

    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    ua = sec_user_agent(args.user_agent, args.config)
    sec = not args.no_sec and MARKET["code"] == "US"
    names = {}
    if MARKET["code"] == "IN":
        names = nse_names(args.universe)
        display = nse_display_names(args.universe)
        for t in watch:
            watch[t] = short_name(watch[t]) if watch[t] else display.get(t)
        if not names:
            errors.append("no %s: NSE items are matched by the symbol in their link only "
                          "(scan.py universe --market IN writes it)" % args.universe)
    if sec and not ua and not args.offline:
        errors.append("SEC filings skipped: set sec_user_agent in trader/config.json or "
                      "SEC_USER_AGENT (\"Your Name you@example.com\") to include them")
        sec = False
    fetcher = Fetcher(ua, offline=args.offline)
    forms = [f.strip().upper() for f in args.forms.split(",") if f.strip()]
    items = collect(fetcher, watch, state, sec=sec, forms=forms, errors=errors, names=names,
                    media=not args.no_media, hours=args.hours)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
