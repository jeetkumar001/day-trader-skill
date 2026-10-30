#!/usr/bin/env python3
"""Congress trade tracker for trader-pelosi. Standard library only.

Reads the House Clerk's public financial disclosure index and the Periodic
Transaction Reports (PTRs) members of the House file when they, their spouse
or a dependent child buy or sell a stock.

    python3 ptr.py index --member pelosi                 # this year's PTRs by a member
    python3 ptr.py index --year 2026 --since 2026-10-01  # every PTR filed since a date
    python3 ptr.py new --member pelosi                   # only what is new since last check
    python3 ptr.py fetch 20035143                        # download one PTR and read it
    python3 ptr.py parse ptr.txt --filed 2026-08-21      # rows from `pdftotext -layout` text

Where the data comes from:
  - The index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/<YEAR>FD.zip
    (a ZIP with <YEAR>FD.xml: one <Member> per filing, FilingType P = PTR).
  - Each PTR: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/<YEAR>/<DocID>.pdf
  - Reading a PDF needs `pdftotext` (poppler) if you want rows from this tool.
    Without it, the tool prints the PDF path and Claude reads the PDF itself.

What a PTR is, and is not:
  - It is a PUBLIC disclosure under the STOCK Act, due within 30 days of the
    member learning of the trade and never later than 45 days after it. So it
    arrives days or weeks after the trade, and amounts are ranges, not prices.
  - Reading them fast is not inside information. There is none here.
  - This covers the House. Senate PTRs are on efdsearch.senate.gov behind an
    agreement you must accept yourself; see SKILL.md for the manual route.

India: there is no STOCK Act for Indian politicians. The closest public record
of who traded big is NSE's daily bulk and block deals:

    python3 ptr.py deals                                 # today's bulk + block deals
    python3 ptr.py deals --tickers BSOFT AEROFLEX         # flag these (and your holdings)
    python3 ptr.py deals --client "SOCIETE GENERALE"      # one buyer or seller
    python3 ptr.py deals --new                           # only deals not shown before

  - https://nsearchives.nseindia.com/content/equities/bulk.csv  (a client trading
    more than 0.5% of a company's shares in one day)
  - https://nsearchives.nseindia.com/content/equities/block.csv (one large trade
    in NSE's separate block-deal window)
  Each file holds the latest trading day only, posted after the close. Many
  clients are trading firms that buy and sell the same day; the tool marks a
  client on both sides. Offline: --offline DIR reads bulk.csv and block.csv.

State: `new` remembers which DocIDs it has shown in trader/state/pelosi.json,
so each PTR alerts once. Offline: --offline FILE reads a saved <YEAR>FD.zip
(or the .xml inside it) instead of the network. The tests use it.
"""

import argparse
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d%s.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/%d/%s.pdf"
FD_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/%d/%s.pdf"
UA = "day-trader-skill/2.0 (+https://github.com/Jakeschincariol/day-trader-skill)"
FILING_TYPES = {
    "P": "Periodic Transaction Report", "O": "Annual report", "A": "Amendment",
    "C": "Candidate report", "X": "Extension", "T": "Termination report",
    "W": "Waiver", "D": "Disclosure (new member or candidate)", "H": "New filer report",
}
TXN_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "partial sale", "E": "exchange"}
OWNERS = {"SP": "spouse", "JT": "joint", "DC": "dependent child", "": "self"}
LATE_DAYS = 45
NSE_DEALS = {"bulk": "https://nsearchives.nseindia.com/content/equities/bulk.csv",
             "block": "https://nsearchives.nseindia.com/content/equities/block.csv"}
CRORE = 1e7


class PtrError(Exception):
    pass


# ---------------------------------------------------------------- the index

def iso(mdy):
    """'8/21/2026' or '08/21/2026' -> '2026-08-21' (two-digit years -> 20xx)."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", mdy or "")
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 2000
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def download(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise PtrError("HTTP %s for %s" % (exc.code, url))
    except urllib.error.URLError as exc:
        raise PtrError("could not reach %s: %s" % (url, exc.reason))


def read_index_bytes(data):
    """Index XML bytes from the ZIP (or the XML itself)."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise PtrError("no XML inside the ZIP (files: %s)" % ", ".join(z.namelist()))
            return z.read(names[0])
    return data


