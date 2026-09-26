"""
PC Parts Price Tracker
Uses Playwright (real browser) to scrape prices from Newegg, Amazon, eBay,
and Facebook Marketplace, then generates a morning HTML report.

Run manually:  python price_tracker.py
Schedule it:   run setup_scheduler.bat as Administrator
Add items:     edit config.json
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};
const orig = navigator.permissions.query;
navigator.permissions.query = (p) => p.name === 'notifications' ? Promise.resolve({state: 'denied'}) : orig(p);
"""


def parse_price(text):
    """Extract the first plausible dollar price from a string."""
    if not text:
        return None
    text = str(text).strip()
    match = re.search(r"\$?([\d,]+(?:\.\d{1,2})?)", text.replace(",", ""))
    if match:
        try:
            val = float(match.group(1))
            if 5 < val < 100_000:
                return val
        except ValueError:
            pass
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _capacity_gb(text):
    """Return capacity in GB if found (TB converted), else None."""
    t = _norm(text)
    m = re.search(r"\b(\d+)\s*tb\b", t)
    if m:
        return int(m.group(1)) * 1024
    m = re.search(r"\b(\d+)\s*gb\b", t)
    if m:
        return int(m.group(1))
    return None


def _gpu_model(text):
    """Parse GPU model: (series, number, ti, super) e.g. ('rtx', '4070', False, True)."""
    t = _norm(text)
    m = re.search(r"\b(rtx|rx)\s*(\d{3,4})\s*(ti)?\s*(super)?\b", t)
    if not m:
        return None
    return (m.group(1), m.group(2), bool(m.group(3)), bool(m.group(4)))


def title_matches_item(title, item):
    """Reject mismatched SKUs so a 64GB kit doesn't become the 'best' 32GB price."""
    title_n = _norm(title)
    name = item.get("name", "")
    name_n = _norm(name)
    cat = (item.get("category") or "").upper()

    junk = ("laptop", "notebook", "chromebook", "empty box", "warranty only", "for parts",
            "not working", "as-is", "as is", "broken", "damaged", "defective", "defect",
            "no gpu", "board only", "die shot")
    if any(j in title_n for j in junk) and "laptop" not in name_n:
        return False

    if cat == "GPU":
        # Reject whole PCs / notebooks sold as "has a 4070"
        if any(x in title_n for x in (
            "gaming pc", "desktop", "prebuilt", "pre built", "whole pc",
            "complete pc", "sff pc", "custom pc", "pc build", "gaming desktop",
        )):
            return False
        want = _gpu_model(name) or _gpu_model(" ".join(item.get("search_terms", {}).values()))
        got = _gpu_model(title)
        if want and got:
            if got[0] != want[0] or got[1] != want[1]:
                return False
            # Exact variant: 4070 must not match 4070 Ti / 4070 Super
            if got[2] != want[2] or got[3] != want[3]:
                return False
        elif want and not got:
            # Title must at least mention the model number
            if want[1] not in title_n:
                return False
        return True

    if cat == "RAM":
        want_gb = _capacity_gb(name)
        if want_gb is None:
            return True
        # Prefer explicit capacity in title; reject clearly wrong total sizes
        title_gb = _capacity_gb(title)
        # Kit notation 2x16 => 32
        kit = re.search(r"\b(\d+)\s*x\s*(\d+)\s*gb\b", title_n)
        if kit:
            title_gb = int(kit.group(1)) * int(kit.group(2))
        if title_gb is not None and title_gb != want_gb:
            # Allow 2x16 when wanting 32 even if parser saw 16 first — handled above via kit
            return False
        # Generation match
        if "ddr5" in name_n and "ddr4" in title_n and "ddr5" not in title_n:
            return False
        if "ddr4" in name_n and "ddr5" in title_n and "ddr4" not in title_n:
            return False
        if f"{want_gb}gb" not in title_n.replace(" ", "") and not kit:
            # Soft require capacity token when present in name
            if want_gb >= 16:
                return False
        return True

    if cat == "SSD":
        want_gb = _capacity_gb(name)
        title_gb = _capacity_gb(title)
        if want_gb and title_gb and title_gb != want_gb:
            return False
        title_compact = title_n.replace(" ", "")
        # Model tokens only — skip pure capacity like 2tb / 2000gb
        tokens = []
        for tok in re.findall(r"[a-z]*\d+[a-z0-9]*", name_n):
            if len(tok) < 3:
                continue
            if re.fullmatch(r"\d+tb", tok) or re.fullmatch(r"\d+gb", tok):
                continue
            tokens.append(tok)
        if tokens and not any(tok in title_compact for tok in tokens):
            return False
        # Brand cue when name has a known brand
        for brand in ("samsung", "wd", "western digital", "crucial", "seagate", "sabrent", "hynix"):
            if brand in name_n and brand not in title_n and brand.split()[0] not in title_n:
                # "wd" vs "western digital"
                if brand == "western digital" and ("wd" in title_n or "westerndigital" in title_compact):
                    continue
                if brand == "wd" and ("western" in title_n or "westerndigital" in title_compact):
                    continue
                return False
        return True

    return True


def filter_results(results, item):
    """Filter each retailer's hits by title match and optional min_price floor."""
    filtered = {}
    min_price = item.get("min_price")
    try:
        min_price = float(min_price) if min_price is not None else None
    except (TypeError, ValueError):
        min_price = None

    for r_key, res_list in results.items():
        kept = []
        dropped_title = 0
        dropped_min = 0
        for r in res_list:
            if not title_matches_item(r.get("name", ""), item):
                dropped_title += 1
                continue
            if min_price is not None and r.get("price") is not None and r["price"] < min_price:
                dropped_min += 1
                continue
            kept.append(r)
        filtered[r_key] = kept
        if dropped_title:
            print(f"      [{r_key}] filtered out {dropped_title} mismatched title(s)")
        if dropped_min:
            print(f"      [{r_key}] filtered out {dropped_min} below min ${min_price:,.2f}")
    return filtered


def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def load_history(history_file):
    if os.path.exists(history_file):
        with open(history_file, "r") as f:
            return json.load(f)
    return {}


def save_history(history_file, history):
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2)


def notify_deals(deals):
    """Windows toast when any item is at/below target. No-op if none."""
    if not deals or os.name != "nt":
        return
    title = f"{len(deals)} PC part deal{'s' if len(deals) != 1 else ''}"
    body = "; ".join(f"{d['name']}: ${d['price']:,.2f}" for d in deals[:4])
    # Escape for PowerShell single-quoted string
    title_ps = title.replace("'", "''")
    body_ps = body.replace("'", "''")
    script = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
        f"$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        f"[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$text = $template.GetElementsByTagName('text'); "
        f"$text.Item(0).AppendChild($template.CreateTextNode('{title_ps}')) | Out-Null; "
        f"$text.Item(1).AppendChild($template.CreateTextNode('{body_ps}')) | Out-Null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PC Parts Tracker').Show($toast)"
    )
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            timeout=8,
        )
        print(f"  Desktop notification sent ({len(deals)} deal(s)).")
    except Exception as e:
        print(f"  (Notification skipped: {e})")


def sparkline_svg(prices, width=140, height=32):
    """Tiny SVG sparkline from a list of prices (oldest → newest)."""
    pts = [p for p in prices if p is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(pts), max(pts)
    span = (hi - lo) or 1.0
    pad = 2
    coords = []
    for i, p in enumerate(pts):
        x = pad + (width - 2 * pad) * i / (len(pts) - 1)
        y = height - pad - (height - 2 * pad) * ((p - lo) / span)
        coords.append(f"{x:.1f},{y:.1f}")
    last = pts[-1]
    color = "#3dd68c" if last <= lo * 1.02 else ("#f5c542" if last > (lo + hi) / 2 else "#4f8ef7")
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-label="Price trend">'
        f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{" ".join(coords)}"/>'
        f'<circle cx="{coords[-1].split(",")[0]}" cy="{coords[-1].split(",")[1]}" r="2.5" fill="{color}"/>'
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# Browser context manager
# ---------------------------------------------------------------------------

class BrowserSession:
    def __init__(self, headless=True):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        launch_kwargs = dict(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        # Prefer installed Chrome; fall back to bundled Chromium
        try:
            self._browser = self._pw.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            self._browser = self._pw.chromium.launch(**launch_kwargs)

        self._context = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/Chicago",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        self._context.add_init_script(STEALTH_SCRIPT)
        return self

    def __exit__(self, *_):
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def new_page(self):
        return self._context.new_page()

    def fetch(self, url, wait_until="domcontentloaded", timeout=25000, wait_for_selector=None):
        """Navigate to URL and return BeautifulSoup of the final HTML."""
        page = self._context.new_page()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            # If Cloudflare challenge, wait for it to resolve
            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, timeout=15000)
                except PlaywrightTimeout:
                    pass
            else:
                # Extra wait for JS-rendered content
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass
            html = page.content()
        finally:
            page.close()
        return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# Scrapers (one per retailer)
# ---------------------------------------------------------------------------

def scrape_newegg(browser: BrowserSession, query: str, max_results=3):
    results = []
    url = f"https://www.newegg.com/p/pl?d={requests.utils.quote(query)}&N=4131"
    try:
        soup = browser.fetch(url, wait_for_selector=".item-cell")
        items = soup.select(".item-cell")
        for item in items:
            name_el = item.select_one("a.item-title")
            price_el = item.select_one(".price-current")
            if not (name_el and price_el):
                continue
            # Price can be in <strong>+<sup> or as plain text
            strong = price_el.select_one("strong")
            if strong:
                sups = price_el.select("sup")
                cents = sups[-1].get_text(strip=True).lstrip(".") if sups else "00"
                raw = strong.get_text(strip=True).replace("$", "").replace(",", "") + "." + cents
            else:
                raw = price_el.get_text(strip=True)
            price = parse_price(raw)
            if not price:
                continue
            href = name_el.get("href", "")
            if not href.startswith("http"):
                href = "https://www.newegg.com" + href
            results.append({"name": name_el.get_text(strip=True)[:90], "price": price, "url": href, "retailer": "Newegg"})
            if len(results) >= max_results:
                break
    except Exception as e:
        print(f"    [Newegg error] {e}")
    return sorted(results, key=lambda x: x["price"])


def scrape_amazon(browser: BrowserSession, query: str, max_results=3):
    results = []
    url = f"https://www.amazon.com/s?k={requests.utils.quote(query)}"
    try:
        soup = browser.fetch(url, wait_for_selector='[data-component-type="s-search-result"]')
        items = soup.select('[data-component-type="s-search-result"]')
        for item in items:
            # Full title lives in h2; product links always contain /dp/ (ASIN)
            name_el = item.select_one("h2")
            link_el = item.select_one("a[href*='/dp/']")
            if not (name_el and link_el):
                continue
            # .a-offscreen has the full "$369.99" string; skip "List: $X" entries
            price = None
            for el in item.select(".a-offscreen"):
                txt = el.get_text(strip=True)
                if txt.startswith("$") and "List" not in txt:
                    price = parse_price(txt)
                    if price:
                        break
            if not price:
                continue
            href = link_el.get("href", "")
            if not href.startswith("http"):
                href = "https://www.amazon.com" + href
            # Try h2 text first; if it's just a brand name, fall back to the URL slug
            name = " ".join(name_el.get_text(strip=True).split())[:90]
            if len(name) < 25:
                # Try a dedicated title span before falling back to URL
                title_span = item.select_one("span.a-text-normal") or item.select_one("span.a-size-medium")
                if title_span:
                    candidate = title_span.get_text(strip=True)[:90]
                    if len(candidate) > len(name):
                        name = candidate
            if len(name) < 25:
                # Extract name from URL: "/Brand-Model-Name/dp/ASIN" → "Brand Model Name"
                slug = href.split("/dp/")[0].rstrip("/").split("/")[-1]
                if slug and len(slug) > len(name):
                    name = slug.replace("-", " ")[:90]
            results.append({"name": name, "price": price, "url": href, "retailer": "Amazon"})
            if len(results) >= max_results:
                break
    except Exception as e:
        print(f"    [Amazon error] {e}")
    return sorted(results, key=lambda x: x["price"])




def _ebay_shipping(card):
    """Return (shipping_cost_or_None, label). Free → 0; +$X delivery → X; else unknown."""
    text = card.get_text(" ", strip=True)
    if re.search(r"\bfree\s+(delivery|shipping)\b", text, re.I):
        return 0.0, "free"
    m = re.search(
        r"\+\s*\$?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:delivery|shipping)",
        text,
        re.I,
    )
    if m:
        try:
            return float(m.group(1).replace(",", "")), "paid"
        except ValueError:
            pass
    return None, "unknown"


def _ebay_seller_reviews(card):
    """Parse seller feedback count from card text, e.g. '99.9% positive (8.6K)' → 8600."""
    text = card.get_text(" ", strip=True)
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*%\s*positive\s*\(\s*([\d,.]+)\s*([KkMm])?\s*\)",
        text,
        re.I,
    )
    if not m:
        m = re.search(r"positive\s*\(\s*([\d,.]+)\s*([KkMm])?\s*\)", text, re.I)
        if not m:
            return None
        raw, suffix = m.group(1), m.group(2)
    else:
        raw, suffix = m.group(2), m.group(3)
    try:
        num = float(raw.replace(",", ""))
    except ValueError:
        return None
    suf = (suffix or "").upper()
    if suf == "K":
        num *= 1000
    elif suf == "M":
        num *= 1_000_000
    return int(num)


