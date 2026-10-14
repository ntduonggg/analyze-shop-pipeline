"""Gọi Everbee API: session, fetch từng trang và parse shop/listing."""
import random
import time
from typing import Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from analyze_shop.log import C, err, info, ts, warn

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE      = "https://api.everbee.com/shops/analyze_shop"
PER_PAGE      = 20
REQUEST_DELAY = 0.3
MAX_RETRIES   = 5
MAX_PAGES     = 500

DEFAULT_PARAMS = {
    "per_page": PER_PAGE,
    "order_by": "views",
    "order_direction": "desc",
}

# ── LOCKED fields — thay "Please upgrade" bằng rỗng ─────────────────────────
LOCKED_VALUES = {"Please upgrade"}

# ── Shop-level fields (từ shop_details) ──────────────────────────────────────
SHOP_FIELDS: Dict[str, str] = {
    "shop_name":                  "shop_name",
    "shop_id":                    "shop_id",
    "review_count":               "shop_review_count",
    "review_average":             "shop_review_avg",
    "transaction_sold_count":     "shop_total_sold",
    "shop_age_month":             "shop_age_month",
    "shipping_from_country_iso":  "ship_from",
    "listing_active_count":       "listing_active_count",
    "num_favorers":               "shop_favorites",
    "views":                      "shop_views",
    "revenue":                    "shop_revenue",
    "revenue_30_days":            "shop_revenue_30d",
    "sales_30_days":              "shop_sales_30d",
    "conversion_rate":            "shop_conversion_rate",
    "review_rate":                "shop_review_rate",
    "average_listing_price":      "shop_avg_listing_price",
    "sale_per_listing":           "shop_sale_per_listing",
    "shop_type":                  "shop_type",
    "category":                   "shop_category",
    "url":                        "shop_url",
    "shop_logo":                  "shop_logo_url",
    "is_deactivated":             "is_deactivated",
}

# ── Listing-level fields (từ results[]) ──────────────────────────────────────
LISTING_FIELDS: Dict[str, str] = {
    "listing_id":                    "listing_id",
    "title":                         "title",
    "url":                           "etsy_url",
    "Images":                        "image_url",
    "price":                         "price",
    "state":                         "state",
    "cached_listing_age_in_months":  "listing_age",
    "views":                         "views",
    "num_favorers":                  "favorites",
    "review_count":                  "reviews",
    "cached_est_reviews_in_months":  "avg_reviews",
    "transaction_sold_count":        "total_sold",
    "listing_active_count":          "listing_active_count",
    "main_category":                 "category",
    "sub_category":                  "sub_category",
    "listing_type":                  "listing_type",
    "shipping_from_country_iso":     "ship_from",
    "cached_visibility_score":       "visibility_score",
    "cached_est_reviews":            "est_reviews",
    "when_made":                     "when_made",
    "who_made":                      "who_made",
    "is_customizable":               "is_customizable",
    "has_variations":                "has_variations",
    # Locked
    "est_mo_sales":                  "mo_sales",
    "est_mo_revenue":                "mo_revenue",
    "est_total_sales":               "total_sales_est",
    "growth_rate":                   "growth_rate",
}

# ── Thứ tự cột khi xuất Excel/CSV ────────────────────────────────────────────
SHOP_COLUMN_ORDER = [
    "shop_name", "shop_id", "shop_url",
    "shop_review_count", "shop_review_avg", "shop_review_rate",
    "shop_total_sold", "shop_age_month",
    "listing_active_count", "shop_favorites", "shop_views",
    "shop_revenue", "shop_revenue_30d", "shop_sales_30d",
    "shop_conversion_rate", "shop_avg_listing_price", "shop_sale_per_listing",
    "ship_from", "shop_type", "shop_category",
    "shop_logo_url", "is_deactivated",
]

LISTING_COLUMN_ORDER = [
    "shop_name",
    "listing_id", "title", "price", "tags", "etsy_url", "image_url",
    "listing_age", "views", "favorites", "reviews", "avg_reviews", "state",
    "total_sold", "visibility_score", "est_reviews",
    "category", "sub_category", "listing_type",
    "ship_from", "when_made", "who_made",
    "is_customizable", "has_variations",
    "mo_sales", "mo_revenue", "total_sales_est", "growth_rate",
]


class TokenExpired(Exception):
    pass


