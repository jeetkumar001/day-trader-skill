import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Apple may have a way to dominate the smart home space")
        self.assertEqual(first["time"], datetime.datetime(2026, 10, 13, 19, 46, 52,
                                                          tzinfo=datetime.timezone.utc))
        self.assertTrue(first["link"].startswith("https://finance.yahoo.com/"))
        self.assertTrue(first["id"].startswith("rss:"))

    def test_sec_atom_8k(self):
        items = news.parse_sec_atom(raw("sec-8-K-0000320193.xml"), "AAPL")
        self.assertEqual([i["form"] for i in items], ["8-K/A", "8-K", "8-K"])
        earn = items[1]
        self.assertEqual(earn["items"], ["2.02", "9.01"])
        self.assertTrue(earn["important"])
        self.assertEqual(earn["title"], "Apple Inc. filed a 8-K: earnings results")
        self.assertEqual(earn["id"], "sec:0000320193-26-000018")
        self.assertIn("000032019326000018", earn["link"])
        # 16:30:28 New York time is 20:30:28 UTC
        self.assertEqual(earn["time"].isoformat(), "2026-07-30T20:30:28+00:00")
        self.assertEqual(items[0]["title"], "Apple Inc. filed a 8-K/A: director or officer change")

    def test_times(self):
        self.assertEqual(news.parse_time("Tue, 13 Oct 2026 20:02:03 +0000").hour, 20)
        self.assertEqual(news.parse_time("2026-09-01T16:30:35-04:00").hour, 20)
        self.assertIsNone(news.parse_time("soon"))

    def test_yahoo_symbols(self):
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("yahoo-KO.xml"), "KO")
        watch = {"KO": "Coca-Cola", "CVX": "Chevron", "AAPL": "Apple"}
        news.tag(items, watch)
        by = {i["title"][:20]: i for i in items}
        chevron = by["Coca-Cola or Chevron"]
        self.assertEqual(chevron["touches"], ["CVX", "KO"])
        self.assertIn("capital", chevron["tags"])  # dividend
        self.assertEqual(by["Is Coca-Cola (KO) St"]["touches"], ["KO"])

    def test_earnings_filings_are_tagged(self):
        items = news.tag(news.parse_sec_atom(raw("sec-8-K-0000021344.xml"), "KO"), {"KO": None})
        self.assertEqual(items[0]["tags"][0], "earnings")
        self.assertFalse(items[1]["important"])  # 8.01 other events

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("yahoo-AAPL.xml"), "AAPL")
        b = news.parse_rss(raw("yahoo-AAPL.xml"), "GOOGL")
        items = news.dedupe(news.tag(a + b, {"AAPL": None, "GOOGL": None}))
        self.assertEqual(len(items), 6)
        self.assertEqual(items[0]["touches"], ["AAPL", "GOOGL"])


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json"),
                     "--watchlist", os.path.join(self.tmp.name, "none.json"),
                     "--plan", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "AAPL", "KO", "--hours", "24", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 10)  # 6 Apple + 4 Coca-Cola headlines, filings are older
        code, out, _ = run(["--tickers", "AAPL", "KO", "--hours", "24"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--tickers", "AAPL", "--hours", "2400"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-07-30 16:30 ET  AAPL", out)  # 20:30 UTC, New York time
        self.assertIn("earnings results", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "KO", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "AAPL"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["AAPL", "KO"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "24"] + args)
        self.assertEqual(code, 0, err)
        tickers = {t for i in json.loads(out)["items"] for t in i["touches"]}
        self.assertEqual(tickers, {"KO"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_sec_needs_a_user_agent(self):
        old = os.environ.pop("SEC_USER_AGENT", None)
        try:
            self.assertIsNone(news.sec_user_agent(None, "/nonexistent.json"))
            self.assertIsNone(news.sec_user_agent("Your Name you@example.com"))
            self.assertEqual(news.sec_user_agent("Ada ada@example.org"), "Ada ada@example.org")
            with self.assertRaises(news.NewsError):
                news.Fetcher(None).get("https://www.sec.gov/x", "x", sec=True)
        finally:
            if old is not None:
                os.environ["SEC_USER_AGENT"] = old

    def test_the_reel_tsla_morning(self):
        # The reel's real feed: Yahoo Finance headlines for TSLA, Monday Oct 19 2026, checked at 10:15 AM ET
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--no-moves",
                              "--now", "2026-10-19T10:15:00-04:00"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-19 08:04 ET  TSLA", out)
        self.assertNotIn("10:32 ET", out)  # published after the pinned 10:15 clock
        self.assertIn("Tesla-SpaceX Merger Mania Strikes Wall Street Ahead of Earnings", out)
        merger = [l for l in out.splitlines() if "Merger Mania" in l]
        tags = out.splitlines()[out.splitlines().index(merger[0]) + 1]
        self.assertIn("earnings", tags)
        self.assertIn("deal", tags)
        self.assertIn("Checked 2026-10-19 10:15 ET", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  2026-10-19")]), 16)  # 18 in the feed, 2 after 10:15
        # the price line, from the chart fetched at 10:52
        code, out, err = run(["--tickers", "TSLA", "--hours", "6", "--no-sec", "--dry-run",
                              "--now", "2026-10-19T10:52:00-04:00"] + self.base[:4])
        self.assertRegex(out, r"TSLA\s+366\.4[78] \+3\.31% vs yesterday's close 354\.72 at 10:52 ET  MOVING")

    def test_moves(self):
        m = news.parse_chart(raw("chart-TSLA.json"), "TSLA")
        self.assertEqual(m["prev_close"], 354.72)
        self.assertEqual(m["pct"], 3.31)
        self.assertEqual(news.to_eastern(m["time"]).strftime("%H:%M"), "10:52")
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")
        # no chart file offline: no move line, no error note
        code, out, _ = run(["--tickers", "KO", "--hours", "24", "--dry-run"] + self.base)
        self.assertNotIn("vs yesterday's close", out)
        self.assertNotIn("note:", out)

    def test_new_york_time_follows_daylight_saving(self):
        utc = datetime.datetime(2026, 1, 15, 15, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EST, UTC-5
        utc = datetime.datetime(2026, 10, 19, 14, 15, tzinfo=datetime.timezone.utc)
        self.assertEqual(news.to_eastern(utc).hour, 10)  # EDT, UTC-4
        utc = datetime.datetime(2026, 11, 1, 6, 30)       # 1:30 EST, just after the switch back
        self.assertEqual(news.to_eastern(utc).hour, 1)

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "TSLA",
                  "title": "Trading halted in XYZ after FDA approval; futures rise before CPI",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"TSLA": None})[0]["tags"]
        for t in ("halt", "regulatory", "macro"):
            self.assertIn(t, tags)

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


NSE = os.path.join(DIR, "nse")


class India(unittest.TestCase):
    """"market": "IN": NSE's announcement and board-meeting feeds take the place of SEC."""

    def setUp(self):
        news.use_market("IN")
        self.addCleanup(news.use_market, "US")
        self.names = news.nse_names(os.path.join(NSE, "universe-in.txt"))

    def feed(self, name, kind, watch):
        with open(os.path.join(NSE, name), "rb") as fh:
            return news.parse_nse_rss(fh.read(), kind, watch, dict(self.names))

    def test_symbols_from_the_link_or_the_name(self):
        items = self.feed("nse-announcements.xml", "announcement", {"VBL": None, "AJANTPHARM": None})
        self.assertEqual({i["ticker"] for i in items}, {"VBL", "AJANTPHARM"})
        # Ajanta's filing link says PRACHI_..., so it is matched by its name
        aj = [i for i in items if i["ticker"] == "AJANTPHARM"][0]
        self.assertIn("PRACHI_", aj["link"])
        self.assertTrue(aj["important"])
        self.assertEqual(aj["time"].isoformat(), "2026-10-30T07:32:20+00:00")   # 13:02:20 IST

    def test_mutual_fund_navs_are_skipped(self):
        items = self.feed("nse-announcements.xml", "announcement", {"X": None})
        self.assertEqual(items, [])
        self.assertNotIn("NAV", " ".join(i["summary"] for i in self.feed(
            "nse-announcements.xml", "announcement", {"VBL": None})))

    def test_board_meeting_carries_the_date(self):
        items = self.feed("nse-board-meetings.xml", "board meeting", {"GRASIM": None})
        self.assertTrue(items)
        self.assertTrue(all("13-Nov-2026" in i["title"] for i in items))
        self.assertTrue(any("earnings" in i["tags"] for i in news.tag(items, {"GRASIM": None})))

    def test_cli_offline(self):
        code, out, _ = run(["--market", "IN", "--tickers", "VBL", "GRASIM", "--offline", NSE,
                            "--universe", os.path.join(NSE, "universe-in.txt"), "--no-moves",
                            "--now", "2026-10-30T09:00:00Z", "--dry-run", "--state",
                            os.path.join(tempfile.mkdtemp(), "news.json")])
        self.assertEqual(code, 0)
        self.assertIn("IST", out)
        self.assertIn("Grasim Industries Limited: board meeting (Meeting Date: 13-Nov-2026)", out)
        self.assertNotIn("SEC", out)

    def test_yahoo_symbol(self):
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertIn("region=IN", news.MARKET["yahoo"])


class IndianMedia(unittest.TestCase):
    """India: Mint, Economic Times and BusinessLine feeds, Moneycontrol and Mint via Google News."""

    def setUp(self):
        news.use_market("IN")
        self.addCleanup(news.use_market, "US")
        self.watch = {"RELIANCE": "Reliance Industries", "VBL": "Varun Beverages"}

    def raw(self, name):
        with open(os.path.join(NSE, name), "rb") as fh:
            return fh.read()

    def test_short_names(self):
        self.assertEqual(news.short_name("Bajaj Finance Limited"), "Bajaj Finance")
        self.assertEqual(news.short_name("SKIPPER LIMITED"), "SKIPPER")
        self.assertEqual(news.short_name("Thomas Cook  (India)  Ltd."), "Thomas Cook (India)")

    def test_paper_feed_keeps_only_named_companies(self):
        items = news.parse_media_rss(self.raw("mint-markets.xml"), self.watch, source="Mint")
        self.assertEqual(sorted(i["ticker"] for i in items), ["RELIANCE", "VBL"])   # gold is dropped
        vbl = [i for i in items if i["ticker"] == "VBL"][0]
        self.assertIn("Stocks to watch", vbl["title"])                             # matched by the symbol
        et = news.parse_media_rss(self.raw("et-stocks.xml"), self.watch, source="Economic Times")
        self.assertEqual(len(et), 1)
        self.assertNotIn("<img", et[0]["summary"])

    def test_google_news_search(self):
        items = news.parse_media_rss(self.raw("gnews-RELIANCE.xml"), self.watch, ticker="RELIANCE")
        self.assertEqual([(i["source"], i["title"]) for i in items], [
            ("Moneycontrol", "Reliance Industries Q2 results: Profit may top Rs 21,000 crore"),
            ("Mint", "Reliance Industries Q2 results today: what to watch")])
        self.assertEqual(items[0]["time"].isoformat(), "2026-10-30T04:06:00+00:00")
        self.assertIn("site%3Amoneycontrol.com", news.gnews_url("Reliance Industries", "RELIANCE", 48))
        self.assertIn("when%3A2d", news.gnews_url("Reliance Industries", "RELIANCE", 48))

    def test_cli_offline_all_sources(self):
        code, out, _ = run(["--market", "IN", "--tickers", "RELIANCE", "--offline", NSE,
                            "--universe", os.path.join(NSE, "universe-in.txt"), "--no-moves",
                            "--now", "2026-10-30T09:00:00Z", "--dry-run", "--state",
                            os.path.join(tempfile.mkdtemp(), "news.json")])
        self.assertEqual(code, 0)
        for src in ("Moneycontrol", "Mint", "Economic Times"):
            self.assertIn(src, out)
        # Mint's own feed and the Google News copy of the same story show once
        self.assertEqual(out.count("Reliance Industries Q2 results today: what to watch"), 1)
        code, out, _ = run(["--market", "IN", "--tickers", "RELIANCE", "--offline", NSE, "--no-media",
                            "--universe", os.path.join(NSE, "universe-in.txt"), "--no-moves",
                            "--now", "2026-10-30T09:00:00Z", "--dry-run", "--state",
                            os.path.join(tempfile.mkdtemp(), "news.json")])
        self.assertNotIn("Moneycontrol", out)


class NewsCleanup(unittest.TestCase):
    """India: the noise seen live on 2026-10-30 (BSE the exchange, quote pages, stray Yahoo items)."""

    def setUp(self):
        news.use_market("IN")
        self.addCleanup(news.use_market, "US")

    def match(self, watch, text):
        return sorted(t for t, rx in news.mention_patterns(watch).items() if rx.search(text))

    def test_bse_the_exchange_is_not_bse_the_stock(self):
        watch = {"BSE": "BSE", "INFY": "Infosys"}
        self.assertEqual(self.match(watch, "Trending Stocks on NSE and BSE"), [])
        self.assertEqual(self.match(watch, "Infosys shares rise 3% on BSE"), ["INFY"])
        self.assertEqual(self.match(watch, "BSE shares hit a record high"), ["BSE"])
        self.assertEqual(self.match(watch, "BSE Ltd Q2 results: profit doubles"), ["BSE"])
        self.assertEqual(self.match(watch, "Brokerages on BSE (NSE: BSE) after results"), ["BSE"])
        self.assertIn("%22BSE%20Ltd%22", news.gnews_url("BSE", "BSE", 48))
        self.assertEqual(self.match({"TRENT": "Trent"}, "Trent up 6%"), ["TRENT"])   # others unchanged

    def test_us_market_is_unchanged(self):
        news.use_market("US")
        self.assertFalse(news.ambiguous("BSE", "BSE"))

    def test_quote_pages_are_dropped(self):
        for title in ("COFORGE Ltd.", "Trent Ltd.", "Ather Energy Ltd.",
                      "Steamhouse India Share Price Today 30 Oct 2026: Live NSE/BSE Rates, Technical Analysis",
                      "Most Visited Stocks Today | Trending Stocks on NSE and BSE",
                      "F&O Quote - Live FNO BSE/NSE Quote, Nifty Quotes",
                      "> Listing Details > > Listing Details of - BSE: , NSE:",
                      "28th October, 2026 BSE Limited Corporate Relationship Department Scrip Code: 532538",
                      "TCS Share Price Highlights: TCS Stock Price History",
                      "TCS Share Price Live Updates: TCS Stock Update"):
            self.assertTrue(news.is_quote_page(title), title)
        for title in ("Trent shares zoom 7% after Q2 results, brokerages divided on outlook",
                      "Laurus Labs share price at record high after Q2 results",
                      "Noel Tata to retire as chairman of Trent on Nov 11; Bahram Vakil to take over"):
            self.assertFalse(news.is_quote_page(title), title)

    def test_yahoo_items_must_name_the_company(self):
        rss = (b'<rss><channel>'
               b'<item><title>Thoroughbred Championship Series Expands to Include Oaks Division</title>'
               b'<link>https://finance.yahoo.com/a</link><description>Racing news.</description>'
               b'<pubDate>Thu, 29 Oct 2026 20:01:00 GMT</pubDate></item>'
               b'<item><title>TCS wins a deal with a European bank</title>'
               b'<link>https://finance.yahoo.com/b</link><description></description>'
               b'<pubDate>Thu, 29 Oct 2026 21:01:00 GMT</pubDate></item>'
               b'</channel></rss>')

        class One(object):
            offline = None

            def get(self, url, name, **kw):
                if "yahoo" in url:
                    return rss
                raise news.NewsError("HTTP 403 for %s" % url)

        items = news.collect(One(), {"TCS": "Tata Consultancy Services"}, {}, media=False, errors=[])
        self.assertEqual([i["title"] for i in items], ["TCS wins a deal with a European bank"])

    def test_nse_retries_once_on_no_answer_never_on_a_refusal(self):
        calls = []

        class Slow(object):
            offline = None

            def __init__(self, first):
                self.first = first

            def get(self, url, name, ua=None, timeout=30):
                calls.append(timeout)
                if len(calls) == 1:
                    raise news.NewsError(self.first)
                return b"<rss/>"

        self.assertEqual(news.fetch_nse(Slow("no answer from nsearchives.nseindia.com (TimeoutError)"),
                                        "u", "f", wait=0), b"<rss/>")
        self.assertEqual(calls, [30, 60])
        del calls[:]
        with self.assertRaises(news.NewsError):
            news.fetch_nse(Slow("HTTP 403 for u"), "u", "f", wait=0)
        self.assertEqual(calls, [30])


if __name__ == "__main__":
    unittest.main()
