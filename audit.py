"""lp-audit: speed audit for landing pages.

  audit prelaunch <url> [--runs 3] [--no-gtmetrix] [--no-ai]
  audit history <domain>
  audit clean <domain> | --older-than DAYS
  audit            (no args) -> self-check, prints 'demo ok'

Lab tests (Lighthouse), a real-visitor test (Chrome waits, scrolls, records),
server checks, one GTmetrix run, fixed rules from rules.json, and a short
report. Ollama only writes the summary; the rules decide what is broken.
"""
import argparse, datetime, gzip, html.parser, json, os, re, shutil, sqlite3
import statistics, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request, zipfile
from pathlib import Path

DATA = Path(os.environ.get("REPORTS_DIR", "/data/reports"))
RULES = Path(__file__).with_name("rules.json")
RAW_KEEP_DAYS = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
CHROME_FLAGS = "--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu"


# ---------- small helpers ----------

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_raw(folder, name, obj):
    (folder / "raw").mkdir(parents=True, exist_ok=True)
    (folder / "raw" / f"{name}.json.gz").write_bytes(gzip.compress(json.dumps(obj).encode()))


def chrome_path():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


# ---------- 1. Lighthouse ----------

def lighthouse(url, preset, chrome):
    out = Path(tempfile.mkstemp(suffix=".json")[1])
    cmd = ["lighthouse", url, "--output=json", f"--output-path={out}", "--quiet",
           "--only-categories=performance", f"--chrome-flags={CHROME_FLAGS}"]
    if preset == "desktop":
        cmd.append("--preset=desktop")
    subprocess.run(cmd, env={**os.environ, "CHROME_PATH": chrome}, timeout=240,
                   capture_output=True)
    try:
        return json.loads(out.read_text())
    except (OSError, ValueError):
        return None
    finally:
        out.unlink(missing_ok=True)


def psi(url, strategy, key):
    """Same Lighthouse test, run by Google in the USA (closer to our ad visitors). Free with an API key."""
    q = urllib.parse.urlencode({"url": url, "strategy": strategy, "category": "performance", "key": key})
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}", timeout=180) as r:
            return json.loads(r.read())["lighthouseResult"]
    except Exception as e:
        log(f"PageSpeed API failed ({e}); using local Lighthouse for this run")
        return None


def lh_metrics(lhr):
    """Numbers we care about from one Lighthouse result."""
    a = lhr["audits"]
    num = lambda k: a.get(k, {}).get("numericValue")
    score = lhr["categories"]["performance"]["score"]
    return {
        "score": round(score * 100) if score is not None else None,
        "fcp_ms": num("first-contentful-paint"), "lcp_ms": num("largest-contentful-paint"),
        "tbt_ms": num("total-blocking-time"), "cls": num("cumulative-layout-shift"),
        "si_ms": num("speed-index"),
        "bytes_kb": (num("total-byte-weight") or 0) / 1024 or None,
        "ttfb_ms": num("server-response-time") or num("document-latency-insight"),
    }


def lh_issues(lhr):
    """Failing Lighthouse audits/insights, worst first."""
    metrics = {"first-contentful-paint", "largest-contentful-paint", "total-blocking-time", "speed-index",
               "cumulative-layout-shift", "interactive", "max-potential-fid", "first-meaningful-paint"}
    out = []
    for k, au in lhr["audits"].items():
        if k in metrics or au.get("score") is None or au["score"] >= 0.9:
            continue
        if au.get("scoreDisplayMode") not in ("numeric", "binary", "metricSavings"):
            continue
        sav = sum((au.get("metricSavings") or {}).values())
        out.append({"id": k, "title": au["title"], "value": au.get("displayValue", ""), "savings": sav})
    return sorted(out, key=lambda x: -x["savings"])


# ---------- 2. Real-visitor test ----------

OBSERVE_JS = r"""
window.__lp = {shifts: [], long: [], lcp: 0};
const desc = n => !n || !n.tagName ? '' : n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') +
  (typeof n.className === 'string' && n.className.trim() ? '.' + n.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
const parentDesc = n => { const p = []; for (let i = 0; n && n.tagName && i < 3; i++, n = n.parentElement) p.push(desc(n)); return p.reverse().join(' > '); };
new PerformanceObserver(l => { for (const e of l.getEntries()) { if (e.hadRecentInput) continue;
  const s = (e.sources || []).map(x => parentDesc(x.node)).filter(Boolean);
  window.__lp.shifts.push({t: Math.round(e.startTime), v: e.value, el: s[0] || '?'}); } })
  .observe({type: 'layout-shift', buffered: true});
new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lp.long.push({t: Math.round(e.startTime), d: Math.round(e.duration)}); })
  .observe({type: 'longtask', buffered: true});
new PerformanceObserver(l => { const es = l.getEntries(), e = es[es.length - 1]; window.__lp.lcp = Math.round(e.startTime);
  window.__lp.lcpEl = desc(e.element); }).observe({type: 'largest-contentful-paint', buffered: true});
"""

DEVICES = {
    "mobile": dict(viewport={"width": 412, "height": 823}, device_scale_factor=2.6, is_mobile=True,
                   has_touch=True, user_agent=UA.replace("Windows NT 10.0; Win64; x64", "Linux; Android 14; Pixel 8") + " Mobile"),
    "desktop": dict(viewport={"width": 1350, "height": 940}, user_agent=UA),
}


def cls_windows(shifts):
    """Google's CLS: worst 'session window' (gap < 1 s, window <= 5 s)."""
    best = cur = 0.0
    start = prev = None
    for s in sorted(shifts, key=lambda s: s["t"]):
        if prev is None or s["t"] - prev > 1000 or s["t"] - start > 5000:
            cur, start = 0.0, s["t"]
        cur += s["v"]
        prev = s["t"]
        best = max(best, cur)
    return round(best, 3)


def visitor_test(url, device, wait_s=15):
    """Open the page like a visitor: wait, scroll to the bottom, record what happens."""
    from playwright.sync_api import sync_playwright
    host = urllib.parse.urlparse(url).hostname
    reqs = []
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(**DEVICES[device])
        page = ctx.new_page()
        if device == "mobile":  # slow phone CPU, like Lighthouse mobile
            ctx.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": 4})
        page.add_init_script(OBSERVE_JS)
        t0 = time.time()
        page.on("request", lambda r: reqs.append((round((time.time() - t0) * 1000), r.url, r.resource_type)))
        page.goto(url, wait_until="load", timeout=60000)
        load_ms = round((time.time() - t0) * 1000)
        page.wait_for_timeout(wait_s * 1000)
        scroll_ms = round((time.time() - t0) * 1000)
        h = page.evaluate("document.body.scrollHeight")
        for _ in range(0, h, 400):  # scroll like a person
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(250)
        page.wait_for_timeout(3000)
        lp = page.evaluate("window.__lp")
        b.close()
    tasks_after_load = [t for t in lp["long"] if t["t"] > load_ms]
    late = [{"t_ms": t, "url": u[:150], "type": k} for t, u, k in reqs
            if t > load_ms and urllib.parse.urlparse(u).hostname not in (host, None) and k in ("script", "xhr", "fetch", "document")]
    by_el = {}  # same element shifting many times = one line
    for s in lp["shifts"]:
        e = by_el.setdefault(s["el"], {"el": s["el"], "v": 0, "t": s["t"], "count": 0})
        e["v"], e["count"], e["t"] = e["v"] + s["v"], e["count"] + 1, min(e["t"], s["t"])
    before_scroll = [t for t, _, _ in reqs if t < scroll_ms]  # scrolling loads lazy images; not counted
    return {
        "cls": cls_windows(lp["shifts"]), "shifts": sorted(by_el.values(), key=lambda s: -s["v"])[:10],
        "lcp_ms": lp["lcp"], "lcp_el": lp.get("lcpEl"), "load_ms": load_ms,
        "blocking_ms": sum(max(0, t["d"] - 50) for t in lp["long"]),
        "blocking_after_load_ms": sum(max(0, t["d"] - 50) for t in tasks_after_load),
        "quiet_s": round(max(before_scroll) / 1000, 1) if before_scroll else None,
        "requests": len(reqs), "late_requests": late[:25],
    }


# ---------- 3. Server + HTML checks ----------

