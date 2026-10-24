"""Crawl a curated set of Amazon.in public help-center pages into a markdown knowledge base.

- Polite: obeys robots.txt, one request every 2 s, raw HTML cached (re-runs don't re-download).
- Faithful: only the article body (<article class="help-content">) is kept, converted to markdown
  with headings, lists and tables preserved. Nothing is rewritten.
- Traceable: every file records its source URL, breadcrumb, fetch time and content hash, so each
  answer the agent gives can cite the exact real page it came from.
- Incremental: content hashes are compared with the previous manifest, so a refresh reports which
  pages changed (the trigger for re-indexing and re-running evals in a real deployment).

The content belongs to Amazon; it is fetched for a non-commercial demo and is git-ignored, not
redistributed. Usage:  python -m scripts.fetch_kb [--refresh]
"""
import argparse
import hashlib
import json
import re
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "knowledge_base"
OUT_DIR = KB_DIR / "amazon_in"
RAW_DIR = KB_DIR / "raw"
BASE = "https://www.amazon.in"
URL = BASE + "/gp/help/customer/display.html?nodeId={}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
DELAY_S = 2.0
# Amazon Bazaar is a separate storefront with different policies; mixing them in causes
# contradictory retrievals ("returns within 7 days" vs "10 days"), so it is excluded.
EXCLUDE_TITLE_PREFIXES = ("Amazon Bazaar",)

# Scoped to the intents the agent handles. Crawling the whole help center (thousands of device
# troubleshooting pages) would add noise that lowers retrieval precision.
PAGES = {
    "returns_refunds": [
        "G202111910",  # Returns Policy
        "G202111770",  # Refund Policy
        "201149940",   # Refund timelines
        "G202111750",  # Replacement Policy
        "G202115040",  # Returns and Replacements FAQ
        "G5PG8UG3E7SHGKA6",  # About Refunds
        "G6E3B2E8QPHQ88KF",  # How to schedule a return
        "G202111950",  # Return pick-up and self-ship guidelines
        "TAgXDdtqErF6Zz6HnQ",  # Return pick-up issues
        "GMP8PC8KBY5FCPM2",  # Check the status of your refund
        "GMTDJ6XPUMN7NWNB",  # Return a gift
        "GNLH2NSE3K4PX53L",  # About exchanges
        "GQHC9JYSMPRP39RH",  # Damaged or defective item
        "TWc5BVeaQkU6jODP1i",  # Wrong product delivered
        "Tidow893GJOqa30CNu",  # Returns for third-party seller orders
        "GSZAYH7K2C2NVNC9",  # A-to-z Guarantee refund
        "TcpYzpBE5MNAsk3Hy1",  # Product expiry
        "Ts7lpDVCMmhh8hYKx2",  # Warranty FAQ
        "TlK1fdW161vrTDsknx",  # Service centre replacement policy FAQ
    ],
    "orders_cancellations": [
        "GSL37WQTJZUYA9QE",  # Cancel items or orders
        "GDF5PQP4Z6SUH4CQ",  # Ordering with Amazon FAQ
    ],
    "shipping_delivery": [
        "GENAFPTNLHV7ZACW",  # Track your package
        "G77VXWB69X2D9M6J",  # General shipping information
        "202084980",   # Shipping speeds and delivery charges
        "G3H9BPFKAXSW553X",  # Shipment is late
        "GCC7G9HKYQBZT5FX",  # Undeliverable packages
        "GCU8BWGTQNJKQEBS",  # Missing package that shows as delivered
        "GXHYX22CS752PLE3",  # Secure delivery with OTP
        "GBPEXEXYULHB5WCH",  # Courier partners
        "GTGBAWKHSRPLZDE8",  # Same-day and two-day delivery FAQ
    ],
    "payments": [
        "GFBWMNXEPYVJAY9A",  # Accepted payment methods
        "G202054820",  # Pay on Delivery
        "202054460",   # EMI
        "GF8B8BP3Q4HQADU8",  # Resolve a declined payment
        "GJLLHTPTG32P95DR",  # Payment issues
        "GMEPD5NLAENJEC9H",  # Amazon Pay gift cards
    ],
    "prime": [
        "G6LDPN7YJHYKH2J6",  # Prime and Prime Lite benefits
        "GRPQFCNVUDYCBG24",  # Prime delivery benefits
        "G34EUPKVMYFW8N2U",  # Prime membership fee
    ],
    "account_security": [
        "GH3NM2YWEFEL2CQ4",  # Reset your password
        "G3PWZPU52FKN7PW4",  # Two-step verification
        "GRFTMVHP4HXMESSP",  # Security and privacy
        "TIqtYJYSsh1rJm8RIu",  # Customer service FAQ
        "TDNIJ2mNNgh0GhH99a",  # More support options
    ],
}


# Pages the policy engine and the core scenarios depend on: the crawl fails if any of these is missing.
CRITICAL_PAGES = ("G202111910", "G202111770", "G202111750", "GQHC9JYSMPRP39RH", "GSL37WQTJZUYA9QE", "GENAFPTNLHV7ZACW")
MIN_PAGES = 40


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:70]