# Sellers with this many reviews or fewer are dropped from eBay results
EBAY_MIN_SELLER_REVIEWS = 6  # "5 or less" filtered → need 6+


def _parse_ebay_cards(soup, max_results=3):
    """Parse eBay search cards from BeautifulSoup HTML. Price includes shipping when listed."""
    results = []
    junk_title = ("shop on ebay", "results matching fewer", "related searches")
    junk_listing = (
        "for parts", "parts only", "parts re", "not working", "as-is", "as is",
        "broken", "no gpu", "empty box", "board only", "die shot",
    )

    for card in soup.select("li.s-card"):
        title_el = card.select_one(".s-card__title")
        price_el = card.select_one(".s-card__price")
        link_el = card.select_one("a[href*='/itm/']")
        if not (title_el and price_el and link_el):
            continue

        name = title_el.get_text(" ", strip=True)
        name = re.sub(r"\s*Opens in a new window or tab\s*", "", name, flags=re.I).strip()
        name = re.sub(r"^New Listing\s*", "", name, flags=re.I).strip()
        name_l = name.lower()
        if not name or any(j in name_l for j in junk_title):
            continue
        if any(j in name_l for j in junk_listing):
            continue
        # "Parts" / "Part" listings (damaged boards) — keep "spare parts" kits out of GPU noise
        if re.search(r"\bparts?\b", name_l) and "spare" not in name_l:
            continue

        reviews = _ebay_seller_reviews(card)
        if reviews is None or reviews < EBAY_MIN_SELLER_REVIEWS:
            continue

        href = link_el.get("href", "")
        if "/itm/123456" in href:
            continue
        if href and not href.startswith("http"):
            href = "https://www.ebay.com" + href

        raw_price = price_el.get_text(" ", strip=True)
        # Ranges like "$470.00 to $500.00" — take the low end
        if " to " in raw_price.lower():
            raw_price = re.split(r"\s+to\s+", raw_price, flags=re.I)[0]
        item_price = parse_price(raw_price)
        if not item_price:
            continue

        shipping, ship_kind = _ebay_shipping(card)
        if shipping is not None:
            total = round(item_price + shipping, 2)
        else:
            total = item_price  # shipping not on card — show item price, flag below

        results.append({
            "name": name[:90],
            "price": total,
            "item_price": item_price,
            "shipping": shipping,
            "shipping_kind": ship_kind,
            "seller_reviews": reviews,
            "url": href.split("?")[0] if href else href,
            "retailer": "eBay",
        })
        if len(results) >= max_results:
            break
    return results


def _ebay_search_url(query: str) -> str:
    # Buy It Now only; sort by price + shipping (lowest). Keep URL simple —
    # complex condition filters trigger eBay's error page for automated Chrome.
    return (
        f"https://www.ebay.com/sch/i.html?_nkw={requests.utils.quote(query)}"
        f"&_sacat=0&LH_BIN=1&rt=nc&_sop=15"
    )


def _ebay_worker_main():
    """Subprocess entry: scrape eBay headed and print JSON to stdout."""
    import json as _json

    query = sys.argv[2]
    max_results = int(sys.argv[3])
    url = _ebay_search_url(query)

    from playwright.sync_api import sync_playwright

    html = ""
    with sync_playwright() as pw:
        try:
            headed = pw.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            headed = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        context = headed.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()
        try:
            # Homepage warmup — without this, search often returns eBay's error page
            page.goto("https://www.ebay.com/", wait_until="domcontentloaded", timeout=45000)
            time.sleep(1.5)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("li.s-card .s-card__title", timeout=15000)
            except Exception:
                pass
            time.sleep(1.5)
            html = page.content()
        finally:
            context.close()
            headed.close()

    results = _parse_ebay_cards(
        BeautifulSoup(html, "html.parser"),
        max_results=max(max_results * 3, 12),
    )
    print(_json.dumps(results))