class _Html(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.ext, self.img_no_size, self.classes, self.form_token = set(), 0, set(), False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.classes.update((a.get("class") or "").split())
        src = a.get("src") if tag in ("script", "img", "iframe") else a.get("href") if tag == "link" else None
        if src and src.startswith(("http", "//")):
            self.ext.add(urllib.parse.urlparse(src if src.startswith("http") else "https:" + src).hostname)
        if tag == "img" and not (a.get("width") and a.get("height")):
            self.img_no_size += 1
        if tag == "input" and (a.get("type") or "").lower() == "hidden" and re.search(r"csrf|token|nonce", a.get("name") or "", re.I):
            self.form_token = True


def parse_html(text, own_host):
    p = _Html()
    p.feed(text)
    return {"external": sorted(h for h in p.ext if h and h != own_host and not h.endswith("." + own_host)),
            "img_no_size": p.img_no_size, "classes": sorted(p.classes), "form_token": p.form_token}


def backend_ms(url):
    """Server work only, like GTmetrix 'Backend Duration': first byte minus connection setup minus one round trip.
    Does not depend much on where the laptop is (we talk to the nearest Cloudflare server)."""
    r = run(["curl", "-s", "-o", "/dev/null", "-A", UA, "--max-time", "30", "-w",
             "%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer}", url], 40)
    try:
        dns, conn, tls, first = map(float, r.stdout.split())
        return round(max(0, first - tls - (conn - dns)) * 1000)
    except (AttributeError, ValueError):
        return None


def server_checks(url, tries=3):
    rows, body = [], ""
    for _ in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ttfb = round((time.time() - t) * 1000)
                raw = r.read()
                h = {k.lower(): v for k, v in r.headers.items()}
        except Exception as e:  # a timeout is a finding, not a crash
            rows.append({"ttfb_ms": 30000, "cache": "MISSING", "error": f"{type(e).__name__}: no answer in 30 s"})
            continue
        if not body:
            body = (gzip.decompress(raw) if h.get("content-encoding") == "gzip" else raw).decode("utf-8", "replace")
        rows.append({"ttfb_ms": ttfb, "cache": h.get("cf-cache-status", "MISSING"),
                     "cache_control": h.get("cache-control"), "encoding": h.get("content-encoding"),
                     "server": h.get("server"), "powered_by": h.get("x-powered-by")})
    return rows, body


AD_PARAMS = {"gclid": "lpaudit{n}", "utm_source": "google", "utm_medium": "cpc", "utm_campaign": "lpaudit"}
CACHED = {"HIT", "STALE", "REVALIDATED", "UPDATING"}


def ad_click_check(url):
    """Ad clicks arrive as url?gclid=...&utm_...; every click has a new gclid.
    Two different gclids: if both miss the cache, every real ad visitor waits for the server."""
    rows = []
    for n in (1, 2):
        sep = "&" if "?" in url else "?"
        test = url + sep + urllib.parse.urlencode({k: v.format(n=n) for k, v in AD_PARAMS.items()})
        req = urllib.request.Request(test, headers={"User-Agent": UA})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                final = r.url
                rows.append({"cache": r.headers.get("cf-cache-status", "MISSING"), "ttfb_ms": round((time.time() - t) * 1000),
                             "params_kept": "gclid=" in final, "final_url": final[:200]})
        except Exception as e:
            rows.append({"cache": "ERROR", "error": type(e).__name__, "params_kept": None})
    return {"tries": rows, "cached": any(r["cache"] in CACHED for r in rows),
            "params_lost": any(r.get("params_kept") is False for r in rows)}


def fingerprint_similarity(a, b):
    """Jaccard of CSS class names: < 0.5 means a different site on the same domain."""
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 1.0


# ---------- 4. GTmetrix (one run, dev's own free account) ----------

GT_SCRAPE_JS = r"""async () => {
  const T = () => (document.querySelector('main') || document.body).innerText;
  const tab = async n => { const a = [...document.querySelectorAll('a')].find(x => x.innerText.trim() === n && x.closest('ul,nav')); if (a) { a.click(); await new Promise(r => setTimeout(r, 1500)); } };
  const s0 = T(); const pick = (s, re) => (s.match(re) || [])[1] || null;
  const out = {grade: (document.querySelector('[class*=icon-grade-]')?.className.match(/grade-(\w)/) || [])[1],
    perf: pick(s0, /Performance\s*\n?\s*(\d+)%/), structure: pick(s0, /Structure\s*\n?\s*(\d+)%/)};
  await tab('Performance'); const s = T();
  for (const [k, re] of Object.entries({fcp: /First Contentful Paint[\s\S]{0,60}?([\d.,]+\s?m?s)\b/, tti: /Time to Interactive[\s\S]{0,60}?([\d.,]+\s?m?s)\b/,
      si: /Speed Index[\s\S]{0,60}?([\d.,]+\s?m?s)\b/, tbt: /Total Blocking Time[\s\S]{0,60}?([\d.,]+\s?m?s)\b/, lcp: /Largest Contentful Paint[\s\S]{0,60}?([\d.,]+\s?m?s)\b/,
      cls: /Cumulative Layout Shift[\s\S]{0,60}?\n\s*([\d.]+)\s*\n/, backend: /Backend Duration[\s\S]{0,10}?([\d.,]+\s?m?s)/, fully: /Fully Loaded Time[\s\S]{0,10}?([\d.,]+\s?m?s)/}))
    out[k] = pick(s, re);
  await tab('Structure');
  [...document.querySelectorAll('*')].filter(e => e.childElementCount === 0 && e.textContent.trim() === 'Avoid large layout shifts').forEach(e => e.click());
  await new Promise(r => setTimeout(r, 1500)); const s2 = T();
  const a = s2.indexOf('IMPACT'); out.top_issues = a < 0 ? [] : [...s2.slice(a, a + 1500).matchAll(/\n(High|Med-High|Med|Med-Low|Low)\n([^\n]+)/g)].map(m => m[1] + ': ' + m[2]).slice(0, 8);
  const i = s2.indexOf('LAYOUT SHIFT SCORE'); out.shift_elements = i < 0 ? [] : [...new Set([...s2.slice(i, i + 1500).matchAll(/(body > [^\n]+)/g)].map(m => m[1]))].slice(0, 5);
  out.url = location.href; return out; }"""


def to_ms(v):
    if not v:
        return None
    m = re.match(r"([\d.,]+)\s*(ms|s)", v.replace(",", ""))
    return None if not m else float(m[1]) * (1 if m[2] == "ms" else 1000)


def gtmetrix(url, state_file):
    email, pw = os.environ.get("GTMETRIX_EMAIL"), os.environ.get("GTMETRIX_PASSWORD")
    if not (email and pw):
        return {"skipped": "No GTMETRIX_EMAIL / GTMETRIX_PASSWORD in .env"}
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = b.new_context(storage_state=str(state_file) if state_file.exists() else None,
                            viewport={"width": 1280, "height": 1000}, user_agent=UA)
        page = ctx.new_page()
        try:
            page.goto("https://gtmetrix.com/dashboard/account", timeout=60000)
            if "/dashboard" not in page.url:  # not logged in yet
                page.goto("https://gtmetrix.com/login.html", timeout=60000)
                page.fill("input[type=email], input[name=email]", email)
                page.fill("input[type=password]", pw)
                page.locator("form:has(input[type=password]) button[type=submit], form:has(input[type=password]) input[type=submit]").first.click()
                page.wait_for_url("**/dashboard/**", timeout=60000)
                page.goto("https://gtmetrix.com/dashboard/account", timeout=60000)
            used = re.search(r"On-Demand Tests Used\s*(\d+)\s*of\s*(\d+)", page.inner_text("body"))
            if used and int(used[1]) >= int(used[2]):
                return {"skipped": f"GTmetrix free tests used up ({used[1]} of {used[2]}). Run it manually later."}
            page.goto("https://gtmetrix.com/dashboard/", timeout=60000)
            page.fill("input[type=url]", url)
            page.locator("button[type=submit]:has-text('Analyze')").first.click()
            page.wait_for_url(re.compile(r"/reports/|job_error"), timeout=300000)
            if "job_error" in page.url:
                return {"error": page.locator(".alert").first.inner_text()[:300]}
            page.wait_for_timeout(3000)
            r = page.evaluate(GT_SCRAPE_JS)
            ctx.storage_state(path=str(state_file))
            r["tests_used"] = f"{int(used[1]) + 1} of {used[2]}" if used else None
            return r
        except Exception as e:  # ponytail: login/CAPTCHA/page changes all end here; GTmetrix stays a manual check then
            return {"error": f"GTmetrix step failed ({type(e).__name__}). Run it manually: https://gtmetrix.com"}
        finally:
            b.close()


# ---------- 5. Real users (live mode) ----------

def google_key():
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("PSI_API_KEY")


def post_json(url, body, headers=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def crux_parse(rec):
    m = rec.get("record", {}).get("metrics", {})
    p75 = lambda k: m.get(k, {}).get("percentiles", {}).get("p75")
    cls = p75("cumulative_layout_shift")
    return {"lcp_ms": p75("largest_contentful_paint"), "inp_ms": p75("interaction_to_next_paint"),
            "cls": float(cls) if cls is not None else None,  # CrUX sends CLS as a string
            "fcp_ms": p75("first_contentful_paint"), "ttfb_ms": p75("experimental_time_to_first_byte")}


def crux(url, key):
    """Google's real Chrome users, last 28 days, p75. Falls back to whole-site data if the page has too few visits."""
    out = {}
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(url))
    for ff, name in (("PHONE", "phone"), ("DESKTOP", "desktop")):
        for level, body in (("page", {"url": url}), ("whole site", {"origin": origin})):
            try:
                rec = post_json(f"https://chromeuxreport.googleapis.com/v1/records:queryRecord?key={key}",
                                {**body, "formFactor": ff})
                out[name] = {**crux_parse(rec), "level": level}
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:  # 404 = not enough Chrome visitors yet
                    out[name] = {"error": f"CrUX API error {e.code}"}
                    break
        else:
            out[name] = {"error": "Not enough Chrome visitors yet (Google needs more traffic)"}
    return out


CF_QUERY = """query($acct: String!, $since: Date!, $host: String!, $path: String!) {
  viewer { accounts(filter: {accountTag: $acct}) {
    rumWebVitalsEventsAdaptiveGroups(limit: 20, filter: {date_geq: $since, requestHost: $host, requestPath: $path}) {
      count dimensions { deviceType }
      quantiles { largestContentfulPaintP75 interactionToNextPaintP75 cumulativeLayoutShiftP75 firstContentfulPaintP75 }
} } } }"""


def cf_parse(resp):
    """Group Cloudflare Web Analytics rows into phone / desktop."""
    rows = resp["data"]["viewer"]["accounts"][0]["rumWebVitalsEventsAdaptiveGroups"]
    out = {}
    for r in rows:
        dev = "phone" if "mobile" in (r["dimensions"]["deviceType"] or "").lower() else "desktop"
        q = r["quantiles"]
        # ponytail: Cloudflare's sampled data sometimes returns absurd p75s (e.g. INP 96 s); over 30 s = not reliable, dropped
        ms = lambda v: None if v is None or v > 30000 else v
        cur = out.get(dev)
        if not cur or r["count"] > cur["visits"]:  # keep the biggest group per device
            out[dev] = {"visits": r["count"], "lcp_ms": ms(q["largestContentfulPaintP75"]),
                        "inp_ms": ms(q["interactionToNextPaintP75"]), "cls": q["cumulativeLayoutShiftP75"],
                        "fcp_ms": ms(q["firstContentfulPaintP75"])}
    return out


def cloudflare(url, days=7):
    """Real ad visitors' speed from Cloudflare Web Analytics (already on our sites)."""
    token, acct = os.environ.get("CF_API_TOKEN"), os.environ.get("CF_ACCOUNT_ID")
    if not (token and acct):
        return {"skipped": "No CF_API_TOKEN / CF_ACCOUNT_ID in .env"}
    u = urllib.parse.urlparse(url)
    since = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    api, auth = "https://api.cloudflare.com/client/v4/graphql", {"Authorization": f"Bearer {token}"}
    try:
        resp = post_json(api, {"query": CF_QUERY, "variables": {"acct": acct, "since": since,
                                                               "host": u.hostname, "path": u.path or "/"}}, auth)
        if resp.get("errors"):
            raise ValueError(resp["errors"][0].get("message"))
        data = cf_parse(resp)
        return {**data, "days": days} if data else {"skipped": f"No Cloudflare visits for this page in the last {days} days"}
    except Exception as e:
        try:  # show which RUM fields this account really has, so the query can be fixed quickly
            t = post_json(api, {"query": '{__type(name: "account") {fields {name}}}'}, auth)
            rum = [f["name"] for f in t["data"]["__type"]["fields"] if "rum" in f["name"].lower()]
        except Exception:
            rum = []
        return {"error": f"Cloudflare query failed: {e}", "rum_fields": rum}


# ---------- 6. Rules ----------

def get(d, path):
    for k in path.split("."):
        d = d.get(k) if isinstance(d, dict) else None
    return d


def evaluate(rules, facts):
    """Return failed rules with the evidence that failed them."""
    failed = []
    for r in rules:
        if "list" in r:
            hits = [x for x in get(facts, r["list"]) or []
                    if re.search(r["match"], str(x.get(r["field"]) if r.get("field") else x), re.I)]
            if hits:
                failed.append({**r, "evidence": hits[:5]})
            continue
        v = get(facts, r["metric"])
        if v is None:
            continue
        bad = ("max" in r and v > r["max"]) or ("min" in r and v < r["min"]) or \
              ("not_in" in r and v in r["not_in"])
        if bad:
            failed.append({**r, "evidence": v})
    return sorted(failed, key=lambda r: r.get("severity", 3))


# ---------- 6. Report ----------

def fmt(v, key=""):
    if v is None:
        return "-"
    if key.endswith("_ms"):
        return f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"
    if key.endswith("_kb"):
        return f"{v:.0f} KB"
    if key.endswith("_s"):
        return f"{v} s"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def render(url, mode, when, facts, failed, lh_top, ai_summary):
    L = [f"# Speed audit: {url}", "", f"{when} · mode: {mode} · rules failed: {len(failed)}", ""]
    if ai_summary:
        L += ["## Summary", "", ai_summary.strip(), ""]
    L += ["## Results (middle of runs)", "", f"Lab tests: {get(facts, 'lab.source') or '-'}.", "", "| Check | Phone | Desktop | Target |", "| --- | --- | --- | --- |"]
    rows = [("Lighthouse score", "lab.{}.score", "", "90+"), ("LCP (lab)", "lab.{}.lcp_ms", "_ms", "2.5 s"),
            ("TBT (lab)", "lab.{}.tbt_ms", "_ms", "200 ms"),
            ("CLS (lab)", "lab.{}.cls", "", "0.1"),
            ("CLS real visitor (wait + scroll)", "visitor.{}.cls", "", "0.1"),
            ("Blocking time real visitor", "visitor.{}.blocking_ms", "_ms", "300 ms"),
            ("Network quiet after", "visitor.{}.quiet_s", "", "8 s"),
            ("Page weight", "lab.{}.bytes_kb", "_kb", "1500 KB")]
    for name, path, _, target in rows:
        key = path.split(".")[-1]
        L.append(f"| {name} | {fmt(get(facts, path.format('mobile')), key)} | {fmt(get(facts, path.format('desktop')), key)} | {target} |")
    s = facts["server"]
    ad = s.get("ad_click") or {}
    L += ["", f"Server: backend time {fmt(s.get('backend_ms'), '_ms')} "
          f"(tries: {', '.join(fmt(t, '_ms') for t in s.get('backend_all') or [])}; server work only, like GTmetrix 'Backend Duration'). "
          f"Full answer time from this laptop {fmt(s['ttfb_ms'], '_ms')}. Cloudflare cache `{s['cache']}`, compression `{s['encoding']}`."]
    if ad:
        L += ["", f"Ad-click URL (?gclid=...&utm_...): cache `{' / '.join(t['cache'] for t in ad['tries'])}`, "
              f"gclid kept after redirects: {'no' if ad['params_lost'] else 'yes'}."]
    L += [
          "", "Server checks and the real-visitor test ran from this laptop's internet. "
              "Visitors in other countries can see different server times.", ""]
    gt = facts.get("gtmetrix") or {}
    if gt.get("perf") is not None:
        L += [f"GTmetrix: Grade {gt.get('grade')}, performance {gt['perf']}%, structure {gt.get('structure')}%, "
              f"LCP {fmt(gt.get('lcp_ms'), '_ms')}, TBT {fmt(gt.get('tbt_ms'), '_ms')}, CLS {gt.get('cls')}, "
              f"fully loaded {gt.get('fully')}. [Report]({gt.get('url')})", ""]
    elif gt:
        L += [f"GTmetrix: {gt.get('skipped') or gt.get('error')}", ""]
    field = facts.get("field")
    if field:
        L += ["## Real users", "", "Google: Chrome users, last 28 days. Cloudflare: your visitors, last 7 days. "
              "Both show the 75th percentile (3 of 4 visitors are this fast or faster).", "",
              "| Metric | Google phone | Google desktop | Cloudflare phone | Cloudflare desktop | Target |",
              "| --- | --- | --- | --- | --- | --- |"]
        for name, k, target in (("LCP", "lcp_ms", "2.5 s"), ("INP (reacts to taps)", "inp_ms", "200 ms"),
                                ("CLS", "cls", "0.1"), ("FCP", "fcp_ms", "1.8 s"), ("TTFB", "ttfb_ms", "0.8 s")):
            cells = [fmt(get(field, f"{src}.{dev}.{k}"), k) for src in ("google", "cloudflare") for dev in ("phone", "desktop")]
            L.append(f"| {name} | {' | '.join(cells)} | {target} |")
        notes = [f"Google {d}: {get(field, f'google.{d}.level')} data" for d in ("phone", "desktop") if get(field, f"google.{d}.level")]
        notes += [f"Cloudflare {d}: {get(field, f'cloudflare.{d}.visits')} visits" for d in ("phone", "desktop") if get(field, f"cloudflare.{d}.visits")]
        for src in ("google", "cloudflare"):
            for d in ("", ".phone", ".desktop"):
                msg = get(field, f"{src}{d}.skipped") or get(field, f"{src}{d}.error")
                if msg:
                    notes.append(f"{src.title()}{d.replace('.', ' ')}: {msg}")
        if get(field, "cloudflare.rum_fields"):
            notes.append("Cloudflare fields available: " + ", ".join(field["cloudflare"]["rum_fields"]))
        L += ["", "; ".join(dict.fromkeys(notes)) + ".", ""] if notes else [""]
    L += ["## Problems to fix", ""] if failed else ["## Problems to fix", "", "None. All rules passed.", ""]
    for i, r in enumerate(failed, 1):
        ev = r["evidence"]
        ev = "; ".join(f"{x.get('el', x)} ({x.get('v', 0):.2f})" if isinstance(x, dict) else str(x) for x in ev) \
            if isinstance(ev, list) else fmt(ev, r.get("metric", ""))
        L += [f"### {i}. {r['title']}", "", f"**Found:** {ev}", ""] + [f"- {f}" for f in r["fix"]] + \
             ["", f"**Check:** {r['check']}", ""]
    shifts = get(facts, "visitor.shifts") or []
    if shifts:
        L += ["## Layout shifts (real visitor)", "", "| Element | Device | Total score | Times | First at |", "| --- | --- | --- | --- | --- |"]
        L += [f"| `{x['el']}` | {x.get('device', '')} | {x['v']:.3f} | {x.get('count', 1)} | {x.get('t', 0) / 1000:.1f} s |"
              for x in sorted(shifts, key=lambda x: -x["v"])[:8]]
        L.append("")
    late = get(facts, "visitor.desktop.late_requests") or []
    if late:
        L += ["## Late requests (after page load, other sites)", ""]
        L += [f"- {x['t_ms'] / 1000:.1f} s {x['type']}: {x['url']}" for x in late[:12]] + [""]
    if facts["html"]["external"]:
        L += ["## External files", "", ", ".join(facts["html"]["external"]), ""]
    if lh_top:
        L += ["## Other Lighthouse findings (phone)", ""] + [f"- {x['title']} {x['value']}" for x in lh_top[:8]] + [""]
    L += ["## How to check again", "", "Run the same audit again after the fixes. Compare with this report.", ""]
    return "\n".join(L)


SPEED_WHY = "in plain words why it hurts visitors who come from ads (slow page, page jumps, they leave)"
CODE_WHY = "in plain words what can go wrong on the live site (visitors see errors, data leaks, hackers, broken page)"


def ollama_summary(failed, kind="speed problems found on a landing page", why=SPEED_WHY):
    url = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    brief = [{"problem": r["title"], "found": r.get("evidence"), "fix": r["fix"]} for r in failed]
    prompt = ("You write for a web developer whose first language is not English. Use short, simple sentences. "
              f"Below are {kind}, already sorted: most important first. "
              f"Write 3 to 6 bullet points in that same order. Each bullet: what to fix, and {why}. "
              "Use only the facts given. Do not invent scores, ranks or numbers.\n\n"
              + json.dumps(brief, default=str)[:6000])
    req = urllib.request.Request(f"{url}/api/generate", data=json.dumps(
        {"model": model, "prompt": prompt, "stream": False,
         "options": {"temperature": 0.2, "num_predict": 500}}).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["response"]
    except Exception as e:
        log(f"Ollama skipped ({e.__class__.__name__}): is Ollama running with model {model}?")
        return None


# ---------- 7. Storage ----------

def db():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / "index.db")
    c.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, domain TEXT, url TEXT, mode TEXT, "
              "ts TEXT, folder TEXT, version INTEGER, classes TEXT, summary TEXT)")
    return c