def parse_index(xml_bytes, year):
    """Every filing in a <YEAR>FD.xml as a dict, newest first."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise PtrError("index is not valid XML: %s" % exc)
    out = []
    for m in root.iter("Member"):
        f = {c.tag: (c.text or "").strip() for c in m}
        doc = f.get("DocID", "")
        kind = f.get("FilingType", "")
        y = int(f.get("Year") or year)
        url = (PTR_URL if kind == "P" else FD_URL) % (y, doc)
        name = " ".join(x for x in (f.get("Prefix"), f.get("First"), f.get("Last"),
                                    f.get("Suffix")) if x)
        out.append({"doc_id": doc, "type": kind, "type_name": FILING_TYPES.get(kind, kind),
                    "last": f.get("Last", ""), "first": f.get("First", ""), "name": name,
                    "district": f.get("StateDst", ""), "year": y,
                    "filed": iso(f.get("FilingDate", "")), "url": url})
    out.sort(key=lambda r: (r["filed"] or "", r["doc_id"]), reverse=True)
    return out


def load_index(year, offline=None):
    if offline:
        with open(offline, "rb") as fh:
            data = fh.read()
    else:
        data = download(INDEX_URL % (year, "FD"))
    return parse_index(read_index_bytes(data), year)


def member_match(row, member):
    """'pelosi', 'Nancy Pelosi' or 'Pelosi, Nancy' against an index row."""
    member = (member or "").strip().lower()
    if not member:
        return True
    if "," in member:
        last, first = [x.strip() for x in member.split(",", 1)]
    else:
        parts = member.split()
        last, first = parts[-1], " ".join(parts[:-1])
    if row["last"].lower() != last:
        return False
    return not first or row["first"].lower().startswith(first)


def select(rows, members=None, kind="P", since=None):
    out = []
    for r in rows:
        if kind and r["type"] != kind:
            continue
        if members and not any(member_match(r, m) for m in members):
            continue
        if since and (r["filed"] or "") < since:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- state

def load_state(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"seen": [], "last_check": None}


def save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def new_since(rows, state, today, first_days=LATE_DAYS):
    """Rows not seen before. On the first run: only those filed in the last
    first_days days, so you are not flooded with the whole year."""
    seen = set(state.get("seen", []))
    if state.get("last_check") is None:
        cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
        return [r for r in rows if (r["filed"] or "") >= cutoff]
    return [r for r in rows if r["doc_id"] not in seen]


# ---------------------------------------------------------------- parsing a PTR

TXN = re.compile(
    r"^\s*(?:(?P<owner>SP|JT|DC)\s+)?(?P<asset>.*?)\s+(?P<type>S \(partial\)|P|S|E)\s+"
    r"(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<notified>\d{2}/\d{2}/\d{4})\s+(?P<amount>\S.*?)\s*$")
LABELS = [
    ("status", re.compile(r"^\s*F(?:ILING)?\s+S(?:TATUS)?\s*:\s*(.*)$", re.I)),
    ("description", re.compile(r"^\s*D(?:ESCRIPTION)?\s*:\s*(.*)$", re.I)),
    ("account", re.compile(r"^\s*S(?:UBHOLDING)?\s+O(?:F)?\s*:\s*(.*)$", re.I)),
    ("location", re.compile(r"^\s*L(?:OCATION)?\s*:\s*(.*)$", re.I)),
]
# Column headers repeat at the top of every page, sometimes in the middle of a
# row that continues on the next page: skip them without ending the row.
PAGE_HEADER = re.compile(r"^\s*(ID\s+Owner\s+Asset|Type\s+Date\b|Type$|Date$|Gains >$|\$200\?$)",
                         re.I)
END = re.compile(r"^\s*(\* For the complete|I CERTIFY|Digitally Signed|Filing ID #)", re.I)
AMOUNT = re.compile(r"\$([\d,]+)")
OPTION = re.compile(r"([\d,]+)\s+(call|put)\s+options?\b.*?strike price of \$([\d,.]+)"
                    r".*?expiration date of (\d{1,2}/\d{1,2}/\d{2,4})", re.I | re.S)
SHARES = re.compile(r"(?:purchased|sold|bought)\s+([\d,]+)\s+shares", re.I)


def _amount(text):
    nums = [int(x.replace(",", "")) for x in AMOUNT.findall(text or "")]
    low = nums[0] if nums else None
    high = nums[1] if len(nums) > 1 else None
    return low, high


def _finish(row, filed):
    asset = " ".join(row.pop("_asset").split())
    tick = re.findall(r"\(([A-Z][A-Z0-9.\-/]{0,9})\)", asset)
    code = re.findall(r"\[([A-Z]{2})\]", asset)
    row["ticker"] = tick[-1] if tick else None
    row["asset_type"] = code[-1] if code else None
    name = re.sub(r"\s*\[[A-Z]{2}\]", "", asset)
    if tick:
        name = re.sub(r"\s*\(%s\)" % re.escape(tick[-1]), "", name)
    row["asset"] = name.strip(" -")
    row["amount"] = " ".join(row["amount"].split())
    row["amount_low"], row["amount_high"] = _amount(row["amount"])
    row["description"] = " ".join(row.get("description", "").split()) or None
    opt = OPTION.search(row["description"] or "")
    if opt:
        row["option"] = {"contracts": int(opt.group(1).replace(",", "")),
                         "kind": opt.group(2).lower(),
                         "strike": float(opt.group(3).replace(",", "")),
                         "expires": iso(opt.group(4)),
                         "exercised": bool(re.match(r"\s*exercised\b", row["description"], re.I))}
    sh = SHARES.search(row["description"] or "")
    if sh:
        row["shares"] = int(sh.group(1).replace(",", ""))
    if filed and row["date"]:
        days = (_dt.date.fromisoformat(filed) - _dt.date.fromisoformat(row["date"])).days
        row["days_to_disclose"] = days
        row["late"] = days > LATE_DAYS
    return row


def parse_ptr_text(text, filed=None):
    """Rows from `pdftotext -layout` output of a House PTR.

    Returns {"name", "status", "district", "doc_id", "filed", "rows": [...]}.
    Each row: owner, owner_name, asset, ticker, asset_type, type, type_name,
    date, notified, amount, amount_low, amount_high, status, description,
    account, option {contracts, kind, strike, expires}, shares,
    days_to_disclose, late."""
    head = {"name": None, "status": None, "district": None, "doc_id": None, "filed": filed}
    rows, cur, mode = [], None, None
    amount_open = False
    for raw in text.splitlines():
        line = raw.rstrip()
        s = line.strip()
        m = re.match(r"^Name:\s+(.*)$", s)
        if m and not head["name"]:
            head["name"] = m.group(1).strip()
            continue
        m = re.match(r"^Status:\s+(.*)$", s)
        if m and not head["status"]:
            head["status"] = m.group(1).strip()
            continue
        m = re.match(r"^State/District:\s*(\S+)", s)
        if m:
            head["district"] = m.group(1)
            continue
        m = re.search(r"Filing ID #(\d+)", s)
        if m:
            head["doc_id"] = m.group(1)
            continue
        m = re.match(r"^Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})", s)
        if m:
            head["filed"] = head["filed"] or iso(m.group(1))
            continue
        t = TXN.match(line)
        if t and iso(t.group("date")):
            if cur:
                rows.append(cur)
            owner = t.group("owner") or ""
            cur = {"owner": owner, "owner_name": OWNERS.get(owner, owner),
                   "_asset": t.group("asset"), "type": t.group("type"),
                   "type_name": TXN_TYPES.get(t.group("type"), t.group("type")),
                   "date": iso(t.group("date")), "notified": iso(t.group("notified")),
                   "amount": t.group("amount"), "status": None, "account": None}
            amount_open = cur["amount"].endswith("-")
            mode = "asset"
            continue
        if cur is None:
            continue
        if not s:
            if mode in ("description", "asset"):
                mode = "labels" if mode == "description" or "[" in cur["_asset"] else mode
            continue
        if PAGE_HEADER.match(s):
            continue
        if END.match(s):
            mode = "labels"
            continue
        label = next(((k, rx.match(line)) for k, rx in LABELS if rx.match(line)), None)
        if label:
            key, lm = label
            value = lm.group(1).strip()
            if key == "description":
                cur["description"] = value
                mode = "description"
            elif key in ("status", "account"):
                cur[key] = value
                mode = "labels"
            else:
                mode = "labels"
            continue
        if mode == "asset":
            parts = re.split(r"\s{2,}", s)
            if amount_open and re.fullmatch(r"\$[\d,]+", parts[-1]):
                cur["amount"] += " " + parts.pop()
                amount_open = False
            if parts and parts[0]:
                cur["_asset"] += " " + " ".join(parts)
        elif mode == "description":
            cur["description"] = cur.get("description", "") + " " + s
    if cur:
        rows.append(cur)
    head["rows"] = [_finish(r, head["filed"]) for r in rows]
    return head


def pdf_to_text(pdf_path):
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-layout", pdf_path, "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


# ---------------------------------------------------------------- output

def watchlist(args):
    tickers = {t.upper() for t in (args.tickers or [])}
    path = getattr(args, "portfolio", None)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for p in json.load(fh).get("positions", []):
                if p.get("ticker"):
                    tickers.add(p["ticker"].upper())
    return tickers


def describe(row):
    bits = ["%s %s" % (row["owner_name"], row["type_name"])]
    what = row["asset"]
    if row.get("ticker"):
        what += " (%s)" % row["ticker"]
    if row.get("option"):
        o = row["option"]
        what += ": %s%d %s options, $%g strike, expires %s" % (
            "exercised " if o.get("exercised") else "", o["contracts"], o["kind"],
            o["strike"], o["expires"])
    elif row.get("shares"):
        what += ": %s shares" % "{:,}".format(row["shares"])
    bits.append(what)
    bits.append(row["amount"])
    when = "traded %s" % row["date"]
    if row.get("days_to_disclose") is not None:
        when += ", disclosed %d days later%s" % (row["days_to_disclose"],
                                                   " (LATE: over 45)" if row.get("late") else "")
    bits.append(when)
    if row.get("account"):
        bits.append("account: %s" % row["account"])
    return ", ".join(bits)


def render_report(report, watch=()):
    out = ["%s (%s), %s. PTR %s, filed %s." % (
        report.get("name") or "?", report.get("district") or "?", report.get("status") or "?",
        report.get("doc_id") or "?", report.get("filed") or "?")]
    for r in report["rows"]:
        flag = "  <- %s is on your list" % r["ticker"] if r.get("ticker") in watch else ""
        out.append("  - %s%s" % (describe(r), flag))
        if r.get("description") and not r.get("option") and not r.get("shares"):
            out.append("      %s" % r["description"])
    if not report["rows"]:
        out.append("  no transaction rows found: read the PDF directly")
    out.append("Public disclosure under the STOCK Act, filed up to 45 days after the trade. "
               "Amounts are ranges. Not inside information.")
    return "\n".join(out) + "\n"


def render_index(rows):
    out = []
    for r in rows:
        out.append("%s  %-28s %-5s %-4s doc %-9s %s" % (
            r["filed"], r["name"][:28], r["district"], r["type"], r["doc_id"], r["url"]))
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------- NSE bulk and block deals (India)

def _col(head, *starts):
    for i, h in enumerate(head):
        if any(h.startswith(s) for s in starts):
            return i
    return None


def parse_deals(text, kind):
    """NSE bulk.csv / block.csv -> rows. "NO RECORDS" (a quiet day) -> []."""
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().lower() for h in next(reader, [])]
    ix = {"date": _col(head, "date"), "symbol": _col(head, "symbol"), "name": _col(head, "security"),
          "client": _col(head, "client"), "side": _col(head, "buy"), "qty": _col(head, "quantity"),
          "price": _col(head, "trade price", "price")}
    if ix["symbol"] is None or ix["qty"] is None:
        raise PtrError("the %s deals file has no Symbol / Quantity columns: is it NSE's %s.csv?" % (kind, kind))
    out = []
    for r in reader:
        if not r or not any(x.strip() for x in r) or r[0].strip().upper().startswith("NO RECORDS"):
            continue

        def g(k):
            i = ix[k]
            return r[i].strip() if i is not None and i < len(r) else ""
        try:
            qty = int(float(g("qty").replace(",", "")))
            price = float(g("price").replace(",", ""))
        except ValueError:
            continue
        try:
            day = _dt.datetime.strptime(g("date").title(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            day = g("date")
        row = {"kind": kind, "date": day, "symbol": g("symbol").upper(), "security": g("name"),
               "client": g("client"), "side": g("side").upper(), "qty": qty, "price": price,
               "value_crore": round(qty * price / CRORE, 2)}
        row["id"] = hashlib.sha1("|".join(str(row[k]) for k in ("kind", "date", "symbol", "client", "side",
                                                                "qty", "price")).encode("utf-8")).hexdigest()[:16]
        out.append(row)
    both = {}
    for r in out:
        both.setdefault((r["symbol"], r["client"]), set()).add(r["side"])
    for r in out:
        r["both_sides"] = len(both[(r["symbol"], r["client"])]) > 1
    return out


def load_deals(kinds, offline=None):
    rows, notes = [], []
    for kind in kinds:
        if offline:
            path = os.path.join(offline, "%s.csv" % kind)
            if not os.path.isfile(path):
                raise PtrError("offline: no %s" % path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            try:
                text = download(NSE_DEALS[kind]).decode("utf-8", "replace")
            except PtrError as exc:
                notes.append("%s deals: %s. Open %s in your browser, save it, and pass --offline DIR"
                             % (kind, exc, NSE_DEALS[kind]))
                continue
        rows += parse_deals(text, kind)
    return rows, notes


def deal_watch(args):
    """Your tickers: --tickers, the portfolio, the scanner's watchlist and the plan."""
    tickers = watchlist(args)
    for path, pick in ((args.watchlist, lambda d: [h.get("symbol") for h in d.get("hits", [])]),
                       (args.plan, lambda d: [d.get("symbol")])):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    tickers.update(str(t).upper() for t in pick(json.load(fh)) if t)
            except (OSError, ValueError, AttributeError):
                pass
    return tickers