def scrape_ebay(browser: BrowserSession, query: str, max_results=3):
    """
    eBay Buy-It-Now listings.
    Runs in a separate headed Chrome subprocess — eBay blocks headless automation.
    A Chrome window may flash briefly per search (~15–25s each).
    """
    import json as _json
    import subprocess
    import sys as _sys

    # Skip in-session headless attempt — eBay returns an error page almost always.
    print("      eBay via headed Chrome subprocess…")
    try:
        proc = subprocess.run(
            [_sys.executable, str(Path(__file__).resolve()), "--ebay-worker", query, str(max_results)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=90,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[-400:]
            print(f"    [eBay worker error] {err}")
            return []
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            print("    [eBay worker] no output")
            return []
        results = _json.loads(lines[-1])
        return sorted(results, key=lambda x: x["price"])
    except Exception as e:
        print(f"    [eBay error] {e}")
        return []


def _fb_search_url(query: str, radius_miles: int = 100) -> str:
    """Build Marketplace search URL. ZIP is applied via the location picker UI."""
    q = requests.utils.quote(query)
    r = int(radius_miles) if radius_miles else 100
    if r not in (1, 2, 5, 10, 20, 40, 60, 80, 100, 250, 500):
        r = 100
    return (
        f"https://www.facebook.com/marketplace/search/?query={q}"
        f"&exact=false&radius={r}&sortBy=distance_ascend"
    )


def _fb_apply_zip_and_radius(page, zipcode: str, radius_miles: int = 100) -> bool:
    """Open Marketplace location picker, enter ZIP, set radius (miles), Apply."""
    zipcode = (zipcode or "").strip()
    if not zipcode:
        return False

    opened = False
    for sel in (
        'div[role="button"]:has-text("Within")',
        'div[role="button"]:has-text("mi")',
        'div[role="button"]:has-text("km")',
        '[aria-label*="Change location"]',
        '[aria-label*="Location"]',
    ):
        try:
            page.locator(sel).first.click(timeout=2500)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        try:
            page.get_by_text(re.compile(r"Within\s+\d+", re.I)).first.click(timeout=2500)
            opened = True
        except Exception:
            pass
    if not opened:
        print("      [Facebook] could not open location picker", file=sys.stderr)
        return False

    time.sleep(0.8)

    typed = False
    for sel in (
        'div[role="dialog"] input[type="search"]',
        'div[role="dialog"] input[type="text"]',
        'div[role="dialog"] input',
        'input[placeholder*="location" i]',
        'input[placeholder*="city" i]',
        'input[placeholder*="ZIP" i]',
        'input[placeholder*="zip" i]',
    ):
        try:
            box = page.locator(sel).first
            box.click(timeout=2000)
            box.fill("")
            box.type(zipcode, delay=60)
            typed = True
            break
        except Exception:
            continue
    if not typed:
        print("      [Facebook] location input not found", file=sys.stderr)
        return False

    time.sleep(1.2)

    picked = False
    for sel in (
        'div[role="dialog"] ul[role="listbox"] li',
        'div[role="dialog"] [role="option"]',
        'div[role="listbox"] [role="option"]',
        'ul[role="listbox"] li',
    ):
        try:
            page.locator(sel).first.click(timeout=3000)
            picked = True
            break
        except Exception:
            continue
    if not picked:
        try:
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            picked = True
        except Exception:
            pass

    time.sleep(0.6)

    try:
        for sel in (
            'div[role="dialog"] [role="combobox"]',
            'div[role="dialog"] label:has-text("Radius") + *',
        ):
            try:
                page.locator(sel).first.click(timeout=1500)
                break
            except Exception:
                continue
        time.sleep(0.3)
        for label in (f"{int(radius_miles)} miles", f"{int(radius_miles)} mi", str(int(radius_miles))):
            try:
                page.get_by_role("option", name=re.compile(re.escape(label), re.I)).first.click(timeout=1500)
                break
            except Exception:
                try:
                    page.get_by_text(
                        re.compile(rf"^{re.escape(str(int(radius_miles)))}\s*(miles|mi)?$", re.I)
                    ).first.click(timeout=1500)
                    break
                except Exception:
                    continue
    except Exception:
        pass

    applied = False
    for name in ("Apply", "Done", "Update", "Save"):
        try:
            page.get_by_role("button", name=name).click(timeout=2000)
            applied = True
            break
        except Exception:
            continue
    if not applied:
        try:
            page.locator('div[role="dialog"] [role="button"]:has-text("Apply")').first.click(timeout=2000)
            applied = True
        except Exception:
            pass

    time.sleep(1.5)
    return applied or picked


def _parse_facebook_cards(soup, max_results=3):
    """Parse Marketplace search cards: '$price | Title | City, ST'."""
    results = []
    seen = set()
    junk = (
        "pc parts", "for parts", "not working", "damaged", "defective",
        "broken", "as-is", "as is",
    )

    for a in soup.select('a[href*="/marketplace/item/"]'):
        href = a.get("href") or ""
        m = re.search(r"/marketplace/item/(\d+)", href)
        if not m:
            continue
        iid = m.group(1)
        if iid in seen:
            continue
        seen.add(iid)

        raw = a.get_text(" | ", strip=True)
        bits = [b.strip() for b in raw.split("|") if b.strip()]
        prices = []
        others = []
        for b in bits:
            bl = b.lower()
            if bl in ("just listed", "pending"):
                continue
            if b.lstrip().startswith("$"):
                pr = parse_price(b)
                if pr:
                    prices.append(pr)
                continue
            others.append(b)

        if not prices or not others:
            continue

        location = ""
        if others and re.search(r",\s*[A-Za-z]{2,}\s*$", others[-1]):
            location = others.pop()
        name = " ".join(others).strip()
        if not name:
            continue
        name_l = name.lower()
        if any(j in name_l for j in junk):
            continue

        if not href.startswith("http"):
            href = "https://www.facebook.com" + href.split("?")[0]
        else:
            href = href.split("?")[0]

        entry = {
            "name": name[:90],
            "price": prices[0],
            "url": href,
            "retailer": "Facebook",
        }
        if location:
            entry["location"] = location[:60]
        results.append(entry)
        if len(results) >= max_results:
            break
    return results


def _fb_worker_main():
    """Subprocess: headed Chrome scrape of Facebook Marketplace → JSON stdout."""
    import json as _json

    query = sys.argv[2]
    max_results = int(sys.argv[3])
    cfg = load_config()
    settings = cfg.get("settings") or {}
    zipcode = str(settings.get("facebook_zipcode") or "").strip()
    legacy_loc = str(settings.get("facebook_location") or "").strip()
    radius = int(settings.get("facebook_radius_miles") or 100)
    url = _fb_search_url(query, radius_miles=radius)

    from playwright.sync_api import sync_playwright

    html = ""
    with sync_playwright() as pw:
        try:
            headed = pw.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception:
            headed = pw.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
        context = headed.new_context(
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            for sel in ('[aria-label="Close"]', 'div[role="dialog"] [aria-label="Close"]'):
                try:
                    page.locator(sel).first.click(timeout=2000)
                    time.sleep(0.8)
                    break
                except Exception:
                    pass

            if zipcode and re.fullmatch(r"\d{5}(-\d{4})?", zipcode):
                print(f"      [Facebook] ZIP {zipcode} · {radius} mi…", file=sys.stderr)
                ok = _fb_apply_zip_and_radius(page, zipcode, radius_miles=radius)
                if not ok:
                    print("      [Facebook] ZIP picker failed — may use IP location", file=sys.stderr)
                else:
                    # Reload search with radius; re-apply zip if FB reset location
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=45000)
                        time.sleep(2)
                        _fb_apply_zip_and_radius(page, zipcode, radius_miles=radius)
                    except Exception:
                        pass
            elif legacy_loc and not re.fullmatch(r"\d{5}(-\d{4})?", legacy_loc):
                slug = legacy_loc.strip("/").lower().replace(" ", "")
                slug_url = (
                    f"https://www.facebook.com/marketplace/{slug}/search/"
                    f"?query={requests.utils.quote(query)}&exact=false&radius={radius}"
                    f"&sortBy=distance_ascend"
                )
                page.goto(slug_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(2)

            try:
                page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=15000)
            except Exception:
                pass
            time.sleep(1.5)
            html = page.content()
        finally:
            context.close()
            headed.close()

    results = _parse_facebook_cards(
        BeautifulSoup(html, "html.parser"),
        max_results=max(max_results * 3, 12),
    )
    print(_json.dumps(results))


def scrape_facebook(browser: BrowserSession, query: str, max_results=3):
    """
    Facebook Marketplace local listings.
    Headed Chrome subprocess (login wall / bot detection). May be empty if FB blocks.
    """
    import json as _json
    import subprocess
    import sys as _sys

    print("      Facebook Marketplace via headed Chrome…")
    try:
        proc = subprocess.run(
            [_sys.executable, str(Path(__file__).resolve()), "--fb-worker", query, str(max_results)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=100,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[-400:]
            print(f"    [Facebook worker error] {err}")
            return []
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            print("    [Facebook worker] no output")
            return []
        results = _json.loads(lines[-1])
        return sorted(results, key=lambda x: x["price"])
    except Exception as e:
        print(f"    [Facebook error] {e}")
        return []


# ---------------------------------------------------------------------------
# Import requests just for URL quoting (not used for HTTP)
# ---------------------------------------------------------------------------
try:
    import requests as _req
    requests = _req
except ImportError:
    from urllib.parse import quote as _quote
    class _FakeRequests:
        class utils:
            @staticmethod
            def quote(s):
                return _quote(s)
    requests = _FakeRequests()


SCRAPERS = {
    "newegg": scrape_newegg,
    "amazon": scrape_amazon,
    "ebay": scrape_ebay,
    "facebook": scrape_facebook,
}

RETAILER_ORDER = ["newegg", "amazon", "ebay", "facebook"]
RETAILER_LABELS = {
    "newegg": "Newegg",
    "amazon": "Amazon",
    "ebay": "eBay",
    "facebook": "Facebook",
}
# Trust notes shown on the report (fair = titles/search can drift)
RETAILER_META = {
    "newegg": {"trust": "good", "note": "Reliable titles & prices"},
    "amazon": {"trust": "fair", "note": "Titles sometimes approximate"},
    "ebay": {"trust": "fair", "note": "Incl. shipping; sellers need 6+ reviews"},
    "facebook": {"trust": "fair", "note": "Local pickup · ZIP + radius from settings"},
}


# ---------------------------------------------------------------------------
# History tracking
# ---------------------------------------------------------------------------

def update_history(history, item_id, results, max_days=30):
    today = datetime.now().strftime("%Y-%m-%d")
    all_prices = [r["price"] for res_list in results.values() for r in res_list]
    best = min(all_prices) if all_prices else None
    history.setdefault(item_id, {})[today] = {"best_price": best, "ts": datetime.now().isoformat()}
    # Prune old entries
    keys = sorted(history[item_id].keys())
    for old_key in keys[: max(0, len(keys) - max_days)]:
        del history[item_id][old_key]


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

CATEGORY_COLORS = {"GPU": "gpu", "RAM": "ram", "SSD": "ssd"}

STATUS_COLORS = {
    "rumored":   ("#7880a0", "#2e3349"),
    "leaked":    ("#f5c542", "#2a2510"),
    "confirmed": ("#4f8ef7", "#0e1a2e"),
    "available": ("#3dd68c", "#0b1f15"),
}


def fmt(p):
    return f"${p:,.2f}" if p else "—"


def load_releases():
    releases_file = BASE_DIR / "releases.json"
    if releases_file.exists():
        with open(releases_file, "r") as f:
            return json.load(f).get("releases", [])
    return []


def build_releases_html(releases):
    if not releases:
        return ""
    rows = []
    for r in releases:
        status = r.get("status", "rumored").lower()
        color, bg = STATUS_COLORS.get(status, STATUS_COLORS["rumored"])
        cat = r.get("category", "")
        cat_colors = {"GPU": "var(--gpu)", "CPU": "#c4823b", "RAM": "var(--ram)", "SSD": "var(--ssd)"}
        cat_color = cat_colors.get(cat, "var(--muted)")
        note = r.get("note", "")
        rows.append(f"""
        <div class="rel-row">
          <div class="rel-top">
            <span class="rel-name">{r['name']}</span>
            <span class="rel-badge" style="color:{color};background:{bg};border-color:{color}33">{status}</span>
          </div>
          <div class="rel-meta">
            <span class="rel-cat" style="color:{cat_color}">{cat}</span>
            <span class="rel-when">{r.get('expected','TBD')}</span>
          </div>
          {f'<div class="rel-note">{note}</div>' if note else ''}
        </div>""")
    return "\n".join(rows)


# PC parts knowledge base for fuzzy search — embedded in HTML as JSON
PARTS_DB_JSON = json.dumps([
    # --- NVIDIA GPUs ---
    {"name": "NVIDIA GeForce RTX 5090",        "short": "RTX 5090",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5090"]},
    {"name": "NVIDIA GeForce RTX 5080 Super",  "short": "RTX 5080 Super",  "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5080s","5080 super"]},
    {"name": "NVIDIA GeForce RTX 5080",        "short": "RTX 5080",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5080"]},
    {"name": "NVIDIA GeForce RTX 5070 Ti Super","short": "RTX 5070 Ti Super","cat": "GPU", "mfr": "NVIDIA", "aliases": ["5070tis"]},
    {"name": "NVIDIA GeForce RTX 5070 Ti",     "short": "RTX 5070 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5070ti","5070 ti"]},
    {"name": "NVIDIA GeForce RTX 5070 Super",  "short": "RTX 5070 Super",  "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5070s","5070 super"]},
    {"name": "NVIDIA GeForce RTX 5070",        "short": "RTX 5070",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5070"]},
    {"name": "NVIDIA GeForce RTX 5060 Ti",     "short": "RTX 5060 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5060ti","5060 ti"]},
    {"name": "NVIDIA GeForce RTX 5060",        "short": "RTX 5060",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5060"]},
    {"name": "NVIDIA GeForce RTX 5050",        "short": "RTX 5050",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["5050"]},
    {"name": "NVIDIA GeForce RTX 4090",        "short": "RTX 4090",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4090"]},
    {"name": "NVIDIA GeForce RTX 4080 Super",  "short": "RTX 4080 Super",  "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4080s","4080 super"]},
    {"name": "NVIDIA GeForce RTX 4080",        "short": "RTX 4080",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4080"]},
    {"name": "NVIDIA GeForce RTX 4070 Ti Super","short": "RTX 4070 Ti Super","cat": "GPU", "mfr": "NVIDIA", "aliases": ["4070tis"]},
    {"name": "NVIDIA GeForce RTX 4070 Ti",     "short": "RTX 4070 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4070ti"]},
    {"name": "NVIDIA GeForce RTX 4070 Super",  "short": "RTX 4070 Super",  "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4070s","4070 super"]},
    {"name": "NVIDIA GeForce RTX 4070",        "short": "RTX 4070",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4070"]},
    {"name": "NVIDIA GeForce RTX 4060 Ti",     "short": "RTX 4060 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4060ti"]},
    {"name": "NVIDIA GeForce RTX 4060",        "short": "RTX 4060",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["4060"]},
    {"name": "NVIDIA GeForce RTX 3090 Ti",     "short": "RTX 3090 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3090ti"]},
    {"name": "NVIDIA GeForce RTX 3090",        "short": "RTX 3090",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3090"]},
    {"name": "NVIDIA GeForce RTX 3080 Ti",     "short": "RTX 3080 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3080ti"]},
    {"name": "NVIDIA GeForce RTX 3080",        "short": "RTX 3080",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3080"]},
    {"name": "NVIDIA GeForce RTX 3070 Ti",     "short": "RTX 3070 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3070ti"]},
    {"name": "NVIDIA GeForce RTX 3070",        "short": "RTX 3070",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3070"]},
    {"name": "NVIDIA GeForce RTX 3060 Ti",     "short": "RTX 3060 Ti",     "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3060ti"]},
    {"name": "NVIDIA GeForce RTX 3060",        "short": "RTX 3060",        "cat": "GPU", "mfr": "NVIDIA", "aliases": ["3060"]},
    # --- AMD GPUs ---
    {"name": "AMD Radeon RX 9070 XT",          "short": "RX 9070 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["rx9070xt","9070xt","9070 xt"]},
    {"name": "AMD Radeon RX 9070",             "short": "RX 9070",         "cat": "GPU", "mfr": "AMD", "aliases": ["rx9070","9070"]},
    {"name": "AMD Radeon RX 9060 XT",          "short": "RX 9060 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["rx9060xt","9060xt"]},
    {"name": "AMD Radeon RX 9060",             "short": "RX 9060",         "cat": "GPU", "mfr": "AMD", "aliases": ["rx9060","9060"]},
    {"name": "AMD Radeon RX 7900 XTX",         "short": "RX 7900 XTX",     "cat": "GPU", "mfr": "AMD", "aliases": ["7900xtx","rx7900xtx"]},
    {"name": "AMD Radeon RX 7900 XT",          "short": "RX 7900 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["7900xt"]},
    {"name": "AMD Radeon RX 7900 GRE",         "short": "RX 7900 GRE",     "cat": "GPU", "mfr": "AMD", "aliases": ["7900gre"]},
    {"name": "AMD Radeon RX 7800 XT",          "short": "RX 7800 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["7800xt"]},
    {"name": "AMD Radeon RX 7700 XT",          "short": "RX 7700 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["7700xt"]},
    {"name": "AMD Radeon RX 7600 XT",          "short": "RX 7600 XT",      "cat": "GPU", "mfr": "AMD", "aliases": ["7600xt"]},
    {"name": "AMD Radeon RX 7600",             "short": "RX 7600",         "cat": "GPU", "mfr": "AMD", "aliases": ["7600"]},
    # --- Intel GPUs ---
    {"name": "Intel Arc B580",                 "short": "Arc B580",        "cat": "GPU", "mfr": "Intel", "aliases": ["b580","arc b580"]},
    {"name": "Intel Arc B770",                 "short": "Arc B770",        "cat": "GPU", "mfr": "Intel", "aliases": ["b770","arc b770"]},
    {"name": "Intel Arc A770",                 "short": "Arc A770",        "cat": "GPU", "mfr": "Intel", "aliases": ["a770","arc a770"]},
    # --- AMD CPUs ---
    {"name": "AMD Ryzen 9 9950X",              "short": "Ryzen 9 9950X",   "cat": "CPU", "mfr": "AMD", "aliases": ["9950x","r9 9950x"]},
    {"name": "AMD Ryzen 9 9900X",              "short": "Ryzen 9 9900X",   "cat": "CPU", "mfr": "AMD", "aliases": ["9900x"]},
    {"name": "AMD Ryzen 7 9700X",              "short": "Ryzen 7 9700X",   "cat": "CPU", "mfr": "AMD", "aliases": ["9700x"]},
    {"name": "AMD Ryzen 5 9600X",              "short": "Ryzen 5 9600X",   "cat": "CPU", "mfr": "AMD", "aliases": ["9600x"]},
    {"name": "AMD Ryzen 9 7950X3D",            "short": "Ryzen 9 7950X3D", "cat": "CPU", "mfr": "AMD", "aliases": ["7950x3d"]},
    {"name": "AMD Ryzen 9 7900X3D",            "short": "Ryzen 9 7900X3D", "cat": "CPU", "mfr": "AMD", "aliases": ["7900x3d"]},
    {"name": "AMD Ryzen 7 7800X3D",            "short": "Ryzen 7 7800X3D", "cat": "CPU", "mfr": "AMD", "aliases": ["7800x3d"]},
    {"name": "AMD Ryzen 7 7700X",              "short": "Ryzen 7 7700X",   "cat": "CPU", "mfr": "AMD", "aliases": ["7700x"]},
    {"name": "AMD Ryzen 5 7600X",              "short": "Ryzen 5 7600X",   "cat": "CPU", "mfr": "AMD", "aliases": ["7600x"]},
    # --- Intel CPUs ---
    {"name": "Intel Core Ultra 9 285K",        "short": "Core Ultra 9 285K","cat": "CPU", "mfr": "Intel", "aliases": ["285k","i9 285k"]},
    {"name": "Intel Core Ultra 7 265K",        "short": "Core Ultra 7 265K","cat": "CPU", "mfr": "Intel", "aliases": ["265k"]},
    {"name": "Intel Core Ultra 5 245K",        "short": "Core Ultra 5 245K","cat": "CPU", "mfr": "Intel", "aliases": ["245k"]},
    {"name": "Intel Core i9-14900K",           "short": "i9-14900K",       "cat": "CPU", "mfr": "Intel", "aliases": ["14900k","i9 14900k"]},
    {"name": "Intel Core i7-14700K",           "short": "i7-14700K",       "cat": "CPU", "mfr": "Intel", "aliases": ["14700k"]},
    {"name": "Intel Core i5-14600K",           "short": "i5-14600K",       "cat": "CPU", "mfr": "Intel", "aliases": ["14600k"]},
    # --- RAM ---
    {"name": "DDR5 32GB Kit (2x16GB)",         "short": "DDR5 32GB Kit",   "cat": "RAM", "mfr": "Various", "aliases": ["ddr5 32gb","32gb ddr5","32 gb ddr5"]},
    {"name": "DDR5 64GB Kit (2x32GB)",         "short": "DDR5 64GB Kit",   "cat": "RAM", "mfr": "Various", "aliases": ["ddr5 64gb","64gb ddr5"]},
    {"name": "DDR5 96GB Kit (2x48GB)",         "short": "DDR5 96GB Kit",   "cat": "RAM", "mfr": "Various", "aliases": ["ddr5 96gb","96gb ddr5"]},
    {"name": "DDR4 32GB Kit (2x16GB)",         "short": "DDR4 32GB Kit",   "cat": "RAM", "mfr": "Various", "aliases": ["ddr4 32gb","32gb ddr4"]},
    {"name": "DDR4 16GB Kit (2x8GB)",          "short": "DDR4 16GB Kit",   "cat": "RAM", "mfr": "Various", "aliases": ["ddr4 16gb","16gb ddr4"]},
    {"name": "Corsair Dominator Titanium DDR5","short": "Corsair Dominator DDR5","cat": "RAM","mfr":"Corsair","aliases":["dominator ddr5","corsair dominator"]},
    {"name": "G.Skill Trident Z5 DDR5",        "short": "Trident Z5",      "cat": "RAM", "mfr": "G.Skill", "aliases": ["trident z5","gskill trident"]},
    # --- SSDs ---
    {"name": "Samsung 990 Pro 2TB",            "short": "990 Pro 2TB",     "cat": "SSD", "mfr": "Samsung", "aliases": ["samsung 990 pro 2tb","990pro 2tb"]},
    {"name": "Samsung 990 Pro 4TB",            "short": "990 Pro 4TB",     "cat": "SSD", "mfr": "Samsung", "aliases": ["samsung 990 pro 4tb"]},
    {"name": "Samsung 980 Pro 2TB",            "short": "980 Pro 2TB",     "cat": "SSD", "mfr": "Samsung", "aliases": ["980 pro 2tb"]},
    {"name": "WD Black SN850X 2TB",            "short": "SN850X 2TB",      "cat": "SSD", "mfr": "WD", "aliases": ["sn850x 2tb","wd black 2tb","sn850 2tb"]},
    {"name": "WD Black SN850X 4TB",            "short": "SN850X 4TB",      "cat": "SSD", "mfr": "WD", "aliases": ["sn850x 4tb"]},
    {"name": "WD Black SN8100 2TB",            "short": "SN8100 2TB",      "cat": "SSD", "mfr": "WD", "aliases": ["sn8100 2tb"]},
    {"name": "Seagate FireCuda 530 2TB",       "short": "FireCuda 530 2TB","cat": "SSD", "mfr": "Seagate", "aliases": ["firecuda 530","seagate firecuda 2tb"]},
    {"name": "SK Hynix Platinum P41 2TB",      "short": "Hynix P41 2TB",   "cat": "SSD", "mfr": "SK Hynix", "aliases": ["p41 2tb","hynix p41"]},
    {"name": "Crucial T705 2TB",               "short": "Crucial T705 2TB","cat": "SSD", "mfr": "Crucial", "aliases": ["t705 2tb","crucial t705"]},
    {"name": "Crucial T500 2TB",               "short": "Crucial T500 2TB","cat": "SSD", "mfr": "Crucial", "aliases": ["t500 2tb"]},
    {"name": "Sabrent Rocket 4 Plus 2TB",      "short": "Rocket 4+ 2TB",   "cat": "SSD", "mfr": "Sabrent", "aliases": ["rocket 4 plus","sabrent rocket"]},
    # ── Spec / category searches (best deal on a spec, not a specific product) ──
    {"name": "Best 1TB NVMe SSD (Any Brand)",  "short": "1TB NVMe",        "cat": "SSD", "mfr": "Any",    "spec": True, "specQuery": "1TB NVMe SSD M.2",     "aliases": ["1tb nvme","1 tb nvme","1tb ssd","nvme 1tb"]},
    {"name": "Best 2TB NVMe SSD (Any Brand)",  "short": "2TB NVMe",        "cat": "SSD", "mfr": "Any",    "spec": True, "specQuery": "2TB NVMe SSD M.2",     "aliases": ["2tb nvme","2 tb nvme","2tb ssd","nvme 2tb"]},
    {"name": "Best 4TB NVMe SSD (Any Brand)",  "short": "4TB NVMe",        "cat": "SSD", "mfr": "Any",    "spec": True, "specQuery": "4TB NVMe SSD M.2",     "aliases": ["4tb nvme","4tb ssd","nvme 4tb"]},
    {"name": "Best 1TB SATA SSD (Any Brand)",  "short": "1TB SATA SSD",    "cat": "SSD", "mfr": "Any",    "spec": True, "specQuery": "1TB SATA SSD 2.5",     "aliases": ["1tb sata","sata ssd 1tb"]},
    {"name": "Best DDR5 32GB Kit (Any Brand)", "short": "DDR5 32GB Any",   "cat": "RAM", "mfr": "Any",    "spec": True, "specQuery": "DDR5 32GB kit 6000",   "aliases": ["ddr5 32 any","cheap ddr5 32","best ddr5 32gb"]},
    {"name": "Best DDR5 64GB Kit (Any Brand)", "short": "DDR5 64GB Any",   "cat": "RAM", "mfr": "Any",    "spec": True, "specQuery": "DDR5 64GB kit",        "aliases": ["ddr5 64 any","cheap ddr5 64"]},
    {"name": "Best DDR4 16GB Kit (Any Brand)", "short": "DDR4 16GB Any",   "cat": "RAM", "mfr": "Any",    "spec": True, "specQuery": "DDR4 16GB 3200 kit",   "aliases": ["ddr4 16 any","cheap ddr4"]},
    {"name": "Best Budget GPU Under $300",     "short": "Budget GPU <$300","cat": "GPU", "mfr": "Any",    "spec": True, "specQuery": "graphics card GPU",    "aliases": ["budget gpu","cheap gpu","gpu under 300","gpu 300"]},
    {"name": "Best Mid-Range GPU (RTX/RX)",    "short": "Mid GPU",         "cat": "GPU", "mfr": "Any",    "spec": True, "specQuery": "graphics card GeForce Radeon", "aliases": ["mid range gpu","midrange gpu","mid gpu"]},
    {"name": "Best Gaming CPU (Any Brand)",    "short": "Gaming CPU",      "cat": "CPU", "mfr": "Any",    "spec": True, "specQuery": "gaming CPU processor Ryzen Intel", "aliases": ["gaming cpu","best cpu","cpu deal"]},
])


def build_report(config, all_item_data, history):
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")
    dt_full = now.strftime("%B %d, %Y at %I:%M %p")

    # Build tracked IDs for search JS to know what's already watched
    tracked_ids = [item["id"] for item in config.get("items", [])]
    tracked_ids_json = json.dumps(tracked_ids)

    categories = {}
    for d in all_item_data:
        cat = d["item"].get("category", "Other")
        categories.setdefault(cat, []).append(d)

    total_deals = 0
    deal_rows = []
    categories_parts = []

    for cat, items_in_cat in categories.items():
        cards = []
        for d in items_in_cat:
            item = d["item"]
            retailer_results = d["results"]
            item_id = item["id"]
            target = item.get("target_price")

            all_prices = [
                (r["price"], r["url"], rkey, r["name"])
                for rkey, res_list in retailer_results.items()
                for r in res_list
            ]
            all_prices.sort(key=lambda x: x[0])

            best_price = all_prices[0][0] if all_prices else None
            best_url = all_prices[0][1] if all_prices else "#"
            best_retailer = RETAILER_LABELS.get(all_prices[0][2], all_prices[0][2]) if all_prices else "—"

            is_deal = target and best_price and best_price <= target
            if is_deal:
                total_deals += 1
                deal_rows.append(
                    f'<a class="deal-row" href="{best_url}" target="_blank">'
                    f'<strong>{item["name"]}</strong> {fmt(best_price)} '
                    f'<span>at {best_retailer} (target {fmt(target)})</span></a>'
                )

            if best_price:
                best_html = f"""<div class="best-block">
                  <div class="best-label">Best Price</div>
                  <div class="best-val"><a href="{best_url}" target="_blank">{fmt(best_price)}</a></div>
                  <div class="best-at">at {best_retailer}</div>
                </div>"""
            else:
                best_html = '<div class="best-block"><div class="best-label">Best Price</div><div class="best-val" style="color:var(--muted)">—</div></div>'

            badges = ""
            if target:
                badges += f'<span class="badge badge-target">Target: {fmt(target)}</span>'
            min_price = item.get("min_price")
            if min_price is not None:
                badges += (
                    f'<span class="badge badge-min" title="Listings below this are ignored" '
                    f'onclick="event.stopPropagation(); setMinPrice(\'{item_id}\')" style="cursor:pointer">'
                    f'Min: {fmt(float(min_price))} ✎</span>'
                )
            else:
                badges += (
                    f'<span class="badge badge-min-empty" title="Set a minimum listing price" '
                    f'onclick="event.stopPropagation(); setMinPrice(\'{item_id}\')" style="cursor:pointer">'
                    f'+ Min price</span>'
                )
            if is_deal:
                badges += f'<span class="badge badge-deal">DEAL! Save {fmt(target - best_price)}</span>'

            cells = []
            for r_key in RETAILER_ORDER:
                res_list = retailer_results.get(r_key, [])
                label = RETAILER_LABELS[r_key]
                meta = RETAILER_META.get(r_key, {})
                trust = meta.get("trust", "fair")
                trust_note = meta.get("note", "")
                trust_badge = f'<span class="trust trust-{trust}" title="{trust_note}">{trust}</span>'
                if not res_list:
                    if item.get("search_terms", {}).get(r_key):
                        cells.append(
                            f'<div class="price-cell">'
                            f'<div class="r-name">{label} {trust_badge}</div>'
                            f'<div class="no-data">No results</div></div>'
                        )
                    continue
                top = res_list[0]
                p = top["price"]
                is_best = p == best_price
                price_cls = ""
                if target:
                    if p <= target:
                        price_cls = "good"
                    elif p <= target * 1.1:
                        price_cls = "warn"
                    else:
                        price_cls = "miss"
                cell_cls = "best" if is_best else ("warn" if price_cls == "warn" else "")
                safe_name = top["name"].replace('"', "&quot;")
                href = top.get("url") or "#"
                ship_html = ""
                if r_key == "ebay":
                    ship = top.get("shipping")
                    kind = top.get("shipping_kind")
                    item_p = top.get("item_price")
                    if kind == "free" or ship == 0:
                        ship_html = '<div class="r-ship">incl. free shipping</div>'
                    elif ship is not None:
                        ship_html = (
                            f'<div class="r-ship">incl. ${ship:,.2f} ship'
                            f'{f" (item {fmt(item_p)})" if item_p else ""}</div>'
                        )
                    else:
                        ship_html = '<div class="r-ship">shipping not listed</div>'
                elif r_key == "facebook" and top.get("location"):
                    loc = str(top["location"]).replace('"', "&quot;")
                    ship_html = f'<div class="r-ship">local · {loc}</div>'
                cells.append(f"""<a class="price-cell {cell_cls}" href="{href}" target="_blank" rel="noopener">
                  <div class="r-name">{label} {trust_badge}</div>
                  <div class="r-price {price_cls}">{fmt(p)}</div>
                  {ship_html}
                  <div class="r-item" title="{safe_name}">{top['name'][:55]}</div>
                </a>""")

            item_hist = history.get(item_id, {})
            hist_html = ""
            if item_hist:
                hist_dates = sorted(item_hist.keys())
                hist_prices = [item_hist[d].get("best_price") for d in hist_dates if item_hist[d].get("best_price")]
                if hist_prices:
                    spark = sparkline_svg(hist_prices) if len(hist_prices) > 1 else ""
                    stats = ""
                    if len(hist_prices) > 1:
                        stats = f"""
                      <div class="hist-stat"><span>30-day Low</span><strong>{fmt(min(hist_prices))}</strong></div>
                      <div class="hist-stat"><span>30-day High</span><strong>{fmt(max(hist_prices))}</strong></div>
                      <div class="hist-stat"><span>Average</span><strong>{fmt(sum(hist_prices)/len(hist_prices))}</strong></div>
                      <div class="hist-stat"><span>Days</span><strong>{len(hist_prices)}</strong></div>"""
                    hist_html = f"""<div class="hist-line">
                      {f'<div class="hist-spark">{spark}</div>' if spark else ''}
                      {stats if stats else '<div class="hist-stat"><span>History</span><strong>1 day — more after next runs</strong></div>'}
                    </div>"""

            remove_btn = f'<button class="remove-btn" onclick="removeItem(\'{item_id}\')" title="Remove from watchlist">✕</button>'
            deal_cls = " item-card-deal" if is_deal else ""

            cards.append(f"""<div class="item-card{deal_cls}" id="card-{item_id}">
              <div class="item-header">
                <div><div class="item-name">{item['name']}</div><div class="badges">{badges}</div></div>
                <div style="display:flex;align-items:flex-start;gap:10px">
                  {best_html}
                  {remove_btn}
                </div>
              </div>
              <div class="price-grid">{''.join(cells) if cells else '<div class="no-data">No prices found for any retailer.</div>'}</div>
              {hist_html}
            </div>""")

        categories_parts.append(f"""<div class="cat-section" id="cat-{cat}">
          <div class="cat-title"><span class="cat-dot cat-{cat}"></span>{cat}</div>
          {''.join(cards)}
        </div>""")

    total_items = len(all_item_data)
    priced = sum(1 for d in all_item_data if any(d["results"].values()))

    releases = load_releases()
    releases_html = build_releases_html(releases)

    failed = [d["item"]["name"] for d in all_item_data if not any(d["results"].values())]
    error_banner = ""
    if failed:
        error_banner = f'<div class="error-banner"><strong>No prices found:</strong> {", ".join(failed)} — check search terms in config.json</div>'

    deals_banner = ""
    if deal_rows:
        deals_banner = (
            f'<div class="deals-banner" id="dealsBanner">'
            f'<div class="deals-banner-title">At or below target — {total_deals} deal{"s" if total_deals != 1 else ""}</div>'
            f'{"".join(deal_rows)}</div>'
        )

    trust_legend = " · ".join(
        f'<span class="trust trust-{RETAILER_META[k]["trust"]}" title="{RETAILER_META[k]["note"]}">{RETAILER_LABELS[k]}</span>'
        for k in RETAILER_ORDER
    )

    settings = config.get("settings") or {}
    fb_zip = str(settings.get("facebook_zipcode") or "").strip()
    fb_radius = int(settings.get("facebook_radius_miles") or 100)
    if fb_zip:
        fb_loc_html = f'Facebook Marketplace: ZIP <strong id="fbZipLabel">{fb_zip}</strong> · <span id="fbRadiusLabel">{fb_radius}</span> mi'
    else:
        fb_loc_html = 'Facebook Marketplace: <strong id="fbZipLabel">no ZIP set</strong> · <span id="fbRadiusLabel">100</span> mi'

    # Inline all the dynamic values into the HTML template
    return _build_html(
        date=date_str,
        datetime_full=dt_full,
        total_items=total_items,
        priced=priced,
        total_deals=total_deals,
        error_banner=error_banner,
        deals_banner=deals_banner,
        trust_legend=trust_legend,
        fb_loc_html=fb_loc_html,
        categories_html="\n".join(categories_parts),
        releases_html=releases_html,
        tracked_ids_json=tracked_ids_json,
        parts_db_json=PARTS_DB_JSON,
    )


def _build_html(*, date, datetime_full, total_items, priced, total_deals,
                error_banner, deals_banner, trust_legend, fb_loc_html, categories_html, releases_html,
                tracked_ids_json, parts_db_json):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PC Parts Price Report – {date}</title>
<style>
  :root {{
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #22263a;
    --border: #2e3349;
    --text: #e4e6f0;
    --muted: #7880a0;
    --accent: #4f8ef7;
    --green: #3dd68c;
    --yellow: #f5c542;
    --red: #f56767;
    --gpu: #7c5cbf;
    --ram: #3b9ac4;
    --ssd: #2fa87e;
    --other: #8a6542;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    line-height: 1.5;
  }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}

  /* ── Layout ── */
  .page-wrap {{ display: flex; min-height: 100vh; }}
  .main-col {{ flex: 1 1 0; min-width: 0; padding: 24px 20px 48px; }}
  .sidebar {{ width: 300px; flex-shrink: 0; background: var(--surface); border-left: 1px solid var(--border); padding: 0; }}
  .sidebar-inner {{ position: sticky; top: 0; height: 100vh; overflow-y: auto; padding: 20px 16px; }}
  @media (max-width: 900px) {{
    .page-wrap {{ flex-direction: column; }}
    .sidebar {{ width: 100%; border-left: none; border-top: 1px solid var(--border); }}
    .sidebar-inner {{ position: static; height: auto; }}
  }}

  /* ── Header ── */
  .header {{
    max-width: 860px; margin: 0 auto 20px;
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
    border-bottom: 1px solid var(--border); padding-bottom: 14px;
  }}
  .header h1 {{ font-size: 1.4rem; font-weight: 700; letter-spacing: -0.5px; }}
  .header .ts {{ color: var(--muted); font-size: 0.82rem; margin-left: auto; }}
  .header-actions {{ display: flex; align-items: center; gap: 10px; width: 100%; margin-top: 4px; }}
  .btn-scan {{
    padding: 7px 14px; border-radius: 7px; border: 1px solid var(--accent);
    background: rgba(79,142,247,.12); color: var(--accent); cursor: pointer;
    font-size: 0.82rem; font-weight: 700;
  }}
  .btn-scan:hover {{ background: rgba(79,142,247,.22); }}
  .btn-scan:disabled {{ opacity: .45; cursor: default; }}
  .scan-status {{ font-size: 0.78rem; color: var(--muted); }}
  .scan-status.running {{ color: var(--yellow); }}
  .scan-status.ok {{ color: var(--green); }}
  .scan-status.err {{ color: var(--red); }}

  /* ── Search bar ── */
  .search-section {{ max-width: 860px; margin: 0 auto 20px; }}
  .search-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); font-weight: 700; margin-bottom: 6px; }}
  .search-wrap {{ position: relative; }}
  .search-input {{
    width: 100%; padding: 12px 16px; font-size: 1rem;
    background: var(--surface); border: 1.5px solid var(--border);
    border-radius: 10px; color: var(--text);
    outline: none; transition: border-color .2s;
  }}
  .search-input:focus {{ border-color: var(--accent); }}
  .search-input::placeholder {{ color: var(--muted); }}
  .search-dropdown {{
    position: absolute; top: calc(100% + 4px); left: 0; right: 0; z-index: 100;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; overflow: hidden; display: none;
    box-shadow: 0 8px 32px rgba(0,0,0,.5);
  }}
  .search-dropdown.open {{ display: block; }}
  .dd-item {{
    display: flex; align-items: center; gap: 10px;
    padding: 10px 14px; cursor: pointer; border-bottom: 1px solid var(--border);
    transition: background .12s;
  }}
  .dd-item:last-child {{ border-bottom: none; }}
  .dd-item:hover, .dd-item.active {{ background: var(--surface2); }}
  .dd-cat {{
    font-size: 0.65rem; font-weight: 700; padding: 2px 6px;
    border-radius: 4px; flex-shrink: 0;
  }}
  .dd-cat-GPU  {{ background: rgba(124,92,191,.25); color: var(--gpu); }}
  .dd-cat-CPU  {{ background: rgba(196,130,59,.25);  color: #c4823b; }}
  .dd-cat-RAM  {{ background: rgba(59,154,196,.25);  color: var(--ram); }}
  .dd-cat-SSD  {{ background: rgba(47,168,126,.25);  color: var(--ssd); }}
  .dd-cat-Other{{ background: rgba(138,101,66,.25);  color: var(--other); }}
  .dd-name {{ flex: 1; font-size: 0.9rem; }}
  .dd-mfr {{ font-size: 0.72rem; color: var(--muted); }}
  .dd-tracked {{ font-size: 0.7rem; color: var(--green); font-weight: 600; }}
  .dd-no-results {{ padding: 12px 14px; color: var(--muted); font-size: 0.85rem; font-style: italic; }}

  /* ── Add panel ── */
  .add-panel {{
    max-width: 860px; margin: 0 auto 20px;
    background: var(--surface); border: 1.5px solid var(--accent);
    border-radius: 10px; padding: 14px 16px;
    display: none; align-items: center; gap: 12px; flex-wrap: wrap;
  }}
  .add-panel.visible {{ display: flex; }}
  .add-panel-name {{ font-weight: 600; flex: 1; min-width: 180px; }}
  .add-panel-cat {{ font-size: 0.75rem; color: var(--muted); }}
  .add-price-wrap {{ display: flex; align-items: center; gap: 6px; }}
  .add-price-wrap label {{ font-size: 0.78rem; color: var(--muted); }}
  .add-price-input {{
    width: 90px; padding: 6px 10px; font-size: 0.88rem;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); outline: none;
  }}
  .add-price-input:focus {{ border-color: var(--accent); }}
  .add-cat-select {{
    padding: 6px 10px; font-size: 0.88rem;
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 6px; color: var(--text); outline: none;
  }}
  .dd-custom {{
    border-bottom: 1px solid var(--border);
    background: rgba(79,142,247,.06);
  }}
  .dd-custom .dd-name {{ font-weight: 600; }}
  .dd-hint {{ font-size: 0.68rem; color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: .4px; }}
  .btn-add {{
    padding: 7px 18px; border-radius: 7px; border: none; cursor: pointer;
    font-size: 0.85rem; font-weight: 700;
    background: var(--accent); color: #fff; transition: opacity .15s;
  }}
  .btn-add:hover {{ opacity: .85; }}
  .btn-add:disabled {{ opacity: .4; cursor: default; }}
  .btn-cancel {{
    padding: 7px 12px; border-radius: 7px; border: 1px solid var(--border);
    cursor: pointer; font-size: 0.85rem; background: transparent; color: var(--muted);
  }}
  .add-status {{ font-size: 0.8rem; width: 100%; }}
  .add-status.ok {{ color: var(--green); }}
  .add-status.err {{ color: var(--red); }}

  /* ── Chips ── */
  .summary-bar {{ max-width: 860px; margin: 0 auto 22px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .chip {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 7px 12px; font-size: 0.8rem; color: var(--muted);
  }}
  .chip strong {{ color: var(--text); display: block; font-size: 0.95rem; }}

  /* ── Price cards ── */
  .cat-section {{ max-width: 860px; margin: 0 auto 32px; }}
  .cat-title {{
    font-size: 0.67rem; font-weight: 700; letter-spacing: 1.4px; text-transform: uppercase;
    color: var(--muted); margin-bottom: 10px; padding-left: 2px;
    display: flex; align-items: center; gap: 6px;
  }}
  .cat-dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .cat-GPU {{ background: var(--gpu); }}
  .cat-RAM {{ background: var(--ram); }}
  .cat-SSD {{ background: var(--ssd); }}
  .cat-Other {{ background: var(--other); }}
  .item-card {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; margin-bottom: 12px;
  }}
  .item-card-deal {{ border-color: rgba(61,214,140,.45); box-shadow: 0 0 0 1px rgba(61,214,140,.12); }}
  .deals-banner {{
    max-width: 860px; margin: 0 auto 16px; padding: 12px 14px;
    background: rgba(61,214,140,.08); border: 1px solid rgba(61,214,140,.35);
    border-radius: 10px;
  }}
  .deals-banner-title {{
    font-size: 0.72rem; font-weight: 700; letter-spacing: .8px; text-transform: uppercase;
    color: var(--green); margin-bottom: 8px;
  }}
  .deal-row {{
    display: block; font-size: 0.88rem; padding: 4px 0; color: var(--text);
  }}
  .deal-row span {{ color: var(--muted); font-size: 0.8rem; }}
  .trust {{
    font-size: 0.58rem; font-weight: 700; text-transform: uppercase; letter-spacing: .3px;
    padding: 1px 5px; border-radius: 3px; margin-left: 6px; vertical-align: middle;
  }}
  .trust-good {{ background: rgba(61,214,140,.15); color: var(--green); }}
  .trust-fair {{ background: rgba(245,197,66,.15); color: var(--yellow); }}
  .hist-spark {{ display: flex; align-items: center; margin-right: 8px; }}
  .spark {{ display: block; }}
  .item-header {{
    display: flex; align-items: flex-start; justify-content: space-between;
    margin-bottom: 14px; gap: 10px;
  }}
  .item-name {{ font-size: 0.98rem; font-weight: 600; }}
  .badges {{ display: flex; gap: 7px; margin-top: 5px; flex-wrap: wrap; }}
  .badge {{ font-size: 0.68rem; padding: 2px 7px; border-radius: 4px; font-weight: 600; }}
  .badge-target {{ background: rgba(79,142,247,.15); color: var(--accent); border: 1px solid rgba(79,142,247,.3); }}
  .badge-min {{ background: rgba(245,197,66,.12); color: var(--yellow); border: 1px solid rgba(245,197,66,.3); }}
  .badge-min-empty {{ background: transparent; color: var(--muted); border: 1px dashed var(--border); }}
  .badge-deal {{ background: rgba(61,214,140,.15); color: var(--green); border: 1px solid rgba(61,214,140,.3); animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.6 }} }}
  .best-block {{ text-align: right; flex-shrink: 0; }}
  .best-label {{ font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }}
  .best-val {{ font-size: 1.55rem; font-weight: 800; color: var(--green); }}
  .best-at {{ font-size: 0.72rem; color: var(--muted); }}
  .remove-btn {{
    background: transparent; border: 1px solid var(--border);
    border-radius: 5px; color: var(--muted); cursor: pointer;
    font-size: 0.75rem; padding: 2px 6px; margin-top: 2px;
    transition: border-color .15s, color .15s;
  }}
  .remove-btn:hover {{ border-color: var(--red); color: var(--red); }}
  .price-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }}
  .price-cell {{
    background: var(--surface2); border: 1px solid var(--border);
    border-radius: 8px; padding: 9px 11px;
    display: block; text-decoration: none; color: inherit; cursor: pointer;
    transition: border-color .15s, background .15s, transform .12s;
  }}
  .price-cell:hover {{
    border-color: var(--accent); background: rgba(79,142,247,.08);
    text-decoration: none; transform: translateY(-1px);
  }}
  .price-cell.best {{ border-color: var(--green); background: rgba(61,214,140,.07); }}
  .price-cell.best:hover {{ border-color: var(--green); background: rgba(61,214,140,.12); }}
  .price-cell.warn {{ border-color: var(--yellow); }}
  .r-name {{ font-size: 0.68rem; color: var(--muted); margin-bottom: 3px; font-weight: 600; letter-spacing: .3px; display: flex; align-items: center; flex-wrap: wrap; }}
  .r-price {{ font-size: 0.95rem; font-weight: 700; }}
  .r-price.good {{ color: var(--green); }}
  .r-price.warn {{ color: var(--yellow); }}
  .r-price.miss {{ color: var(--muted); }}
  .r-ship {{ font-size: 0.62rem; color: var(--muted); margin-top: 2px; }}
  .r-item {{ font-size: 0.66rem; color: var(--muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .no-data {{ font-size: 0.76rem; color: var(--muted); font-style: italic; }}
  .hist-line {{
    margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--border);
    font-size: 0.73rem; color: var(--muted); display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
  }}
  .hist-stat {{ display: flex; flex-direction: column; gap: 1px; }}
  .hist-stat strong {{ color: var(--text); font-size: 0.8rem; }}
  .error-banner {{
    max-width: 860px; margin: 0 auto 16px; padding: 9px 14px;
    background: rgba(245,103,103,.08); border: 1px solid rgba(245,103,103,.25);
    border-radius: 8px; font-size: 0.76rem; color: var(--muted);
  }}
  .error-banner strong {{ color: var(--red); }}
  .footer {{
    max-width: 860px; margin: 32px auto 0; font-size: 0.7rem; color: var(--muted);
    text-align: center; border-top: 1px solid var(--border); padding-top: 12px;
  }}
  .footer .trust {{ cursor: help; }}

  /* ── Sidebar / Release window ── */
  .sidebar-title {{
    font-size: 0.68rem; font-weight: 700; letter-spacing: 1.4px; text-transform: uppercase;
    color: var(--muted); margin-bottom: 14px; display: flex; align-items: center; gap: 6px;
  }}
  .sidebar-title::after {{
    content: ''; flex: 1; height: 1px; background: var(--border);
  }}
  .rel-row {{
    padding: 11px 0; border-bottom: 1px solid var(--border);
  }}
  .rel-row:last-child {{ border-bottom: none; }}
  .rel-top {{ display: flex; align-items: flex-start; gap: 6px; margin-bottom: 4px; }}
  .rel-name {{ font-size: 0.85rem; font-weight: 600; flex: 1; line-height: 1.3; }}
  .rel-badge {{
    font-size: 0.62rem; font-weight: 700; padding: 2px 6px;
    border-radius: 4px; border: 1px solid; text-transform: uppercase;
    letter-spacing: .4px; flex-shrink: 0; margin-top: 2px;
  }}
  .rel-meta {{ display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }}
  .rel-cat {{ font-size: 0.7rem; font-weight: 700; }}
  .rel-when {{ font-size: 0.75rem; color: var(--muted); }}
  .rel-note {{ font-size: 0.72rem; color: var(--muted); line-height: 1.4; }}
  .edit-releases-link {{
    display: block; margin-top: 14px; font-size: 0.7rem; color: var(--muted); text-align: center;
  }}

  /* ── Toast ── */
  .toast {{
    position: fixed; bottom: 24px; right: 24px; z-index: 999;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 18px;
    font-size: 0.85rem; box-shadow: 0 4px 20px rgba(0,0,0,.5);
    opacity: 0; transform: translateY(8px);
    transition: opacity .25s, transform .25s; pointer-events: none;
  }}
  .toast.show {{ opacity: 1; transform: translateY(0); }}
  .toast.ok {{ border-color: var(--green); color: var(--green); }}
  .toast.err {{ border-color: var(--red); color: var(--red); }}
</style>
</head>
<body>
<div class="page-wrap">

<!-- ═══ MAIN COLUMN ═══ -->
<div class="main-col">

  <div class="header">
    <h1>PC Parts Price Report</h1>
    <span class="ts">Last scrape: {datetime_full}</span>
    <div class="header-actions">
      <button id="scanBtn" class="btn-scan" onclick="startScan()">Scan again</button>
      <span id="scanStatus" class="scan-status">Needs companion server on :5001</span>
    </div>
  </div>

  {deals_banner}

  <!-- Search bar -->
  <div class="search-section">
    <div class="search-label">What are you looking for? — type anything, or pick a suggestion</div>
    <div class="search-wrap">
      <input
        id="searchInput"
        class="search-input"
        type="text"
        placeholder="e.g. Corsair Vengeance RGB Pro 32GB DDR4 3600 CL18…"
        autocomplete="off"
        spellcheck="false"
      />
      <div id="searchDropdown" class="search-dropdown"></div>
    </div>
  </div>

  <!-- Add panel (shown after selecting a part) -->
  <div id="addPanel" class="add-panel">
    <div>
      <div id="addPanelName" class="add-panel-name"></div>
      <div id="addPanelCat" class="add-panel-cat"></div>
    </div>
    <div class="add-price-wrap">
      <label for="addCatSelect">Category</label>
      <select id="addCatSelect" class="add-cat-select">
        <option value="GPU">GPU</option>
        <option value="RAM">RAM</option>
        <option value="SSD">SSD</option>
        <option value="CPU">CPU</option>
        <option value="Other">Other</option>
      </select>
    </div>
    <div class="add-price-wrap">
      <label for="addPriceInput">Target $</label>
      <input id="addPriceInput" class="add-price-input" type="number" min="0" step="1" placeholder="optional" />
    </div>
    <div class="add-price-wrap">
      <label for="addMinInput" title="Ignore listings cheaper than this (filters damaged/parts junk)">Min $</label>
      <input id="addMinInput" class="add-price-input" type="number" min="0" step="1" placeholder="floor" />
    </div>
    <button id="addBtn" class="btn-add" onclick="confirmAdd()">Add to Watchlist</button>
    <button class="btn-cancel" onclick="closeAddPanel()">Cancel</button>
    <div id="addStatus" class="add-status"></div>
  </div>

  <!-- Summary chips -->
  <div class="summary-bar">
    <div class="chip"><strong id="chipTotal">{total_items}</strong>Tracked</div>
    <div class="chip"><strong>{priced}</strong>With Prices</div>
    <div class="chip"><strong style="color:var(--green)">{total_deals}</strong>At or Below Target</div>
  </div>

  {error_banner}

  <!-- Price cards -->
  <div id="priceArea">
    {categories_html}
  </div>

  <div class="footer">
    <div style="margin-bottom:6px">Retailer trust: {trust_legend}</div>
    <div style="margin-bottom:8px">{fb_loc_html}
      <button type="button" class="btn-cancel" style="margin-left:8px;padding:4px 10px;font-size:0.72rem" onclick="setFbZip()">Set ZIP</button>
    </div>
    Hover a badge for notes. Always verify before purchasing — prices change frequently.
  </div>

</div><!-- /main-col -->

<!-- ═══ SIDEBAR ═══ -->
<div class="sidebar">
  <div class="sidebar-inner">
    <div class="sidebar-title">Upcoming Releases</div>
    {releases_html if releases_html else '<div class="no-data">No upcoming releases — edit releases.json to add entries.</div>'}
    <a class="edit-releases-link" href="#" onclick="alert('Edit releases.json in the project folder to add or update upcoming releases.')">
      Edit releases.json
    </a>
  </div>
</div>

</div><!-- /page-wrap -->

<div id="toast" class="toast"></div>

<script>
// ── Parts database & fuzzy search ──────────────────────────────────────────
const PARTS_DB = {parts_db_json};
const TRACKED_IDS = new Set({tracked_ids_json});

function normalize(s) {{
  return s.toLowerCase().replace(/[^a-z0-9]/g, '');
}}

function scoreMatch(query, part) {{
  const q = normalize(query);
  const name = normalize(part.name);
  const short = normalize(part.short);
  const mfr = normalize(part.mfr);
  let score = 0;

  // Strong: short name contains query or vice versa
  if (short.includes(q) || q.includes(short)) score += 80;
  else if (name.includes(q)) score += 60;

  // Alias match
  for (const alias of (part.aliases || [])) {{
    const a = normalize(alias);
    if (a.includes(q) || q.includes(a)) {{ score += 70; break; }}
  }}

  // Token overlap: how many query tokens appear in the full name
  const qTokens = query.toLowerCase().split(/\\s+/).filter(t => t.length > 1);
  const nameLower = (part.name + ' ' + part.short + ' ' + part.mfr).toLowerCase();
  for (const tok of qTokens) {{
    if (nameLower.includes(tok)) score += 10;
  }}

  // Fuzzy character run: count longest subsequence match
  let qi = 0;
  for (let i = 0; i < name.length && qi < q.length; i++) {{
    if (name[i] === q[qi]) qi++;
  }}
  score += (qi / Math.max(q.length, 1)) * 15;

  return score;
}}

let selectedPart = null;

const input = document.getElementById('searchInput');
const dropdown = document.getElementById('searchDropdown');
const addPanel = document.getElementById('addPanel');

input.addEventListener('input', debounce(onSearch, 120));
input.addEventListener('keydown', onKey);
document.addEventListener('click', e => {{
  if (!e.target.closest('.search-wrap')) hideDropdown();
}});

function debounce(fn, ms) {{
  let t; return (...args) => {{ clearTimeout(t); t = setTimeout(() => fn(...args), ms); }};
}}

function guessCategory(q) {{
  const s = q.toLowerCase();
  if (/\\b(rtx|rx\\s*\\d|arc\\s*[ab]|geforce|radeon|\\bgpu\\b|graphics card)\\b/.test(s)) return 'GPU';
  if (/\\b(ddr[45]|\\bram\\b|vengeance|trident|dominator|ripjaws|ballistix)\\b/.test(s)) return 'RAM';
  if (/\\b(ssd|nvme|m\\.?2|sn\\d|990\\s*pro|980\\s*pro|firecuda|crucial\\s*t\\d)\\b/.test(s)) return 'SSD';
  if (/\\b(ryzen|core\\s*i[3579]|core\\s*ultra|\\bcpu\\b|processor)\\b/.test(s)) return 'CPU';
  return 'Other';
}}

function customPartFromQuery(q) {{
  const cat = guessCategory(q);
  return {{
    name: q,
    short: q,
    cat,
    mfr: 'Custom',
    custom: true,
    aliases: [],
  }};
}}

function escapeAttr(obj) {{
  return JSON.stringify(obj).replace(/"/g, '&quot;');
}}

function renderPartRow(part, i, extraClass) {{
  const tracked = TRACKED_IDS.has(slugify(part.name));
  const specTag = part.spec ? '<span style="font-size:.65rem;color:var(--yellow);margin-left:4px">SPEC</span>' : '';
  const customTag = part.custom ? '<span class="dd-hint">Custom</span>' : '';
  const mfr = part.custom ? 'Use exact search' : (part.spec ? 'Any Brand' : part.mfr);
  return `<div class="dd-item ${{extraClass || ''}}" data-idx="${{i}}" onclick="selectPart(${{escapeAttr(part)}})">
    <span class="dd-cat dd-cat-${{part.cat}}">${{part.cat}}</span>
    <span class="dd-name">${{part.custom ? 'Track: ' + part.name : part.name}}${{specTag}}</span>
    <span class="dd-mfr">${{mfr}}</span>
    ${{customTag}}
    ${{tracked ? '<span class="dd-tracked">✓ Tracked</span>' : ''}}
  </div>`;
}}

function onSearch() {{
  const q = input.value.trim();
  if (q.length < 2) {{ hideDropdown(); return; }}

  const scored = PARTS_DB
    .map(p => ({{ part: p, score: scoreMatch(q, p) }}))
    .filter(x => x.score > 10)
    .sort((a, b) => b.score - a.score)
    .slice(0, 7);

  const custom = customPartFromQuery(q);
  let html = renderPartRow(custom, 0, 'dd-custom');
  html += scored.map(({{part}}, i) => renderPartRow(part, i + 1, '')).join('');
  dropdown.innerHTML = html;
  dropdown.classList.add('open');
}}

function onKey(e) {{
  const items = dropdown.querySelectorAll('.dd-item');
  const active = dropdown.querySelector('.dd-item.active');
  let idx = active ? parseInt(active.dataset.idx) : -1;
  if (e.key === 'ArrowDown') {{ e.preventDefault(); idx = Math.min(idx + 1, items.length - 1); }}
  else if (e.key === 'ArrowUp') {{ e.preventDefault(); idx = Math.max(idx - 1, 0); }}
  else if (e.key === 'Enter') {{
    e.preventDefault();
    if (active) {{ active.click(); return; }}
    // Enter with no highlight → add whatever you typed as a custom search
    const q = input.value.trim();
    if (q.length >= 2) selectPart(customPartFromQuery(q));
    return;
  }}
  else if (e.key === 'Escape') {{ hideDropdown(); return; }}
  items.forEach(el => el.classList.toggle('active', parseInt(el.dataset.idx) === idx));
  if (items[idx]) items[idx].scrollIntoView({{block:'nearest'}});
}}

function hideDropdown() {{
  dropdown.classList.remove('open');
  dropdown.innerHTML = '';
}}

function selectPart(part) {{
  selectedPart = part;
  hideDropdown();
  input.value = part.name;

  document.getElementById('addPanelName').textContent = part.name;
  const catEl = document.getElementById('addPanelCat');
  if (part.custom) {{
    catEl.innerHTML = '<span style="color:var(--accent);font-weight:700">CUSTOM SEARCH</span> · exact query across all retailers';
  }} else if (part.spec) {{
    catEl.innerHTML = '<span style="color:var(--yellow);font-weight:700">SPEC SEARCH</span> · Any brand matching: <em>' + (part.specQuery || part.short) + '</em>';
  }} else {{
    catEl.textContent = part.mfr + ' · ' + part.cat;
  }}
  const catSelect = document.getElementById('addCatSelect');
  if (catSelect) catSelect.value = part.cat || 'Other';
  document.getElementById('addStatus').textContent = '';
  document.getElementById('addStatus').className = 'add-status';
  document.getElementById('addPriceInput').value = '';
  const minInput = document.getElementById('addMinInput');
  if (minInput) minInput.value = '';
  document.getElementById('addBtn').disabled = false;
  document.getElementById('addBtn').textContent = 'Add to Watchlist';

  if (TRACKED_IDS.has(slugify(part.name))) {{
    document.getElementById('addStatus').textContent = '✓ Already being tracked.';
    document.getElementById('addStatus').className = 'add-status ok';
    document.getElementById('addBtn').disabled = true;
  }}

  addPanel.classList.add('visible');
  document.getElementById('addPriceInput').focus();
}}

function closeAddPanel() {{
  addPanel.classList.remove('visible');
  selectedPart = null;
  input.value = '';
}}

function slugify(name) {{
  return name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}}

async function confirmAdd() {{
  if (!selectedPart) return;
  const target = parseFloat(document.getElementById('addPriceInput').value) || null;
  const minRaw = document.getElementById('addMinInput');
  const minPrice = minRaw && minRaw.value !== '' ? parseFloat(minRaw.value) : null;
  const catSelect = document.getElementById('addCatSelect');
  const category = (catSelect && catSelect.value) || selectedPart.cat || 'Other';
  selectedPart.cat = category;
  const statusEl = document.getElementById('addStatus');
  const btn = document.getElementById('addBtn');
  btn.disabled = true;
  btn.textContent = 'Adding...';

  const isSpec = !!selectedPart.spec;
  const isCustom = !!selectedPart.custom;
  const payload = {{
    name: selectedPart.name,
    category,
    target_price: target,
    min_price: (minPrice != null && !Number.isNaN(minPrice)) ? minPrice : null,
    spec_query: isSpec ? (selectedPart.specQuery || selectedPart.short) : null,
    custom: isCustom,
    exact_query: isCustom,
  }};

  try {{
    const res = await fetch('http://localhost:5001/api/add-item', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(4000),
    }});
    const data = await res.json();
    if (data.status === 'added') {{
      TRACKED_IDS.add(slugify(selectedPart.name));
      showToast('Added: ' + selectedPart.name, 'ok');
      updateChip(1);
      statusEl.textContent = '✓ Added! Refresh after next scrape run to see prices.';
      statusEl.className = 'add-status ok';
      btn.textContent = 'Added ✓';
    }} else if (data.status === 'exists') {{
      statusEl.textContent = '✓ Already tracked.';
      statusEl.className = 'add-status ok';
      btn.textContent = 'Add to Watchlist';
      btn.disabled = false;
    }} else {{
      throw new Error(data.message || 'Unknown error');
    }}
  }} catch (err) {{
    const id = slugify(selectedPart.name);
    const q = selectedPart.name;
    const snippet = JSON.stringify({{
      id, name: q, category,
      target_price: target,
      min_price: payload.min_price,
      search_terms: {{
        newegg: q, amazon: q, ebay: q, facebook: q,
      }}
    }}, null, 2);
    navigator.clipboard.writeText(snippet).catch(() => {{}});
    showToast('Server offline — config snippet copied to clipboard', 'err');
    statusEl.innerHTML = '⚠ Server offline. Snippet copied — paste into config.json → items array.<br>Start server: <code>python server.py</code>';
    statusEl.className = 'add-status err';
    btn.textContent = 'Add to Watchlist';
    btn.disabled = false;
  }}
}}

async function setMinPrice(id) {{
  const raw = prompt('Minimum listing price for this item (blank = no floor):');
  if (raw === null) return;
  const min_price = raw.trim() === '' ? null : parseFloat(raw);
  if (raw.trim() !== '' && (Number.isNaN(min_price) || min_price < 0)) {{
    showToast('Enter a valid number', 'err');
    return;
  }}
  try {{
    const res = await fetch('http://localhost:5001/api/update-item', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ id, min_price }}),
      signal: AbortSignal.timeout(4000),
    }});
    const data = await res.json();
    if (data.status !== 'updated') throw new Error(data.message || 'Update failed');
    showToast(min_price == null ? 'Min price cleared' : ('Min set to $' + min_price), 'ok');
    // Refresh badge text without full reload
    const card = document.getElementById('card-' + id);
    if (card) {{
      const badges = card.querySelector('.badges');
      if (badges) {{
        badges.querySelectorAll('.badge-min, .badge-min-empty').forEach(el => el.remove());
        const span = document.createElement('span');
        if (min_price == null) {{
          span.className = 'badge badge-min-empty';
          span.textContent = '+ Min price';
        }} else {{
          span.className = 'badge badge-min';
          span.textContent = 'Min: $' + Number(min_price).toFixed(2) + ' ✎';
        }}
        span.title = 'Listings below this are ignored';
        span.style.cursor = 'pointer';
        span.onclick = (e) => {{ e.stopPropagation(); setMinPrice(id); }};
        const deal = badges.querySelector('.badge-deal');
        if (deal) badges.insertBefore(span, deal);
        else badges.appendChild(span);
      }}
    }}
  }} catch {{
    showToast('Server offline — edit min_price in config.json', 'err');
  }}
}}