def clean_raw(days=RAW_KEEP_DAYS):
    cutoff = time.time() - days * 86400
    for raw in DATA.glob("*/*/raw"):
        if raw.stat().st_mtime < cutoff:
            shutil.rmtree(raw)


def cmd_clean(args):
    c = db()
    if args.older_than:
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=args.older_than)).isoformat()
        rows = c.execute("SELECT id, folder FROM runs WHERE ts < ?", (cutoff,)).fetchall()
    else:
        rows = c.execute("SELECT id, folder FROM runs WHERE domain = ?", (args.domain,)).fetchall()
        shutil.rmtree(DATA / args.domain, ignore_errors=True)
    for rid, folder in rows:
        shutil.rmtree(DATA / folder, ignore_errors=True)
        c.execute("DELETE FROM runs WHERE id = ?", (rid,))
    c.commit()
    print(f"Deleted {len(rows)} audit(s).")


def cmd_history(args):
    for ts, mode, version, folder, summ in db().execute(
            "SELECT ts, mode, version, folder, summary FROM runs WHERE domain = ? ORDER BY ts", (args.domain,)):
        s = json.loads(summ)
        print(f"{ts[:16]}  {mode:9}  site v{version}  phone {s.get('mobile')}  desktop {s.get('desktop')}  "
              f"failed {s.get('failed')}  {folder}")