# ── Session ───────────────────────────────────────────────────────────────────
def build_session(auth_token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "X-Access-Token": auth_token,
        "Accept":         "application/json",
        "Content-Type":   "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
        ),
        "Origin":  "https://app.everbee.com",
        "Referer": "https://app.everbee.com/",
    })
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s


# ── Parse ─────────────────────────────────────────────────────────────────────
def clean_val(val) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return "" if s in LOCKED_VALUES else s

def parse_shop_details(shop: Dict) -> Dict:
    return {col: clean_val(shop.get(api_field, "")) for api_field, col in SHOP_FIELDS.items()}

def parse_listing(item: Dict, shop_name: str) -> Dict:
    row: Dict = {"shop_name": shop_name}
    for api_field, col in LISTING_FIELDS.items():
        val = item.get(api_field, "")
        # Images field là string URL trực tiếp
        if api_field == "Images":
            row[col] = clean_val(val) if isinstance(val, str) else ""
        else:
            row[col] = clean_val(val)
    # tags: array → string
    tags = item.get("tags", [])
    row["tags"] = ", ".join(t.strip() for t in tags if t) if isinstance(tags, list) else clean_val(tags)
    return row


# ── Fetch ─────────────────────────────────────────────────────────────────────
def fetch_shop_page(
    session: requests.Session,
    shop_name: str,
    page: int,
) -> Tuple[Optional[Dict], List[Dict], int]:
    """
    Fetch 1 trang của shop.
    Trả về (shop_details, listings, total_pages).
    shop_details chỉ có ở page 1. total_pages = 0: không lấy được, -1: lỗi 500.
    """
    params = {**DEFAULT_PARAMS, "shop_name": shop_name, "page": page}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(API_BASE, params=params, timeout=30)

            if r.status_code == 401:
                raise TokenExpired()

            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 10 * attempt))
                warn(f"Rate limit → ngủ {wait:.0f}s (attempt {attempt})")
                time.sleep(wait)
                continue

            if r.status_code == 500:
                warn(f"500 Server Error tại page {page} shop '{shop_name}' — dừng shop này.")
                return None, [], -1

            if r.status_code == 404:
                warn(f"Shop '{shop_name}' không tìm thấy (404).")
                return None, [], 0

            r.raise_for_status()
            data = r.json()

            shop_details = data.get("shop_details") if page == 1 else None
            listings     = data.get("results", [])
            total_pages  = int(data.get("total_pages", 1))
            return shop_details, listings, total_pages

        except TokenExpired:
            raise
        except requests.exceptions.ConnectionError as e:
            if "NameResolutionError" in str(e):
                err("Không resolve được host. Kiểm tra kết nối internet.")
                raise
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Connection error → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)
        except requests.exceptions.RequestException as e:
            wait = 2 ** attempt + random.uniform(0, 1)
            warn(f"Request error: {e} → retry {attempt}/{MAX_RETRIES} sau {wait:.1f}s")
            time.sleep(wait)

    err(f"Không fetch được page {page} shop '{shop_name}' sau {MAX_RETRIES} lần.")
    return None, [], 0


def scrape_shop(session: requests.Session, shop_name: str) -> Tuple[Optional[Dict], List[Dict]]:
    """Crawl toàn bộ pages của 1 shop. Trả về (shop_row, listing_rows)."""
    listing_rows: List[Dict] = []

    shop_details, listings, total_pages = fetch_shop_page(session, shop_name, 1)
    if total_pages <= 0:
        return None, []

    shop_row = parse_shop_details(shop_details) if shop_details else None
    listing_rows.extend(parse_listing(item, shop_name) for item in listings)

    effective_pages = min(total_pages, MAX_PAGES)
    info(f"  Shop '{shop_name}': {total_pages} trang listings (crawl tối đa {effective_pages})")

    for page in range(2, effective_pages + 1):
        _, listings, sig = fetch_shop_page(session, shop_name, page)
        if sig == -1:
            warn(f"  Dừng sớm tại page {page}.")
            break
        if not listings:
            break
        listing_rows.extend(parse_listing(item, shop_name) for item in listings)

        print(
            f"  {ts()} page {page:>4}/{effective_pages}"
            f"  listings {C.tag(C.INFO, str(len(listing_rows)))}",
            flush=True,
        )
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.1))

    return shop_row, listing_rows