async function setFbZip() {{
  const cur = (document.getElementById('fbZipLabel')?.textContent || '').trim();
  const raw = prompt('Facebook Marketplace ZIP (blank clears). Radius stays 100 mi:', cur === 'no ZIP set' ? '' : cur);
  if (raw === null) return;
  const zip = raw.trim();
  if (zip && !/^\\d{{5}}(-\\d{{4}})?$/.test(zip)) {{
    showToast('Enter a 5-digit ZIP', 'err');
    return;
  }}
  try {{
    const res = await fetch('http://localhost:5001/api/settings', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ facebook_zipcode: zip, facebook_radius_miles: 100 }}),
      signal: AbortSignal.timeout(4000),
    }});
    const data = await res.json();
    if (data.status !== 'updated') throw new Error(data.message || 'Update failed');
    const label = document.getElementById('fbZipLabel');
    if (label) label.textContent = zip || 'no ZIP set';
    const rLabel = document.getElementById('fbRadiusLabel');
    if (rLabel) rLabel.textContent = '100';
    showToast(zip ? ('FB ZIP set to ' + zip + ' · 100 mi') : 'FB ZIP cleared', 'ok');
  }} catch (err) {{
    showToast('Server offline — set facebook_zipcode in config.json', 'err');
  }}
}}