# ---------- main audit ----------

def cmd_audit(args):
    url, mode = args.url, args.cmd
    host = urllib.parse.urlparse(url).hostname
    now = datetime.datetime.now()
    folder_rel = f"{host}/{now:%Y-%m-%d_%H%M}_{mode}"
    folder = DATA / folder_rel
    folder.mkdir(parents=True, exist_ok=True)
    clean_raw()
    facts = {"lab": {}, "visitor": {}}

    log("Server checks (3 tries)")
    rows, body = server_checks(url)
    facts["server"] = {"ttfb_ms": median(r["ttfb_ms"] for r in rows), "ttfb_all": [r["ttfb_ms"] for r in rows],
                       "cache": rows[-1]["cache"], "encoding": rows[-1].get("encoding"), "tries": rows,
                       "max_ttfb_ms": max(r["ttfb_ms"] for r in rows)}
    backend = [backend_ms(url) for _ in range(3)]
    facts["server"]["backend_ms"], facts["server"]["backend_all"] = median(backend), backend
    facts["html"] = parse_html(body, host)
    log("Ad-click URL (gclid + utm) and exposed files")
    ad = ad_click_check(url)
    plain_cached = facts["server"]["cache"] in CACHED
    facts["server"]["ad_click"] = {**ad, "cache_miss": plain_cached and not ad["cached"]}
    facts["server"]["exposed"] = exposed_files(url)
    facts["html"]["cached_token"] = facts["html"]["form_token"] and (plain_cached or ad["cached"])

    chrome = chrome_path()
    key = google_key() if not args.local_lab else None
    facts["lab"]["source"] = "PageSpeed API (Google, USA)" if key else "local Lighthouse (this laptop)"
    lh_top = []
    for preset in ("mobile", "desktop"):
        runs = []
        for i in range(args.runs):
            log(f"Lighthouse {preset} run {i + 1}/{args.runs} ({facts['lab']['source']})")
            lhr = (key and psi(url, preset, key)) or lighthouse(url, preset, chrome)
            if lhr:
                runs.append(lh_metrics(lhr))
                save_raw(folder, f"lighthouse_{preset}_{i + 1}", lhr)
                if preset == "mobile" and i == 0:
                    lh_top = lh_issues(lhr)
        facts["lab"][preset] = {k: median(r[k] for r in runs) for k in runs[0]} if runs else {}
        facts["lab"][preset]["runs"] = runs

    for device in ("mobile", "desktop"):
        log(f"Real-visitor test ({device}: wait 15 s, scroll, record)")
        try:
            facts["visitor"][device] = visitor_test(url, device)
        except Exception as e:
            log(f"Visitor test {device} failed: {e}")
    facts["visitor"]["shifts"] = [{**s, "device": d} for d in ("mobile", "desktop")
                                  for s in get(facts, f"visitor.{d}.shifts") or [] if s["v"] >= 0.001]

    if (mode == "prelaunch" and not args.no_gtmetrix) or (mode == "live" and args.gtmetrix):
        log("GTmetrix (1 run, your free account)")
        gt = gtmetrix(url, DATA / ".gtmetrix_state.json")
        for k in ("lcp", "tbt", "fcp", "si", "tti"):
            gt[f"{k}_ms"] = to_ms(gt.get(k))
        gt["perf"] = int(gt["perf"]) if gt.get("perf") else None
        facts["gtmetrix"] = gt

    if mode == "live":
        log("Real users: Google (28 days) and Cloudflare (7 days)")
        k = google_key()
        facts["field"] = {"google": crux(url, k) if k else {"skipped": "No GOOGLE_API_KEY in .env"},
                          "cloudflare": cloudflare(url)}

    failed = evaluate(json.loads(RULES.read_text()), facts)
    ai = None if args.no_ai or not failed else (log("Ollama summary") or ollama_summary(failed))

    # site version: a very different page on the same domain starts a new history
    c = db()
    last = c.execute("SELECT version, classes FROM runs WHERE domain = ? ORDER BY ts DESC LIMIT 1", (host,)).fetchone()
    version = 1 if not last else last[0] + (fingerprint_similarity(json.loads(last[1]), facts["html"]["classes"]) < 0.5)

    when = f"{now:%Y-%m-%d %H:%M}"
    report = render(url, mode, when, facts, failed, lh_top, ai)
    if last and version != last[0]:
        report = report.replace("\n\n", f"\n\nNew site version (v{version}) on this domain: history starts fresh.\n\n", 1)
    (folder / "report.md").write_text(report)
    summary = {"mobile": get(facts, "lab.mobile.score"), "desktop": get(facts, "lab.desktop.score"),
               "failed": [r["id"] for r in failed]}
    (folder / "summary.json").write_text(json.dumps({"url": url, "mode": mode, "when": when, "version": version,
                                                     "facts": facts, "failed": failed}, indent=1, default=str))
    c.execute("INSERT INTO runs (domain, url, mode, ts, folder, version, classes, summary) VALUES (?,?,?,?,?,?,?,?)",
              (host, url, mode, now.isoformat(), folder_rel, version, json.dumps(facts["html"]["classes"]), json.dumps(summary)))
    c.commit()
    log(f"Done. Report: reports/{folder_rel}/report.md")
    print(f"\nPhone {summary['mobile']} · Desktop {summary['desktop']} · {len(failed)} problem(s): "
          + ", ".join(r["title"] for r in failed))