def fetch(session: requests.Session, robots, node: str, refresh: bool) -> str | None:
    cache = RAW_DIR / f"{node}.html"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    # Most pages answer to two ids ("G202111910" and "202111910"); try the other one if the first is refused.
    variants = [node] + ([node[1:]] if node[:1] == "G" and node[1:].isdigit() else [f"G{node}"] if node.isdigit() else [])
    status = None
    for variant in variants:
        url = URL.format(variant)
        if not robots.can_fetch(UA, url):
            print(f"  robots.txt disallows {url}; skipped")
            return None
        for attempt in range(3):
            resp = session.get(url, timeout=20)
            time.sleep(DELAY_S)
            status = resp.status_code
            if status == 200 and "help-content" in resp.text:
                cache.write_text(resp.text, encoding="utf-8")
                return resp.text
            # 403/429/503/captcha are how the site says "slow down" (seen in practice: a transient 403 on the
            # returns-policy page after repeated crawls), so back off hard and retry.
            if status in (403, 429, 503) or "captcha" in resp.text.lower():
                time.sleep(15 * (attempt + 1))
                continue
            break
    print(f"  failed {node}: HTTP {status}")
    return None


def extract(page_html: str) -> dict | None:
    soup = BeautifulSoup(page_html, "html.parser")
    article = soup.select_one("article.help-content")
    if not article:
        return None
    for tag in article.select("script, style, noscript, img, svg, button, form, iframe"):
        tag.decompose()
    for a in article.select("a[href]"):
        if a["href"].startswith("/"):
            a["href"] = BASE + a["href"]
    h1 = article.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else "Untitled"
    if h1:
        h1.decompose()
    md = markdownify(str(article), heading_style="ATX", bullets="-", strip=["span"])
    md = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", md)).strip()
    crumbs = [a.get_text(" ", strip=True) for a in soup.select(".cs-help-breadcrumb a")]
    return {"title": title, "breadcrumb": " > ".join(crumbs), "markdown": md}


def main(refresh: bool):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = KB_DIR / "manifest.json"
    previous = {p["node_id"]: p for p in json.loads(manifest_path.read_text(encoding="utf-8"))["pages"]} \
        if manifest_path.exists() else {}

    robots = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
    robots.read()
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})

    pages, seen_hashes = [], set()
    for topic, nodes in PAGES.items():
        for node in nodes:
            raw = fetch(session, robots, node, refresh)
            page = extract(raw) if raw else None
            if not page or len(page["markdown"]) < 200:
                old = previous.get(node)
                if old and (OUT_DIR / old["file"]).exists():  # a transient failure must not delete knowledge
                    pages.append({**old, "status": "stale"})
                    seen_hashes.add(old["sha256"])
                    print(f"  [stale    ] {old['title']} (fetch failed; keeping the copy from {old['fetched_at']})")
                else:
                    print(f"  no article content for {node}; skipped")
                continue
            if page["title"].startswith(EXCLUDE_TITLE_PREFIXES):
                print(f"  excluded (other storefront): {page['title']}")
                continue
            digest = hashlib.sha256(page["markdown"].encode()).hexdigest()[:16]
            if digest in seen_hashes:  # same article reachable under two node ids
                continue
            seen_hashes.add(digest)
            filename = f"{slugify(page['title'])}.md"
            url = URL.format(node)
            fetched_at = previous.get(node, {}).get("fetched_at") if previous.get(node, {}).get("sha256") == digest \
                else datetime.now(timezone.utc).isoformat(timespec="seconds")
            front = "\n".join([
                "---", f"title: {page['title']}", f"url: {url}", f"topic: {topic}",
                f"breadcrumb: {page['breadcrumb']}", "source: Amazon.in Help & Customer Service (public page)",
                f"fetched_at: {fetched_at}", f"sha256: {digest}", "---", "",
            ])
            (OUT_DIR / filename).write_text(front + f"# {page['title']}\n\n{page['markdown']}\n", encoding="utf-8")
            status = "new" if node not in previous else ("changed" if previous[node]["sha256"] != digest else "unchanged")
            pages.append({"node_id": node, "title": page["title"], "file": filename, "url": url, "topic": topic,
                          "sha256": digest, "fetched_at": fetched_at, "chars": len(page["markdown"]), "status": status})
            print(f"  [{status:9}] {page['title']} ({len(page['markdown'])} chars)")

    manifest_path.write_text(json.dumps({"source": BASE + "/gp/help/customer/display.html",
                                         "pages": pages}, indent=2, ensure_ascii=False), encoding="utf-8")
    changed = [p for p in pages if p["status"] in ("new", "changed")]
    print(f"\n{len(pages)} pages saved to {OUT_DIR} ({len(changed)} new/changed). Manifest: {manifest_path}")

    # Fail loudly rather than ship a bot that silently lacks its core policies (e.g. a Docker build on a
    # server the site happens to block): the build stops instead of deploying a degraded knowledge base.
    missing = [n for n in CRITICAL_PAGES if n not in {p["node_id"] for p in pages}]
    if missing or len(pages) < MIN_PAGES:
        raise SystemExit(f"Knowledge base incomplete: {len(pages)} pages (need {MIN_PAGES}); "
                         f"missing critical pages: {missing or 'none'}. Re-run later (the site may be rate-limiting).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="re-download instead of using the HTML cache")
    main(parser.parse_args().refresh)