async function removeItem(id) {{
  if (!confirm('Remove this item from tracking?')) return;
  try {{
    const res = await fetch('http://localhost:5001/api/remove-item', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id}}),
      signal: AbortSignal.timeout(4000),
    }});
    const data = await res.json();
    if (data.status === 'removed') {{
      document.getElementById('card-' + id)?.remove();
      TRACKED_IDS.delete(id);
      updateChip(-1);
      showToast('Removed from watchlist', 'ok');
    }}
  }} catch {{
    showToast('Server offline — edit config.json manually to remove', 'err');
  }}
}}

function updateChip(delta) {{
  const chipEl = document.getElementById('chipTotal');
  if (chipEl) chipEl.textContent = Math.max(0, parseInt(chipEl.textContent || '0') + (delta || 1));
}}

function showToast(msg, type) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show ' + type;
  setTimeout(() => el.classList.remove('show'), 3500);
}}

// ── Scan again ─────────────────────────────────────────────────────────────
let scanPoll = null;
const scanBtn = document.getElementById('scanBtn');
const scanStatus = document.getElementById('scanStatus');
let reportMtimeAtStart = null;

async function startScan() {{
  scanBtn.disabled = true;
  scanStatus.className = 'scan-status running';
  scanStatus.textContent = 'Starting scrape… (~10–15 min)';
  try {{
    const st = await fetch('http://localhost:5001/api/scan-status', {{ signal: AbortSignal.timeout(3000) }});
    const stData = await st.json();
    reportMtimeAtStart = stData.report_mtime;

    const res = await fetch('http://localhost:5001/api/run-tracker', {{
      method: 'POST',
      signal: AbortSignal.timeout(4000),
    }});
    const data = await res.json();
    if (data.status === 'already_running') {{
      scanStatus.textContent = 'Scan already running — waiting…';
    }} else if (data.status !== 'started' && data.status !== 'already_running') {{
      throw new Error(data.message || 'Could not start');
    }} else {{
      scanStatus.textContent = 'Scanning retailers… page will refresh when done';
      showToast('Scan started', 'ok');
    }}
    if (scanPoll) clearInterval(scanPoll);
    scanPoll = setInterval(pollScan, 8000);
  }} catch (err) {{
    scanBtn.disabled = false;
    scanStatus.className = 'scan-status err';
    scanStatus.textContent = 'Server offline — run start_server.bat';
    showToast('Companion server not running', 'err');
  }}
}}