# ---------- 8. Code audit ----------

CODE_RULES = Path(__file__).with_name("code_rules.json")
SKIP_DIRS = {".git", "node_modules", "vendor", ".idea", ".vscode"}
TEXT_FILE = re.compile(r"\.(php|phtml|inc|js|html?|css|ini|json|txt)$|\.htaccess$", re.I)
RISKY_FILE = re.compile(r"\.(bak|old|orig|swp|sql|zip|tar|gz|rar|7z|log)$|~$|(^|/)\.env$|(^|/)(phpinfo|test|debug|info)\.php$", re.I)
GITIGNORE = ".env\n*.env\nuploads/\nvendor/\nnode_modules/\n*.zip\n*.sql\n*.log\n*.bak\n"


def code_files(root):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not SKIP_DIRS & set(rel.parts):
            yield p, rel.as_posix()


def scan_patterns(root, rules, per_file=5):
    """Our own rules: regex over every text file. Learned rules (phase 4) land in code_rules.json too."""
    hits = []
    for p, rel in code_files(root):
        if not TEXT_FILE.search(rel) or p.stat().st_size > 2_000_000:
            continue
        text = p.read_text(errors="replace")
        lines = text.splitlines()
        for r in rules:
            if not re.search(r["files"], rel, re.I):
                continue
            seen = set()
            for m in re.finditer(r["match"], text, re.I):
                n = text.count("\n", 0, m.start()) + 1
                if n in seen:
                    continue
                seen.add(n)
                if len(seen) > per_file:
                    break
                hits.append({"rule": r["id"], "file": rel, "line": n, "code": lines[n - 1].strip()[:160] if lines else ""})
    return hits


def run(cmd, timeout=300):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"{cmd[0]} skipped: {e}")
        return None


def php_lint(root):
    out = []
    for p, rel in code_files(root):
        if rel.endswith(".php"):
            r = run(["php", "-l", str(p)], 30)
            if r and r.returncode:
                m = re.search(r"(Parse error|Fatal error): (.+?) in .+ on line (\d+)", r.stdout + r.stderr)
                out.append({"tool": "php -l", "file": rel, "line": int(m[3]) if m else 0,
                            "message": m[2] if m else (r.stdout + r.stderr).strip()[:200]})
    return out


def phpstan(root):
    r = run(["phpstan", "analyse", str(root), "--level=0", "--error-format=json", "--no-progress",
             "--memory-limit=1G", "--no-interaction"])
    try:
        files = json.loads(r.stdout)["files"]
    except (AttributeError, ValueError, KeyError, TypeError):
        return []
    return [{"tool": "PHPStan", "file": Path(f).relative_to(root).as_posix() if f.startswith(str(root)) else f,
             "line": m.get("line") or 0, "message": m["message"]} for f, v in files.items() for m in v["messages"]][:40]


def semgrep(root):
    """Security patterns from the free Semgrep registry (needs internet) + our learned rules."""
    cfg = ["--config", "p/php", "--config", "p/javascript"]
    learned = Path(__file__).with_name("semgrep_rules")
    if learned.is_dir() and any(learned.glob("*.yml")):
        cfg += ["--config", str(learned)]
    r = run(["semgrep", "scan", *cfg, "--json", "--metrics=off", "--quiet", "--exclude", "vendor", str(root)], 600)
    try:
        res = json.loads(r.stdout)["results"]
    except (AttributeError, ValueError, KeyError, TypeError):
        return []
    return [{"tool": "Semgrep", "file": Path(x["path"]).relative_to(root).as_posix() if x["path"].startswith(str(root)) else x["path"],
             "line": x["start"]["line"], "message": x["extra"]["message"].strip().split("\n")[0][:200],
             "rule": x["check_id"].split(".")[-1], "severity": x["extra"].get("severity")} for x in res][:40]


def gitleaks(root):
    out = Path(tempfile.mkstemp(suffix=".json")[1])
    run(["gitleaks", "dir", str(root), "--report-format", "json", "--report-path", str(out), "--no-banner", "--exit-code", "0"])
    try:
        res = json.loads(out.read_text() or "[]")
    except ValueError:
        res = []
    out.unlink(missing_ok=True)
    return [{"tool": "gitleaks", "file": Path(x["File"]).relative_to(root).as_posix() if x["File"].startswith(str(root)) else x["File"],
             "line": x.get("StartLine", 0), "message": f"{x.get('RuleID')}: secret starting '{(x.get('Secret') or '')[:4]}…'"}
            for x in res if "/.git/" not in x["File"]][:20]


def snap_dir(name):
    return DATA / ".snapshots" / f"{slug(name)}.git"


def git_snapshot(root, name, label):
    """Save the exact code version this audit looked at. The git data lives in reports/.snapshots/,
    never in the site folder, so nothing extra can be uploaded to the server by mistake."""
    gd = snap_dir(name)
    g = lambda *a: run(["git", f"--git-dir={gd}", f"--work-tree={root}", *a], 60)
    if not gd.exists():
        gd.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", "--bare", str(gd)], 60)
        (gd / "info").mkdir(exist_ok=True)
        (gd / "info" / "exclude").write_text(GITIGNORE + ".git/\n")
    g("add", "-A")
    st = g("status", "--porcelain")
    if st and st.stdout.strip():
        g("-c", "user.name=lp-audit", "-c", "user.email=lp-audit@localhost", "commit", "-qm", label)
    head = g("rev-parse", "--short", "HEAD")
    return head.stdout.strip() if head and head.returncode == 0 else None


def changed_since(name, commit):
    r = run(["git", f"--git-dir={snap_dir(name)}", "diff", "--name-only", f"{commit}..HEAD"], 60) if commit else None
    return [l for l in r.stdout.splitlines() if l] if r and r.returncode == 0 else None


def exposed_files(url):
    """Files that must never be downloadable from the live site (.git, .env, backups)."""
    u = urllib.parse.urlparse(url)
    bases = dict.fromkeys([f"{u.scheme}://{u.netloc}/", urllib.parse.urljoin(url, "./")])
    found = []
    for base in bases:
        for path, looks_real in ((".git/HEAD", lambda t: t.startswith("ref:")), (".env", lambda t: "=" in t and "<html" not in t.lower()),
                                 ("backup.sql", lambda t: "create table" in t.lower() or "insert into" in t.lower())):
            try:
                req = urllib.request.Request(base + path, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=15) as r:
                    if r.status == 200 and looks_real(r.read(2000).decode("utf-8", "replace")):
                        found.append(base + path)
            except Exception:
                pass
    return found