def render_deals(rows, watch=(), notes=()):
    out = []
    for r in rows:
        flags = []
        if r["symbol"] in watch:
            flags.append("<- %s is on your list" % r["symbol"])
        if r["both_sides"]:
            flags.append("(same client bought and sold)")
        out.append("%s  %-5s %-12s %-4s %12s sh @ \u20b9%s = \u20b9%s Cr  %s  %s" % (
            r["date"], r["kind"].upper(), r["symbol"], r["side"], "{:,}".format(r["qty"]),
            "{:,.2f}".format(r["price"]), "{:,.2f}".format(r["value_crore"]), r["client"][:44],
            "  ".join(flags)))
    for n in notes:
        out.append("note: %s" % n)
    out.append("NSE bulk deals (one client over 0.5% of a company's shares in a day) and block deals "
               "(one large trade in the block window), published after the close. A BUY by a trading "
               "firm is often sold again the same day. Public data, not inside information.")
    return "\n".join(out) + "\n"


def _deals(args):
    kinds = ["bulk", "block"] if args.kind == "both" else [args.kind]
    rows, notes = load_deals(kinds, args.offline)
    watch = deal_watch(args)
    if args.symbol:
        want = {x.upper() for x in args.symbol}
        rows = [r for r in rows if r["symbol"] in want]
    if args.client:
        rows = [r for r in rows if any(c.lower() in r["client"].lower() for c in args.client)]
    if args.mine:
        rows = [r for r in rows if r["symbol"] in watch]
    if args.min_crore:
        rows = [r for r in rows if r["value_crore"] >= args.min_crore]
    state = load_state(args.state) if args.new else None
    if args.new:
        seen = set(state.get("deals_seen", []))
        all_ids = [r["id"] for r in rows]
        rows = [r for r in rows if r["id"] not in seen]
    rows.sort(key=lambda r: (r["symbol"] not in watch, -r["value_crore"]))
    shown = rows[:args.limit] if args.limit else rows
    if args.json:
        print(json.dumps({"deals": shown, "watch": sorted(watch), "notes": notes}, indent=2))
    else:
        if not shown:
            print("No %sdeals%s." % ("new " if args.new else "", " matching" if (args.symbol or args.client
                                                                                 or args.mine) else ""))
        sys.stdout.write(render_deals(shown, watch, notes))
        if len(rows) > len(shown):
            print("%d more not shown (--limit 0 shows all)." % (len(rows) - len(shown)))
    if args.new and not args.dry_run:
        state["deals_seen"] = sorted(set(state.get("deals_seen", [])) | set(all_ids))[-5000:]
        state["deals_last_check"] = _dt.datetime.now().replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if shown else 1


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    year = _dt.date.today().year

    def common(p):
        p.add_argument("--year", type=int, default=year)
        p.add_argument("--member", action="append",
                       help="last name or 'First Last'; repeat for more members")
        p.add_argument("--type", default="P", help="FilingType (default P = PTR; '' for all)")
        p.add_argument("--since", help="only filings on or after YYYY-MM-DD")
        p.add_argument("--offline", metavar="FILE", help="a saved <YEAR>FD.zip or .xml")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("index", help="list filings from the yearly index")
    common(p)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("new", help="filings not seen at the last check")
    common(p)
    p.add_argument("--state", default="trader/state/pelosi.json")
    p.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    p.add_argument("--read", action="store_true",
                   help="also download and read each new PTR (needs pdftotext)")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*", help="flag trades in these tickers")
    p.add_argument("--portfolio", default="trader/portfolio.json")

    p = sub.add_parser("fetch", help="download one PTR PDF and read it")
    p.add_argument("doc_id")
    p.add_argument("--year", type=int, default=year)
    p.add_argument("--filed", help="filing date YYYY-MM-DD if known")
    p.add_argument("--pdf-dir", default="trader/state/ptr-pdfs")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("parse", help="rows from pdftotext -layout text of a PTR")
    p.add_argument("text_file")
    p.add_argument("--filed", help="filing date YYYY-MM-DD (else read from the signature)")
    p.add_argument("--tickers", nargs="*")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("deals", help="India: NSE bulk and block deals (who traded big)")
    p.add_argument("--kind", choices=("bulk", "block", "both"), default="both")
    p.add_argument("--tickers", nargs="*", help="flag deals in these NSE symbols")
    p.add_argument("--symbol", nargs="*", help="only these symbols")
    p.add_argument("--client", action="append", help="only clients whose name contains this; repeat")
    p.add_argument("--mine", action="store_true", help="only deals in your tickers")
    p.add_argument("--min-crore", type=float, help="only deals worth at least this many rupee crore")
    p.add_argument("--new", action="store_true", help="only deals not shown before")
    p.add_argument("--state", default="trader/state/pelosi.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--offline", metavar="DIR", help="a folder with saved bulk.csv and block.csv")
    p.add_argument("--json", action="store_true")
    return ap


