"""
Take 5 hourly AutoPoll scraper.

Each run: check store hours (Central) -> if any store open, log in to AutoPoll
-> pull each store's Current Sales Report -> parse -> save raw+parsed locally
-> upsert to Supabase. Per-store isolation, retries, session re-login, and a
health check so silent breakage is caught.

Config via environment variables (see .env.example):
  AUTOPOLL_USER, AUTOPOLL_PASS   AutoPoll login
  STORES                          comma-separated store numbers, e.g. "1512,1515"
  SUPABASE_URL, SUPABASE_KEY      optional; if unset, only local files are written
  OUTPUT_DIR                      where raw/parsed files go (default "Hourly Data Pull")
  FORCE_RUN=1                     bypass the open-hours gate (for manual/testing)
"""
import os, re, sys, json, time, datetime as dt
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

import autopoll_parser as ap

CENTRAL = ZoneInfo("America/Chicago")
BASE = "https://autopoll.drivenbrands.com"
LOGIN_URL = f"{BASE}/Account/LogOn"
REPORT_URL = f"{BASE}/MainMenu/DisplaySalesReport/csr{{store}}"

# ---------------------------------------------------------------------------
# The 15 stores. THIS is the single source of truth for which stores we pull.
# Override at runtime with the STORES env var (comma-separated) for testing or
# a smaller pilot; if STORES is unset we pull all 15. Do not hardcode a single
# store anywhere else in the flow.
# ---------------------------------------------------------------------------
ALL_STORES = [
    "1503", "1504", "1505", "1506", "1507", "1508", "1509", "1511",
    "1512", "1513", "1515", "1516", "1517", "1521", "1522",
]


def get_stores():
    env = os.environ.get("STORES", "").strip()
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    return list(ALL_STORES)

# Store open hours in Central time, keyed by Python weekday (Mon=0 .. Sun=6).
# (open_hour, close_hour) 24h. We pull on any hour where open <= hour <= close.
HOURS = {
    0: (6, 20), 1: (6, 20), 2: (6, 20), 3: (6, 20), 4: (6, 20),  # Mon-Fri 6a-8p
    5: (6, 18),   # Sat 6a-6p
    6: (9, 17),   # Sun 9a-5p
}


def is_open_now(now=None):
    now = now or dt.datetime.now(CENTRAL)
    open_h, close_h = HOURS[now.weekday()]
    return open_h <= now.hour <= close_h