def speed_links(root, host):
    """Point each speed problem from the latest speed audit to the file and line that causes it."""
    row = db().execute("SELECT folder FROM runs WHERE domain = ? AND mode != 'code' ORDER BY ts DESC LIMIT 1", (host,)).fetchone()
    if not row:
        return []
    failed = [r["id"] for r in json.loads((DATA / row[0] / "summary.json").read_text())["failed"]]
    cur = {r["id"]: r for r in json.loads(RULES.read_text())}  # current rules: code_match may be newer than the audit
    rules = [{"id": i, "files": cur[i].get("code_files", r"\.(php|html?|js)$"), "match": cur[i]["code_match"]}
             for i in failed if cur.get(i, {}).get("code_match")]
    titles = {i: cur[i]["title"] for i in failed if i in cur}
    return [{**h, "problem": titles[h["rule"]]} for h in scan_patterns(root, rules, per_file=3)]


def deploy_check(root, url):
    """Do the CSS/JS files the live page loads match the files in your folder? (catches 'forgot to upload')"""
    host = urllib.parse.urlparse(url).hostname
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        return [{"file": url, "status": f"could not open live page ({type(e).__name__})"}]
    out = []
    for ref in sorted(set(re.findall(r"""(?:src|href)=["']([^"']+\.(?:css|js))(?:\?[^"']*)?["']""", page)))[:40]:
        full = urllib.parse.urljoin(url, ref)
        rel = urllib.parse.urlparse(full).path.lstrip("/")
        if urllib.parse.urlparse(full).hostname != host or rel.startswith("cdn-cgi/"):  # cdn-cgi = added by Cloudflare
            continue
        # the folder may be the whole site or just this landing page (/lp/<name>/): match the longest path that exists
        parts = rel.split("/")
        local = next((root.joinpath(*parts[i:]) for i in range(len(parts)) if root.joinpath(*parts[i:]).exists()), root / rel)
        if not local.exists():
            out.append({"file": rel, "status": "live page uses it, but it is not in your folder"})
            continue
        try:
            live = urllib.request.urlopen(urllib.request.Request(full, headers={"User-Agent": UA}), timeout=30).read()
        except Exception:
            out.append({"file": rel, "status": "in your folder, but missing on the live site"})
            continue
        if live.strip() != local.read_bytes().strip():
            out.append({"file": rel, "status": "different on the live site (not uploaded, or changed on the server)"})
    return out


CLAUDE_PROMPT = """You are reviewing a PHP landing page before launch. The code is in the folder you are running in.
Our tools already found the problems listed below. Do NOT repeat them.

Look for problems the tools missed: bugs, security holes, broken forms or tracking, things that will fail on the
live server, missing error handling, leftovers from development, accessibility basics, and anything that makes
the page slower or jump while loading. Keep it to real problems, not style.

Write your result as JSON to this file: {out}
Format: a list of objects:
  {{"file": "...", "line": 0, "severity": 1-3 (1 = must fix before launch), "title": "short", "why": "simple English",
    "fix": "simple English",
    "rule": {{"type": "regex", "files": "regex for file names", "match": "regex that finds this problem",
             "bad_example": "code the regex must match", "good_example": "fixed code it must not match"}}
            or null if the problem cannot be found with a pattern (logic problems)}}
Only add a "rule" when the pattern would also catch the same mistake on other sites without many false alarms.
If you find nothing new, write [].

Problems the tools already found:
{known}
"""


def cmd_code(args):
    root = Path(args.path).resolve()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")
    host = urllib.parse.urlparse(args.url).hostname if args.url else None
    name = args.name or host or os.environ.get("CODE_NAME") or root.name
    now = datetime.datetime.now()
    folder_rel = f"{name}/{now:%Y-%m-%d_%H%M}_code"
    folder = DATA / folder_rel
    folder.mkdir(parents=True, exist_ok=True)
    c = db()
    last = c.execute("SELECT summary FROM runs WHERE domain = ? AND mode = 'code' ORDER BY ts DESC LIMIT 1", (name,)).fetchone()
    last_commit = json.loads(last[0]).get("commit") if last else None

    log("Saving code version (local git)")
    commit = git_snapshot(root, name, f"audit {now:%Y-%m-%d %H:%M} code")
    changed = changed_since(name, last_commit)

    rules = json.loads(CODE_RULES.read_text())
    log("Our rules (leftovers, security basics)")
    hits = scan_patterns(root, rules)
    risky = [rel for _, rel in code_files(root) if RISKY_FILE.search(rel)]
    log("php -l (syntax)")
    tools = php_lint(root)
    log("PHPStan (PHP errors)")
    tools += phpstan(root)
    if not args.no_semgrep:
        log("Semgrep (security, needs internet)")
        tools += semgrep(root)
    log("gitleaks (passwords, keys)")
    tools += gitleaks(root)
    links = speed_links(root, host) if host else []
    deploy = deploy_check(root, args.url) if args.url else []
    exposed = exposed_files(args.url) if args.url else []
    if links:
        log(f"Linked {len(links)} speed problem(s) to code lines")

    by_rule = {r["id"]: r for r in rules}
    problems = [{**by_rule[h["rule"]], "evidence": f"{h['file']}:{h['line']}"} for h in hits]
    problems += [{"title": "Private file downloadable from the live site", "fix": "Delete it from the server and change the passwords inside.",
                  "severity": 1, "evidence": e} for e in exposed]
    problems += [{"title": f"{t['tool']}: {t['message']}", "fix": "See the file and line.", "severity": 1 if t["tool"] in ("php -l", "gitleaks") else 2,
                  "evidence": f"{t['file']}:{t['line']}"} for t in tools]
    problems.sort(key=lambda p: p.get("severity", 3))
    ai = None if args.no_ai or not problems else (log("Ollama summary") or ollama_summary(
        problems[:25], "problems found in the code of a PHP landing page before launch", CODE_WHY))

    known = "\n".join(f"- {p['evidence']} {p['title']}" for p in problems) or "- none"
    (folder / "claude_prompt.md").write_text(CLAUDE_PROMPT.format(out=f"{os.environ.get('LP_AUDIT_DIR', '.')}/reports/{folder_rel}/claude_findings.json", known=known))

    L = [f"# Code audit: {name}", "", f"{now:%Y-%m-%d %H:%M} · code version `{commit or '-'}` · problems: {len(problems)}", ""]
    if ai:
        L += ["## Summary", "", ai.strip(), ""]
    if changed is not None:
        L += ["## Changed since last code audit", "", ", ".join(changed[:30]) or "Nothing changed.", ""]
    if links:
        L += ["## Speed problems found in the code", "", "From the latest speed audit of this site:", ""]
        L += [f"- **{l['problem']}**: `{l['file']}:{l['line']}` `{l['code']}`" for l in links] + [""]
    if deploy:
        L += ["## Live site vs your folder", ""] + [f"- `{d['file']}`: {d['status']}" for d in deploy] + [""]
    if exposed:
        L += ["## Private files can be downloaded from the live site", "",
              "Delete them from the server now. If .env or .git was public, change the passwords inside.", ""]
        L += [f"- {e}" for e in exposed] + [""]
    if risky:
        L += ["## Files that should not be on the live site", ""] + [f"- `{r}`" for r in risky] + [""]
    L += ["## Our rules", ""]
    if hits:
        for rid in dict.fromkeys(h["rule"] for h in hits):
            r = by_rule[rid]
            L += [f"### {r['title']}", "", r["fix"], ""] + \
                 [f"- `{h['file']}:{h['line']}` `{h['code']}`" for h in hits if h["rule"] == rid] + [""]
    else:
        L += ["Nothing found.", ""]
    L += ["## Tools (php -l, PHPStan, Semgrep, gitleaks)", ""]
    L += [f"- **{t['tool']}** `{t['file']}:{t['line']}` {t['message']}" for t in tools] + [""] if tools else ["Nothing found.", ""]
    checks = json.loads(CHECKLIST.read_text()) if CHECKLIST.exists() else []
    if checks:
        L += ["## Manual checks (learned from past reviews)", ""] + [f"- [ ] {x['title']}: {x.get('why') or ''}" for x in checks] + [""]
    L += ["## Before upload", "", "Do not upload `.env`, backup files (.bak, .sql, .zip) or a `.git` folder to the server.", "",
          "## Claude Code review (one pass)", "",
          "In the site folder, run:", "", "```",
          f'claude "Follow the instructions in {os.environ.get("LP_AUDIT_DIR", ".")}/reports/{folder_rel}/claude_prompt.md"',
          "```", "", "Claude writes its new findings to `claude_findings.json` in this report folder.", ""]
    (folder / "report.md").write_text("\n".join(L))
    summary = {"commit": commit, "rules": len(hits), "tools": len(tools), "risky": len(risky), "links": len(links)}
    (folder / "summary.json").write_text(json.dumps({"name": name, "path": str(root), "commit": commit, "hits": hits,
                                                     "tools": tools, "risky": risky, "links": links, "deploy": deploy, "exposed": exposed}, indent=1))
    rule_stats(c, {h["rule"] for h in hits})
    c.execute("INSERT INTO runs (domain, url, mode, ts, folder, version, classes, summary) VALUES (?,?,?,?,?,?,?,?)",
              (name, args.url, "code", now.isoformat(), folder_rel, 1, "[]", json.dumps(summary)))
    c.commit()
    log(f"Done. Report: reports/{folder_rel}/report.md")
    print(f"\n{len(problems)} problem(s): {len(hits)} from our rules, {len(tools)} from tools, {len(risky)} risky file(s), "
          f"{len(links)} speed link(s)")