async function pollScan() {{
  try {{
    const res = await fetch('http://localhost:5001/api/scan-status', {{ signal: AbortSignal.timeout(3000) }});
    const data = await res.json();
    if (data.running) {{
      scanStatus.className = 'scan-status running';
      scanStatus.textContent = 'Still scanning…';
      return;
    }}
    // Finished: refresh if report is newer
    if (reportMtimeAtStart != null && data.report_mtime && data.report_mtime !== reportMtimeAtStart) {{
      clearInterval(scanPoll);
      scanStatus.className = 'scan-status ok';
      scanStatus.textContent = 'Done — refreshing…';
      showToast('Scan complete', 'ok');
      setTimeout(() => location.reload(), 900);
      return;
    }}
    if (!data.running && reportMtimeAtStart != null) {{
      // Process ended but mtime unchanged yet — keep polling a bit
      scanStatus.textContent = 'Finishing report…';
    }}
  }} catch {{
    /* keep waiting */
  }}
}}

// On load: if a scan is already running, show status
(async () => {{
  try {{
    const res = await fetch('http://localhost:5001/api/scan-status', {{ signal: AbortSignal.timeout(2500) }});
    const data = await res.json();
    if (data.running) {{
      scanBtn.disabled = true;
      scanStatus.className = 'scan-status running';
      scanStatus.textContent = 'Scan in progress…';
      reportMtimeAtStart = data.report_mtime;
      scanPoll = setInterval(pollScan, 8000);
    }} else {{
      scanStatus.className = 'scan-status';
      scanStatus.textContent = 'Ready';
    }}
  }} catch {{
    scanStatus.className = 'scan-status err';
    scanStatus.textContent = 'Server offline — run start_server.bat for Scan / Add';
  }}
}})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*55}")
    print(f"  PC Parts Price Tracker  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}\n")

    config = load_config()
    settings = config.get("settings", {})
    items = config.get("items", [])
    max_results = settings.get("max_results_per_retailer", 3)
    delay = settings.get("request_delay_seconds", 2)
    timeout = settings.get("timeout_seconds", 30)

    history_file = BASE_DIR / settings.get("history_file", "price_history.json")
    output_file = BASE_DIR / settings.get("output_file", "morning_report.html")

    history = load_history(history_file)
    all_item_data = []

    with BrowserSession(headless=True) as browser:
        for item in items:
            print(f"[{item['category']}] {item['name']}")
            search_terms = item.get("search_terms", {})
            results = {}

            for r_key in RETAILER_ORDER:
                query = search_terms.get(r_key)
                if not query:
                    continue
                scraper = SCRAPERS[r_key]
                label = RETAILER_LABELS[r_key]
                print(f"    Checking {label}...")
                try:
                    # Fetch a few extra so title filters still leave max_results
                    found = scraper(browser, query, max_results=max_results + 4)
                    results[r_key] = found
                    if found:
                        print(f"      => {fmt(found[0]['price'])} — {found[0]['name'][:50]}")
                    else:
                        print(f"      => No results")
                except Exception as e:
                    print(f"      => Error: {e}")
                    results[r_key] = []
                time.sleep(delay)

            results = filter_results(results, item)
            # Cap to configured max after filtering
            results = {k: v[:max_results] for k, v in results.items()}

            update_history(history, item["id"], results, max_days=30)
            all_item_data.append({"item": item, "results": results})
            print()

    save_history(history_file, history)

    # Desktop toast for at/below-target deals
    deals = []
    for d in all_item_data:
        item = d["item"]
        target = item.get("target_price")
        prices = [r["price"] for res in d["results"].values() for r in res]
        if target and prices:
            best = min(prices)
            if best <= target:
                deals.append({"name": item["name"], "price": best, "target": target})
    notify_deals(deals)

    report_html = build_report(config, all_item_data, history)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report_html)

    print(f"Report written to: {output_file}")
    if deals:
        print(f"Deals at/below target: {len(deals)}")
        for d in deals:
            print(f"  • {d['name']}: {fmt(d['price'])} (target {fmt(d['target'])})")

    try:
        import webbrowser
        webbrowser.open(str(output_file))
        print("Opened in browser.")
    except Exception:
        pass

    print(f"\nDone.\n")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--ebay-worker":
        _ebay_worker_main()
    elif len(sys.argv) >= 4 and sys.argv[1] == "--fb-worker":
        _fb_worker_main()
    else:
        main()