def login(user, password):
    """Return an authenticated requests.Session, or raise."""
    s = requests.Session()
    s.headers["User-Agent"] = "Take5-Scorecard/1.0"
    # ASP.NET MVC forms login: standard field names are UserName / Password.
    resp = s.post(LOGIN_URL, data={
        "UserName": user, "Password": password, "RememberMe": "true",
    }, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    if "/Account/LogOn" in resp.url or "Log On" in resp.text[:2000]:
        raise RuntimeError("login failed - still on LogOn page (check credentials/field names)")
    return s


def fetch_report_text(session, store):
    """Fetch a store's report and return clean text for the parser.

    AutoPoll serves the report as: a small header <table> (date/store/city in
    cells RepDate1 / StoreNum1 / StoreCity1) followed by a monospace <pre> that
    holds the columnar body. We rebuild a single clean header line + the full
    page text so the parser (built for the plain-text layout) works unchanged.
    """
    url = REPORT_URL.format(store=store)
    # Cache-buster: a unique query param + no-cache headers force a fresh report each pull.
    # Guards against AutoPoll / a CDN serving a stale cached snapshot for a store (seen when
    # one store's report_timestamp stayed frozen while every other store advanced).
    r = session.get(url, timeout=30, params={"_": int(time.time())},
                    headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    r.raise_for_status()
    if "/Account/LogOn" in r.url:
        raise PermissionError("session expired")
    soup = BeautifulSoup(r.text, "html.parser")

    # Sanity: this must actually be the report page.
    if not soup.find(id="P1") and not re.search(r"CURRENT\s+SALES\s+REPORT", r.text, re.I):
        raise ValueError("report body not found (page layout may have changed)")

    def cell(cid):
        el = soup.find(id=cid)
        return el.get_text(" ", strip=True) if el else ""

    header = f"{cell('RepDate1')} {cell('StoreNum1')} {cell('StoreCity1')} Page 1"
    body = soup.get_text("\n")
    return f"CURRENT SALES REPORT\n{header}\n{body}"


def save_local(out_dir, store, data, raw_text, now):
    date = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H%M")
    raw_dir = os.path.join(out_dir, "raw", date)
    parsed_dir = os.path.join(out_dir, "parsed", date)
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(parsed_dir, exist_ok=True)
    with open(os.path.join(raw_dir, f"{store}_{hhmm}.txt"), "w") as f:
        f.write(raw_text)
    with open(os.path.join(parsed_dir, f"{store}_{hhmm}.json"), "w") as f:
        json.dump(data, f, indent=2)


def upsert_supabase(url, key, data, now):
    """Upsert one row keyed on (store_number, pull_hour) so re-runs replace."""
    row = {
        "store_number": data.get("store_number"),
        "store_name": data.get("store_name"),
        "report_timestamp": data.get("report_timestamp"),
        "pull_time": now.isoformat(),
        "pull_hour": now.strftime("%Y-%m-%d-%H"),
        "cars": data.get("cars"),
        "net_sales": data.get("net_sales"),
        "gross_sales": data.get("gross_sales"),
        "total_receipts": data.get("total_receipts"),
        "materials_pct": data.get("materials_pct"),
        "asa": data.get("asa"),
        "coupons": data.get("coupons"),
        "discounts": data.get("discounts"),
        "new_customers": data.get("new_customers"),
        "repeat_customers": data.get("repeat_customers"),
        "big4_total_units": data.get("big4_total_units"),
        "big4_total_amount": data.get("big4_total_amount"),
        "big4": data.get("big4"),
        "line_items": data.get("line_items"),
        "data": data,  # full parsed payload as jsonb
    }
    endpoint = f"{url}/rest/v1/daily_sales_pull?on_conflict=store_number,pull_hour"
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(endpoint, headers=headers, data=json.dumps([row]), timeout=30)
    resp.raise_for_status()


# How far behind "now" a report's own timestamp may be before we treat the store's
# feed as frozen/stale (see report_datetime + the freshness guard in pull_store).
STALE_REPORT_MAX = dt.timedelta(hours=2)


def report_datetime(data):
    """The full date+time the REPORT itself is stamped with (naive Central), parsed from
    report_timestamp (e.g. '7/16/2026  07:10 AM'). Used for both the wrong-day guard and
    the frozen-feed freshness guard."""
    ts = (data.get("report_timestamp") or "").strip()
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])", ts)
    if not m:
        return None
    mo, da, yr, hh, mm, ap = m.groups()
    hh = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
    try:
        return dt.datetime(int(yr), int(mo), int(da), hh, int(mm))
    except ValueError:
        return None


def report_date(data):
    """The calendar date the REPORT itself is for. Catches a store whose AutoPoll hasn't
    rolled over to today yet and is still serving yesterday's final report."""
    rdt = report_datetime(data)
    if rdt is not None:
        return rdt.date()
    ts = (data.get("report_timestamp") or "").strip()
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", ts)
    if not m:
        return None
    mo, da, yr = (int(x) for x in m.groups())
    try:
        return dt.date(yr, mo, da)
    except ValueError:
        return None