# ---------- 9. Learning: Claude's findings become rules ----------

CHECKLIST = Path(__file__).with_name("manual_checklist.json")
RULE_FILES = ("rules.json", "code_rules.json", "manual_checklist.json")


def check_rule(rule, existing, sample_roots):
    """Is a proposed rule safe to add? Returns (ok, notes)."""
    notes = []
    try:
        rx = re.compile(rule["match"], re.I)
        re.compile(rule.get("files") or ".", re.I)
    except (re.error, KeyError, TypeError) as e:
        return False, [f"pattern does not work: {e}"]
    bad, good = rule.get("bad_example") or "", rule.get("good_example") or ""
    if not bad or not rx.search(bad):
        return False, ["does not catch its own bad example"]
    if good and rx.search(good):
        return False, ["also flags the fixed (good) example: would give false alarms"]
    dup = [r["id"] for r in existing if re.search(r["match"], bad, re.I)]
    if dup:
        return False, [f"already covered by: {', '.join(dup)}"]
    probe = [{"id": "probe", "files": rule.get("files") or ".", "match": rule["match"]}]
    for root in sample_roots:
        n = len(scan_patterns(root, probe, per_file=50))
        notes.append(f"{n} hit(s) in {root.name}")
        if n > 25:
            return False, notes + ["fires too often: probably many false alarms"]
    return True, notes or ["tests passed"]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "rule"


def ask(q, choices):
    while True:
        a = input(f"{q} [{'/'.join(choices)}] ").strip().lower()[:1]
        if a in choices:
            return a


def cmd_learn(args):
    pending = sorted(DATA.glob("*/*_code/claude_findings.json"))
    pending = [p for p in pending if not (p.parent / "learned.json").exists()]
    if not pending:
        print("No new Claude findings. Run 'audit code', then the Claude command from its report.")
        return
    rules = json.loads(CODE_RULES.read_text())
    checklist = json.loads(CHECKLIST.read_text()) if CHECKLIST.exists() else []
    roots = [r for r in (Path("/code"), Path(__file__).parent / "samples" / "demo-site") if r.is_dir()]
    added_rules = added_checks = 0
    for f in pending:
        try:
            findings = json.loads(f.read_text())
        except ValueError:
            print(f"Skipping {f}: not valid JSON")
            continue
        print(f"\n=== {f.parent.relative_to(DATA)}: {len(findings)} finding(s) from Claude ===")
        decisions = []
        for i, x in enumerate(findings, 1):
            print(f"\n[{i}/{len(findings)}] {x.get('title')}  ({x.get('file')}:{x.get('line')}, severity {x.get('severity')})")
            print(f"  Why: {x.get('why')}\n  Fix: {x.get('fix')}")
            rule = x.get("rule")
            if rule:
                ok, notes = check_rule(rule, rules, roots)
                print(f"  Proposed rule: files {rule.get('files')!r}  match {rule.get('match')!r}")
                print(f"  Check: {'OK' if ok else 'REJECTED'}: {'; '.join(notes)}")
                if ok and (args.yes or ask("  Add this rule?", "yns") == "y"):
                    rid = f"learned-{slug(x.get('title', 'rule'))}"
                    rules.append({"id": rid, "files": rule.get("files") or r"\.(php|html?|js)$", "match": rule["match"],
                                  "severity": x.get("severity", 2), "title": x.get("title"), "fix": x.get("fix"),
                                  "learned": {"from": str(f.parent.relative_to(DATA)), "date": f"{datetime.date.today()}",
                                              "bad_example": rule.get("bad_example"), "good_example": rule.get("good_example")}})
                    added_rules += 1
                    decisions.append({"title": x.get("title"), "result": f"rule {rid}"})
                    continue
                if ok:
                    decisions.append({"title": x.get("title"), "result": "skipped"})
                    continue
            # no rule possible (logic problem) or rule rejected: offer it as a manual check
            if args.yes or ask("  Add to the manual checklist (shown in every code report)?", "yn") == "y":
                checklist.append({"title": x.get("title"), "why": x.get("why"), "from": str(f.parent.relative_to(DATA))})
                added_checks += 1
                decisions.append({"title": x.get("title"), "result": "checklist"})
            else:
                decisions.append({"title": x.get("title"), "result": "rejected"})
        (f.parent / "learned.json").write_text(json.dumps(decisions, indent=1))
    CODE_RULES.write_text("[\n" + ",\n".join("  " + json.dumps(r) for r in rules) + "\n]\n")
    CHECKLIST.write_text(json.dumps(checklist, indent=1))
    print(f"\nAdded {added_rules} rule(s) and {added_checks} checklist item(s). "
          f"Share them with the team: audit rules export")


def rule_stats(c, fired):
    c.execute("CREATE TABLE IF NOT EXISTS rule_stats (rule TEXT PRIMARY KEY, fires INTEGER DEFAULT 0, false_alarms INTEGER DEFAULT 0)")
    for rid in fired:
        c.execute("INSERT INTO rule_stats (rule, fires) VALUES (?, 1) ON CONFLICT(rule) DO UPDATE SET fires = fires + 1", (rid,))


def cmd_rules(args):
    c = db()
    rule_stats(c, [])
    if args.action == "false-alarm":
        c.execute("INSERT INTO rule_stats (rule, false_alarms) VALUES (?, 1) "
                  "ON CONFLICT(rule) DO UPDATE SET false_alarms = false_alarms + 1", (args.rule,))
        c.commit()
        print(f"Noted: {args.rule} gave a false alarm.")
    elif args.action == "export":
        out = DATA / f"rules-export-{datetime.date.today()}.zip"
        with zipfile.ZipFile(out, "w") as z:
            for name in RULE_FILES:
                p = Path(__file__).with_name(name)
                if p.exists():
                    z.write(p, name)
        print(f"Saved reports/{out.name}. Send it to the team; they run: audit rules import {out.name}")
    elif args.action == "import":
        added = 0
        with zipfile.ZipFile(DATA / args.rule) as z:
            for name in RULE_FILES:
                if name not in z.namelist():
                    continue
                theirs, p = json.loads(z.read(name)), Path(__file__).with_name(name)
                mine = json.loads(p.read_text()) if p.exists() else []
                key = (lambda r: r.get("id") or r.get("title"))
                have = {key(r) for r in mine}
                new = [r for r in theirs if key(r) not in have]
                if new:
                    p.write_text(json.dumps(mine + new, indent=1) if name == "manual_checklist.json" else
                                 "[\n" + ",\n".join("  " + json.dumps(r) for r in mine + new) + "\n]\n")
                added += len(new)
        print(f"Imported {added} new rule(s)/check(s). Rules you already had were kept as they are.")
    else:  # list
        stats = {r: (f, fa) for r, f, fa in c.execute("SELECT rule, fires, false_alarms FROM rule_stats")}
        for name in ("code_rules.json", "rules.json"):
            print(f"\n{name}")
            for r in json.loads(Path(__file__).with_name(name).read_text()):
                f, fa = stats.get(r["id"], (0, 0))
                flag = "  <- noisy, review it" if f >= 5 and fa / f > 0.3 else ""
                print(f"  {r['id']:38} fired {f:3}  false alarms {fa:3}{flag}")


# ---------- self-check ----------