def _fetch_and_read(doc_id, year, pdf_dir, filed=None):
    os.makedirs(pdf_dir, exist_ok=True)
    path = os.path.join(pdf_dir, "%s.pdf" % doc_id)
    if not os.path.isfile(path):
        data = download(PTR_URL % (year, doc_id))
        if not data.startswith(b"%PDF"):
            raise PtrError("doc %s did not come back as a PDF" % doc_id)
        with open(path, "wb") as fh:
            fh.write(data)
    text = pdf_to_text(path)
    if text is None:
        return path, None
    report = parse_ptr_text(text, filed=filed)
    report["pdf"] = path
    report["url"] = PTR_URL % (year, doc_id)
    return path, report


def main(argv=None):
    args = _parser().parse_args(argv)
    if not args.cmd:
        _parser().print_help()
        return 2
    try:
        return _run(args)
    except (PtrError, OSError) as exc:
        print("ptr: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    if args.cmd == "deals":
        return _deals(args)
    if args.cmd == "parse":
        with open(args.text_file, encoding="utf-8", errors="replace") as fh:
            report = parse_ptr_text(fh.read(), filed=args.filed)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    if args.cmd == "fetch":
        path, report = _fetch_and_read(args.doc_id, args.year, args.pdf_dir, args.filed)
        if report is None:
            print("Saved %s. pdftotext is not installed, so read the PDF directly "
                  "(Claude can open it), or install poppler (brew install poppler)." % path)
            return 0
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_report(report, watchlist(args)))
        return 0 if report["rows"] else 1

    rows = load_index(args.year, args.offline)
    if args.cmd == "new" and not args.offline:
        today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
        if today.month == 1 and args.year == today.year:
            try:
                rows += load_index(args.year - 1)
            except PtrError:
                pass
    picked = select(rows, args.member, args.type or None, args.since)

    if args.cmd == "index":
        picked = picked[:args.limit]
        if args.json:
            print(json.dumps(picked, indent=2))
        else:
            sys.stdout.write(render_index(picked))
            if not picked:
                print("No matching filings in the %d index." % args.year)
        return 0 if picked else 1

    # new
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    state = load_state(args.state)
    fresh = new_since(picked, state, today)
    watch = watchlist(args)
    reports = []
    if args.read:
        for r in fresh:
            if r["type"] != "P":
                continue
            path, report = _fetch_and_read(r["doc_id"], r["year"], args.pdf_dir, r["filed"])
            reports.append({"filing": r, "pdf": path, "report": report})
    if args.json:
        print(json.dumps({"new": fresh, "reports": reports}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        for r in fresh:
            print("NEW %s: %s (%s) filed %s, doc %s" % (
                r["type_name"], r["name"], r["district"], r["filed"], r["doc_id"]))
            print("    %s" % r["url"])
        for rep in reports:
            if rep["report"] is None:
                print("    read %s (pdftotext not installed)" % rep["pdf"])
            else:
                sys.stdout.write(render_report(rep["report"], watch))
    if not args.dry_run:
        seen = set(state.get("seen", []))
        seen.update(r["doc_id"] for r in picked)
        state["seen"] = sorted(seen)
        state["last_check"] = _dt.datetime.now().replace(microsecond=0).isoformat()
        save_state(args.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