def pull_store(session, store, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            text = fetch_report_text(session, store)
            data = ap.parse_report(text)
            problems = ap.validate(data)
            if problems:
                raise ValueError(f"validation: {problems}")
            # Non-fatal footing check: log a report that doesn't reconcile to its Sub Total,
            # but SAVE it anyway (cars/net/big4 don't depend on that sum). A transient gap
            # here used to reject the store outright and black-hole its whole day (1515,
            # 2026-08-04).
            note = ap.reconcile_note(data)
            if note:
                print(f"  [store {store}] NOTE (non-fatal): {note} — saved anyway")
            # Reject a stale report: if the report's own date isn't today (Central),
            # AutoPoll hasn't rolled this store to the new business day yet and is still
            # showing yesterday's totals. Saving it would poison today's early hour with
            # yesterday's cumulative (a big spike + a negative correction next hour). Skip
            # this store this cycle; the next pull (once AutoPoll rolls over) captures it.
            rd = report_date(data)
            now = dt.datetime.now(CENTRAL)
            if rd is not None and rd != now.date():
                raise ValueError(f"stale report dated {rd} (expected {now.date()}); "
                                 f"AutoPoll not rolled to today yet")
            # Frozen-feed guard: during open hours a live report's own timestamp advances
            # every pull. If it lags "now" by more than STALE_REPORT_MAX, this store's feed
            # is stuck (AutoPoll served a frozen/cached snapshot) — skip it rather than store
            # a misleading 0-car row. Logged in the run summary; captured once the feed moves.
            rdt = report_datetime(data)
            if rdt is not None:
                lag = now.replace(tzinfo=None) - rdt
                if lag > STALE_REPORT_MAX:
                    raise ValueError(f"stale/frozen feed: report is {int(lag.total_seconds()//60)} min "
                                     f"old (as of {data.get('report_timestamp')}); feed not advancing")
            return data, text
        except Exception as e:
            last = e
            print(f"  [store {store}] attempt {attempt} failed: {e}")
            time.sleep(2 * attempt)
    raise last


def main():
    user = os.environ.get("AUTOPOLL_USER")
    password = os.environ.get("AUTOPOLL_PASS")
    stores = get_stores()
    out_dir = os.environ.get("OUTPUT_DIR", "Hourly Data Pull")
    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = os.environ.get("SUPABASE_KEY")
    force = os.environ.get("FORCE_RUN") == "1"

    now = dt.datetime.now(CENTRAL)
    if not force and not is_open_now(now):
        print(f"{now:%Y-%m-%d %H:%M %Z}: stores closed, nothing to do.")
        return 0
    if not user or not password:
        print("ERROR: AUTOPOLL_USER / AUTOPOLL_PASS not set.")
        return 1

    print(f"{now:%Y-%m-%d %H:%M %Z}: pulling stores {stores}")
    session = login(user, password)

    # One bad store must never kill the run: every store is fetched, parsed,
    # validated and upserted inside its own try/except. failed[] records the
    # reason per store for the end-of-run summary.
    ok, failed = [], {}
    for store in stores:
        try:
            try:
                data, text = pull_store(session, store)
            except PermissionError:
                print("  session expired, re-logging in...")
                session = login(user, password)
                data, text = pull_store(session, store)
            save_local(out_dir, store, data, text, now)
            if sb_url and sb_key:
                upsert_supabase(sb_url, sb_key, data, now)
        except Exception as e:
            print(f"  [store {store}] GAVE UP: {e}")
            failed[store] = str(e)
            continue
        ok.append(store)
        print(f"  [store {store}] OK - {data.get('store_name')} "
              f"cars={data.get('cars')} net=${data.get('net_sales')}")

    n_att, n_ok, n_fail = len(stores), len(ok), len(failed)
    fail_list = "; ".join(f"{s} ({why})" for s, why in failed.items()) or "-"
    print("\n===== RUN SUMMARY =====")
    print(f"  stores attempted : {n_att}")
    print(f"  succeeded ({n_ok}) : {', '.join(ok) or '-'}")
    print(f"  failed ({n_fail})   : " + fail_list)

    # --- Alerting (added 2026-08-03) --------------------------------------
    # Make failures PUSH, not just buried in the log. Three layers:
    #   1. a GitHub error annotation per failed store (shows on the run + commit),
    #   2. a markdown RUN SUMMARY on the Actions run page ($GITHUB_STEP_SUMMARY),
    #   3. a NON-ZERO exit when the run is UNHEALTHY -> GitHub's built-in
    #      "workflow failed" email goes to the repo owner (no extra secrets).
    # "Unhealthy" = zero stores succeeded, OR more than ALERT_MAX_FAIL stores
    # failed (repo/Actions variable, default 3). This closes the PARTIAL-failure
    # gap: previously a run stayed green as long as ANY store succeeded, so a
    # handful of stores could go dark unnoticed. A small number of flaky stores
    # stays green but is annotated as a warning so it's still visible.
    # Tuning: raise ALERT_MAX_FAIL (e.g. to 5) if slow AutoPoll day-rollover
    # makes early-morning stale rejections noisy.
    try:
        max_fail = int(os.environ.get("ALERT_MAX_FAIL", "3"))
    except ValueError:
        max_fail = 3
    unhealthy = (n_ok == 0) or (n_fail > max_fail)
    for s, why in failed.items():
        print(f"::error title=Store {s} pull failed::{why}")
    if failed and not unhealthy:
        print(f"::warning title=Partial AutoPoll pull::{n_fail} store(s) failed: {fail_list}")
    summ_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ_path:
        status = "❌ UNHEALTHY" if unhealthy else ("⚠️ partial" if failed else "✅ healthy")
        try:
            with open(summ_path, "a", encoding="utf-8") as fh:
                fh.write(f"## AutoPoll hourly pull — {status}\n\n")
                fh.write(f"- **{now:%Y-%m-%d %H:%M %Z}**\n")
                fh.write(f"- Attempted **{n_att}** · succeeded **{n_ok}** · failed **{n_fail}**\n")
                if failed:
                    fh.write(f"- Failed stores: {fail_list}\n")
        except OSError:
            pass
    # Non-zero exit surfaces as a failed Actions run -> owner is emailed.
    return 0 if not unhealthy else 2


if __name__ == "__main__":
    sys.exit(main())