def demo():
    assert median([3, None, 1, 2]) == 2 and median([None]) is None
    # CLS = worst session window, not the sum of all shifts
    assert cls_windows([{"t": 0, "v": .05}, {"t": 500, "v": .05}, {"t": 9000, "v": .08}]) == 0.1
    assert cls_windows([]) == 0
    h = parse_html('<link href="https://fonts.googleapis.com/x"><img src="/a.png"><img src="b" width=1 height=2>'
                   '<script src="//cdn.jsdelivr.net/x.js"></script><img src="https://cdn.site.com/a"><div class="a b">', "site.com")
    assert h["external"] == ["cdn.jsdelivr.net", "fonts.googleapis.com"] and h["img_no_size"] == 2 and h["classes"] == ["a", "b"]
    assert fingerprint_similarity(["a", "b"], ["a", "b"]) == 1 and fingerprint_similarity(["a"], ["z"]) == 0
    assert to_ms("1.9s") == 1900 and to_ms("378ms") == 378 and to_ms(None) is None
    facts = {"visitor": {"mobile": {"cls": .23}, "shifts": [{"el": "div#chat-widget-container", "v": .12}]},
             "server": {"cache": "DYNAMIC", "ttfb_ms": 300}, "html": {"external": ["fonts.gstatic.com"]},
             "lab": {"mobile": {"lcp_ms": 2000}}}
    ids = [r["id"] for r in evaluate(json.loads(RULES.read_text()), facts)]
    assert {"visitor-cls", "chat-widget-shift", "server-cache", "external-fonts"} <= set(ids), ids
    assert "lab-mobile-lcp" not in ids and "server-ttfb" not in ids and "gtmetrix-perf" not in ids
    facts.update({"html": {"external": [], "img_no_size": 0}, "server": {**facts["server"], "ttfb_all": [300], "encoding": "gzip"}})
    md = render("https://x.com/", "prelaunch", "now", facts, evaluate(json.loads(RULES.read_text()), facts), [], None)
    assert "Chat widget moves the page" in md and "chat-widget-container" in md
    # fixes 1-4: form token in cached HTML, ad-click cache miss, lost gclid, exposed files, lab TTFB
    assert parse_html('<form><input type="hidden" name="csrf_token" value="x"></form>', "x.com")["form_token"]
    f2 = {"server": {"ad_click": {"cache_miss": True, "params_lost": True}, "exposed": ["https://x.com/.git/HEAD"]},
          "html": {"cached_token": True}, "lab": {}}; f2["server"]["backend_ms"] = 900
    ids2 = {r["id"] for r in evaluate(json.loads(RULES.read_text()), f2)}
    assert {"ad-click-cache-miss", "ad-click-params-lost", "exposed-files", "cached-form-token", "server-ttfb"} <= ids2, ids2
    f2["server"] = {"ad_click": {"cache_miss": False, "params_lost": False}, "exposed": []}
    f2["html"]["cached_token"], f2["server"]["backend_ms"] = False, 150
    assert not {r["id"] for r in evaluate(json.loads(RULES.read_text()), f2)}
    # real-user data (shapes from the CrUX API docs; Cloudflare shape = our query)
    g = crux_parse({"record": {"metrics": {"largest_contentful_paint": {"percentiles": {"p75": 2900}},
                                           "cumulative_layout_shift": {"percentiles": {"p75": "0.25"}}}}})
    assert g["lcp_ms"] == 2900 and g["cls"] == 0.25 and g["inp_ms"] is None
    cf = cf_parse({"data": {"viewer": {"accounts": [{"rumWebVitalsEventsAdaptiveGroups": [
        {"count": 40, "dimensions": {"deviceType": "mobile"}, "quantiles": {"largestContentfulPaintP75": 3100,
         "interactionToNextPaintP75": 180, "cumulativeLayoutShiftP75": 0.3, "firstContentfulPaintP75": None}},
        {"count": 5, "dimensions": {"deviceType": "desktop"}, "quantiles": {"largestContentfulPaintP75": 900,
         "interactionToNextPaintP75": 40, "cumulativeLayoutShiftP75": 0.0, "firstContentfulPaintP75": 500}}]}]}}})
    assert cf["phone"]["lcp_ms"] == 3100 and cf["phone"]["visits"] == 40 and cf["desktop"]["fcp_ms"] == 500
    live = {**facts, "field": {"google": {"phone": {**g, "level": "page"}}, "cloudflare": cf}}
    ids = [r["id"] for r in evaluate(json.loads(RULES.read_text()), live)]
    assert {"real-cls-phone", "real-lcp-phone", "cf-cls-phone", "cf-lcp-phone"} <= set(ids) and "cf-inp-phone" not in ids
    md = render("https://x.com/", "live", "now", live, [], [], None)
    assert "## Real users" in md and "| LCP | 2.9 s | - | 3.1 s | 900 ms | 2.5 s |" in md, md
    # code rules: each planted mistake is found, clean code is not
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.php").write_text("<?php\nvar_dump($x);\n$pass = 'hunter22';\n$r = mysqli_query($c, 'x' . $_GET['id']);\n"
                                    "echo $_GET['q'];\n?><form method=\"post\"><img src=a.png></form>\n")
        (root / "ok.php").write_text("<?php\n$s = $db->prepare('SELECT 1');\necho htmlspecialchars($q);\n"
                                     "?><form method=\"post\"><input type=hidden name=csrf></form><img src=a width=1 height=1>\n")
        (root / "old.bak").write_text("x")
        found = {(h["rule"], h["file"], h["line"]) for h in scan_patterns(root, json.loads(CODE_RULES.read_text()))}
        assert {("php-var-dump", "a.php", 2), ("hardcoded-password", "a.php", 3), ("php-sql-concat", "a.php", 4),
                ("php-echo-input", "a.php", 5), ("form-no-csrf", "a.php", 6), ("img-no-size-code", "a.php", 6)} <= found, found
        assert not [f for f in found if f[1] == "ok.php"], found
        assert [rel for _, rel in code_files(root) if RISKY_FILE.search(rel)] == ["old.bak"]
        # learning: a proposed rule must catch its bad example, skip the good one, not duplicate, not fire everywhere
        existing = [r for r in json.loads(CODE_RULES.read_text()) if not r["id"].startswith("learned-")]
        good_rule = {"files": r"\.(php|html)$", "match": r"target=[\"']_blank[\"'](?![^>]*rel=)",
                     "bad_example": '<a href="x" target="_blank">', "good_example": '<a href="x" target="_blank" rel="noopener">'}
        assert check_rule(good_rule, existing, [root])[0]
        assert not check_rule({**good_rule, "match": "target"}, existing, [root])[0]          # flags the good example
        assert not check_rule({**good_rule, "match": "(("}, existing, [root])[0]              # broken pattern
        assert "already covered" in check_rule({"match": r"var_dump\(", "bad_example": "var_dump($a);"}, existing, [root])[1][0]
    print("demo ok")


def main():
    if len(sys.argv) == 1:
        return demo()
    ap = argparse.ArgumentParser(prog="audit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("prelaunch", "before launch: lab + real-visitor test + server + GTmetrix x1"),
                      ("live", "ads running: same tests + Google and Cloudflare real-user data")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("url")
        p.add_argument("--runs", type=int, default=3)
        p.add_argument("--no-ai", action="store_true")
        p.add_argument("--local-lab", action="store_true", help="use this laptop's Lighthouse even with a Google key (e.g. staging)")
        if name == "prelaunch":
            p.add_argument("--no-gtmetrix", action="store_true")
        else:
            p.add_argument("--gtmetrix", action="store_true", help="also run GTmetrix once (uses a free test)")
        p.set_defaults(fn=cmd_audit)
    p = sub.add_parser("code", help="code audit of a site folder (mounted at /code, set CODE=<folder>)")
    p.add_argument("path", nargs="?", default="/code")
    p.add_argument("--url", help="live page URL: links speed problems to code lines and checks what is deployed")
    p.add_argument("--name", help="name for reports (default: the URL's domain)")
    p.add_argument("--no-ai", action="store_true")
    p.add_argument("--no-semgrep", action="store_true", help="skip Semgrep (it needs internet)")
    p.set_defaults(fn=cmd_code)
    p = sub.add_parser("learn", help="review Claude's new findings and turn them into rules")
    p.add_argument("--yes", action="store_true", help="accept every finding that passes the checks (no questions)")
    p.set_defaults(fn=cmd_learn)
    p = sub.add_parser("rules", help="list rules, mark a false alarm, export/import for the team")
    p.add_argument("action", nargs="?", default="list", choices=["list", "false-alarm", "export", "import"])
    p.add_argument("rule", nargs="?", help="rule id (false-alarm) or zip file name in reports/ (import)")
    p.set_defaults(fn=cmd_rules)
    p = sub.add_parser("history", help="past audits for a domain")
    p.add_argument("domain")
    p.set_defaults(fn=cmd_history)
    p = sub.add_parser("clean", help="delete audits")
    p.add_argument("domain", nargs="?")
    p.add_argument("--older-than", type=int, metavar="DAYS")
    p.set_defaults(fn=cmd_clean)
    args = ap.parse_args()
    if args.cmd == "clean" and not (args.domain or args.older_than):
        ap.error("clean needs a domain or --older-than DAYS")
    args.fn(args)


if __name__ == "__main__":
    main()
