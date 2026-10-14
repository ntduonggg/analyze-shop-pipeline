#!/usr/bin/env python3
"""Upload 2 file CSV (shop-level + listing-level) -> phan tich muc phu hop custom gift.

Chi dung Python stdlib + pandas (khong can cai them Flask/Streamlit).

Chay:
    analyzer [--port 8765] [--no-browser]          # trong project: uv run analyzer
    docker compose up -d analyzer                  # chay bang Docker

Moi duong dan tuong doi (analysis_output/, shop_data/, ...) tinh tu DATA_DIR
(mac dinh data/ o goc project, /app/data trong container).

Mo http://127.0.0.1:8765 (tu mo trinh duyet), keo-tha 2 file CSV bat ky thu tu
(tool tu nhan dien file nao la shop-level, file nao la listing-level qua ten
cot), bam "Phan tich" -> ra ngay trang bao cao 4 tab (giong ledger da lam thu
cong truoc do): shop co listing + verdict, doi chieu nguoc (listing -> shop),
chi so kinh doanh cho shop "Phu hop", va top ban chay theo heyetsy.

Moi lan phan tich deu tra ve report ngay tren trinh duyet, nhung CHI luu 1 ban
HTML moi vao analysis_output/<timestamp>/ khi cap file CSV upload (theo noi
dung, khong phai ten file) khac voi tat ca cac lan phan tich truoc do - tranh
ra hang chuc folder trung nhau khi bam lai "Phan tich" voi dung 2 file cu.
"""
import argparse
import glob
import hashlib
import html
import io
import json
import os
import re
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pandas as pd

from analyze_shop import settings
from analyze_shop.analyzer import listing_plan

# ── Nguong & bo tu khoa (khop voi everbee_api_young_shop_scraper.py) ────────
GIFT_KW = re.compile(
    r"personali[sz]ed|custom(?:iz|is)?ed?|engrav|monogram|initial|keepsake|"
    r"memorial|anniversary|birthday gift|gift for",
    re.I,
)
GIFT_CATS = {
    "Paper & Party Supplies", "Jewelry", "Accessories", "Home & Living",
    "Art & Collectibles", "Bags & Purses", "Weddings", "Bath & Beauty",
}
PENALTY_CATS = {"Craft Supplies & Tools"}

# Danh sach nay khop EXCLUDE_SHOP_KEYWORDS trong everbee_api_young_shop_scraper.py
# — dung o tab "Doi chieu nguoc" de bao cho biet scraper hien tai co tu loai
# shop nay khong, khong dung de tinh verdict phu hop custom gift.
EXCLUDE_SHOP_KEYWORDS = [
    "tarot", "plant", "psychic", "medium", "oracle", "spell",
    "crochet", "amigurumi", "digital", "printable", "pdf", "template",
]

# (cot trong file shop-level, nhan hien thi, format hien thi trong bang)
BUSINESS_COLUMNS = [
    ("shop_review_count", "Reviews", {}),
    ("shop_review_avg", "Review TB", {"decimals": 2}),
    ("shop_review_rate", "Review rate%", {"pct": True}),
    ("shop_total_sold", "Total sold", {}),
    ("shop_age_month", "Tuổi (tháng)", {}),
    ("listing_active_count", "Listing active", {}),
    ("shop_favorites", "Favorites", {}),
    ("shop_views", "Views", {}),
    ("shop_revenue", "Revenue (life)", {"currency": True}),
    ("shop_revenue_30d", "Revenue 30d", {"currency": True}),
    ("shop_sales_30d", "Sales 30d", {}),
    ("shop_conversion_rate", "Conversion%", {"pct": True}),
]

HEYETSY_COLUMNS = [
    "heyetsy_avg_weekly_sold", "heyetsy_sold_this_month", "heyetsy_sold_last_month",
    "heyetsy_this_week_sold", "heyetsy_last_week_sold", "heyetsy_avg_daily_sold",
    "heyetsy_today_sold", "heyetsy_avg_weekly_favorites", "heyetsy_reviews_this_month",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def matched_keywords(*values):
    text = " ".join(str(v) for v in values if v).lower()
    return [kw for kw in EXCLUDE_SHOP_KEYWORDS if kw in text]


def detect_kind(df):
    """Nhan dien 1 DataFrame la file shop-level hay listing-level, dua vao cot."""
    cols = set(df.columns)
    if "listing_id" in cols and "title" in cols:
        return "listings"
    if "shop_review_count" in cols or "shop_avg_listing_price" in cols or "shop_id" in cols:
        return "shops"
    return "unknown"


def score_verdict(pct_cust, pct_kw, top_cat):
    score = 0
    # pct_customizable CHỈ được cộng điểm khi có ít nhất 1 tín hiệu từ khoá gift
    # đi kèm (pct_kw > 0) — case thật gặp: shop Elitebodyoil bán body oil chỉ
    # cho chọn size/mùi qua dropdown Etsy, is_customizable=True ở 100% listing
    # nhưng KHÔNG PHẢI cá nhân hoá quà tặng (0% listing có từ khoá gift trong
    # title/tag) — trước đây vẫn được cộng +2 nên bị chấm nhầm "Phù hợp".
    if pct_kw > 0:
        if pct_cust >= 40:
            score += 2
        elif pct_cust >= 15:
            score += 1
    if pct_kw >= 50:
        score += 2
    elif pct_kw >= 20:
        score += 1
    if top_cat in GIFT_CATS:
        score += 1
    if top_cat in PENALTY_CATS:
        score -= 1
    if score >= 2:
        return "good"
    if score >= 1:
        return "review"
    return "no"


def score_verdict_shop_only(shop_category: str, shop_description: str) -> str:
    """Verdict SƠ BỘ khi chỉ có 1 file shop-level (chưa crawl listing) — không
    có %gift-keyword/%customizable thật từ listing như score_verdict(), chỉ
    dùng được category + description ở cấp shop. Kém tin cậy hơn hẳn, luôn
    phải gắn cờ status='prelim' ở nơi gọi để người dùng biết cần crawl thêm
    listing mới chấm chính xác được."""
    score = 0
    if GIFT_KW.search(shop_description or ""):
        score += 2
    if shop_category in GIFT_CATS:
        score += 1
    if shop_category in PENALTY_CATS:
        score -= 1
    if score >= 2:
        return "good"
    if score >= 1:
        return "review"
    return "no"


def shop_metrics_from_listings(g: pd.DataFrame):
    """g: cac dong listing cua 1 shop. Tra ve dict metric, hoac None neu rong."""
    n = len(g)
    if n == 0:
        return None

    is_cust = g.get("is_customizable", pd.Series([""] * n)).astype(str).str.lower().isin(["true", "1", "yes"])
    pct_cust = round(is_cust.mean() * 100, 1)

    titles = g.get("title", pd.Series([""] * n, index=g.index)).astype(str)
    tags = g.get("tags", pd.Series([""] * n, index=g.index)).astype(str)
    text = (titles + " " + tags).str.lower()
    kw_match = text.str.contains(GIFT_KW, na=False)
    pct_kw = round(kw_match.mean() * 100, 1)

    cat_series = g.get("category", pd.Series([""] * n, index=g.index)).astype(str)
    nonempty_cats = cat_series[cat_series.str.len() > 0]
    top_cat = nonempty_cats.mode()
    top_cat = top_cat.iloc[0] if len(top_cat) else ""

    when_made = g.get("when_made", pd.Series([""] * n, index=g.index)).astype(str)
    pct_mto = round(when_made.str.contains("made_to_order", case=False, na=False).mean() * 100, 1)

    price_num = pd.to_numeric(g.get("price", pd.Series(dtype=str)), errors="coerce")
    avg_price = round(price_num.mean(), 2) if price_num.notna().any() else None

    sold_num = pd.to_numeric(g.get("total_sold", pd.Series(dtype=str, index=g.index)), errors="coerce").fillna(0)
    order = titles.to_frame("title")
    order["_kw"] = kw_match.values
    order["_sold"] = sold_num.values
    order = order.sort_values(["_kw", "_sold"], ascending=[False, False])
    samples = order["title"].head(3).tolist()

    return {
        "n_listings": n,
        "pct_customizable": pct_cust,
        "pct_gift_kw": pct_kw,
        "pct_made_to_order": pct_mto,
        "top_category": top_cat,
        "avg_price": avg_price,
        "sample_titles": samples,
    }


def analyze(shops_df: pd.DataFrame, listings_df: pd.DataFrame):
    """Tra ve (main_rows, filtered_rows, reverse_rows)."""
    shops_df = shops_df.fillna("")
    listings_df = listings_df.fillna("")

    has_shop_col = "shop_name" in listings_df.columns
    # pandas >=3.0: GroupBy co attribute .keys (ten cot group) nen dict(groupby_obj)
    # bi vo hieu (dict() nham dung .keys() nhu mapping) -> phai duyet tay qua dict comprehension.
    listing_groups = {name: g for name, g in listings_df.groupby("shop_name")} if has_shop_col else {}
    listing_shop_names = set(listing_groups.keys())

    shop_names = shops_df["shop_name"].astype(str).tolist() if "shop_name" in shops_df.columns else []
    shop_meta = shops_df.set_index("shop_name").to_dict("index") if "shop_name" in shops_df.columns else {}

    main_rows, filtered_rows = [], []
    for name in shop_names:
        meta = shop_meta.get(name, {})
        g = listing_groups.get(name)
        if g is None:
            filtered_rows.append({
                "shop": name,
                "status": "filtered",
                "shop_category": meta.get("shop_category", ""),
                "shop_description": meta.get("shop_description", ""),
            })
            continue
        metrics = shop_metrics_from_listings(g)
        if metrics is None:
            continue
        verdict = score_verdict(metrics["pct_customizable"], metrics["pct_gift_kw"], metrics["top_category"])
        row = {
            "shop": name,
            "status": "present",
            "verdict": verdict,
            "shop_category": meta.get("shop_category", ""),
            "shop_description": meta.get("shop_description", ""),
            "total_sold": meta.get("shop_total_sold", ""),
            "age_month": meta.get("shop_age_month", ""),
        }
        row.update(metrics)
        main_rows.append(row)

    reverse_names = sorted(listing_shop_names - set(shop_names))
    reverse_rows = []
    for name in reverse_names:
        g = listing_groups[name]
        metrics = shop_metrics_from_listings(g)
        if metrics is None:
            continue
        verdict = score_verdict(metrics["pct_customizable"], metrics["pct_gift_kw"], metrics["top_category"])

        name_kw = matched_keywords(name, metrics["top_category"])
        title_texts = g.get("title", pd.Series(dtype=str)).astype(str).tolist()
        title_kw_hits = sorted({kw for t in title_texts for kw in EXCLUDE_SHOP_KEYWORDS if kw in t.lower()})
        all_kw = sorted(set(name_kw) | set(title_kw_hits))
        if all_kw:
            note = f"Khớp từ khoá loại trừ: {', '.join(all_kw)} — scraper hiện tại nhiều khả năng sẽ tự loại shop này."
        else:
            note = "Không khớp từ khoá loại trừ nào (tên/ngành hàng/tiêu đề) — không có lý do rõ ràng để loại shop này."

        row = {
            "shop": name,
            "verdict": verdict,
            "note": note,
            "shop_category": "",
            "shop_description": "",
        }
        row.update(metrics)
        reverse_rows.append(row)

    return main_rows, filtered_rows, reverse_rows


def analyze_shop_only(shops_df: pd.DataFrame) -> list:
    """Chế độ 1 file: chỉ upload file shop-level, CHƯA có file listing-level.
    Chấm verdict SƠ BỘ cho từng shop (score_verdict_shop_only — chỉ dựa
    category/description shop) và trả về list row status='prelim', cùng
    shape với main_rows của analyze() (n_listings/pct_customizable/pct_gift_kw/
    avg_price/sample_titles đều None vì không có listing để tính) để tái dùng
    được nguyên vẹn render_report()/business_rows()/top_rows()."""
    shops_df = shops_df.fillna("")
    if "shop_name" not in shops_df.columns:
        return []
    rows = []
    for _, r in shops_df.iterrows():
        name = str(r.get("shop_name", "")).strip()
        if not name:
            continue
        category = str(r.get("shop_category", ""))
        description = str(r.get("shop_description", ""))
        verdict = score_verdict_shop_only(category, description)
        rows.append({
            "shop": name,
            "status": "prelim",
            "verdict": verdict,
            "shop_category": category,
            "shop_description": description,
            "top_category": category,
            "total_sold": r.get("shop_total_sold", ""),
            "age_month": r.get("shop_age_month", ""),
            "n_listings": None,
            "pct_customizable": None,
            "pct_gift_kw": None,
            "pct_made_to_order": None,
            "avg_price": None,
            "sample_titles": [],
        })
    return rows


def business_rows(shops_df: pd.DataFrame, good_names: set):
    if "shop_name" not in shops_df.columns:
        return [], []
    df = shops_df[shops_df["shop_name"].astype(str).isin(good_names)].fillna("")
    available = [(k, label, fmt) for k, label, fmt in BUSINESS_COLUMNS if k in df.columns]
    rows = []
    for _, r in df.iterrows():
        row = {
            "shop_name": r.get("shop_name", ""),
            "shop_url": r.get("shop_url", ""),
            "shop_category": r.get("shop_category", ""),
        }
        for k, _label, _fmt in available:
            row[k] = _num(r.get(k))
        rows.append(row)
    columns = [{"key": k, "label": label, "format": fmt} for k, label, fmt in available]
    return rows, columns


def _parse_daily_series(dates_str, series_str):
    """Parse cot heyetsy_daily_sold_dates/_series (chuoi noi dau phay, cung
    index) tu report1m thanh 2 list cung do dai. Cac diem BI THIEU trong
    report goc (ngay khong doi so sold - xem comment o flatten_shop_stats)
    khong duoc suy dien lai o day, chi lam viec voi diem thuc su co."""
    if not dates_str or not series_str or pd.isna(dates_str) or pd.isna(series_str):
        return [], []
    dates = [d for d in str(dates_str).split(",") if d]
    try:
        values = [int(float(v)) for v in str(series_str).split(",") if v != ""]
    except ValueError:
        return [], []
    n = min(len(dates), len(values))
    return dates[:n], values[:n]


def _trend_and_volatility(values):
    """Tu chuoi diffSolds (report1m): xu huong tang/giam (so nua dau vs nua
    sau chuoi diem THUC SU co, khong theo lich) + nhan bien dong (dua tren
    he so bien thien std/mean). Tra ve (trend_pct hoac None, label)."""
    n = len(values)
    if n == 0:
        return None, "no_data"
    if all(v == 0 for v in values):
        return 0.0, "no_sales"

    mean_v = sum(values) / n
    variance = sum((v - mean_v) ** 2 for v in values) / n
    cv = (variance ** 0.5) / mean_v if mean_v else 0
    vol_label = "stable" if cv < 0.4 else ("moderate" if cv < 0.8 else "volatile")

    half = n // 2
    if half == 0:
        return None, vol_label
    avg1 = sum(values[:half]) / half
    avg2 = sum(values[half:]) / (n - half)
    if avg1 == 0:
        trend_pct = None if avg2 == 0 else 100.0  # moi phat sinh sales o nua sau, khong co mau de tinh %
    else:
        trend_pct = round((avg2 - avg1) / avg1 * 100, 1)
    return trend_pct, vol_label


def top_rows(shops_df: pd.DataFrame, good_names: set, n: int = 20):
    if "shop_name" not in shops_df.columns or "heyetsy_avg_weekly_sold" not in shops_df.columns:
        return [], False

    df = shops_df[shops_df["shop_name"].astype(str).isin(good_names)].copy()
    for c in HEYETSY_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else None

    ranked = df.dropna(subset=["heyetsy_avg_weekly_sold"]).sort_values(
        "heyetsy_avg_weekly_sold", ascending=False
    ).head(n)

    rows = []
    for i, (_, r) in enumerate(ranked.iterrows(), 1):
        tw, lw = r.get("heyetsy_this_week_sold"), r.get("heyetsy_last_week_sold")
        week_growth = None
        if pd.notna(lw) and lw and pd.notna(tw):
            week_growth = round((tw - lw) / lw * 100, 1)
        avg_weekly = _num(r.get("heyetsy_avg_weekly_sold"))

        daily_dates, daily_series = _parse_daily_series(
            r.get("heyetsy_daily_sold_dates"), r.get("heyetsy_daily_sold_series")
        )
        trend_pct, vol_label = _trend_and_volatility(daily_series)

        rows.append({
            "rank": i,
            "shop": r.get("shop_name", ""),
            "shop_url": r.get("shop_url", ""),
            "shop_category": r.get("shop_category", ""),
            "daily_dates": daily_dates,
            "daily_series": daily_series,
            "trend_pct": trend_pct,
            "volatility": vol_label,
            "avg_weekly_sold": round(avg_weekly) if avg_weekly is not None else None,
            "sold_this_month": _num(r.get("heyetsy_sold_this_month")),
            "sold_last_month": _num(r.get("heyetsy_sold_last_month")),
            "this_week_sold": _num(tw),
            "last_week_sold": _num(lw),
            "week_growth_pct": week_growth,
            "avg_daily_sold": _num(r.get("heyetsy_avg_daily_sold")),
            "today_sold": _num(r.get("heyetsy_today_sold")),
            "avg_weekly_favorites": _num(r.get("heyetsy_avg_weekly_favorites")),
            "reviews_this_month": _num(r.get("heyetsy_reviews_this_month")),
        })
    return rows, True


# (key trong row JSON, nhãn hiển thị ở dropdown) — views/est_reviews/favorites/
# listing_age đều là cột listing-level gốc từ everbee_api_young_shop_scraper.py
# (LISTING_COLUMN_ORDER), không suy ra. Riêng "review_per_age" (Review/Age) là
# metric suy ra ở JS = est_reviews / listing_age (listing_age tính bằng THÁNG,
# xem cached_listing_age_in_months trong scraper) — không tính sẵn ở đây vì
# không cần server re-render mỗi lần đổi lựa chọn.
PRODUCT_METRICS = [
    ("views", "Views"),
    ("est_reviews", "Ước tính reviews"),
    ("favorites", "Favorites"),
    ("review_per_age", "Review/Age (reviews mỗi tháng tuổi)"),
]


def product_rows(listings_df: pd.DataFrame, good_names: set):
    """Toàn bộ listing thuộc các shop đã chấm 'Phù hợp' (good) làm custom gift
    ở tab đầu, kèm views/est_reviews/favorites/listing_age/category — dữ liệu
    thô cho tab Top 50 sản phẩm. KHÔNG rank sẵn theo 1 metric cố định ở
    server: JS phía client tự sắp lại top 20 theo metric người dùng chọn qua
    dropdown (kể cả review_per_age suy ra từ est_reviews/listing_age), tránh
    phải re-render lại toàn trang mỗi lần đổi lựa chọn."""
    if listings_df.empty or "shop_name" not in listings_df.columns:
        return []
    df = listings_df[listings_df["shop_name"].astype(str).isin(good_names)]
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "shop": r.get("shop_name", ""),
            "title": r.get("title", ""),
            "etsy_url": r.get("etsy_url", ""),
            "category": r.get("category", "") or r.get("sub_category", ""),
            "views": _num(r.get("views")),
            "favorites": _num(r.get("favorites")),
            "est_reviews": _num(r.get("est_reviews")),
            "listing_age": _num(r.get("listing_age")),
        })
    return rows


def keyword_listing_rows(listings_df: pd.DataFrame) -> list:
    """Toàn bộ listing (MỌI shop, không lọc theo verdict 'Phù hợp' như
    product_rows()) kèm title/tags/category — dữ liệu thô cho tab 'Tìm theo
    từ khoá': người dùng nhập 1 keyword, JS phía client tự lọc theo
    title/tags/category rồi gộp lại thành bảng theo shop (không cần re-render
    server mỗi lần đổi từ khoá)."""
    if listings_df.empty or "shop_name" not in listings_df.columns:
        return []
    rows = []
    for _, r in listings_df.iterrows():
        rows.append({
            "shop": r.get("shop_name", ""),
            "title": r.get("title", ""),
            "tags": r.get("tags", ""),
            "category": r.get("category", "") or r.get("sub_category", ""),
            "etsy_url": r.get("etsy_url", ""),
        })
    return rows


# ── Tab "Build vs Conflict" ─────────────────────────────────────────────────
# So sánh shop đang build với các shop conflict của nó (danh sách do PIC khai
# trong file "Shop build.csv": cột Shop name / PIC / Conflict shop, ô Conflict
# shop chứa nhiều tên cách nhau bởi xuống dòng). Dữ liệu listing đọc từ mọi file
# listing-level trong BUILD_CONFLICT_DIR (vd everbee_shop_build_data_listings.csv
# + everbee_shop_conflict_data_listings.csv) — độc lập với 2 file upload, nên
# tab này giống nhau ở mọi report. Đổi đường dẫn qua --build-dir / --build-list.
BUILD_CONFLICT_DIR = "shop_data"
BUILD_LIST_CSV = os.path.join("input", "shop_build.csv")

# Phân loại theo tiêu đề listing, lấy luật khớp ĐẦU TIÊN — thứ tự quan trọng
# (vd "Photo Album ... Ornament" phải ra Photo Album, "Wreath Sash" trước
# "Sign/Hoop"). sub_category trong export Everbee luôn trống nên không dùng được.
# Tên loại SP chi tiết theo chất liệu/kiểu (Ceramic Ornament, Pottery Mug,
# Frosted Cup, Book Vase…) — khớp cách đặt tên catalog nội bộ của các shop build.
# Luật dạng (?=.*a)(?=.*b) = title chứa cả a lẫn b, ở bất kỳ vị trí nào.
PRODUCT_TYPE_RULES = [
    # ── Sản phẩm đặc thù (ưu tiên trước vì title hay nhắc kèm "ornament/sign/banner") ──
    ("Press-on Nails", r"\bnails?\b"),
    ("Newlywed Photobooth Strip Key", r"photo ?booth strip|strip key|photo ?booth key"),
    ("Reusable Advent Calendar", r"\badvent\b"),
    ("Book Vase", r"book vase"),
    ("Embroidered Pennant Bag Tag", r"bag tag|bag charm|pennant (?:charm|keychain)|backpack tag|flag charm"),
    ("Acrylic Keychain", r"keychain|key chain"),
    ("Custom Badge Reel", r"badge reel|badge holder"),
    ("Embroidered Wreath Sash", r"wreath sash|door sash|wreath bow|wreath ribbon|door ribbon|\bsash"),
    ("Custom Can Cooler", r"can cooler|koozie|coozie"),
    ("Wedding Napkins", r"napkin"),
    ("Ski Mask", r"ski mask|balaclava"),
    # ── Ornament theo chất liệu ──
    ("Ceramic Ornament", r"(?=.*ornament)(?=.*(?:ceramic|porcelain))"),
    ("Acrylic Ornament", r"(?=.*ornament)(?=.*acrylic)"),
    ("Glass Ornament", r"(?=.*ornament)(?=.*glass)"),
    ("Two Layer Wooden Ornament", r"(?=.*ornament)(?=.*(?:two|2|double)[ -]?layer|.*layered)"),
    ("Wooden Ornament 1 Layer", r"(?=.*ornament)(?=.*wood)"),
    ("Embroidered Ornament", r"(?=.*ornament)(?=.*(?:embroider|hoop))"),
    ("Ornament (khác)", r"ornament"),
    ("Stained Glass Suncatcher", r"suncatcher|stained glass"),
    # Apparel đặt TRƯỚC sign/banner/flag: title áo hay có "flag" (US flag shirt…).
    ("Apparel", r"sweatshirt|sweater|shirt|pajama|hoodie|\btee\b|dress|skirt|skort|activewear|"
                r"costume|leggings|swim|romper|onesie|bodysuit|jacket|\bshorts?\b|jersey|outfit|jumpsuit|pjs|sleepwear|loungewear|nightwear|underwear|thong|\bhat\b|beanie|socks|tank top|blouse"),
    # ── Guest book / album ──
    ("Embroidered Wedding Pennant Flag", r"(?=.*(?:pennant|flag))(?=.*(?:wedding|bridal|last name|mr ?& ?mrs))"),
    ("Embroidered Wedding Vow Book", r"vow book"),
    ("Canvas Guest Book Alternative", r"guest ?book alternative"),
    ("Embroidered Wedding Guest Book", r"guest ?book|sign ?in (?:album|book)|signbook"),
    ("Embroidered Baby Photo Album", r"(?=.*(?:photo album|memory book|scrapbook|photo book|journal))(?=.*(?:baby|newborn|first year))"),
    ("Embroidered Photo Album (khác)", r"photo album|memory (?:book|album)|scrapbook|photo book|journal|name book"),
    # ── Drinkware ──
    ("Pottery Mug", r"(?=.*(?:mug|cup))(?=.*(?:pottery|stoneware))"),
    ("Accent Two-Tone Mug", r"(?=.*mug)(?=.*(?:two.tone|accent))"),
    ("Ceramic Mug", r"\bmugs?\b"),
    ("Frosted Cup", r"frosted"),
    ("Tumbler", r"tumbler"),
    ("Cup (khác)", r"\bcups?\b|drinkware|wine glass"),
    # ── Magnet ──
    ("Cruise Door Magnet", r"(?=.*magnet)(?=.*(?:cruise|stateroom|door magnet))"),
    ("Acrylic Magnet", r"magnet"),
    # ── Jewelry dish ──
    ("Engraved Jewelry Dish", r"(?=.*(?:dish|trinket|ring holder))(?=.*engrav)"),
    ("Personalized Jewelry Dish", r"jewelry dish|ring dish|trinket|ring holder"),
    # ── Sign / banner / decor vải ──
    ("Lace Wedding Chair Sign", r"chair sign"),
    ("Lace/Sheer Wedding Sign", r"(?=.*(?:lace|sheer))(?=.*(?:sign|banner))"),
    ("Wall Tapestry", r"tapestry|backdrop"),
    ("Yarn Banner Name", r"yarn|crochet letter|garland"),
    ("Wooden Sign 1 Layer", r"(?=.*(?:sign|hanger|plaque))(?=.*wood)|door hanger"),
    ("Linen/Fabric Sign", r"(?=.*(?:sign|banner))(?=.*(?:linen|fabric|canvas|felt))"),
    ("Embroidered Name Pennant", r"pennant|name flag|baby flag|nursery flag|nursery banner|name banner"),
    ("Embroidered Name Hoop", r"hoop"),
    ("Embroidered Canvas Wine Tote", r"wine tote|tote|canvas bag"),
    ("Sign/Banner (khác)", r"\bsigns?\b|banner|flag"),
    # ── Còn lại ──
    ("Night Light/Lamp", r"night light|night lamp|led lamp|\blamp\b"),
    ("Pillow", r"pillow"),
    ("Blanket", r"blanket"),
    ("Stocking", r"stocking"),
    ("Basket", r"basket"),
    ("Pen Holder", r"pen holder|pencil pot|pencil holder"),
    ("Acrylic Plaque/Block", r"plaque|acrylic block|acrylic figure|figure|cake topper"),
    ("Doormat", r"doormat|door mat"),
    ("Car Accessory", r"car seat|seat cover|car accessor|steering|sun ?shade"),
    ("Personalized Compact Mirror", r"compact mirror|pocket mirror|makeup mirror"),
    ("Beach Towel", r"towel"),
    ("Ceramic Flower Vase", r"\bvase"),
    ("Plant Pot", r"plant pot|flower pot|planter"),
    ("Plastic Platter", r"platter|\bplates?\b"),
    ("Wooden Key Holder", r"key holder|key rack|key organizer"),
    ("Wooden Couple Figurine/Block", r"figurine|sculpture|wooden block"),
    ("Sticker/Decal", r"sticker|decal"),
    ("Jewelry", r"necklace|bracelet|earring|\bring\b|pendant|charm"),
    ("Bag/Pouch", r"\bbags?\b|pouch"),
]
NICHE_RULES = [
    ("Disney/Licensed", r"disney|mickey|minnie|pooh|eeyore|toy story|buzz|woody|lightning mcqueen|stitch|"
                        r"bluey|dolly parton|princess|marvel|spider|star wars|darth|elsa|moana|rapunzel"),
    ("Funny/Gag", r"funny|gag|meme|humor|naughty|ugly"),
    ("Pet", r"\bpets?\b|\bdogs?\b|\bcats?\b|puppy|paw"),
    ("Wedding/Couple", r"wedding|engage|mr ?& ?mrs|mr and mrs|couple|bride|anniversary|together|married"),
    ("Baby/Newborn", r"baby|newborn|nursery|1st christmas|first christmas|infant"),
    ("Teacher/School", r"teacher|school|classroom|grade|kindergarten|graduat"),
    ("Halloween/Fall", r"halloween|pumpkin|ghost|spooky|\bfall\b|witch"),
    ("Memorial", r"memorial|in loving memory|remembrance"),
    ("Family", r"family|grandma|\bmom\b|\bdad\b|sibling"),
    ("Kids", r"\bkids?\b|child|toddler"),
    ("Birthday/Party", r"birthday|party"),
    ("Christmas", r"christmas|xmas|santa"),
]
_PRODUCT_TYPE_RES = [(n, re.compile(p)) for n, p in PRODUCT_TYPE_RULES]
_NICHE_RES = [(n, re.compile(p)) for n, p in NICHE_RULES]


def _classify(title: str, rules) -> str:
    t = html.unescape(str(title or "")).lower()
    for name, rx in rules:
        if rx.search(t):
            return name
    return "Other"


def load_build_list(path: str) -> list:
    """[{build_shop, pic, conflicts: [..]}] từ file Shop build.csv."""
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    cols = {c.strip().lower(): c for c in df.columns}
    shop_col, pic_col, conf_col = cols.get("shop name"), cols.get("pic"), cols.get("conflict shop")
    if not shop_col or not conf_col:
        raise ValueError("Shop build CSV cần cột 'Shop name' và 'Conflict shop'")
    out = []
    for _, r in df.iterrows():
        shop = r[shop_col].strip()
        if not shop:
            continue
        conflicts = [c.strip() for c in re.split(r"[\r\n,;]+", r[conf_col]) if c.strip()]
        out.append({"build_shop": shop, "pic": r[pic_col].strip() if pic_col else "", "conflicts": conflicts})
    return out


def _distribution(series: pd.Series) -> dict:
    vc = series.value_counts(normalize=True)
    return {k: round(float(v) * 100, 1) for k, v in vc.items()}


def _overlap(a: dict, b: dict) -> float:
    """Histogram intersection của 2 phân bố % — 100 = cơ cấu giống hệt, 0 = không chung gì.
    Bỏ qua nhóm "Other" (gom nhiều loại khác nhau, 2 shop cùng "Other" chưa chắc trùng)."""
    return round(sum(min(a[k], b[k]) for k in (set(a) & set(b)) - {"Other"}), 1)


def _shop_profile(g: pd.DataFrame) -> dict:
    views = pd.to_numeric(g["views"], errors="coerce") if "views" in g else pd.Series(dtype=float)
    price = pd.to_numeric(g["price"], errors="coerce") if "price" in g else pd.Series(dtype=float)
    tag_counts = {}
    for tags in g.get("tags", pd.Series(dtype=str)):
        for t in {x.strip().lower() for x in str(tags).split(",") if x.strip()}:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    top_tags = sorted(tag_counts, key=lambda k: -tag_counts[k])[:50]
    total_views = views.sum()
    ptype_detail = []
    for name, sub in g.groupby("_ptype"):
        v = pd.to_numeric(sub["views"], errors="coerce") if "views" in sub else pd.Series(dtype=float)
        pr = pd.to_numeric(sub["price"], errors="coerce") if "price" in sub else pd.Series(dtype=float)
        ptype_detail.append({
            "ptype": name,
            "n": int(len(sub)),
            "pct": round(len(sub) / len(g) * 100, 1),
            "views": _num(v.sum()),
            "pct_views": round(float(v.sum()) / total_views * 100, 1) if total_views else None,
            "median_price": _num(pr.median()),
            "sample_titles": [html.unescape(str(t)) for t in sub["title"].head(3)],
        })
    ptype_detail.sort(key=lambda d: -d["n"])
    return {
        "n_listings": int(len(g)),
        "views": _num(views.sum()),
        "median_price": _num(price.median()),
        "ptype": _distribution(g["_ptype"]),
        "ptype_n": {k: int(v) for k, v in g["_ptype"].value_counts().items()},
        "niche": _distribution(g["_niche"]),
        "top_tags": top_tags,
        "ptype_detail": ptype_detail,
    }


def _conflict_level(score: float) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


# Nhóm "tay nghề/xưởng" của loại SP, suy từ tên loại — shop đang làm thêu thì
# dễ mở thêm SP thêu khác hơn là chuyển sang gốm/in acrylic.
CRAFT_FAMILIES = [
    ("Thêu", r"embroider|wreath sash|hoop|pennant|bag tag|wine tote"),
    ("Acrylic", r"acrylic|badge reel|keychain|advent|photobooth"),
    ("Gốm/Sứ", r"ceramic|pottery|two-tone"),
    ("Gỗ", r"wood"),
    ("Thuỷ tinh", r"glass|suncatcher"),
    ("Vải in/Ren", r"lace|sheer|linen|fabric|tapestry|napkin|yarn|towel|pillow|blanket|stocking|canvas"),
    ("Drinkware", r"mug|cup|tumbler|can cooler"),
]
_CRAFT_RES = [(n, re.compile(p, re.I)) for n, p in CRAFT_FAMILIES]
SUGGEST_MIN_LISTINGS = 5   # loại SP phải có >= 5 listing và >= 2 shop trong data mới đủ tin cậy
SUGGEST_CONFLICT_MIN_LISTINGS = 2  # shop conflict có > 1 listing thuộc loại này -> coi là "trùng conflict"


def _craft_family(ptype: str) -> str:
    for name, rx in _CRAFT_RES:
        if rx.search(ptype):
            return name
    return "Khác"


def _market_stats(listings: pd.DataFrame) -> dict:
    """Nhu cầu từng loại SP trên toàn bộ data (build + conflict): views/tháng và
    favorites/tháng trung bình mỗi listing (chia theo listing_age để listing cũ
    không lấn át), số shop đang bán, giá trung vị, phân bố niche."""
    df = listings.copy()
    age = pd.to_numeric(df["listing_age"], errors="coerce").clip(lower=1).fillna(1)
    df["_vpm"] = pd.to_numeric(df["views"], errors="coerce").fillna(0) / age
    df["_fpm"] = pd.to_numeric(df["favorites"], errors="coerce").fillna(0) / age
    df["_price"] = pd.to_numeric(df["price"], errors="coerce")
    stats = {}
    for ptype, g in df.groupby("_ptype"):
        stats[ptype] = {
            "n": int(len(g)),
            "n_shops": int(g["shop_name"].nunique()),
            "views_pm": float(g["_vpm"].mean()),
            "favs_pm": float(g["_fpm"].mean()),
            "median_price": _num(g["_price"].median()),
            "niche": _distribution(g["_niche"]),
            "top_shops": [str(x) for x in g["shop_name"].value_counts().index[:3]],
            "sample_titles": [html.unescape(str(t)) for t in g.sort_values("_vpm", ascending=False)["title"].head(3)],
        }
    # Điểm nhu cầu 0-100 = trung bình percentile của views/tháng và favorites/tháng,
    # chỉ xếp hạng giữa các loại đủ mẫu.
    eligible = [k for k, v in stats.items()
                if v["n"] >= SUGGEST_MIN_LISTINGS and v["n_shops"] >= 2 and k != "Other" and "(khác)" not in k]
    if eligible:
        vp = pd.Series({k: stats[k]["views_pm"] for k in eligible}).rank(pct=True)
        fp = pd.Series({k: stats[k]["favs_pm"] for k in eligible}).rank(pct=True)
        for k in eligible:
            stats[k]["demand"] = round(float((vp[k] + fp[k]) / 2 * 100), 1)
    return stats


def _suggestions_for(build_shop: str, prof: dict, conflict_profiles: dict, market: dict) -> list:
    """Loại SP shop build CHƯA bán (hoặc < 5% listing), xếp theo:
    40% nhu cầu thị trường + 30% hợp xưởng (cùng nhóm tay nghề với SP đang bán)
    + 30% hợp niche (khách của shop có mua niche của loại SP này không).
    Loại SP mà shop conflict có >= SUGGEST_CONFLICT_MIN_LISTINGS listing bị gắn cờ, không cộng điểm."""
    own = {d["ptype"]: d["pct"] for d in prof["ptype_detail"]}
    family_share = {}
    for t, pct in own.items():
        fam = _craft_family(t)
        family_share[fam] = family_share.get(fam, 0) + pct
    out = []
    for ptype, m in market.items():
        if "demand" not in m or own.get(ptype, 0) >= 5:
            continue
        fam = _craft_family(ptype)
        craft_fit = min(family_share.get(fam, 0), 100.0) if fam != "Khác" else 0.0
        niche_fit = _overlap(prof["niche"], m["niche"])
        conflicts = sorted(
            [(c, cp["ptype_n"].get(ptype, 0), cp["ptype"].get(ptype, 0)) for c, cp in conflict_profiles.items()
             if cp and cp["ptype_n"].get(ptype, 0) >= SUGGEST_CONFLICT_MIN_LISTINGS],
            key=lambda x: -x[1])
        score = round(0.4 * m["demand"] + 0.3 * craft_fit + 0.3 * niche_fit, 1)
        out.append({
            "build_shop": build_shop,
            "ptype": ptype,
            "family": fam,
            "score": score,
            "demand": m["demand"],
            "craft_fit": round(craft_fit, 1),
            "niche_fit": niche_fit,
            "views_pm": round(m["views_pm"], 1),
            "favs_pm": round(m["favs_pm"], 2),
            "median_price": m["median_price"],
            "market_listings": m["n"],
            "market_shops": m["n_shops"],
            "own_pct": own.get(ptype, 0),
            "licensed_pct": m["niche"].get("Disney/Licensed", 0),
            "conflict_with": [{"shop": c, "n": n, "pct": pct} for c, n, pct in conflicts],
            "top_shops": m["top_shops"],
            "sample_titles": m["sample_titles"],
        })
    out.sort(key=lambda r: -r["score"])
    return out


def build_conflict_rows(data_dir: str, build_list_path: str) -> dict:
    """Mỗi cặp (shop build, shop conflict) 1 row: cơ cấu loại SP/niche của 2 bên
    và mức trùng lặp. Không đủ file -> available=False kèm lý do, tab hiện thông báo."""
    if not os.path.isfile(build_list_path):
        return {"available": False, "reason": f"Không tìm thấy file danh sách shop build: {build_list_path}"}
    if not os.path.isdir(data_dir):
        return {"available": False, "reason": f"Không tìm thấy thư mục dữ liệu: {data_dir}"}
    try:
        build_list = load_build_list(build_list_path)
    except Exception as e:
        return {"available": False, "reason": f"Không đọc được {build_list_path}: {e}"}

    frames = []
    for p in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        try:
            df = pd.read_csv(p, dtype=str, encoding="utf-8-sig").fillna("")
        except Exception as e:
            print(f"[warn] Bỏ qua '{p}' — không đọc được: {e}")
            continue
        if detect_kind(df) == "listings":
            frames.append(df)
    if not frames:
        return {"available": False, "reason": f"Không có file listing-level nào trong {data_dir}"}
    listings = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["shop_name", "listing_id"])
    listings["_ptype"] = listings["title"].map(lambda t: _classify(t, _PRODUCT_TYPE_RES))
    listings["_niche"] = listings["title"].map(lambda t: _classify(t, _NICHE_RES))

    # So khớp tên shop không phân biệt hoa/thường (danh sách PIC gõ tay, vd "mailydecor").
    groups = {str(k).lower(): g for k, g in listings.groupby("shop_name")}
    profiles = {}

    def profile(name):
        key = name.lower()
        if key not in profiles:
            profiles[key] = _shop_profile(groups[key]) if key in groups else None
        return profiles[key]

    pairs = []
    for b in build_list:
        pb = profile(b["build_shop"])
        for c in b["conflicts"]:
            pc = profile(c)
            row = {"build_shop": b["build_shop"], "pic": b["pic"], "conflict_shop": c,
                   "build": pb, "conflict": pc}
            if pb and pc:
                ptype_ov = _overlap(pb["ptype"], pc["ptype"])
                niche_ov = _overlap(pb["niche"], pc["niche"])
                tb, tc = set(pb["top_tags"]), set(pc["top_tags"])
                tag_ov = round(len(tb & tc) / len(tb | tc) * 100, 1) if tb | tc else 0.0
                score = round(0.5 * ptype_ov + 0.3 * niche_ov + 0.2 * tag_ov, 1)
                shared = sorted(set(pb["ptype"]) & set(pc["ptype"]),
                                key=lambda k: -min(pb["ptype"][k], pc["ptype"][k]))
                row.update({
                    "ptype_overlap": ptype_ov, "niche_overlap": niche_ov, "tag_overlap": tag_ov,
                    "score": score, "level": _conflict_level(score),
                    "shared_ptypes": [k for k in shared if k != "Other"][:3],
                    "shared_tags": sorted(tb & tc, key=pb["top_tags"].index)[:8],
                })
            else:
                row.update({"ptype_overlap": None, "niche_overlap": None, "tag_overlap": None,
                            "score": None, "level": "missing", "shared_ptypes": [], "shared_tags": []})
            pairs.append(row)
    # Danh sách shop cho tab con "Loại SP theo shop": mỗi shop build rồi tới các
    # shop conflict của nó (1 shop có thể xuất hiện ở nhiều nhóm, vd MontiKasGift).
    shops = []
    for b in build_list:
        for role, name in [("build", b["build_shop"])] + [("conflict", c) for c in b["conflicts"]]:
            shops.append({"group": b["build_shop"], "role": role, "shop": name, "profile": profile(name)})
    market = _market_stats(listings)
    suggestions = []
    for b in build_list:
        pb = profile(b["build_shop"])
        if pb:
            suggestions.extend(_suggestions_for(
                b["build_shop"], pb, {c: profile(c) for c in b["conflicts"]}, market))

    # Chỉ pairs/shops cần profile — bỏ ptype_detail lặp lại trong pairs cho nhẹ JSON.
    for r in pairs:
        for side in ("build", "conflict"):
            if r[side]:
                r[side] = {k: v for k, v in r[side].items() if k != "ptype_detail"}
    return {
        "available": True,
        "source": f"{build_list_path} + {data_dir}/",
        "pairs": pairs,
        "shops": shops,
        "suggestions": suggestions,
        # Cho tab con "Lập kế hoạch sản phẩm": JS tự phân loại tên SP người dùng gõ
        # bằng đúng bộ luật Python (cú pháp regex dùng ở đây tương thích JS), check
        # conflict theo % loại SP của shop conflict, và suy niche từ phân bố niche
        # thị trường của loại SP khi tên SP không tự chứa từ khoá niche.
        "planner": {
            "ptype_rules": PRODUCT_TYPE_RULES,
            "niche_rules": NICHE_RULES,
            "craft_rules": CRAFT_FAMILIES,
            "conflict_min_n": SUGGEST_CONFLICT_MIN_LISTINGS,
            "build_list": build_list,
            "conflict_ptype": {c: (profile(c) or {}).get("ptype", {})
                               for b in build_list for c in b["conflicts"]},
            "conflict_ptype_n": {c: (profile(c) or {}).get("ptype_n", {})
                                 for b in build_list for c in b["conflicts"]},
            "market_niche": {k: v["niche"] for k, v in market.items()},
        },
        # Tab con "Kế hoạch listing": target listing mới theo tuần × dòng SP (listing_plan.py).
        "listing_plan": listing_plan.build_payload(listings, build_list, profile, market, data_dir),
    }


def _json(obj):
    # allow_nan=False: json.dumps mặc định cho phép NaN/Infinity (mở rộng
    # riêng của Python, không chuẩn JSON) — browser's JSON.parse() sẽ throw
    # SyntaxError trên các token này, làm đứng cả script (mất luôn phần gắn
    # sự kiện chuyển tab). Thà lộ lỗi 500 rõ ràng ở đây (dễ phát hiện sớm nếu
    # còn sót đường nào chưa fillna) còn hơn để trang render âm thầm hỏng.
    return json.dumps(obj, ensure_ascii=False, allow_nan=False).replace("</script>", "<\\/script>")


def render_report(payload: dict) -> str:
    if payload.get("shop_only"):
        listings_line = (
            '<strong style="color:var(--warn, #a8762a)">Chưa có — chỉ upload 1 file shop-level. '
            'Verdict trong bảng dưới là <u>SƠ BỘ</u> (chỉ dựa category/description shop, chưa có '
            '%gift-keyword/%customizable thật từ listing) — crawl thêm listing cho các shop này để '
            'chấm chính xác.</strong>'
        )
    else:
        listings_line = f"file listing-level: <strong>{html.escape(payload['listings_filename'])}</strong>"
    meta_html = (
        f"file shop-level: <strong>{html.escape(payload['shops_filename'])}</strong><br>"
        f"{listings_line}"
    )
    out = REPORT_TEMPLATE.replace('<div class="wrap">', nav_html("analyzer") + '<div class="wrap">', 1)
    out = out.replace("__META_HTML__", meta_html)
    out = out.replace("__DATA_JSON__", _json(payload["ledger_data"]))
    out = out.replace("__REVERSE_JSON__", _json(payload["reverse_rows"]))
    out = out.replace("__BIZ_JSON__", _json(payload["biz_rows"]))
    out = out.replace("__BIZ_COLUMNS_JSON__", _json(payload["biz_columns"]))
    out = out.replace("__TOP10_JSON__", _json(payload["top_rows"]))
    out = out.replace("__TOP10_META_JSON__", _json({"available": payload["top_available"]}))
    out = out.replace("__PRODUCTS_JSON__", _json(payload["product_rows"]))
    out = out.replace("__KEYWORD_ROWS_JSON__", _json(payload["keyword_rows"]))
    out = out.replace("__KEYWORD_BIZ_JSON__", _json(payload["keyword_biz_rows"]))
    return out


# ── Multipart upload parsing (stdlib only, khong can Flask) ─────────────────
def parse_multipart(body: bytes, boundary: str) -> dict:
    delimiter = b"--" + boundary.encode()
    fields = {}
    for part in body.split(delimiter):
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        headers = header_blob.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', headers)
        if not name_m:
            continue
        filename_m = re.search(r'filename="([^"]*)"', headers)
        fields[name_m.group(1)] = {
            "filename": filename_m.group(1) if filename_m else None,
            "content": content,
        }
    return fields


# ── Navbar dùng chung cho mọi trang của server ──────────────────────────────
# Style tự chứa (màu cố định + dark mode riêng) vì mỗi trang có bộ CSS khác
# nhau: form upload/compare dùng màu hard-code, report + compare dùng biến
# --paper/--ink... Navbar fixed trên cùng, body được đệm padding-top tương ứng.
NAV_ITEMS = [
    ("analyzer", "/", "Shop Gift-Fit Analyzer"),
    ("compare", "/compare", "So sánh 2 phiên bản"),
    ("buildconf", "/build-conflict", "Build vs Conflict"),
    ("buildhist", "/build-history", "Shop build theo thời gian"),
]
NAV_CSS = """
body { padding-top: 52px !important; }
.app-nav { position: fixed; top: 0; left: 0; right: 0; z-index: 1000; height: 52px;
  display: flex; align-items: center; gap: 4px; padding: 0 20px; overflow-x: auto;
  background: #20261f; box-shadow: 0 1px 3px rgba(0,0,0,.25);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
.app-nav .brand { color: #e7e9e1; font-family: Georgia, "Iowan Old Style", serif; font-size: 15px;
  margin-right: 18px; white-space: nowrap; }
.app-nav a { color: #b9c0b0; text-decoration: none; font-size: 13.5px; font-weight: 600; white-space: nowrap;
  padding: 7px 12px; border-radius: 4px; }
.app-nav a:hover { color: #fff; background: rgba(255,255,255,.08); }
.app-nav a[aria-current="page"] { color: #fff; background: #2f6b6b; }
"""


def nav_html(active: str) -> str:
    links = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if key == active else '') + f'>{label}</a>'
        for key, href, label in NAV_ITEMS
    )
    return f'<style>{NAV_CSS}</style><nav class="app-nav"><span class="brand">Shop Tools</span>{links}</nav>'


def _nav_for_format(active: str) -> str:
    """nav_html() đã escape {} để chèn vào template dùng str.format()."""
    return nav_html(active).replace("{", "{{").replace("}", "}}")


UPLOAD_FORM_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>Shop Gift-Fit Analyzer</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #eff1ec; color: #20261f; margin: 0;
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    background: #f6f7f3; border: 1px solid #d2d6c9; border-radius: 6px;
    padding: 34px 38px; max-width: 460px; width: 90%;
    box-shadow: 0 1px 2px rgba(32,38,31,.06), 0 6px 16px rgba(32,38,31,.05);
  }}
  h1 {{ font-family: Georgia, "Iowan Old Style", serif; font-size: 22px; margin: 0 0 8px; }}
  p.sub {{ color: #5a6455; font-size: 13.5px; line-height: 1.5; margin: 0 0 22px; }}
  label {{ display: block; font-size: 12.5px; font-weight: 600; color: #5a6455; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: .03em; }}
  input[type=file] {{
    width: 100%; padding: 9px; border: 1px solid #b7bcac; border-radius: 4px;
    background: #fff; font-size: 13px; font-family: inherit;
  }}
  button {{
    margin-top: 22px; width: 100%; padding: 12px; border: none; border-radius: 4px;
    background: #2f6b6b; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #25554f; }}
  .err {{ color: #a8483d; background: #f2ded9; border-radius: 4px; padding: 10px 12px; font-size: 13px; margin-top: 16px; }}
  .hint {{ color: #8c8677; font-size: 12px; margin-top: 18px; }}
  a.compare-link {{ display: inline-block; margin-top: 14px; font-size: 13px; color: #2f6b6b; }}
</style></head>
<body>
  __NAV__
  <div class="card">
    <h1>Shop Gift-Fit Analyzer</h1>
    <p class="sub">
      Upload 2 file CSV — 1 file shop-level (mỗi dòng 1 shop, ví dụ
      <code>everbee_shop_data_shops.csv</code>) và 1 file listing-level (mỗi dòng
      1 listing, ví dụ <code>everbee_shop_data_listings.csv</code>). Không cần đúng
      thứ tự, tool tự nhận diện qua tên cột. <strong>Chỉ có file shop-level (chưa
      crawl listing)?</strong> Bỏ trống File CSV #2 — tool vẫn phân tích được, verdict
      sẽ là SƠ BỘ (chỉ dựa category/description shop, cần crawl thêm listing để
      chấm chính xác).
    </p>
    <form action="/analyze" method="post" enctype="multipart/form-data">
      <label>File CSV #1 (shop-level, bắt buộc)</label>
      <input type="file" name="file1" accept=".csv" required>
      <label>File CSV #2 (listing-level, bỏ trống nếu chưa có)</label>
      <input type="file" name="file2" accept=".csv">
      <button type="submit">Phân tích</button>
    </form>
    {error_html}
    <p class="hint">Kết quả cũng được lưu vào analysis_output/&lt;timestamp&gt;/ trên máy chạy script
    (chỉ khi cặp file upload khác nội dung với tất cả lần phân tích trước).</p>
  </div>
</body></html>""".replace("__NAV__", _nav_for_format("analyzer"))


# ── Manifest chống lưu trùng ──────────────────────────────────────────────────
# Ghi lại hash nội dung (không phải tên file — nhiều lần upload dùng CHUNG 1 tên
# file như "everbee_shop_data_shops.csv" nhưng nội dung mỗi lần khác nhau) của
# cặp file shops/listings đã từng phân tích, để lần upload sau nếu đúng cặp nội
# dung này thì bỏ qua việc tạo folder + ghi HTML mới, chỉ trả report như bình
# thường. Các folder analysis_output/<timestamp>/ tạo TRƯỚC khi có manifest này
# không có trong đây (không giữ lại bytes gốc để hash lại) — chỉ áp dụng từ giờ.
IMPORT_MANIFEST_PATH = os.path.join("analysis_output", ".import_manifest.json")


def _load_import_manifest() -> dict:
    if not os.path.exists(IMPORT_MANIFEST_PATH):
        return {}
    try:
        with open(IMPORT_MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_import_manifest(manifest: dict) -> None:
    os.makedirs(os.path.dirname(IMPORT_MANIFEST_PATH), exist_ok=True)
    with open(IMPORT_MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


# ── So sánh 2 phiên bản đã lưu ────────────────────────────────────────────────
# Cấp shop: đọc lại JSON đã nhúng sẵn trong shop_gift_fit_report.html (không cần
# đổi gì) — áp dụng được cho MỌI lần phân tích đã lưu, kể cả trước khi có
# RAW_SHOPS_CSV/RAW_LISTINGS_CSV bên dưới.
# Cấp listing: cần 2 file CSV gốc (RAW_LISTINGS_CSV) — chỉ có ở các lần phân
# tích lưu TỪ lúc thêm tính năng này trở đi.
RAW_SHOPS_CSV = "shops_raw.csv"
RAW_LISTINGS_CSV = "listings_raw.csv"

LISTING_DIFF_NUMERIC_COLS = ["views", "favorites", "reviews", "total_sold", "price", "visibility_score"]


def _delta(a, b):
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return None
    return round(fb - fa, 2)


def _extract_embedded_json(html_text: str, script_id: str):
    m = re.search(
        rf'<script id="{re.escape(script_id)}" type="application/json">(.*?)</script>',
        html_text, re.S,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _ledger_by_shop(ledger_data: list) -> dict:
    return {row.get("shop", ""): row for row in ledger_data if row.get("shop")}


def _shop_label(row: dict) -> str:
    return "filtered" if row.get("status") == "filtered" else row.get("verdict", "no")


def diff_shop_ledger(ledger_a: dict, ledger_b: dict) -> dict:
    """So 2 ledger (shop -> row) đọc từ __DATA_JSON__ của 2 report — shop
    thêm/mất, verdict đổi (good/review/no/filtered), và các shop có metric
    (total_sold/%customizable/%gift) đổi nhiều nhất."""
    names_a, names_b = set(ledger_a), set(ledger_b)
    added = sorted(names_b - names_a)
    removed = sorted(names_a - names_b)
    common = sorted(names_a & names_b)

    verdict_changes = []
    metric_movers = []
    for name in common:
        ra, rb = ledger_a[name], ledger_b[name]
        la, lb = _shop_label(ra), _shop_label(rb)
        if la != lb:
            verdict_changes.append({"shop": name, "from": la, "to": lb})
        if ra.get("status") == "present" and rb.get("status") == "present":
            d_sold = _delta(ra.get("total_sold"), rb.get("total_sold"))
            d_cust = _delta(ra.get("pct_customizable"), rb.get("pct_customizable"))
            d_kw = _delta(ra.get("pct_gift_kw"), rb.get("pct_gift_kw"))
            d_n = _delta(ra.get("n_listings"), rb.get("n_listings"))
            if any(v not in (None, 0) for v in (d_sold, d_cust, d_kw, d_n)):
                metric_movers.append({
                    "shop": name,
                    "total_sold_from": _num(ra.get("total_sold")), "total_sold_to": _num(rb.get("total_sold")),
                    "total_sold_delta": d_sold,
                    "pct_customizable_delta": d_cust,
                    "pct_gift_kw_delta": d_kw,
                    "n_listings_delta": d_n,
                })
    metric_movers.sort(key=lambda r: abs(r["total_sold_delta"] or 0), reverse=True)

    return {
        "added": [{"shop": n, "shop_category": ledger_b[n].get("shop_category", ""), "label": _shop_label(ledger_b[n])} for n in added],
        "removed": [{"shop": n, "shop_category": ledger_a[n].get("shop_category", ""), "label": _shop_label(ledger_a[n])} for n in removed],
        "verdict_changes": verdict_changes,
        "metric_movers": metric_movers[:20],
        "counts": {"added": len(added), "removed": len(removed), "common": len(common)},
    }


def diff_top10(top_a: list, top_b: list) -> dict:
    """So 2 danh sách top_rows (heyetsy) — shop vào/ra top20 + đổi hạng/tăng
    trưởng avg_weekly_sold cho shop còn nằm trong cả 2 top."""
    by_a = {r.get("shop", ""): r for r in top_a if r.get("shop")}
    by_b = {r.get("shop", ""): r for r in top_b if r.get("shop")}
    entered = sorted(set(by_b) - set(by_a))
    left = sorted(set(by_a) - set(by_b))

    rank_changes = []
    for shop in sorted(set(by_a) & set(by_b)):
        ra, rb = by_a[shop], by_b[shop]
        rank_changes.append({
            "shop": shop,
            "rank_from": ra.get("rank"), "rank_to": rb.get("rank"),
            "avg_weekly_sold_from": ra.get("avg_weekly_sold"), "avg_weekly_sold_to": rb.get("avg_weekly_sold"),
            "avg_weekly_sold_delta": _delta(ra.get("avg_weekly_sold"), rb.get("avg_weekly_sold")),
        })
    rank_changes.sort(key=lambda r: abs(r["avg_weekly_sold_delta"] or 0), reverse=True)

    return {"entered": entered, "left": left, "rank_changes": rank_changes[:20]}


def diff_listings(listings_a_path: str, listings_b_path: str) -> Optional[dict]:
    """So 2 file listing GỐC (raw bytes đã lưu) theo key (shop_name, listing_id)
    — listing mới/mất, và delta views/favorites/reviews/total_sold/price/
    visibility_score cho listing còn tồn tại ở cả 2 lần."""
    df_a = pd.read_csv(listings_a_path, dtype=str, encoding="utf-8-sig").fillna("")
    df_b = pd.read_csv(listings_b_path, dtype=str, encoding="utf-8-sig").fillna("")
    if "listing_id" not in df_a.columns or "listing_id" not in df_b.columns:
        return None

    df_a = df_a.assign(_key=df_a["shop_name"].astype(str) + "::" + df_a["listing_id"].astype(str))
    df_b = df_b.assign(_key=df_b["shop_name"].astype(str) + "::" + df_b["listing_id"].astype(str))
    a_idx = df_a.drop_duplicates("_key").set_index("_key")
    b_idx = df_b.drop_duplicates("_key").set_index("_key")
    keys_a, keys_b = set(a_idx.index), set(b_idx.index)
    new_keys, removed_keys, common_keys = keys_b - keys_a, keys_a - keys_b, keys_a & keys_b

    def _brief(row):
        return {
            "shop_name": row.get("shop_name", ""),
            "title": row.get("title", ""),
            "etsy_url": row.get("etsy_url", ""),
            "total_sold": _num(row.get("total_sold")),
            "views": _num(row.get("views")),
            "price": _num(row.get("price")),
        }

    new_listings = sorted((_brief(b_idx.loc[k]) for k in new_keys), key=lambda r: r["total_sold"] or 0, reverse=True)
    removed_listings = sorted((_brief(a_idx.loc[k]) for k in removed_keys), key=lambda r: r["total_sold"] or 0, reverse=True)

    movers = []
    for k in common_keys:
        ra, rb = a_idx.loc[k], b_idx.loc[k]
        deltas = {c: _delta(ra.get(c), rb.get(c)) for c in LISTING_DIFF_NUMERIC_COLS}
        if any(v not in (None, 0) for v in deltas.values()):
            movers.append({
                "shop_name": rb.get("shop_name", ""),
                "title": rb.get("title", ""),
                "etsy_url": rb.get("etsy_url", ""),
                **{f"{c}_delta": deltas[c] for c in LISTING_DIFF_NUMERIC_COLS},
                **{f"{c}_to": _num(rb.get(c)) for c in LISTING_DIFF_NUMERIC_COLS},
            })

    return {
        "counts": {"new": len(new_keys), "removed": len(removed_keys), "common": len(common_keys)},
        "new_listings": new_listings[:20],
        "removed_listings": removed_listings[:20],
        "top_gainers": sorted(movers, key=lambda r: r["total_sold_delta"] or 0, reverse=True)[:20],
        "top_losers": sorted(movers, key=lambda r: r["total_sold_delta"] or 0)[:20],
        "price_changes": sorted(
            [m for m in movers if m.get("price_delta")], key=lambda r: abs(r["price_delta"] or 0), reverse=True
        )[:20],
    }


def _list_available_runs() -> list:
    """Quét analysis_output/*/ cho các lần phân tích đã lưu (có report HTML),
    mới nhất trước, kèm tên file gốc (từ manifest) và có raw CSV listing hay
    không (quyết định có so được cấp listing hay chỉ cấp shop)."""
    dirs = sorted(
        (d for d in glob.glob(os.path.join("analysis_output", "*"))
         if os.path.isdir(d) and os.path.exists(os.path.join(d, "shop_gift_fit_report.html"))),
        reverse=True,
    )
    manifest = _load_import_manifest()
    meta_by_dir = {v.get("dir"): v for v in manifest.values()}
    runs = []
    for d in dirs:
        meta = meta_by_dir.get(d, {})
        has_raw = os.path.exists(os.path.join(d, RAW_SHOPS_CSV)) and os.path.exists(os.path.join(d, RAW_LISTINGS_CSV))
        runs.append({
            "dir": os.path.basename(d),
            "timestamp": meta.get("timestamp", os.path.basename(d)),
            "shops_filename": meta.get("shops_filename", ""),
            "listings_filename": meta.get("listings_filename", ""),
            "has_raw": has_raw,
        })
    return runs


def _table_html(headers: list, rows: list, keys: list, formatters: dict = None) -> str:
    if not rows:
        return '<p class="empty">Không có gì.</p>'
    formatters = formatters or {}
    thead = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for r in rows:
        cells = []
        for k in keys:
            v = r.get(k)
            fmt = formatters.get(k)
            cells.append(f"<td>{fmt(r) if fmt else html.escape('' if v is None else str(v))}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f'<table><thead><tr>{thead}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def _fmt_delta(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    css = "up" if v > 0 else ("down" if v < 0 else "flat")
    return f'<span class="delta {css}">{sign}{v:g}</span>'


def _fmt_num(v):
    return "—" if v is None else f"{v:g}"


def _fmt_verdict(v):
    return f'<span class="tag tag-{html.escape(str(v))}">{html.escape(str(v))}</span>'


def _fmt_title_link(row):
    url = row.get("etsy_url") or ""
    title = row.get("title") or "(không có tiêu đề)"
    if url:
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{html.escape(title)}</a>'
    return html.escape(title)


COMPARE_THEME_CSS = """
:root {
  --paper: #eff1ec; --paper-raised: #f6f7f3; --ink: #20261f; --ink-soft: #5a6455;
  --line: #d2d6c9; --line-strong: #b7bcac; --accent: #2f6b6b; --accent-soft: #dbe8e6;
  --good: #3f8452; --good-soft: #e1eee2; --warn: #a8762a; --warn-soft: #f2e6cf;
  --critical: #a8483d; --critical-soft: #f2ded9; --muted: #8c8677; --muted-soft: #e6e3da;
  --shadow: 0 1px 2px rgba(32,38,31,0.06), 0 6px 16px rgba(32,38,31,0.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #171b16; --paper-raised: #1e231c; --ink: #e7e9e1; --ink-soft: #a3aa9a;
    --line: #333a2f; --line-strong: #454e3d; --accent: #6fada6; --accent-soft: #253330;
    --good: #7cbb85; --good-soft: #24322a; --warn: #d3a962; --warn-soft: #35301f;
    --critical: #d98d80; --critical-soft: #382522; --muted: #9c9788; --muted-soft: #262620;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.35);
  }
}
:root[data-theme="dark"] {
  --paper: #171b16; --paper-raised: #1e231c; --ink: #e7e9e1; --ink-soft: #a3aa9a;
  --line: #333a2f; --line-strong: #454e3d; --accent: #6fada6; --accent-soft: #253330;
  --good: #7cbb85; --good-soft: #24322a; --warn: #d3a962; --warn-soft: #35301f;
  --critical: #d98d80; --critical-soft: #382522; --muted: #9c9788; --muted-soft: #262620;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.35);
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.5; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 80px; }
a { color: var(--accent); }
h1 { font-family: Georgia, "Iowan Old Style", serif; font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 36px 0 10px; border-bottom: 1px solid var(--line-strong); padding-bottom: 6px; }
.meta { color: var(--ink-soft); font-size: 13px; margin-bottom: 4px; }
.counts { display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0 18px; }
.count-pill { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 20px; padding: 5px 14px; font-size: 12.5px; box-shadow: var(--shadow); }
table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--paper-raised); box-shadow: var(--shadow); border-radius: 6px; overflow: hidden; }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--line); }
th { color: var(--ink-soft); font-size: 11.5px; text-transform: uppercase; letter-spacing: .03em; }
tr:last-child td { border-bottom: none; }
.delta.up { color: var(--good); font-weight: 600; }
.delta.down { color: var(--critical); font-weight: 600; }
.delta.flat { color: var(--muted); }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11.5px; }
.tag-good { background: var(--good-soft); color: var(--good); }
.tag-review { background: var(--warn-soft); color: var(--warn); }
.tag-no { background: var(--muted-soft); color: var(--muted); }
.tag-filtered { background: var(--critical-soft); color: var(--critical); }
.empty { color: var(--muted); font-size: 13px; }
.note { background: var(--warn-soft); color: var(--warn); border-radius: 6px; padding: 10px 14px; font-size: 13px; margin: 10px 0 18px; }
.tables-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
@media (max-width: 800px) { .tables-2col { grid-template-columns: 1fr; } }
"""


def render_compare_report(payload: dict) -> str:
    run_a, run_b = payload["run_a"], payload["run_b"]
    sd, td, ld = payload["shop_diff"], payload["top_diff"], payload["listing_diff"]

    added_html = _table_html(
        ["Shop mới (B)", "Ngành hàng", "Trạng thái"], sd["added"], ["shop", "shop_category", "label"],
        {"label": lambda r: _fmt_verdict(r["label"])},
    )
    removed_html = _table_html(
        ["Shop mất (không còn ở B)", "Ngành hàng", "Trạng thái"], sd["removed"], ["shop", "shop_category", "label"],
        {"label": lambda r: _fmt_verdict(r["label"])},
    )
    verdict_html = _table_html(
        ["Shop", "Trước (A)", "Sau (B)"], sd["verdict_changes"], ["shop", "from", "to"],
        {"from": lambda r: _fmt_verdict(r["from"]), "to": lambda r: _fmt_verdict(r["to"])},
    )
    movers_html = _table_html(
        ["Shop", "Total sold A→B", "Δ Total sold", "Δ %customizable", "Δ %gift kw", "Δ n_listings"],
        sd["metric_movers"], ["shop", "_sold_pair", "total_sold_delta", "pct_customizable_delta", "pct_gift_kw_delta", "n_listings_delta"],
        {
            "_sold_pair": lambda r: f'{_fmt_num(r["total_sold_from"])} → {_fmt_num(r["total_sold_to"])}',
            "total_sold_delta": lambda r: _fmt_delta(r["total_sold_delta"]),
            "pct_customizable_delta": lambda r: _fmt_delta(r["pct_customizable_delta"]),
            "pct_gift_kw_delta": lambda r: _fmt_delta(r["pct_gift_kw_delta"]),
            "n_listings_delta": lambda r: _fmt_delta(r["n_listings_delta"]),
        },
    )

    entered_html = "".join(f'<li>{html.escape(s)}</li>' for s in td["entered"]) or '<li class="empty">Không có.</li>'
    left_html = "".join(f'<li>{html.escape(s)}</li>' for s in td["left"]) or '<li class="empty">Không có.</li>'
    rank_html = _table_html(
        ["Shop", "Hạng A→B", "Avg weekly sold A→B", "Δ"], td["rank_changes"],
        ["shop", "_rank_pair", "_sold_pair", "avg_weekly_sold_delta"],
        {
            "_rank_pair": lambda r: f'#{_fmt_num(r["rank_from"])} → #{_fmt_num(r["rank_to"])}',
            "_sold_pair": lambda r: f'{_fmt_num(r["avg_weekly_sold_from"])} → {_fmt_num(r["avg_weekly_sold_to"])}',
            "avg_weekly_sold_delta": lambda r: _fmt_delta(r["avg_weekly_sold_delta"]),
        },
    )

    if ld is None:
        listing_section = (
            '<div class="note">Không so được cấp listing: 1 trong 2 (hoặc cả 2) phiên bản không có '
            'file listing gốc đã lưu (chỉ các lần phân tích SAU khi có tính năng so sánh mới lưu '
            'thêm file này).</div>'
        )
    else:
        new_l_html = _table_html(
            ["Shop", "Listing mới (B)", "Total sold", "Views"], ld["new_listings"],
            ["shop_name", "_title", "total_sold", "views"],
            {"_title": lambda r: _fmt_title_link(r), "total_sold": lambda r: _fmt_num(r["total_sold"]), "views": lambda r: _fmt_num(r["views"])},
        )
        removed_l_html = _table_html(
            ["Shop", "Listing mất (chỉ ở A)", "Total sold", "Views"], ld["removed_listings"],
            ["shop_name", "_title", "total_sold", "views"],
            {"_title": lambda r: _fmt_title_link(r), "total_sold": lambda r: _fmt_num(r["total_sold"]), "views": lambda r: _fmt_num(r["views"])},
        )
        gainers_html = _table_html(
            ["Shop", "Listing", "Δ Total sold", "Δ Views", "Δ Favorites"], ld["top_gainers"],
            ["shop_name", "_title", "total_sold_delta", "views_delta", "favorites_delta"],
            {
                "_title": lambda r: _fmt_title_link(r),
                "total_sold_delta": lambda r: _fmt_delta(r["total_sold_delta"]),
                "views_delta": lambda r: _fmt_delta(r["views_delta"]),
                "favorites_delta": lambda r: _fmt_delta(r["favorites_delta"]),
            },
        )
        losers_html = _table_html(
            ["Shop", "Listing", "Δ Total sold", "Δ Views", "Δ Favorites"], ld["top_losers"],
            ["shop_name", "_title", "total_sold_delta", "views_delta", "favorites_delta"],
            {
                "_title": lambda r: _fmt_title_link(r),
                "total_sold_delta": lambda r: _fmt_delta(r["total_sold_delta"]),
                "views_delta": lambda r: _fmt_delta(r["views_delta"]),
                "favorites_delta": lambda r: _fmt_delta(r["favorites_delta"]),
            },
        )
        price_html = _table_html(
            ["Shop", "Listing", "Δ Giá"], ld["price_changes"], ["shop_name", "_title", "price_delta"],
            {"_title": lambda r: _fmt_title_link(r), "price_delta": lambda r: _fmt_delta(r["price_delta"])},
        )
        listing_section = f"""
        <div class="counts">
          <span class="count-pill">Listing mới: {ld['counts']['new']}</span>
          <span class="count-pill">Listing mất: {ld['counts']['removed']}</span>
          <span class="count-pill">Còn ở cả 2: {ld['counts']['common']}</span>
        </div>
        <div class="tables-2col">
          <div><h2 style="margin-top:0">Listing mới</h2>{new_l_html}</div>
          <div><h2 style="margin-top:0">Listing mất</h2>{removed_l_html}</div>
        </div>
        <div class="tables-2col">
          <div><h2>Tăng trưởng mạnh nhất</h2>{gainers_html}</div>
          <div><h2>Giảm mạnh nhất</h2>{losers_html}</div>
        </div>
        <h2>Đổi giá nhiều nhất</h2>
        {price_html}
        """

    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>So sánh {html.escape(run_a)} vs {html.escape(run_b)}</title>
<style>{COMPARE_THEME_CSS}</style>
</head><body>{nav_html("compare")}<div class="wrap">
  <h1>So sánh 2 phiên bản dashboard</h1>
  <p class="meta">Phiên bản A (cũ): <strong>{html.escape(run_a)}</strong> — Phiên bản B (mới): <strong>{html.escape(run_b)}</strong></p>

  <h2 style="margin-top:20px">Shop — tổng quan</h2>
  <div class="counts">
    <span class="count-pill">Shop mới: {sd['counts']['added']}</span>
    <span class="count-pill">Shop mất: {sd['counts']['removed']}</span>
    <span class="count-pill">Còn ở cả 2: {sd['counts']['common']}</span>
  </div>
  <div class="tables-2col">
    <div>{added_html}</div>
    <div>{removed_html}</div>
  </div>

  <h2>Verdict đổi (good / review / no / filtered)</h2>
  {verdict_html}

  <h2>Metric đổi nhiều nhất (top 20 theo |Δ total_sold|)</h2>
  {movers_html}

  <h2>Top bán chạy heyetsy — vào/ra top 20</h2>
  <div class="tables-2col">
    <div><strong>Vào top:</strong><ul>{entered_html}</ul></div>
    <div><strong>Ra khỏi top:</strong><ul>{left_html}</ul></div>
  </div>
  <h2>Đổi hạng trong top (top 20 theo |Δ avg_weekly_sold|)</h2>
  {rank_html}

  <h2>Listing</h2>
  {listing_section}
</div></body></html>"""


COMPARE_FORM_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>So sánh phiên bản</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #eff1ec; color: #20261f; margin: 0; min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    background: #f6f7f3; border: 1px solid #d2d6c9; border-radius: 6px;
    padding: 34px 38px; max-width: 520px; width: 90%;
    box-shadow: 0 1px 2px rgba(32,38,31,.06), 0 6px 16px rgba(32,38,31,.05);
  }}
  h1 {{ font-family: Georgia, "Iowan Old Style", serif; font-size: 22px; margin: 0 0 8px; }}
  a.back {{ font-size: 13px; }}
  label {{ display: block; font-size: 12.5px; font-weight: 600; color: #5a6455; margin: 16px 0 6px; text-transform: uppercase; letter-spacing: .03em; }}
  select {{
    width: 100%; padding: 9px; border: 1px solid #b7bcac; border-radius: 4px;
    background: #fff; font-size: 13px; font-family: inherit;
  }}
  button {{
    margin-top: 22px; width: 100%; padding: 12px; border: none; border-radius: 4px;
    background: #2f6b6b; color: #fff; font-size: 14px; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #25554f; }}
  .err {{ color: #a8483d; background: #f2ded9; border-radius: 4px; padding: 10px 12px; font-size: 13px; margin-top: 16px; }}
  .hint {{ color: #8c8677; font-size: 12px; margin-top: 18px; }}
</style></head>
<body>
  __NAV__
  <div class="card">
    <h1>So sánh 2 phiên bản dashboard</h1>
    {error_html}
    <form action="/compare" method="post">
      <label>Phiên bản A (cũ hơn)</label>
      <select name="run_a" required>{options_a}</select>
      <label>Phiên bản B (mới hơn)</label>
      <select name="run_b" required>{options_b}</select>
      <button type="submit">So sánh</button>
    </form>
    <p class="hint">Cấp shop so được cho mọi phiên bản đã lưu. Cấp listing (listing mới/mất, đổi
    giá/views...) chỉ so được nếu CẢ 2 phiên bản đều được lưu sau khi có tính năng này (đánh dấu
    "chưa có raw CSV" nếu chưa lưu được).</p>
  </div>
</body></html>""".replace("__NAV__", _nav_for_format("compare"))


BUILD_CONFLICT_CSS = """
.wrap { max-width: 1480px; }
.reverse-intro { color: var(--ink-soft); font-size: 13.5px; max-width: 760px; margin: 0 0 16px; }
.controls { display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 10px; }
.controls select { background: var(--paper-raised); color: var(--ink); border: 1px solid var(--line-strong);
  border-radius: 3px; padding: 8px 10px; font-size: 14px; font-family: inherit; }
.chart-caveat { color: var(--ink-soft); font-size: 12.5px; margin: 0 0 10px; }
.empty-state { padding: 40px; text-align: center; color: var(--ink-soft); font-size: 14px; }
.table-scroll { overflow: auto; max-height: 720px; border: 1px solid var(--line); border-radius: 4px; box-shadow: var(--shadow); }
.table-scroll table { box-shadow: none; border-radius: 0; overflow: visible; }
thead th { position: sticky; top: 0; background: var(--paper-raised); white-space: nowrap; }
td { vertical-align: top; }
td.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
td.shop { font-family: "SF Mono", "Menlo", "Consolas", ui-monospace, monospace; font-weight: 600; white-space: nowrap; }
td.shop a { color: var(--ink); text-decoration: none; border-bottom: 1px dotted var(--line-strong); }
td.shop a:hover { color: var(--accent); }
td .cat { color: var(--ink-soft); }
td.sample { color: var(--ink-soft); font-size: 12.5px; }
td.sample .t { display: block; white-space: nowrap; }
.bar-cell { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
.bar-track { width: 46px; height: 6px; border-radius: 3px; background: var(--muted-soft); overflow: hidden; }
.bar-fill { display: block; height: 100%; border-radius: 3px; background: var(--accent); }
.planner-form { display: flex; gap: 10px; flex-wrap: wrap; margin: 0 0 6px; }
.planner-form input[type="search"] { flex: 1 1 320px; min-width: 220px; background: var(--paper-raised); color: var(--ink);
  border: 1px solid var(--line-strong); border-radius: 3px; padding: 8px 12px; font-size: 14px; font-family: inherit; }
.planner-form select { flex: 0 1 380px; min-width: 220px; background: var(--paper-raised); color: var(--ink);
  border: 1px solid var(--line-strong); border-radius: 3px; padding: 8px 10px; font-size: 14px; font-family: inherit; }
.btn { background: var(--accent); color: #fff; border: none; border-radius: 3px; padding: 8px 14px; font: inherit;
  font-size: 13.5px; font-weight: 600; cursor: pointer; }
.btn:disabled { opacity: .45; cursor: not-allowed; }
.btn.ghost { background: none; color: var(--accent); border: 1px solid var(--line-strong); }
.niche-line { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; margin: 6px 0 0; font-size: 12px; color: var(--ink-soft); }
.niche-line .lbl { min-width: 92px; }
.chipn { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11.5px;
  background: var(--muted-soft); color: var(--ink-soft); }
.chipn.main { background: var(--accent-soft); color: var(--accent); font-weight: 700; }
.pl-list { list-style: none; margin: 10px 0 0; padding: 0; border-top: 1px solid var(--line); }
.pl-list li { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; padding: 6px 0;
  border-bottom: 1px solid var(--line); font-size: 13px; }
.pl-list li .pt { color: var(--ink-soft); font-size: 11.5px; }
.pl-list button { background: none; border: none; color: var(--critical); cursor: pointer; font-size: 15px; line-height: 1; }
.pl-empty { color: var(--muted); font-size: 12.5px; padding: 10px 0 2px; }
.chk { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; color: var(--ink-soft); cursor: pointer; }
.subtabs { display: flex; gap: 6px; border-bottom: 1px solid var(--line-strong); margin: 0 0 14px; }
.subtab { background: none; border: none; border-bottom: 2px solid transparent; margin-bottom: -1px;
  padding: 8px 12px; font: inherit; font-size: 13.5px; font-weight: 600; color: var(--ink-soft); cursor: pointer; }
.subtab:hover { color: var(--ink); }
.subtab[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
.group-head { font-size: 13px; font-weight: 600; color: var(--ink-soft); text-transform: uppercase;
  letter-spacing: .04em; margin: 22px 0 8px; }
.shop-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(500px, 1fr)); gap: 14px; }
@media (max-width: 520px) { .shop-grid { grid-template-columns: 1fr; } }
.shop-card { background: var(--paper-raised); border: 1px solid var(--line); border-radius: 6px;
  padding: 14px 16px; box-shadow: var(--shadow); }
.shop-card.build { border-left: 3px solid var(--accent); }
.shop-card .head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; margin-bottom: 4px; }
.shop-card .name { font-family: "SF Mono", "Menlo", "Consolas", ui-monospace, monospace; font-weight: 600; }
.shop-card .name a { color: var(--ink); text-decoration: none; border-bottom: 1px dotted var(--line-strong); }
.shop-card .sub { color: var(--ink-soft); font-size: 12px; margin-bottom: 10px; }
.pt-row { display: grid; grid-template-columns: 215px 1fr 170px; gap: 10px; align-items: center;
  font-size: 12.5px; padding: 3px 0; cursor: default; }
.pt-row .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pt-row .track { height: 8px; border-radius: 4px; background: var(--muted-soft); overflow: hidden; }
.pt-row .fill { display: block; height: 100%; border-radius: 4px; background: var(--accent); }
.pt-row.other .fill { background: var(--muted); }
.pt-row .val { text-align: right; font-variant-numeric: tabular-nums; color: var(--ink-soft); white-space: nowrap; }
.pt-row .val strong { color: var(--ink); }
.pill.role-build { background: var(--accent-soft); color: var(--accent); }
.pill.role-conflict { background: var(--muted-soft); color: var(--ink-soft); }
.pill { display: inline-block; font-size: 11.5px; font-weight: 600; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }
.pill.high { background: var(--critical-soft); color: var(--critical); }
.pill.medium { background: var(--warn-soft); color: var(--warn); }
.pill.low { background: var(--good-soft); color: var(--good); }
.pill.missing { background: var(--muted-soft); color: var(--muted); }
"""


def render_build_conflict_page(data: dict) -> str:
    """Trang riêng /build-conflict — tính lại từ đĩa mỗi lần mở (BUILD_CONFLICT_DIR
    + BUILD_LIST_CSV), không gắn với lần upload nào."""
    return (BUILD_CONFLICT_PAGE
            .replace("__NAV__", nav_html("buildconf"))
            .replace("__PLAN_END__", listing_plan.PLAN_END.strftime("%d/%m/%Y"))
            .replace("__PER_DAY_MIN__", str(listing_plan.PER_DAY_MIN))
            .replace("__PER_DAY_MAX__", str(listing_plan.PER_DAY_MAX))
            .replace("__BUILD_CONFLICT_JSON__", _json(data)))


BUILD_CONFLICT_PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>Build vs Conflict</title>
<style>""" + COMPARE_THEME_CSS + BUILD_CONFLICT_CSS + listing_plan.PLAN_CSS + """</style>
</head><body>
__NAV__
<div class="wrap">
  <h1>Build vs Conflict</h1>
    <p class="reverse-intro">
      So sánh từng <strong>shop đang build</strong> với các <strong>shop conflict</strong> PIC đã khai trong file
      "Shop build.csv". Loại sản phẩm và niche được suy ra từ <strong>tiêu đề listing</strong> (khớp từ khoá, mỗi
      listing 1 nhãn). <strong>% trùng loại SP / niche</strong> = phần cơ cấu % chung của 2 shop (100 = giống hệt,
      bỏ qua nhóm "Other"); <strong>% trùng tag</strong> = Jaccard của top 50 tag mỗi shop.
      <strong>Điểm conflict</strong> = 50% loại SP + 30% niche + 20% tag → Cao ≥ 50, Trung bình ≥ 25.
      Dữ liệu đọc lại từ thư mục build/conflict trên đĩa mỗi lần mở trang.
    </p>
    <div class="empty-state" id="buildconf-unavailable" style="display:none;"></div>
    <div id="buildconf-content">
      <div class="subtabs" role="tablist">
        <button class="subtab" role="tab" data-pane="pairs" aria-selected="true">So sánh cặp build ↔ conflict</button>
        <button class="subtab" role="tab" data-pane="shops" aria-selected="false">Loại sản phẩm theo shop</button>
        <button class="subtab" role="tab" data-pane="suggest" aria-selected="false">Đề xuất sản phẩm</button>
        <button class="subtab" role="tab" data-pane="planner" aria-selected="false">Lập kế hoạch sản phẩm</button>
        <button class="subtab" role="tab" data-pane="listingplan" aria-selected="false">Kế hoạch listing</button>
      </div>""" + listing_plan.PLAN_PANE + """
      <div class="pane" id="bc-pane-suggest" hidden>
        <p class="reverse-intro">
          Loại sản phẩm shop build <strong>chưa bán</strong> (hoặc &lt; 5% listing), lấy từ toàn bộ data build + conflict
          (loại SP cần ≥ 5 listing, ≥ 2 shop). <strong>Điểm</strong> = 40% <strong>nhu cầu</strong> (percentile views/tháng
          &amp; favorites/tháng mỗi listing) + 30% <strong>hợp xưởng</strong> (% listing hiện tại của shop cùng nhóm tay
          nghề: thêu, acrylic, gốm, gỗ…) + 30% <strong>hợp niche</strong> (độ trùng niche khách hàng của shop với niche
          của loại SP). Cờ <span class="pill high">Trùng conflict</span> = shop conflict của nhóm đang bán loại này
          từ 2 listing trở lên; <span class="pill medium">Rủi ro IP</span> = ≥ 50% listing loại này là Disney/licensed.
        </p>
        <div class="controls">
          <select id="sg-shop" aria-label="Chọn shop build"></select>
          <select id="sg-top" aria-label="Số đề xuất">
            <option value="10">Top 10</option>
            <option value="20">Top 20</option>
            <option value="999">Tất cả</option>
          </select>
          <label class="chk"><input type="checkbox" id="sg-hide-conflict" checked> Ẩn SP trùng conflict</label>
          <label class="chk"><input type="checkbox" id="sg-hide-ip"> Ẩn SP rủi ro IP</label>
        </div>
        <p class="chart-caveat" id="sg-summary"></p>
        <div class="table-scroll">
          <table style="min-width:1500px">
            <thead>
              <tr>
                <th>#</th>
                <th>Loại sản phẩm</th>
                <th>Nhóm xưởng</th>
                <th>Điểm</th>
                <th>Nhu cầu</th>
                <th>Hợp xưởng</th>
                <th>Hợp niche</th>
                <th>Views/tháng</th>
                <th>Fav/tháng</th>
                <th>Giá trung vị</th>
                <th>Thị trường</th>
                <th>Cảnh báo</th>
                <th>Shop đang bán nhiều</th>
                <th>Tiêu đề mẫu (views/tháng cao nhất)</th>
              </tr>
            </thead>
            <tbody id="tbody-suggest"></tbody>
          </table>
        </div>
      </div>
      <div class="pane" id="bc-pane-shops" hidden>
        <div class="controls">
          <select id="shops-group" aria-label="Lọc theo nhóm shop build"><option value="">Tất cả nhóm</option></select>
          <select id="shops-role" aria-label="Lọc theo vai trò">
            <option value="">Build + conflict</option>
            <option value="build">Chỉ shop build</option>
            <option value="conflict">Chỉ shop conflict</option>
          </select>
          <select id="shops-metric" aria-label="Thanh % theo">
            <option value="pct">Thanh % theo số listing</option>
            <option value="pct_views">Thanh % theo views</option>
          </select>
        </div>
        <div id="shops-cards"></div>
      </div>
      <div class="pane" id="bc-pane-planner" hidden>
        <p class="reverse-intro">
          Nhập tên sản phẩm → tool tự nhận diện <strong>loại SP</strong> (cùng bộ luật với các tab khác) và liệt kê shop
          build trong dropdown: shop nào có shop conflict đang bán loại này từ 2 listing trở lên (hoặc shop build conflict
          của nó đã được thêm loại này ở đây) sẽ bị khoá kèm lý do. Mỗi ô shop tự tính <strong>niche đối tượng</strong>
          (Wedding, Baby…) và <strong>niche loại SP</strong> (Thêu, Gốm/Sứ…) từ danh sách sản phẩm trong ô — niche
          chiếm ≥ 1/3 số SP được tô đậm là niche chính. Danh sách lưu trên trình duyệt này (localStorage).
        </p>
        <div class="planner-form">
          <input type="search" id="pl-name" placeholder="Tên sản phẩm, vd: Embroidered Wedding Guest Book" aria-label="Tên sản phẩm" autocomplete="off">
          <select id="pl-shop" aria-label="Shop có thể bán" disabled><option>Nhập tên sản phẩm trước</option></select>
          <button type="button" id="pl-add" class="btn" disabled>Thêm vào shop</button>
          <button type="button" id="pl-export" class="btn ghost">Xuất CSV</button>
          <button type="button" id="pl-clear" class="btn ghost">Xoá hết</button>
        </div>
        <p class="chart-caveat" id="pl-detect">&nbsp;</p>
        <div class="shop-grid" id="pl-cards"></div>
      </div>
      <div class="pane" id="bc-pane-pairs">
      <div class="controls">
        <select id="buildconf-shop" aria-label="Lọc theo shop build"><option value="">Tất cả shop build</option></select>
        <select id="buildconf-sort" aria-label="Sắp xếp">
          <option value="group">Nhóm theo shop build</option>
          <option value="score">Điểm conflict cao → thấp</option>
        </select>
      </div>
      <p class="chart-caveat" id="buildconf-source"></p>
      <div class="table-scroll">
        <table id="buildconf-table" style="min-width:1700px">
          <thead>
            <tr>
              <th>Shop build</th>
              <th>Shop conflict</th>
              <th>Mức</th>
              <th>Điểm</th>
              <th>% trùng loại SP</th>
              <th>% trùng niche</th>
              <th>% trùng tag</th>
              <th>Listings (B / C)</th>
              <th>Giá trung vị (B / C)</th>
              <th>Views (B / C)</th>
              <th>Loại SP chính — build</th>
              <th>Loại SP chính — conflict</th>
              <th>Loại SP trùng</th>
              <th>Tag trùng</th>
            </tr>
          </thead>
          <tbody id="tbody-buildconf"></tbody>
        </table>
      </div>
      </div>
    </div>
  </div>
<script id="build-conflict-data" type="application/json">__BUILD_CONFLICT_JSON__</script>""" + listing_plan.PLAN_JS + """
<script>
(function () {
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function etsyUrl(shop) { return 'https://www.etsy.com/shop/' + encodeURIComponent(shop); }
  // ── Build vs Conflict ───────────────────────────────────────────────────
  (function () {
    var bc = JSON.parse(document.getElementById('build-conflict-data').textContent);
    if (!bc.available) {
      document.getElementById('buildconf-content').style.display = 'none';
      var un = document.getElementById('buildconf-unavailable');
      un.style.display = 'block';
      un.textContent = bc.reason || 'Không có dữ liệu build/conflict.';
      return;
    }
    var LEVEL_LABEL = { high: 'Cao', medium: 'Trung bình', low: 'Thấp', missing: 'Chưa có data' };
    var pairs = bc.pairs || [];
    document.getElementById('buildconf-source').textContent =
      'Nguồn: ' + bc.source + ' — ' + pairs.length + ' cặp shop build ↔ conflict';

    var shopSel = document.getElementById('buildconf-shop');
    var sortSel = document.getElementById('buildconf-sort');
    var buildShops = [];
    pairs.forEach(function (p) { if (buildShops.indexOf(p.build_shop) === -1) buildShops.push(p.build_shop); });
    shopSel.innerHTML += buildShops.map(function (s) {
      return '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>';
    }).join('');

    function pct(v) { return v == null ? '–' : v.toFixed(1) + '%'; }
    function pctBar(v) {
      if (v == null) return '–';
      return '<div class="bar-cell">' + v.toFixed(1) + '%<span class="bar-track">' +
        '<span class="bar-fill" style="width:' + Math.min(v, 100) + '%"></span></span></div>';
    }
    function pair(a, b, f) { return f(a) + ' / ' + f(b); }
    function side(p, key, f) { return p[key] ? f(p[key]) : null; }
    function money(v) { return v == null ? '–' : '$' + v.toFixed(2); }
    function int(v) { return v == null ? '–' : Math.round(v).toLocaleString('en-US'); }
    function mix(prof) {
      if (!prof) return '<span class="t">—</span>';
      return Object.keys(prof.ptype).slice(0, 3).map(function (k) {
        return '<span class="t">' + escapeHtml(k) + ' ' + pct(prof.ptype[k]) + '</span>';
      }).join('');
    }
    function shopLink(s) {
      return '<a href="' + etsyUrl(s) + '" target="_blank" rel="noopener">' + escapeHtml(s) + '</a>';
    }
    function rowHtmlBc(p) {
      var b = p.build, c = p.conflict;
      return '<tr>' +
        '<td class="shop">' + shopLink(p.build_shop) +
          (p.pic ? '<div class="cat" style="font-family:inherit;font-weight:400">PIC: ' + escapeHtml(p.pic) + '</div>' : '') + '</td>' +
        '<td class="shop">' + shopLink(p.conflict_shop) + '</td>' +
        '<td><span class="pill ' + p.level + '">' + LEVEL_LABEL[p.level] + '</span></td>' +
        '<td class="num"><strong>' + (p.score == null ? '–' : p.score.toFixed(1)) + '</strong></td>' +
        '<td class="num">' + pctBar(p.ptype_overlap) + '</td>' +
        '<td class="num">' + pctBar(p.niche_overlap) + '</td>' +
        '<td class="num">' + pctBar(p.tag_overlap) + '</td>' +
        '<td class="num">' + int(side(p, 'build', function (x) { return x.n_listings; })) + ' / ' +
          int(side(p, 'conflict', function (x) { return x.n_listings; })) + '</td>' +
        '<td class="num">' + money(side(p, 'build', function (x) { return x.median_price; })) + ' / ' +
          money(side(p, 'conflict', function (x) { return x.median_price; })) + '</td>' +
        '<td class="num">' + int(side(p, 'build', function (x) { return x.views; })) + ' / ' +
          int(side(p, 'conflict', function (x) { return x.views; })) + '</td>' +
        '<td class="sample">' + mix(b) + '</td>' +
        '<td class="sample">' + mix(c) + '</td>' +
        '<td class="sample">' + (p.shared_ptypes.length ? p.shared_ptypes.map(function (k) {
          return '<span class="t">' + escapeHtml(k) + '</span>'; }).join('') : '—') + '</td>' +
        '<td class="sample" style="white-space:normal;min-width:280px">' + (p.shared_tags.length ? escapeHtml(p.shared_tags.join(', ')) : '—') + '</td>' +
        '</tr>';
    }
    function renderBc() {
      var rows = pairs.filter(function (p) { return !shopSel.value || p.build_shop === shopSel.value; });
      if (sortSel.value === 'score') {
        rows = rows.slice().sort(function (a, b) { return (b.score == null ? -1 : b.score) - (a.score == null ? -1 : a.score); });
      } else {
        rows = rows.slice().sort(function (a, b) {
          var ga = buildShops.indexOf(a.build_shop), gb = buildShops.indexOf(b.build_shop);
          return ga !== gb ? ga - gb : (b.score == null ? -1 : b.score) - (a.score == null ? -1 : a.score);
        });
      }
      document.getElementById('tbody-buildconf').innerHTML = rows.map(rowHtmlBc).join('');
    }
    shopSel.addEventListener('change', renderBc);
    sortSel.addEventListener('change', renderBc);
    renderBc();

    // ── Tab con: loại sản phẩm theo shop ──
    var shops = bc.shops || [];
    var groupSel = document.getElementById('shops-group');
    var roleSel = document.getElementById('shops-role');
    var metricSel = document.getElementById('shops-metric');
    groupSel.innerHTML += buildShops.map(function (s) {
      return '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>';
    }).join('');

    function cardHtml(s, metric) {
      var p = s.profile;
      var roleLabel = s.role === 'build' ? 'Build' : 'Conflict';
      var head = '<div class="head"><span class="name">' + shopLink(s.shop) + '</span>' +
        '<span class="pill role-' + s.role + '">' + roleLabel + '</span></div>';
      if (!p) {
        return '<div class="shop-card ' + s.role + '">' + head +
          '<div class="sub">Chưa có dữ liệu listing trong thư mục build/conflict.</div></div>';
      }
      var rows = p.ptype_detail.map(function (d) {
        var w = d[metric] == null ? 0 : d[metric];
        var tip = d.sample_titles.join('\\n');
        return '<div class="pt-row' + (d.ptype === 'Other' ? ' other' : '') + '" title="' + escapeHtml(tip) + '">' +
          '<span class="lbl">' + escapeHtml(d.ptype) + '</span>' +
          '<span class="track"><span class="fill" style="width:' + Math.min(w, 100) + '%"></span></span>' +
          '<span class="val"><strong>' + pct(d[metric]) + '</strong> · ' + d.n + ' lst · ' + money(d.median_price) + '</span>' +
          '</div>';
      }).join('');
      return '<div class="shop-card ' + s.role + '">' + head +
        '<div class="sub">' + int(p.n_listings) + ' listing · ' + int(p.views) + ' views · giá trung vị ' +
        money(p.median_price) + '</div>' + rows + '</div>';
    }
    function renderShops() {
      var metric = metricSel.value;
      var html = '';
      buildShops.forEach(function (g) {
        if (groupSel.value && groupSel.value !== g) return;
        var items = shops.filter(function (s) { return s.group === g && (!roleSel.value || s.role === roleSel.value); });
        if (!items.length) return;
        html += '<div class="group-head">Nhóm ' + escapeHtml(g) + '</div><div class="shop-grid">' +
          items.map(function (s) { return cardHtml(s, metric); }).join('') + '</div>';
      });
      document.getElementById('shops-cards').innerHTML = html || '<div class="empty-state">Không có shop nào.</div>';
    }
    [groupSel, roleSel, metricSel].forEach(function (el) { el.addEventListener('change', renderShops); });
    renderShops();

    // ── Tab con: đề xuất sản phẩm ──
    var suggestions = bc.suggestions || [];
    var sgShop = document.getElementById('sg-shop');
    var sgTop = document.getElementById('sg-top');
    var sgHideConflict = document.getElementById('sg-hide-conflict');
    var sgHideIp = document.getElementById('sg-hide-ip');
    sgShop.innerHTML = buildShops.map(function (s) {
      return '<option value="' + escapeHtml(s) + '">' + escapeHtml(s) + '</option>';
    }).join('');

    function sgRowHtml(r, idx) {
      var warns = [];
      if (r.conflict_with.length) {
        warns.push('<span class="pill high" title="' + escapeHtml(r.conflict_with.map(function (c) {
          return c.shop + ' ' + c.n + ' listing (' + c.pct.toFixed(1) + '%)'; }).join('\\n')) + '">Trùng conflict: ' +
          escapeHtml(r.conflict_with.map(function (c) { return c.shop; }).join(', ')) + '</span>');
      }
      if (r.licensed_pct >= 50) {
        warns.push('<span class="pill medium" title="' + r.licensed_pct.toFixed(1) + '% listing là Disney/licensed">Rủi ro IP</span>');
      }
      if (r.own_pct > 0) {
        warns.push('<span class="pill missing">Đang bán ' + r.own_pct.toFixed(1) + '%</span>');
      }
      return '<tr>' +
        '<td class="num">' + (idx + 1) + '</td>' +
        '<td><strong>' + escapeHtml(r.ptype) + '</strong></td>' +
        '<td class="cat">' + escapeHtml(r.family) + '</td>' +
        '<td class="num"><strong>' + r.score.toFixed(1) + '</strong></td>' +
        '<td class="num">' + pctBar(r.demand) + '</td>' +
        '<td class="num">' + pctBar(r.craft_fit) + '</td>' +
        '<td class="num">' + pctBar(r.niche_fit) + '</td>' +
        '<td class="num">' + int(r.views_pm) + '</td>' +
        '<td class="num">' + r.favs_pm.toFixed(1) + '</td>' +
        '<td class="num">' + money(r.median_price) + '</td>' +
        '<td class="num">' + r.market_listings + ' lst · ' + r.market_shops + ' shop</td>' +
        '<td>' + (warns.join(' ') || '—') + '</td>' +
        '<td class="sample">' + r.top_shops.map(function (s) { return '<span class="t">' + escapeHtml(s) + '</span>'; }).join('') + '</td>' +
        '<td class="sample" style="max-width:380px">' + r.sample_titles.map(function (t) {
          return '<span class="t" title="' + escapeHtml(t) + '" style="overflow:hidden;text-overflow:ellipsis">' + escapeHtml(t) + '</span>'; }).join('') + '</td>' +
        '</tr>';
    }
    function renderSuggest() {
      var all = suggestions.filter(function (r) { return r.build_shop === sgShop.value; });
      var rows = all.filter(function (r) {
        return !(sgHideConflict.checked && r.conflict_with.length) && !(sgHideIp.checked && r.licensed_pct >= 50);
      });
      var shown = rows.slice(0, parseInt(sgTop.value, 10));
      document.getElementById('sg-summary').textContent = 'Hiển thị ' + shown.length + ' / ' + rows.length +
        ' loại SP sau lọc (' + all.length + ' loại SP shop chưa bán đủ mẫu để chấm).';
      document.getElementById('tbody-suggest').innerHTML = shown.map(sgRowHtml).join('') ||
        '<tr><td colspan="14" class="empty-state">Không có đề xuất nào.</td></tr>';
    }
    [sgShop, sgTop, sgHideConflict, sgHideIp].forEach(function (el) { el.addEventListener('change', renderSuggest); });
    renderSuggest();

    // ── Tab con: lập kế hoạch sản phẩm ──
    var pl = bc.planner;
    var PT_RES = pl.ptype_rules.map(function (r) { return [r[0], new RegExp(r[1])]; });
    var NI_RES = pl.niche_rules.map(function (r) { return [r[0], new RegExp(r[1])]; });
    var CR_RES = pl.craft_rules.map(function (r) { return [r[0], new RegExp(r[1], 'i')]; });
    var conflictsOf = {};
    pl.build_list.forEach(function (b) { conflictsOf[b.build_shop] = b.conflicts; });
    var scoreOf = {};
    suggestions.forEach(function (r) { scoreOf[r.build_shop + '|' + r.ptype] = r.score; });
    var STORE_KEY = 'buildconf-planner-v1';
    var plan = {};
    try { plan = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch (e) { plan = {}; }
    buildShops.forEach(function (s) { if (!Array.isArray(plan[s])) plan[s] = []; });
    function savePlan() { try { localStorage.setItem(STORE_KEY, JSON.stringify(plan)); } catch (e) {} }

    function lc(t) { return String(t || '').toLowerCase(); }
    function classifyPtype(name) {
      var t = lc(name);
      for (var i = 0; i < PT_RES.length; i++) if (PT_RES[i][1].test(t)) return PT_RES[i][0];
      return 'Other';
    }
    function craftOf(name, ptype) {
      // Ưu tiên tên SP người dùng gõ (vd "embroidered ..."), sau đó tới tên loại SP.
      for (var i = 0; i < CR_RES.length; i++) if (CR_RES[i][1].test(name)) return CR_RES[i][0];
      for (var j = 0; j < CR_RES.length; j++) if (CR_RES[j][1].test(ptype)) return CR_RES[j][0];
      return null;
    }
    function audienceOf(name, ptype) {
      // Mọi niche khớp trong tên SP (1 SP có thể vừa Baby vừa Christmas); không khớp
      // gì thì lấy niche chiếm >= 40% listing của loại SP đó trên thị trường.
      var t = lc(name), out = [];
      NI_RES.forEach(function (r) { if (r[1].test(t)) out.push(r[0]); });
      if (!out.length && pl.market_niche[ptype]) {
        Object.keys(pl.market_niche[ptype]).forEach(function (k) {
          if (k !== 'Other' && pl.market_niche[ptype][k] >= 40) out.push(k);
        });
      }
      return out;
    }
    // Lý do shop KHÔNG bán được loại SP này (null = bán được).
    function blockReason(shop, ptype, name) {
      if (plan[shop].some(function (p) { return lc(p.name) === lc(name); })) return 'đã có trong danh sách';
      if (ptype === 'Other') return null;
      var hits = [];
      (conflictsOf[shop] || []).forEach(function (c) {
        var n = (pl.conflict_ptype_n[c] || {})[ptype] || 0;
        var pct = (pl.conflict_ptype[c] || {})[ptype] || 0;
        if (n >= pl.conflict_min_n) hits.push(c + ' ' + n + ' listing (' + pct.toFixed(1) + '%)');
        else if (plan[c] && plan[c].some(function (p) { return p.ptype === ptype; })) hits.push(c + ' (đã lên kế hoạch)');
      });
      return hits.length ? 'trùng conflict: ' + hits.join(', ') : null;
    }

    var plName = document.getElementById('pl-name');
    var plShop = document.getElementById('pl-shop');
    var plAdd = document.getElementById('pl-add');
    var plDetect = document.getElementById('pl-detect');

    function refreshDropdown() {
      var name = plName.value.trim();
      if (!name) {
        plShop.innerHTML = '<option>Nhập tên sản phẩm trước</option>';
        plShop.disabled = true; plAdd.disabled = true; plDetect.innerHTML = '&nbsp;';
        return;
      }
      var ptype = classifyPtype(name);
      var craft = craftOf(name, ptype);
      var aud = audienceOf(name, ptype);
      plDetect.innerHTML = 'Loại SP nhận diện: <strong>' + escapeHtml(ptype) + '</strong>' +
        (craft ? ' · nhóm SP: <strong>' + escapeHtml(craft) + '</strong>' : '') +
        (aud.length ? ' · đối tượng: <strong>' + escapeHtml(aud.join(', ')) + '</strong>' : '') +
        (ptype === 'Other' ? ' — <span style="color:var(--warn)">không khớp loại SP nào, chưa kiểm tra được conflict</span>' : '');
      var opts = buildShops.map(function (s) {
        return { shop: s, reason: blockReason(s, ptype, name), score: scoreOf[s + '|' + ptype] };
      });
      opts.sort(function (a, b) {
        if (!a.reason !== !b.reason) return a.reason ? 1 : -1;
        return (b.score == null ? -1 : b.score) - (a.score == null ? -1 : a.score);
      });
      var ok = opts.filter(function (o) { return !o.reason; });
      plShop.innerHTML = opts.map(function (o) {
        var label = o.shop + (o.reason ? ' — ✕ ' + o.reason : (o.score != null ? ' — điểm đề xuất ' + o.score.toFixed(1) : ' — bán được'));
        return '<option value="' + escapeHtml(o.shop) + '"' + (o.reason ? ' disabled' : '') + '>' + escapeHtml(label) + '</option>';
      }).join('');
      if (ok.length) plShop.value = ok[0].shop;
      plShop.disabled = false;
      plAdd.disabled = !ok.length;
    }

    function nicheChips(items, key) {
      var counts = {};
      items.forEach(function (p) {
        var vals = key === 'aud' ? p.aud : (p.craft ? [p.craft] : []);
        vals.forEach(function (v) { counts[v] = (counts[v] || 0) + 1; });
      });
      var keys = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
      if (!keys.length) return '<span class="chipn">—</span>';
      return keys.map(function (k) {
        var main = counts[k] / items.length >= 1 / 3;
        return '<span class="chipn' + (main ? ' main' : '') + '">' + escapeHtml(k) + ' ' + counts[k] + '</span>';
      }).join('');
    }
    function renderPlanner() {
      document.getElementById('pl-cards').innerHTML = buildShops.map(function (s) {
        var items = plan[s];
        var list = items.length
          ? '<ul class="pl-list">' + items.map(function (p, i) {
              return '<li><span>' + escapeHtml(p.name) + ' <span class="pt">· ' + escapeHtml(p.ptype) + '</span></span>' +
                '<button type="button" data-shop="' + escapeHtml(s) + '" data-idx="' + i + '" title="Xoá">×</button></li>';
            }).join('') + '</ul>'
          : '<div class="pl-empty">Chưa có sản phẩm nào.</div>';
        return '<div class="shop-card build">' +
          '<div class="head"><span class="name">' + shopLink(s) + '</span><span class="pill role-build">' + items.length + ' SP</span></div>' +
          '<div class="sub">Conflict: ' + escapeHtml((conflictsOf[s] || []).join(', ') || '—') + '</div>' +
          '<div class="niche-line"><span class="lbl">Niche đối tượng</span>' + nicheChips(items, 'aud') + '</div>' +
          '<div class="niche-line"><span class="lbl">Niche loại SP</span>' + nicheChips(items, 'craft') + '</div>' +
          list + '</div>';
      }).join('');
      document.querySelectorAll('#pl-cards .pl-list button').forEach(function (btn) {
        btn.addEventListener('click', function () {
          plan[btn.getAttribute('data-shop')].splice(parseInt(btn.getAttribute('data-idx'), 10), 1);
          savePlan(); renderPlanner(); refreshDropdown();
        });
      });
    }

    plName.addEventListener('input', refreshDropdown);
    plName.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !plAdd.disabled) plAdd.click(); });
    plAdd.addEventListener('click', function () {
      var name = plName.value.trim(), shop = plShop.value;
      if (!name || !plan[shop]) return;
      var ptype = classifyPtype(name);
      if (blockReason(shop, ptype, name)) return;
      plan[shop].push({ name: name, ptype: ptype, craft: craftOf(name, ptype), aud: audienceOf(name, ptype) });
      savePlan(); renderPlanner();
      plName.value = ''; refreshDropdown(); plName.focus();
    });
    document.getElementById('pl-clear').addEventListener('click', function () {
      if (!confirm('Xoá toàn bộ sản phẩm đã thêm ở tất cả shop?')) return;
      buildShops.forEach(function (s) { plan[s] = []; });
      savePlan(); renderPlanner(); refreshDropdown();
    });
    document.getElementById('pl-export').addEventListener('click', function () {
      function esc(v) { v = String(v == null ? '' : v); return /[",\\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
      var lines = ['build_shop,product_name,product_type,product_niche,audience_niche'];
      buildShops.forEach(function (s) {
        plan[s].forEach(function (p) {
          lines.push([s, p.name, p.ptype, p.craft || '', (p.aud || []).join('; ')].map(esc).join(','));
        });
      });
      var blob = new Blob(['\\ufeff' + lines.join('\\n')], { type: 'text/csv;charset=utf-8' });
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = 'build_shop_product_plan.csv';
      document.body.appendChild(a); a.click(); a.remove();
    });
    renderPlanner();

    document.querySelectorAll('.subtab').forEach(function (btn) {
      btn.addEventListener('click', function () {
        document.querySelectorAll('.subtab').forEach(function (b) {
          b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        });
        document.querySelectorAll('.pane').forEach(function (pane) {
          pane.hidden = pane.id !== 'bc-pane-' + btn.getAttribute('data-pane');
        });
      });
    });
  })();

})();
</script>
</body></html>"""


# ── Tab "Shop build theo thời gian" ─────────────────────────────────────────
# So sánh dữ liệu listing của các shop build tại 2 thời điểm (2 file CSV
# listing-level crawl ở 2 lần khác nhau). Ứng viên = mọi CSV listing-level trong
# BUILD_CONFLICT_DIR (kể cả thư mục con, vd _backup_*) + thư mục hiện tại
# (output của everbee_api_shop_scraper.py --shop-build) có chứa ≥ 1/2 số shop
# build — loại được file conflict lẫn vào. Thời điểm = mtime của file.
HISTORY_NUM_COLS = ["views", "favorites", "est_reviews", "price", "mo_sales"]


def _build_shop_names() -> set:
    try:
        return {b["build_shop"].lower() for b in load_build_list(BUILD_LIST_CSV)}
    except Exception:
        return set()


def list_build_snapshots(build_shops: set) -> list:
    paths = set(glob.glob(os.path.join(BUILD_CONFLICT_DIR, "**", "*.csv"), recursive=True))
    paths |= set(glob.glob("*.csv"))
    out = []
    for p in sorted(paths):
        try:
            head = pd.read_csv(p, dtype=str, encoding="utf-8-sig", nrows=5)
            if detect_kind(head) != "listings" or "shop_name" not in head.columns:
                continue
            shops = set(pd.read_csv(p, dtype=str, encoding="utf-8-sig", usecols=["shop_name"])["shop_name"]
                        .dropna().str.lower())
        except Exception:
            continue
        covered = shops & build_shops if build_shops else shops
        if build_shops and len(covered) * 2 < len(build_shops):
            continue
        mtime = os.path.getmtime(p)
        out.append({"path": os.path.normpath(p), "mtime": mtime,
                    "label": f"{datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M} — {os.path.normpath(p)} ({len(covered)} shop build)"})
    out.sort(key=lambda s: -s["mtime"])  # mới nhất trước
    return out


def _load_build_snapshot(path: str, build_shops: set) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    if build_shops:
        df = df[df["shop_name"].str.lower().isin(build_shops)]
    df = df.drop_duplicates(subset=["shop_name", "listing_id"]).copy()
    df["_key"] = df["shop_name"].str.lower() + "|" + df["listing_id"].str.strip()
    for c in HISTORY_NUM_COLS:
        df[c] = df[c].map(_num) if c in df.columns else None
    return df.set_index("_key")


def build_history_rows(path_a: Optional[str], path_b: Optional[str]) -> dict:
    """A = thời điểm cũ, B = thời điểm mới. path None -> lấy 2 file mới nhất."""
    build_shops = _build_shop_names()
    snaps = list_build_snapshots(build_shops)
    base = {"snapshots": [{"path": s["path"], "label": s["label"]} for s in snaps]}
    if len(snaps) < 2:
        return {**base, "available": False,
                "reason": f"Cần ít nhất 2 file listing của shop build (trong {BUILD_CONFLICT_DIR}/ hoặc thư mục hiện tại) để so sánh."}
    by_path = {s["path"]: s for s in snaps}
    # Chỉ nhận path nằm trong danh sách ứng viên — không đọc file tuỳ ý từ query string.
    a = by_path.get(os.path.normpath(path_a)) if path_a else None
    b = by_path.get(os.path.normpath(path_b)) if path_b else None
    b = b or snaps[0]
    a = a or next(s for s in snaps if s["path"] != b["path"])
    if a["path"] == b["path"]:
        return {**base, "available": False, "reason": "Hãy chọn 2 file khác nhau.", "a": a["path"], "b": b["path"]}

    df_a = _load_build_snapshot(a["path"], build_shops)
    df_b = _load_build_snapshot(b["path"], build_shops)

    def val(row, c):
        v = row.get(c)
        return None if v is None or pd.isna(v) else float(v)

    def brief(key, rb):
        return {"shop": rb["shop_name"], "listing_id": rb["listing_id"], "title": rb.get("title", ""),
                "url": rb.get("etsy_url", ""), "image": rb.get("image_url", ""),
                "listing_age": rb.get("listing_age", ""), "category": rb.get("category", "")}

    def delta(x, y):
        return None if x is None or y is None else round(y - x, 2)

    common, new = [], []
    for key, rb in df_b.iterrows():
        row = brief(key, rb)
        if key in df_a.index:
            ra = df_a.loc[key]
            for c in HISTORY_NUM_COLS:
                row[c + "_a"], row[c + "_b"] = val(ra, c), val(rb, c)
                row[c + "_delta"] = delta(row[c + "_a"], row[c + "_b"])
            common.append(row)
        else:
            for c in HISTORY_NUM_COLS:
                row[c + "_b"] = val(rb, c)
            # Listing mới: chênh lệch view = toàn bộ views hiện tại (trước đó chưa tồn tại).
            row["views_delta"] = row["views_b"]
            new.append(row)
    sort_key = lambda r: -(r["views_delta"] if r["views_delta"] is not None else float("-inf"))
    common.sort(key=sort_key)
    new.sort(key=sort_key)
    return {
        **base, "available": True, "a": a["path"], "b": b["path"],
        "a_label": a["label"], "b_label": b["label"],
        "days": round((b["mtime"] - a["mtime"]) / 86400, 1),
        "removed": int(len(set(df_a.index) - set(df_b.index))),
        "shops": sorted(set(df_b["shop_name"]), key=str.lower),
        "common": common, "new": new,
    }


def render_build_history_page(data: dict) -> str:
    def options(selected):
        return "".join(
            f'<option value="{html.escape(s["path"])}"{" selected" if s["path"] == selected else ""}>'
            f'{html.escape(s["label"])}</option>' for s in data["snapshots"])
    return (BUILD_HISTORY_PAGE
            .replace("__NAV__", nav_html("buildhist"))
            .replace("__OPTIONS_A__", options(data.get("a")))
            .replace("__OPTIONS_B__", options(data.get("b")))
            .replace("__BUILD_HISTORY_JSON__", _json(data)))


BUILD_HISTORY_PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<title>Shop build theo thời gian</title>
<style>""" + COMPARE_THEME_CSS + BUILD_CONFLICT_CSS + """
.controls input[type="search"] { flex: 1 1 260px; background: var(--paper-raised); color: var(--ink);
  border: 1px solid var(--line-strong); border-radius: 3px; padding: 8px 12px; font-size: 14px; font-family: inherit; }
.snap-form { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin: 0 0 16px; }
.snap-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; font-weight: 600; color: var(--ink-soft); }
.snap-form select { max-width: 560px; background: var(--paper-raised); color: var(--ink); border: 1px solid var(--line-strong);
  border-radius: 3px; padding: 8px 10px; font-size: 13px; font-family: inherit; }
td.title { white-space: normal; min-width: 340px; max-width: 520px; }
td.title a { color: var(--ink); text-decoration: none; }
td.title a:hover { color: var(--accent); }
td img.thumb { width: 56px; height: 56px; object-fit: cover; border-radius: 4px; display: block; }
.up { color: var(--good); font-weight: 600; }
.down { color: var(--critical); font-weight: 600; }
</style>
</head><body>
__NAV__
<div class="wrap">
  <h1>Shop build theo thời gian</h1>
  <p class="reverse-intro">
    So sánh listing của các <strong>shop build</strong> giữa 2 lần crawl. Thời điểm của file = lúc file được ghi.
    Mọi bảng sort theo <strong>chênh lệch views</strong> (B − A) giảm dần; với listing mới, chênh lệch = toàn bộ views hiện tại.
  </p>
  <form class="snap-form" method="get" action="/build-history">
    <label>Thời điểm A (cũ)<select name="a">__OPTIONS_A__</select></label>
    <label>Thời điểm B (mới)<select name="b">__OPTIONS_B__</select></label>
    <button class="btn" type="submit">So sánh</button>
  </form>
  <div class="empty-state" id="bh-unavailable" style="display:none;"></div>
  <div id="bh-content">
    <p class="chart-caveat" id="bh-summary"></p>
    <div class="subtabs" role="tablist">
      <button class="subtab" role="tab" data-pane="common" aria-selected="true">Listing có ở cả 2 thời điểm</button>
      <button class="subtab" role="tab" data-pane="new" aria-selected="false">Listing mới</button>
    </div>
    <div class="controls">
      <select id="bh-shop" aria-label="Lọc theo shop"><option value="">Tất cả shop build</option></select>
      <input type="search" id="bh-q" placeholder="Lọc theo tiêu đề…" aria-label="Lọc theo tiêu đề">
    </div>
    <div class="pane" id="bh-pane-common">
      <div class="table-scroll">
        <table style="min-width:1400px">
          <thead><tr>
            <th>#</th><th></th><th>Shop</th><th>Listing</th>
            <th>Views A → B</th><th>Δ Views</th><th>Δ Favorites</th><th>Δ Est. reviews</th><th>Δ Mo. sales</th><th>Giá A → B</th><th>Tuổi</th>
          </tr></thead>
          <tbody id="bh-tbody-common"></tbody>
        </table>
      </div>
    </div>
    <div class="pane" id="bh-pane-new" hidden>
      <div class="table-scroll">
        <table style="min-width:1200px">
          <thead><tr>
            <th>#</th><th></th><th>Shop</th><th>Listing</th>
            <th>Views (Δ)</th><th>Favorites</th><th>Est. reviews</th><th>Mo. sales</th><th>Giá</th><th>Tuổi</th>
          </tr></thead>
          <tbody id="bh-tbody-new"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script id="build-history-data" type="application/json">__BUILD_HISTORY_JSON__</script>
<script>
(function () {
  var d = JSON.parse(document.getElementById('build-history-data').textContent);
  if (!d.available) {
    document.getElementById('bh-content').style.display = 'none';
    var un = document.getElementById('bh-unavailable');
    un.style.display = 'block';
    un.textContent = d.reason || 'Không có dữ liệu.';
    return;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function int(v) { return v == null ? '–' : Math.round(v).toLocaleString('en-US'); }
  function money(v) { return v == null ? '–' : '$' + v.toFixed(2); }
  function signed(v) {
    if (v == null) return '–';
    var s = (v > 0 ? '+' : '') + Math.round(v).toLocaleString('en-US');
    return v > 0 ? '<span class="up">' + s + '</span>' : v < 0 ? '<span class="down">' + s + '</span>' : s;
  }
  function head(r, i) {
    return '<td class="num">' + (i + 1) + '</td>' +
      '<td>' + (r.image ? '<img class="thumb" loading="lazy" src="' + esc(r.image) + '" alt="">' : '') + '</td>' +
      '<td class="shop"><a href="https://www.etsy.com/shop/' + encodeURIComponent(r.shop) + '" target="_blank" rel="noopener">' + esc(r.shop) + '</a></td>' +
      '<td class="title">' + (r.url ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.title) + '</a>' : esc(r.title)) +
        (r.category ? '<div class="cat">' + esc(r.category) + '</div>' : '') + '</td>';
  }
  function rowCommon(r, i) {
    return '<tr>' + head(r, i) +
      '<td class="num">' + int(r.views_a) + ' → ' + int(r.views_b) + '</td>' +
      '<td class="num"><strong>' + signed(r.views_delta) + '</strong></td>' +
      '<td class="num">' + signed(r.favorites_delta) + '</td>' +
      '<td class="num">' + signed(r.est_reviews_delta) + '</td>' +
      '<td class="num">' + signed(r.mo_sales_delta) + '</td>' +
      '<td class="num">' + money(r.price_a) + (r.price_a !== r.price_b ? ' → ' + money(r.price_b) : '') + '</td>' +
      '<td class="num">' + esc(r.listing_age) + '</td></tr>';
  }
  function rowNew(r, i) {
    return '<tr>' + head(r, i) +
      '<td class="num"><strong>' + signed(r.views_delta) + '</strong></td>' +
      '<td class="num">' + int(r.favorites_b) + '</td>' +
      '<td class="num">' + int(r.est_reviews_b) + '</td>' +
      '<td class="num">' + int(r.mo_sales_b) + '</td>' +
      '<td class="num">' + money(r.price_b) + '</td>' +
      '<td class="num">' + esc(r.listing_age) + '</td></tr>';
  }
  document.getElementById('bh-summary').textContent =
    'A: ' + d.a_label + '  |  B: ' + d.b_label + '  |  cách nhau ' + d.days + ' ngày — ' +
    d.common.length + ' listing có ở cả 2, ' + d.new.length + ' listing mới, ' + d.removed + ' listing không còn ở B.';
  var shopSel = document.getElementById('bh-shop'), q = document.getElementById('bh-q');
  shopSel.innerHTML += d.shops.map(function (s) { return '<option value="' + esc(s) + '">' + esc(s) + '</option>'; }).join('');
  function filt(rows) {
    var s = shopSel.value, t = q.value.trim().toLowerCase();
    return rows.filter(function (r) {
      return (!s || r.shop === s) && (!t || String(r.title).toLowerCase().indexOf(t) !== -1);
    });
  }
  var tabBtns = document.querySelectorAll('.subtab');
  function render() {
    var c = filt(d.common), n = filt(d.new);
    document.getElementById('bh-tbody-common').innerHTML = c.map(rowCommon).join('');
    document.getElementById('bh-tbody-new').innerHTML = n.map(rowNew).join('');
    tabBtns[0].textContent = 'Listing có ở cả 2 thời điểm (' + c.length + ')';
    tabBtns[1].textContent = 'Listing mới (' + n.length + ')';
  }
  shopSel.addEventListener('change', render);
  q.addEventListener('input', render);
  tabBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      tabBtns.forEach(function (b) { b.setAttribute('aria-selected', b === btn ? 'true' : 'false'); });
      document.querySelectorAll('.pane').forEach(function (p) {
        p.hidden = p.id !== 'bh-pane-' + btn.getAttribute('data-pane');
      });
    });
  });
  render();
})();
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, status: int, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send_html(200, UPLOAD_FORM_HTML.format(error_html=""))
        elif self.path == "/compare":
            self._handle_compare_form()
        elif self.path == "/build-conflict":
            self._send_html(200, render_build_conflict_page(
                build_conflict_rows(BUILD_CONFLICT_DIR, BUILD_LIST_CSV)))
        elif self.path == "/build-conflict/plan.xlsx":
            self._handle_plan_xlsx()
        elif urllib.parse.urlsplit(self.path).path == "/build-history":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            self._send_html(200, render_build_history_page(build_history_rows(
                qs.get("a", [None])[0], qs.get("b", [None])[0])))
        else:
            self._send_html(404, "<h1>404 Not Found</h1>")

    def _send_json(self, status: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_plan_save(self):
        """Tab "Kế hoạch listing" gửi lại toàn bộ danh sách dòng SP mỗi lần sửa."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            rows = body["rows"]
            if not isinstance(rows, list):
                raise ValueError("rows phải là list")
            saved_at = listing_plan.save_rows(BUILD_CONFLICT_DIR, rows, len(listing_plan.plan_weeks()))
        except Exception as e:
            self._send_json(400, {"error": str(e)})
            return
        self._send_json(200, {"saved_at": saved_at})

    def _handle_plan_xlsx(self):
        data = build_conflict_rows(BUILD_CONFLICT_DIR, BUILD_LIST_CSV)
        if not data.get("available"):
            self._send_html(404, f"<h1>Không có dữ liệu</h1><p>{html.escape(data.get('reason', ''))}</p>")
            return
        conflicts = {b["build_shop"]: b["conflicts"] for b in data["planner"]["build_list"]}
        body = listing_plan.plan_xlsx(data["listing_plan"], conflicts)
        name = f"shop_build_plan_{listing_plan.PLAN_END:%Y-%m-%d}.xlsx"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_compare_form(self):
        # _list_available_runs() trả về mới nhất trước (reverse=True) — quét lại
        # analysis_output/ mỗi lần mở trang này (không cache), nên tên phiên bản
        # luôn khớp đúng trạng thái đĩa hiện tại (đổi tên/xoá/thêm folder ở đâu đó
        # đều được nhận ra ngay, không cần khởi động lại server).
        runs = _list_available_runs()
        if len(runs) < 2:
            self._send_html(200, COMPARE_FORM_HTML.format(
                error_html='<div class="err">Cần ít nhất 2 lần phân tích đã lưu để so sánh.</div>',
                options_a="", options_b=""))
            return

        def _render_options(selected_dir: str) -> str:
            return "".join(
                f'<option value="{html.escape(r["dir"])}"'
                f'{" selected" if r["dir"] == selected_dir else ""}>{html.escape(r["dir"])} '
                f'({html.escape(r["shops_filename"])} / {html.escape(r["listings_filename"])})'
                f'{" — chưa có raw CSV" if not r["has_raw"] else ""}</option>'
                for r in runs
            )

        # Mặc định B = phiên bản mới nhất, A = phiên bản mới thứ nhì — mở trang
        # là có ngay cặp so sánh "mới nhất vs liền trước" mà không cần tự chọn tay.
        options_b = _render_options(runs[0]["dir"])
        options_a = _render_options(runs[1]["dir"])
        self._send_html(200, COMPARE_FORM_HTML.format(error_html="", options_a=options_a, options_b=options_b))

    def do_POST(self):
        if self.path == "/analyze":
            self._handle_analyze()
        elif self.path == "/compare":
            self._handle_compare_run()
        elif self.path == "/build-conflict/plan":
            self._handle_plan_save()
        elif self.path == "/build-conflict/plan/reset":
            listing_plan.reset_rows(BUILD_CONFLICT_DIR)
            self._send_json(200, {"ok": True})
        else:
            self._send_html(404, "<h1>404 Not Found</h1>")

    def _handle_compare_run(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        form = urllib.parse.parse_qs(body)
        run_a = (form.get("run_a") or [""])[0]
        run_b = (form.get("run_b") or [""])[0]

        dir_a = os.path.join("analysis_output", run_a)
        dir_b = os.path.join("analysis_output", run_b)
        if not run_a or not run_b or not os.path.isdir(dir_a) or not os.path.isdir(dir_b):
            self._send_html(400, "<h1>400 Bad Request</h1><p>Phiên bản không hợp lệ.</p>")
            return
        if run_a == run_b:
            self._send_html(400, "<h1>400 Bad Request</h1><p>Chọn 2 phiên bản khác nhau.</p>")
            return

        report_a = os.path.join(dir_a, "shop_gift_fit_report.html")
        report_b = os.path.join(dir_b, "shop_gift_fit_report.html")
        with open(report_a, "r", encoding="utf-8") as f:
            html_a = f.read()
        with open(report_b, "r", encoding="utf-8") as f:
            html_b = f.read()

        ledger_a = _ledger_by_shop(_extract_embedded_json(html_a, "ledger-data") or [])
        ledger_b = _ledger_by_shop(_extract_embedded_json(html_b, "ledger-data") or [])
        shop_diff = diff_shop_ledger(ledger_a, ledger_b)

        top_a = _extract_embedded_json(html_a, "top10-data") or []
        top_b = _extract_embedded_json(html_b, "top10-data") or []
        top_diff = diff_top10(top_a, top_b)

        raw_a = os.path.join(dir_a, RAW_LISTINGS_CSV)
        raw_b = os.path.join(dir_b, RAW_LISTINGS_CSV)
        listing_diff = diff_listings(raw_a, raw_b) if os.path.exists(raw_a) and os.path.exists(raw_b) else None

        html_out = render_compare_report({
            "run_a": run_a, "run_b": run_b,
            "shop_diff": shop_diff, "top_diff": top_diff, "listing_diff": listing_diff,
        })
        self._send_html(200, html_out)

    def _handle_analyze(self):
        content_type = self.headers.get("Content-Type", "")
        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
        if not m:
            self._send_html(400, UPLOAD_FORM_HTML.format(
                error_html='<div class="err">Request không hợp lệ (thiếu boundary multipart).</div>'))
            return
        boundary = m.group(1) or m.group(2)

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        fields = parse_multipart(body, boundary)
        files = [f for f in fields.values() if f.get("filename")]

        if len(files) < 1:
            self._send_html(400, UPLOAD_FORM_HTML.format(
                error_html='<div class="err">Cần upload ít nhất 1 file CSV (shop-level).</div>'))
            return

        try:
            dfs = [pd.read_csv(io.BytesIO(f["content"]), dtype=str, encoding="utf-8-sig") for f in files[:2]]
        except Exception as e:
            self._send_html(400, UPLOAD_FORM_HTML.format(
                error_html=f'<div class="err">Không đọc được CSV: {html.escape(str(e))}</div>'))
            return

        shop_only = len(files) == 1
        if shop_only:
            # Chế độ 1 file: chỉ chấp nhận nếu đó là file shop-level — file
            # listing-level đứng riêng lẻ không đủ để chấm điểm (cần category/
            # description của shop, không có trong file listing).
            if detect_kind(dfs[0]) != "shops":
                self._send_html(400, UPLOAD_FORM_HTML.format(
                    error_html=(
                        '<div class="err">Chỉ upload 1 file thì file đó phải là shop-level (cần cột '
                        'shop_name/shop_id). Nếu đây là file listing-level, cần upload kèm 1 file '
                        'shop-level ở File CSV #1 hoặc #2.</div>'
                    )))
                return
            shops_df = dfs[0].fillna("")
            listings_df = pd.DataFrame()
            shops_name, listings_name = files[0]["filename"], None
            shops_content, listings_content = files[0]["content"], b""
        else:
            df1, df2 = dfs[0], dfs[1]
            kind1, kind2 = detect_kind(df1), detect_kind(df2)
            if kind1 == "shops" and kind2 == "listings":
                shops_df, listings_df = df1, df2
                shops_name, listings_name = files[0]["filename"], files[1]["filename"]
                shops_content, listings_content = files[0]["content"], files[1]["content"]
            elif kind1 == "listings" and kind2 == "shops":
                shops_df, listings_df = df2, df1
                shops_name, listings_name = files[1]["filename"], files[0]["filename"]
                shops_content, listings_content = files[1]["content"], files[0]["content"]
            else:
                self._send_html(400, UPLOAD_FORM_HTML.format(
                    error_html=(
                        '<div class="err">Không nhận diện được file nào là shop-level, file nào là '
                        'listing-level — file shop cần cột shop_name/shop_id, file listing cần thêm '
                        'listing_id/title.</div>'
                    )))
                return

            # fillna Ở ĐÂY (không chỉ trong analyze()) vì business_rows()/top_rows()
            # cũng nhận shops_df — analyze() tự fillna nhưng chỉ rebind biến CỤC BỘ
            # bên trong nó, không sửa DataFrame gốc này. Ô trống trong CSV (dtype=str)
            # vẫn là NaN thật (không phải ""), nếu lọt vào JSON qua .get(key, default)
            # (default chỉ áp dụng khi thiếu key, không áp dụng khi value là NaN) sẽ
            # thành token NaN trần — hợp lệ với json.loads của Python nhưng KHÔNG hợp
            # lệ với JSON.parse() của browser, làm cả script trên trang bị đứng giữa
            # chừng (mất luôn phần gắn sự kiện chuyển tab ở cuối file).
            shops_df = shops_df.fillna("")
            listings_df = listings_df.fillna("")

        if shop_only:
            main_rows = analyze_shop_only(shops_df)
            filtered_rows, reverse_rows = [], []
        else:
            main_rows, filtered_rows, reverse_rows = analyze(shops_df, listings_df)
        good_names = {r["shop"] for r in main_rows if r["verdict"] == "good"}
        biz, biz_columns = business_rows(shops_df, good_names)
        top, top_available = top_rows(shops_df, good_names)
        products = [] if shop_only else product_rows(listings_df, good_names)
        keyword_rows = [] if shop_only else keyword_listing_rows(listings_df)
        all_shop_names = set(shops_df["shop_name"].astype(str)) if "shop_name" in shops_df.columns else set()
        keyword_biz_rows, _keyword_biz_columns = business_rows(shops_df, all_shop_names)

        html_out = render_report({
            "ledger_data": main_rows + filtered_rows,
            "reverse_rows": reverse_rows,
            "biz_rows": biz,
            "biz_columns": biz_columns,
            "top_rows": top,
            "top_available": top_available,
            "product_rows": products,
            "keyword_rows": keyword_rows,
            "keyword_biz_rows": keyword_biz_rows,
            "shops_filename": shops_name or "shops.csv",
            "listings_filename": listings_name or "listings.csv",
            "shop_only": shop_only,
        })

        # Chỉ ghi HTML mới ra đĩa nếu cặp file (theo NỘI DUNG, không phải tên)
        # chưa từng phân tích — nhiều lần upload dùng chung tên file nhưng nội
        # dung khác nhau (vd export mới sau mỗi lần crawl), nên so theo hash.
        import_key = (
            f"{hashlib.sha256(shops_content).hexdigest()}:"
            f"{hashlib.sha256(listings_content).hexdigest()}"
        )
        manifest = _load_import_manifest()
        existing = manifest.get(import_key)
        if existing:
            print(f"[skip-save] Cặp file trùng nội dung với lần phân tích {existing['dir']} "
                  f"({existing['timestamp']}) — không lưu thêm HTML mới.")
        else:
            out_dir = os.path.join("analysis_output", datetime.now().strftime("%Y%m%d_%H%M%S"))
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, "shop_gift_fit_report.html")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html_out)
            # Giữ nguyên bytes CSV gốc (không phải df đã fillna) để /compare có thể
            # so cấp listing (listing mới/mất, delta views/giá...) giữa 2 lần phân
            # tích sau này — chỉ áp dụng cho các lần lưu TỪ giờ trở đi. Chế độ 1 file
            # (shop_only) không có listings_content thật nên KHÔNG ghi RAW_LISTINGS_CSV
            # — _list_available_runs() dựa vào file này thiếu để biết run không so
            # được cấp listing, đúng như thực tế (chưa từng crawl listing).
            with open(os.path.join(out_dir, RAW_SHOPS_CSV), "wb") as f:
                f.write(shops_content)
            if not shop_only:
                with open(os.path.join(out_dir, RAW_LISTINGS_CSV), "wb") as f:
                    f.write(listings_content)
            manifest[import_key] = {
                "dir": out_dir,
                "shops_filename": shops_name or "shops.csv",
                "listings_filename": listings_name or "listings.csv",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            _save_import_manifest(manifest)
            print(f"[saved] {out_path}  ({len(main_rows)} shop {'chấm sơ bộ (chưa có listing)' if shop_only else 'có listing'}, "
                  f"{len(filtered_rows)} không có listing, {len(reverse_rows)} chỉ có ở file listing)")

        self._send_html(200, html_out)

    def log_message(self, fmt, *args):
        print("[http]", fmt % args)


def run_analyze_dir(path: str) -> None:
    """Phân tích trực tiếp 2 file CSV (shop-level + listing-level) đọc TỪ Ổ ĐĨA
    trong thư mục `path` — không qua browser upload nên biết chính xác tên
    thư mục nguồn (browser chỉ gửi được tên file, không gửi được đường dẫn cha).
    Lưu kết quả vào analysis_output/<tên thư mục path>/ — đặt tên phiên bản
    THEO ĐÚNG folder chứa 2 file gốc, thay vì timestamp lúc chạy phân tích."""
    if not os.path.isdir(path):
        print(f"[error] Không tìm thấy thư mục: {path}")
        raise SystemExit(1)

    shops_path = listings_path = None
    for p in sorted(glob.glob(os.path.join(path, "*.csv"))):
        try:
            df_head = pd.read_csv(p, dtype=str, encoding="utf-8-sig", nrows=5)
        except Exception as e:
            print(f"[warn] Bỏ qua '{p}' — không đọc được: {e}")
            continue
        kind = detect_kind(df_head)
        if kind == "shops" and shops_path is None:
            shops_path = p
        elif kind == "listings" and listings_path is None:
            listings_path = p

    if not shops_path or not listings_path:
        print(f"[error] Không tìm đủ 2 file (shop-level + listing-level) trong '{path}'. "
              f"Tìm thấy: shop-level={shops_path}, listing-level={listings_path}")
        raise SystemExit(1)

    print(f"[info] shop-level:    {shops_path}")
    print(f"[info] listing-level: {listings_path}")

    with open(shops_path, "rb") as f:
        shops_content = f.read()
    with open(listings_path, "rb") as f:
        listings_content = f.read()

    shops_df = pd.read_csv(io.BytesIO(shops_content), dtype=str, encoding="utf-8-sig").fillna("")
    listings_df = pd.read_csv(io.BytesIO(listings_content), dtype=str, encoding="utf-8-sig").fillna("")

    main_rows, filtered_rows, reverse_rows = analyze(shops_df, listings_df)
    good_names = {r["shop"] for r in main_rows if r["verdict"] == "good"}
    biz, biz_columns = business_rows(shops_df, good_names)
    top, top_available = top_rows(shops_df, good_names)
    products = product_rows(listings_df, good_names)
    keyword_rows = keyword_listing_rows(listings_df)
    all_shop_names = set(shops_df["shop_name"].astype(str)) if "shop_name" in shops_df.columns else set()
    keyword_biz_rows, _keyword_biz_columns = business_rows(shops_df, all_shop_names)

    html_out = render_report({
        "ledger_data": main_rows + filtered_rows,
        "reverse_rows": reverse_rows,
        "biz_rows": biz,
        "biz_columns": biz_columns,
        "top_rows": top,
        "top_available": top_available,
        "product_rows": products,
        "keyword_rows": keyword_rows,
        "keyword_biz_rows": keyword_biz_rows,
        "shops_filename": os.path.basename(shops_path),
        "listings_filename": os.path.basename(listings_path),
    })

    version_name = os.path.basename(os.path.normpath(path))
    out_dir = os.path.join("analysis_output", version_name)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "shop_gift_fit_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_out)
    with open(os.path.join(out_dir, RAW_SHOPS_CSV), "wb") as f:
        f.write(shops_content)
    with open(os.path.join(out_dir, RAW_LISTINGS_CSV), "wb") as f:
        f.write(listings_content)

    import_key = f"{hashlib.sha256(shops_content).hexdigest()}:{hashlib.sha256(listings_content).hexdigest()}"
    manifest = _load_import_manifest()
    existing = manifest.get(import_key)
    if existing and existing.get("dir") != out_dir:
        print(f"[note] Nội dung CSV này đã phân tích trước đó tại '{existing['dir']}' — "
              f"vẫn lưu thêm bản này tại '{out_dir}' theo tên folder nguồn.")
    manifest[import_key] = {
        "dir": out_dir,
        "shops_filename": os.path.basename(shops_path),
        "listings_filename": os.path.basename(listings_path),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_import_manifest(manifest)

    print(f"[saved] {out_path}  ({len(main_rows)} shop có listing, {len(filtered_rows)} không có listing, "
          f"{len(reverse_rows)} chỉ có ở file listing)")
    print(f"[saved] Phiên bản đặt tên theo folder nguồn: '{version_name}'")


def main():
    global BUILD_CONFLICT_DIR, BUILD_LIST_CSV
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, help="Cổng chạy server (mặc định 8765)")
    parser.add_argument(
        "--host", default=os.getenv("ANALYZER_HOST", "127.0.0.1"),
        help="Địa chỉ bind server (mặc định 127.0.0.1; trong Docker dùng 0.0.0.0)",
    )
    parser.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")
    parser.add_argument(
        "--analyze-dir", metavar="PATH",
        help=(
            "Phân tích trực tiếp 2 file CSV (shop-level + listing-level) đọc từ PATH, "
            "không cần mở browser upload — kết quả lưu vào analysis_output/<tên folder "
            "PATH>/ (đặt tên phiên bản theo đúng folder nguồn, ví dụ "
            "'data/20260814_091753' -> phiên bản '20260814_091753'). Chạy xong thoát "
            "luôn, không mở server."
        ),
    )
    parser.add_argument(
        "--build-dir", metavar="PATH", default=None,
        help=f"Thư mục chứa CSV listing của shop build + shop conflict cho tab 'Build vs Conflict' (mặc định DATA_DIR/{BUILD_CONFLICT_DIR})",
    )
    parser.add_argument(
        "--build-list", metavar="CSV", default=None,
        help=f"File danh sách shop build (cột Shop name / PIC / Conflict shop) (mặc định DATA_DIR/{BUILD_LIST_CSV})",
    )
    args = parser.parse_args()

    # Đường dẫn do người dùng truyền vào tính theo thư mục hiện tại, rồi chuyển
    # sang DATA_DIR để analysis_output/, shop_data/, input/ đều nằm trong data/.
    # Không truyền thì dùng mặc định bên trong DATA_DIR.
    BUILD_CONFLICT_DIR = os.path.abspath(args.build_dir) if args.build_dir else str(settings.DATA_DIR / BUILD_CONFLICT_DIR)
    BUILD_LIST_CSV = os.path.abspath(args.build_list) if args.build_list else str(settings.DATA_DIR / BUILD_LIST_CSV)
    analyze_dir = os.path.abspath(args.analyze_dir) if args.analyze_dir else None
    os.makedirs(settings.DATA_DIR, exist_ok=True)
    os.chdir(settings.DATA_DIR)

    if analyze_dir:
        run_analyze_dir(analyze_dir)
        return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Shop Gift-Fit Analyzer đang chạy tại {url} (Ctrl+C để dừng)")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDừng server.")


REPORT_TEMPLATE = r"""<title>Shop Gift-Fit Ledger</title>
<style>
:root {
  --paper: #eff1ec;
  --paper-raised: #f6f7f3;
  --ink: #20261f;
  --ink-soft: #5a6455;
  --line: #d2d6c9;
  --line-strong: #b7bcac;
  --accent: #2f6b6b;
  --accent-soft: #dbe8e6;
  --good: #3f8452;
  --good-soft: #e1eee2;
  --warn: #a8762a;
  --warn-soft: #f2e6cf;
  --critical: #a8483d;
  --critical-soft: #f2ded9;
  --muted: #8c8677;
  --muted-soft: #e6e3da;
  --shadow: 0 1px 2px rgba(32,38,31,0.06), 0 6px 16px rgba(32,38,31,0.05);
  --chart-1: #2a78d6;
  --chart-2: #eb6834;
  --chart-3: #1baf7a;
  --chart-4: #eda100;
  --chart-5: #e87ba4;
  --chart-6: #008300;
  --chart-7: #4a3aa7;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper: #171b16;
    --paper-raised: #1e231c;
    --ink: #e7e9e1;
    --ink-soft: #a3aa9a;
    --line: #333a2f;
    --line-strong: #454e3d;
    --accent: #6fada6;
    --accent-soft: #253330;
    --good: #7cbb85;
    --good-soft: #24322a;
    --warn: #d3a962;
    --warn-soft: #35301f;
    --critical: #d98d80;
    --critical-soft: #382522;
    --muted: #9c9788;
    --muted-soft: #262620;
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.35);
    --chart-1: #3987e5;
    --chart-2: #d95926;
    --chart-3: #199e70;
    --chart-4: #c98500;
    --chart-5: #d55181;
    --chart-6: #008300;
    --chart-7: #9085e9;
  }
}
:root[data-theme="dark"] {
  --paper: #171b16;
  --paper-raised: #1e231c;
  --ink: #e7e9e1;
  --ink-soft: #a3aa9a;
  --line: #333a2f;
  --line-strong: #454e3d;
  --accent: #6fada6;
  --accent-soft: #253330;
  --good: #7cbb85;
  --good-soft: #24322a;
  --warn: #d3a962;
  --warn-soft: #35301f;
  --critical: #d98d80;
  --critical-soft: #382522;
  --muted: #9c9788;
  --muted-soft: #262620;
  --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 6px 20px rgba(0,0,0,0.35);
  --chart-1: #3987e5;
  --chart-2: #d95926;
  --chart-3: #199e70;
  --chart-4: #c98500;
  --chart-5: #d55181;
  --chart-6: #008300;
  --chart-7: #9085e9;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--paper);
  color: var(--ink);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.wrap { max-width: 1180px; margin: 0 auto; padding: 40px 24px 80px; }

header.masthead {
  border-bottom: 1px solid var(--line-strong);
  padding-bottom: 20px;
  margin-bottom: 28px;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
}
header.masthead .eyebrow {
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 600;
}
h1 {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 30px;
  font-weight: 500;
  margin: 4px 0 0;
  text-wrap: balance;
  letter-spacing: -0.01em;
}
header.masthead .meta {
  font-size: 13px;
  color: var(--ink-soft);
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.nav-link { display: inline-block; margin-top: 8px; font-size: 12.5px; color: var(--accent); }

section.intro {
  font-size: 15px;
  color: var(--ink-soft);
  max-width: 68ch;
  margin: 0 0 28px;
}
section.intro strong { color: var(--ink); font-weight: 600; }

.stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
  margin-bottom: 32px;
}
.stat {
  background: var(--paper-raised);
  border: 1px solid var(--line);
  border-left: 3px solid var(--stat-color, var(--accent));
  border-radius: 3px;
  padding: 14px 16px;
  box-shadow: var(--shadow);
}
.stat .n {
  font-family: Georgia, serif;
  font-size: 26px;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.stat .label {
  font-size: 12px;
  color: var(--ink-soft);
  margin-top: 4px;
}
.stat.total { --stat-color: var(--ink-soft); }
.stat.filtered { --stat-color: var(--muted); }
.stat.good { --stat-color: var(--good); }
.stat.review { --stat-color: var(--warn); }
.stat.no { --stat-color: var(--critical); }

.controls {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 4px;
  background: var(--paper);
  padding: 10px 0;
}
.controls input[type="search"] {
  flex: 1 1 220px;
  min-width: 160px;
  background: var(--paper-raised);
  border: 1px solid var(--line-strong);
  border-radius: 3px;
  padding: 8px 12px;
  font-size: 14px;
  color: var(--ink);
  font-family: inherit;
}
.controls input[type="search"]:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.controls select {
  background: var(--paper-raised);
  border: 1px solid var(--line-strong);
  border-radius: 3px;
  padding: 8px 10px;
  font-size: 14px;
  color: var(--ink);
  font-family: inherit;
}
.controls select:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
.chip {
  border: 1px solid var(--line-strong);
  background: var(--paper-raised);
  color: var(--ink-soft);
  font-size: 13px;
  padding: 7px 13px;
  border-radius: 999px;
  cursor: pointer;
  font-family: inherit;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.chip:hover { border-color: var(--accent); color: var(--ink); }
.chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.chip[aria-pressed="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--paper);
  font-weight: 600;
}
.count-line {
  font-size: 13px;
  color: var(--ink-soft);
  margin: 10px 0 14px;
  font-variant-numeric: tabular-nums;
}

.table-scroll {
  overflow-x: auto;
  overflow-y: auto;
  max-height: 640px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--paper-raised);
  box-shadow: var(--shadow);
}
table { width: 100%; border-collapse: collapse; font-size: 13.5px; min-width: 920px; }
thead th {
  text-align: left;
  font-size: 11px;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--ink-soft);
  border-bottom: 1px solid var(--line-strong);
  padding: 10px 12px;
  position: sticky;
  top: 0;
  background: var(--paper-raised);
  white-space: nowrap;
}
tbody tr { border-bottom: 1px solid var(--line); }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--accent-soft); }
tbody td { padding: 10px 12px; vertical-align: top; }
td.num { font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap; }
td.shop {
  font-family: "SF Mono", "Menlo", "Consolas", ui-monospace, monospace;
  font-weight: 600;
  font-size: 13px;
  white-space: nowrap;
}
td.shop a { color: var(--ink); text-decoration: none; border-bottom: 1px dotted var(--line-strong); }
td.shop a:hover { color: var(--accent); border-bottom-color: var(--accent); }
td.cat { color: var(--ink-soft); white-space: nowrap; }
td.sample { color: var(--ink-soft); max-width: 360px; font-size: 12.5px; }
td.sample .t { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.pill {
  display: inline-block;
  font-size: 11.5px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.pill.good { background: var(--good-soft); color: var(--good); }
.pill.review { background: var(--warn-soft); color: var(--warn); }
.pill.no { background: var(--critical-soft); color: var(--critical); }
.pill.filtered { background: var(--muted-soft); color: var(--muted); }
.pill.prelim { background: var(--warn-soft); color: var(--warn); border: 1px dashed var(--warn); margin-left: 4px; cursor: help; }
#prelim-banner {
  display: none;
  background: var(--warn-soft); color: var(--warn); border-radius: 6px;
  padding: 10px 14px; font-size: 13px; margin: 0 0 16px;
}

.bar-cell { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
.bar-track { width: 46px; height: 6px; border-radius: 3px; background: var(--muted-soft); overflow: hidden; flex-shrink: 0; }
.bar-fill { height: 100%; border-radius: 3px; background: var(--accent); }

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 44px 0 12px;
  border-bottom: 1px solid var(--line-strong);
  padding-bottom: 8px;
}
.section-head h2 {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 19px;
  font-weight: 500;
  margin: 0;
}
.section-head .sub { font-size: 13px; color: var(--ink-soft); }

details.filtered-block summary {
  cursor: pointer;
  font-size: 14px;
  color: var(--accent);
  font-weight: 600;
  padding: 10px 0;
  list-style: none;
}
details.filtered-block summary::-webkit-details-marker { display: none; }
details.filtered-block summary::before {
  content: "▸ ";
  display: inline-block;
  transition: transform 0.15s;
}
details.filtered-block[open] summary::before { transform: rotate(90deg); }
.filtered-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: 8px;
  margin-top: 8px;
}
.filtered-item {
  border: 1px solid var(--line);
  background: var(--paper-raised);
  border-radius: 3px;
  padding: 8px 10px;
  font-size: 12.5px;
}
.filtered-item .name { font-family: ui-monospace, monospace; font-weight: 600; display: block; }
.filtered-item .cat { color: var(--ink-soft); display: block; margin-top: 2px; }

footer.note {
  margin-top: 40px;
  font-size: 12.5px;
  color: var(--ink-soft);
  border-top: 1px solid var(--line);
  padding-top: 16px;
}

.empty-state { padding: 40px; text-align: center; color: var(--ink-soft); font-size: 14px; }

.tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--line-strong);
  margin-bottom: 24px;
}
.tab-btn {
  font-family: inherit;
  font-size: 14px;
  font-weight: 600;
  color: var(--ink-soft);
  background: none;
  border: none;
  padding: 10px 4px;
  margin-right: 22px;
  cursor: pointer;
  position: relative;
  top: 1px;
  border-bottom: 2px solid transparent;
}
.tab-btn:hover { color: var(--ink); }
.tab-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tab-btn[aria-selected="true"] { color: var(--accent); border-bottom-color: var(--accent); }
.tab-btn .tab-count {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  background: var(--muted-soft);
  color: var(--ink-soft);
  border-radius: 999px;
  padding: 1px 7px;
  margin-left: 6px;
  font-variant-numeric: tabular-nums;
}
.tab-panel { display: none; }
.tab-panel[data-active="true"] { display: block; }

.reverse-intro {
  font-size: 14.5px;
  color: var(--ink-soft);
  max-width: 72ch;
  margin: 0 0 22px;
}
.reverse-intro strong { color: var(--ink); font-weight: 600; }
td.note { color: var(--ink-soft); max-width: 420px; font-size: 12.5px; line-height: 1.45; }

th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--ink); }
th.sortable .arrow { display: inline-block; width: 10px; opacity: 0.35; }
th.sortable[data-sort-dir="asc"] .arrow, th.sortable[data-sort-dir="desc"] .arrow { opacity: 1; color: var(--accent); }

.chart-card {
  border: 1px solid var(--line);
  background: var(--paper-raised);
  border-radius: 4px;
  padding: 20px 22px 18px;
  box-shadow: var(--shadow);
  margin-bottom: 22px;
}
.chart-card h3 {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-size: 16px;
  font-weight: 500;
  margin: 0 0 3px;
}
.chart-card .chart-sub { font-size: 12.5px; color: var(--ink-soft); margin: 0 0 6px; max-width: 68ch; }
.chart-caveat {
  font-size: 12px;
  color: var(--warn);
  background: var(--warn-soft);
  border-radius: 3px;
  padding: 7px 10px;
  margin: 8px 0 16px;
  max-width: 68ch;
}
.chart-legend { display: flex; gap: 18px; margin: 4px 0 16px; flex-wrap: wrap; }
.chart-legend .legend-item { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--ink-soft); }
.chart-legend .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
.chart-legend .swatch.s1 { background: var(--chart-1); }
.chart-legend .swatch.s2 { background: var(--chart-2); }
.chart-legend-col { flex-direction: column; gap: 8px; margin: 0; }

.pie-wrap { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
.pie-wrap svg { flex-shrink: 0; }
.pie-slice { cursor: default; }

.hbar-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; position: relative; }
.hbar-label {
  width: 148px;
  flex-shrink: 0;
  font-family: "SF Mono", "Menlo", "Consolas", ui-monospace, monospace;
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hbar-rank { color: var(--ink-soft); font-weight: 400; margin-right: 4px; }
.hbar-track {
  flex: 1;
  position: relative;
  height: 22px;
  background-image: linear-gradient(to right, var(--line) 0, var(--line) 1px, transparent 1px);
  background-size: 25% 100%;
}
.hbar-fill {
  height: 22px;
  border-radius: 0 4px 4px 0;
  background: var(--chart-1);
  position: relative;
  cursor: default;
}
.hbar-value {
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  color: var(--ink);
  white-space: nowrap;
  width: 56px;
  flex-shrink: 0;
}

svg.spark { display: block; vertical-align: middle; cursor: default; }

.growth-group { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.growth-track {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: relative;
  background-image: linear-gradient(to right, var(--line) 0, var(--line) 1px, transparent 1px);
  background-size: 25% 100%;
  padding: 2px 0;
}
.growth-bar { height: 10px; border-radius: 0 4px 4px 0; cursor: default; }
.growth-bar.s1 { background: var(--chart-1); }
.growth-bar.s2 { background: var(--chart-2); }
.growth-value {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  color: var(--ink-soft);
  white-space: nowrap;
  width: 108px;
  flex-shrink: 0;
  text-align: right;
}

.chart-tooltip {
  position: fixed;
  z-index: 50;
  background: var(--paper-raised);
  border: 1px solid var(--line-strong);
  border-radius: 4px;
  box-shadow: var(--shadow);
  padding: 8px 11px;
  font-size: 12px;
  color: var(--ink-soft);
  max-width: 260px;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.1s;
}
.chart-tooltip.visible { opacity: 1; }
.chart-tooltip strong { color: var(--ink); font-weight: 600; }
.chart-tooltip .tt-title { color: var(--ink); font-weight: 700; margin-bottom: 4px; display: block; }
.chart-tooltip .tt-row { display: flex; justify-content: space-between; gap: 12px; }

@media (max-width: 720px) {
  .stats { grid-template-columns: repeat(2, 1fr); }
  header.masthead { flex-direction: column; align-items: flex-start; }
  header.masthead .meta { text-align: left; }
}
</style>

<div class="wrap">
  <header class="masthead">
    <div>
      <div class="eyebrow">Custom-gift screening</div>
      <h1>Shop Gift-Fit Ledger</h1>
    </div>
    <div class="meta">
      __META_HTML__
    </div>
  </header>

  <section class="intro">
    Đối chiếu 2 file CSV vừa upload: file <strong>shop-level</strong> (một dòng/shop) và file
    <strong>listing-level</strong> (một dòng/listing). Shop có trong file shop nhưng không có listing trong file
    listing (chưa crawl, hoặc đã bị lọc ở lần crawl khác) được liệt kê riêng, không tính điểm. Với các shop có
    listing, mức phù hợp làm custom gift được chấm dựa trên tỷ lệ listing khớp từ khoá quà tặng cá nhân hoá
    (personalized/custom/engraved/monogram…) trong tiêu đề &amp; tag, cờ <em>is_customizable</em>, và nhóm ngành
    hàng chủ đạo của shop.
  </section>

  <div class="tabs" role="tablist">
    <button class="tab-btn" role="tab" id="tab-btn-main" aria-controls="tab-panel-main" aria-selected="true" data-tab="main">
      Shop có listing <span class="tab-count" id="tab-count-main">–</span>
    </button>
    <button class="tab-btn" role="tab" id="tab-btn-reverse" aria-controls="tab-panel-reverse" aria-selected="false" data-tab="reverse">
      Đối chiếu ngược (listing → shop) <span class="tab-count" id="tab-count-reverse">–</span>
    </button>
    <button class="tab-btn" role="tab" id="tab-btn-biz" aria-controls="tab-panel-biz" aria-selected="false" data-tab="biz">
      Chỉ số kinh doanh — shop phù hợp <span class="tab-count" id="tab-count-biz">–</span>
    </button>
    <button class="tab-btn" role="tab" id="tab-btn-top10" aria-controls="tab-panel-top10" aria-selected="false" data-tab="top10">
      Top 20 bán chạy — shop phù hợp (Heyetsy) <span class="tab-count" id="tab-count-top10">–</span>
    </button>
    <button class="tab-btn" role="tab" id="tab-btn-products" aria-controls="tab-panel-products" aria-selected="false" data-tab="products">
      Top 50 sản phẩm <span class="tab-count" id="tab-count-products">–</span>
    </button>
    <button class="tab-btn" role="tab" id="tab-btn-keyword" aria-controls="tab-panel-keyword" aria-selected="false" data-tab="keyword">
      Tìm theo từ khoá <span class="tab-count" id="tab-count-keyword">–</span>
    </button>
  </div>

  <div class="tab-panel" id="tab-panel-main" role="tabpanel" aria-labelledby="tab-btn-main" data-active="true">

  <div class="stats">
    <div class="stat total"><div class="n" id="stat-total">–</div><div class="label">Tổng shop đối chiếu</div></div>
    <div class="stat filtered"><div class="n" id="stat-filtered">–</div><div class="label">Không có dữ liệu listing</div></div>
    <div class="stat good"><div class="n" id="stat-good">–</div><div class="label">Phù hợp custom gift</div></div>
    <div class="stat review"><div class="n" id="stat-review">–</div><div class="label">Cần xem xét thêm</div></div>
    <div class="stat no"><div class="n" id="stat-no">–</div><div class="label">Không phù hợp</div></div>
  </div>

  <div class="note" id="prelim-banner">
    <strong id="prelim-count">0</strong> shop trong bảng dưới đang ở verdict <strong>SƠ BỘ</strong> (đánh dấu
    <span class="pill prelim">sơ bộ</span>) — chưa có dữ liệu listing nên chỉ chấm được qua category/description
    của shop, không có %từ khoá gift/%customizable thật. Crawl thêm listing cho các shop này rồi phân tích lại để
    có verdict chính xác.
  </div>

  <div class="controls">
    <input type="search" id="search" placeholder="Tìm shop, ngành hàng, mô tả…" aria-label="Tìm shop">
    <button class="chip" data-filter="all" aria-pressed="true">Tất cả</button>
    <button class="chip" data-filter="good" aria-pressed="false">Phù hợp</button>
    <button class="chip" data-filter="review" aria-pressed="false">Cần xem xét</button>
    <button class="chip" data-filter="no" aria-pressed="false">Không phù hợp</button>
  </div>
  <div class="count-line" id="count-line"></div>

  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Shop</th>
          <th>Ngành hàng chủ đạo</th>
          <th>Listings</th>
          <th>% từ khoá gift</th>
          <th>% customizable</th>
          <th>Giá TB</th>
          <th>Đánh giá</th>
          <th>Mẫu tiêu đề</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
    <div class="empty-state" id="empty-state" style="display:none;">Không có shop khớp bộ lọc hiện tại.</div>
  </div>

  <div class="section-head">
    <h2>Shop không có dữ liệu listing</h2>
    <div class="sub" id="filtered-count"></div>
  </div>
  <details class="filtered-block">
    <summary>Xem danh sách shop không có listing trong file listing</summary>
    <div class="filtered-grid" id="filtered-grid"></div>
  </details>

  <footer class="note">
    Cách chấm điểm: +2 nếu ≥50% listing khớp từ khoá quà cá nhân hoá trong tiêu đề/tag (+1 nếu 20–50%); +2 nếu ≥40%
    listing gắn cờ <em>is_customizable</em> (+1 nếu 15–40%) — <strong>nhưng điểm is_customizable chỉ được cộng khi
    có ít nhất 1 listing khớp từ khoá gift</strong> (tránh case shop chỉ cho chọn size/màu/mùi qua dropdown Etsy,
    is_customizable=True nhưng không phải cá nhân hoá quà tặng thật); +1 nếu ngành hàng chủ đạo thuộc nhóm quà tặng
    phổ biến (Paper &amp; Party, Jewelry, Accessories, Home &amp; Living, Art &amp; Collectibles, Bags &amp; Purses,
    Weddings, Bath &amp; Beauty); −1 nếu ngành hàng là Craft Supplies &amp; Tools (thường bán nguyên liệu/pattern,
    không phải quà thành phẩm). Tổng điểm ≥2 → "Phù hợp", =1 → "Cần xem xét", ≤0 → "Không phù hợp". Đây là bộ lọc
    sàng lọc nhanh — vẫn nên mở thử vài listing mẫu trước khi quyết định tiếp cận shop.
  </footer>

  </div>

  <div class="tab-panel" id="tab-panel-reverse" role="tabpanel" aria-labelledby="tab-btn-reverse" data-active="false">
    <p class="reverse-intro">
      Chiều ngược lại: shop nào <strong>có listing</strong> trong file listing nhưng <strong>không có trong file
      shop-level</strong> vừa upload? Hữu ích để kiểm tra 2 file có khớp nhau không (cùng 1 lần crawl hay 2 lần
      crawl khác nhau), và để không bỏ sót shop tốt chỉ vì thiếu mặt trong file shop-level. Cột cuối đối chiếu tên/
      ngành hàng/tiêu đề của shop với bộ từ khoá loại trừ mà scraper hiện dùng (tarot, psychic, crochet, digital,
      printable…) — biết được liệu shop có bị scraper tự loại nếu crawl lại hay không.
    </p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Shop</th>
            <th>Ngành hàng chủ đạo</th>
            <th>Listings</th>
            <th>% từ khoá gift</th>
            <th>% customizable</th>
            <th>% made-to-order</th>
            <th>Giá TB</th>
            <th>Đánh giá</th>
            <th>Đối chiếu từ khoá loại trừ</th>
          </tr>
        </thead>
        <tbody id="tbody-reverse"></tbody>
      </table>
    </div>
  </div>

  <div class="tab-panel" id="tab-panel-biz" role="tabpanel" aria-labelledby="tab-btn-biz" data-active="false">
    <p class="reverse-intro">
      Chỉ số kinh doanh thật lấy từ <code>everbee_shop_data_shops.csv</code> (run 083803) cho <strong id="biz-n-shops">–</strong>
      shop đã được chấm "Phù hợp" làm custom gift ở tab đầu — review, doanh thu, lượt xem, tỉ lệ chuyển đổi… để ưu tiên
      shop nào đáng tiếp cận trước. Bấm vào tên cột để sắp xếp. Chỉ hiện các cột thực có trong file bạn upload.
    </p>
    <div class="stats" id="biz-stats">
      <div class="stat total"><div class="n" id="biz-stat-shops">–</div><div class="label">Shop phù hợp</div></div>
      <div class="stat good"><div class="n" id="biz-stat-revenue30">–</div><div class="label">Tổng revenue 30d (USD)</div></div>
      <div class="stat good"><div class="n" id="biz-stat-sales30">–</div><div class="label">Tổng sales 30d</div></div>
      <div class="stat review"><div class="n" id="biz-stat-conv">–</div><div class="label">Conversion rate TB</div></div>
      <div class="stat review"><div class="n" id="biz-stat-review">–</div><div class="label">Review avg TB</div></div>
    </div>
    <div class="controls" style="position: static;">
      <input type="search" id="biz-search" placeholder="Tìm shop, ngành hàng…" aria-label="Tìm shop">
    </div>
    <div class="table-scroll">
      <table id="biz-table">
        <thead id="thead-biz"></thead>
        <tbody id="tbody-biz"></tbody>
      </table>
    </div>
    <div class="count-line" id="biz-count-line"></div>
  </div>

  <div class="tab-panel" id="tab-panel-top10" role="tabpanel" aria-labelledby="tab-btn-top10" data-active="false">
    <p class="reverse-intro">
      Xếp hạng theo <strong>heyetsy_avg_weekly_sold</strong> — nhịp bán trung bình mỗi tuần lấy từ heyetsy.com, ổn định
      hơn số liệu "tháng này"/"tuần này" (vốn chỉ tính đến ngày crawl, tháng/tuần chưa hết nên luôn thấp hơn kỳ trước
      đã đủ ngày). Chỉ xếp hạng trong nhóm shop đã chấm "Phù hợp" làm custom gift ở tab đầu — tối đa 20 shop vừa bán
      chạy vừa đúng ngành custom gift, đáng tiếp cận trước tiên.
    </p>
    <p class="chart-caveat">
      Cột <strong>Nhịp bán 1 tháng</strong>/<strong>Xu hướng</strong>/<strong>Biến động</strong> tính từ
      <code>heyetsy_daily_sold_dates</code>/<code>_series</code> (report1m) — chuỗi này chỉ ghi các ngày có thay đổi
      lượt bán, không phải mọi ngày trong tháng đều có điểm, nên khoảng trống giữa các điểm không đồng nghĩa "không
      bán được gì" hôm đó. <strong>Xu hướng</strong> so nửa đầu với nửa sau của các điểm thực có (không phải theo
      lịch); <strong>Biến động</strong> phản ánh mức chênh lệch giữa các lần bán — "Ổn định" đáng tin hơn "Đột biến"
      dù tổng bán bằng nhau, vì ít rủi ro chỉ dựa vào 1 đợt bán nhất thời.
    </p>

    <div class="empty-state" id="top10-empty-state" style="display:none;">
      File shop-level không có cột <code>heyetsy_*</code> nên không thể xếp hạng ở tab này.
    </div>

    <div id="top10-content">
      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Shop</th>
              <th>Ngành hàng</th>
              <th>Nhịp bán 1 tháng (report1m)</th>
              <th>Xu hướng</th>
              <th>Biến động</th>
              <th>TB/tuần</th>
              <th>Tháng này</th>
              <th>Tháng trước</th>
              <th>Tuần này</th>
              <th>Tuần trước</th>
              <th>Hôm nay</th>
              <th>TB/ngày</th>
              <th>Favorites/tuần</th>
              <th>Review tháng này</th>
            </tr>
          </thead>
          <tbody id="tbody-top10"></tbody>
        </table>
      </div>

      <div class="chart-card" style="margin-top: 26px;">
        <h3>Nhịp bán trung bình mỗi tuần</h3>
        <p class="chart-sub">heyetsy_avg_weekly_sold — số lượng bán trung bình mỗi tuần, tính trên nhiều tuần gần nhất nên không bị lệch bởi việc tuần/tháng hiện tại chưa kết thúc.</p>
        <div id="chart-top10-bars"></div>
      </div>

      <div class="chart-card">
        <h3>Tuần trước so với tuần này</h3>
        <p class="chart-sub">So sánh trực tiếp số lượng bán tuần trước và tuần này (cùng danh sách, cùng thứ tự xếp hạng ở trên).</p>
        <p class="chart-caveat">
          ⚠ "Tuần này" chỉ tính đến thời điểm crawl (tuần chưa hết ngày) nên gần như luôn thấp hơn "tuần trước" (đã đủ 7 ngày)
          — chênh lệch âm ở đây <strong>không đồng nghĩa shop đang giảm tốc</strong>. Nên đối chiếu với cột "TB/ngày" và
          "Hôm nay" ở bảng trên để đánh giá nhịp bán thực tế.
        </p>
        <div class="chart-legend">
          <span class="legend-item"><span class="swatch s1"></span>Tuần trước</span>
          <span class="legend-item"><span class="swatch s2"></span>Tuần này</span>
        </div>
        <div id="chart-top10-growth"></div>
      </div>
    </div>
  </div>

  <div class="tab-panel" id="tab-panel-products" role="tabpanel" aria-labelledby="tab-btn-products" data-active="false">
    <p class="reverse-intro">
      Xếp hạng theo <strong>từng listing</strong> (không phải shop) trong nhóm shop đã chấm "Phù hợp" làm custom gift
      ở tab đầu — chọn tiêu chí ở dropdown bên dưới (Views / Ước tính reviews / Favorites / <strong>Review/Age</strong>),
      tối đa 50 sản phẩm mỗi lượt. Biểu đồ tròn bên dưới bảng cho biết 50 sản phẩm đó rơi vào ngành hàng (category)
      nào nhiều nhất.
    </p>
    <p class="chart-caveat">
      <strong>Review/Age</strong> = ước tính reviews chia cho tuổi listing (tháng) — tốc độ tích luỹ review theo
      thời gian, giúp phát hiện listing MỚI nhưng đang lên nhanh (nhiều review dù tuổi còn ít), thay vì chỉ xếp theo
      tổng số tuyệt đối như 3 tiêu chí còn lại. Listing thiếu tuổi (listing_age = 0 hoặc rỗng) sẽ không tính được tỉ
      lệ này nên bị loại khỏi bảng khi chọn tiêu chí này.
    </p>

    <div class="empty-state" id="products-empty-state" style="display:none;">
      Không có listing nào thuộc nhóm shop "Phù hợp" (hoặc file listing-level thiếu cột views/est_reviews/favorites)
      nên không thể xếp hạng ở tab này.
    </div>

    <div id="products-content">
      <div class="controls" style="position: static;">
        <label for="product-metric" style="font-size:12.5px; color:var(--ink-soft);">Xếp theo</label>
        <select id="product-metric" aria-label="Xếp theo tiêu chí">
          <option value="views">Views</option>
          <option value="est_reviews">Ước tính reviews</option>
          <option value="favorites">Favorites</option>
          <option value="review_per_age">Review/Age</option>
        </select>
      </div>

      <div class="table-scroll">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Sản phẩm</th>
              <th>Shop</th>
              <th>Ngành hàng</th>
              <th id="product-metric-th">Views</th>
            </tr>
          </thead>
          <tbody id="tbody-products"></tbody>
        </table>
      </div>

      <div class="chart-card" style="margin-top: 26px;">
        <h3>Ngành hàng trong Top 50</h3>
        <p class="chart-sub">Tỉ trọng ngành hàng (category) của đúng 50 sản phẩm đang hiển thị ở bảng trên — đổi tiêu chí xếp hạng thì biểu đồ cập nhật theo.</p>
        <div class="pie-wrap">
          <svg id="chart-product-pie" width="200" height="200" viewBox="0 0 200 200"></svg>
          <div class="chart-legend chart-legend-col" id="product-pie-legend"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="tab-panel" id="tab-panel-keyword" role="tabpanel" aria-labelledby="tab-btn-keyword" data-active="false">
    <p class="reverse-intro">
      Nhập 1 từ khoá (không phân biệt hoa/thường) — tool quét <strong>toàn bộ listing đã upload</strong> (mọi shop,
      không chỉ nhóm "Phù hợp" như các tab khác) theo <strong>tiêu đề, tag và ngành hàng</strong>, rồi gộp kết quả
      thành bảng theo <strong>shop</strong>, kèm các chỉ số kinh doanh giống tab "Chỉ số kinh doanh" (chỉ hiện cột
      thực có trong file shop-level bạn upload). Bấm vào tên cột để sắp xếp, hoặc bấm "Xuất CSV" để tải bảng đang
      hiển thị.
    </p>

    <div class="controls" style="position: static;">
      <input type="search" id="keyword-input" placeholder="Ví dụ: dog, wedding, mom…" aria-label="Nhập từ khoá">
      <button class="chip" id="keyword-export-btn" type="button">Xuất CSV</button>
    </div>

    <div class="empty-state" id="keyword-idle-state">
      Nhập từ khoá ở ô trên để bắt đầu.
    </div>
    <div class="empty-state" id="keyword-empty-state" style="display:none;">
      Không có listing nào khớp từ khoá này.
    </div>

    <div id="keyword-content" style="display:none;">
      <p class="chart-caveat" id="keyword-summary-line"></p>
      <div class="table-scroll">
        <table id="keyword-table">
          <thead id="thead-keyword"></thead>
          <tbody id="tbody-keyword"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<div class="chart-tooltip" id="chart-tooltip"></div>

<script id="ledger-data" type="application/json">__DATA_JSON__</script>
<script id="reverse-data" type="application/json">__REVERSE_JSON__</script>
<script id="biz-data" type="application/json">__BIZ_JSON__</script>
<script id="biz-columns" type="application/json">__BIZ_COLUMNS_JSON__</script>
<script id="top10-data" type="application/json">__TOP10_JSON__</script>
<script id="top10-meta" type="application/json">__TOP10_META_JSON__</script>
<script id="products-data" type="application/json">__PRODUCTS_JSON__</script>
<script id="keyword-listing-data" type="application/json">__KEYWORD_ROWS_JSON__</script>
<script id="keyword-biz-data" type="application/json">__KEYWORD_BIZ_JSON__</script>
<script>
(function () {
  var data = JSON.parse(document.getElementById('ledger-data').textContent);
  // 'prelim' = shop chỉ có dữ liệu shop-level (chưa crawl listing, upload 1 file
  // duy nhất) — vẫn được chấm verdict SƠ BỘ nên hiện cùng bảng chính với 'present',
  // chỉ khác là các cột phụ thuộc listing (n_listings/%gift kw/%customizable/giá/
  // mẫu tiêu đề) đều rỗng và có thêm badge "sơ bộ" cạnh verdict.
  var present = data.filter(function (r) { return r.status === 'present' || r.status === 'prelim'; });
  var filtered = data.filter(function (r) { return r.status === 'filtered'; });
  var prelim = data.filter(function (r) { return r.status === 'prelim'; });

  document.getElementById('stat-total').textContent = data.length;
  document.getElementById('stat-filtered').textContent = filtered.length;
  document.getElementById('stat-good').textContent = present.filter(function (r) { return r.verdict === 'good'; }).length;
  document.getElementById('stat-review').textContent = present.filter(function (r) { return r.verdict === 'review'; }).length;
  document.getElementById('stat-no').textContent = present.filter(function (r) { return r.verdict === 'no'; }).length;
  document.getElementById('filtered-count').textContent = filtered.length + ' shop';

  if (prelim.length) {
    document.getElementById('prelim-count').textContent = prelim.length;
    document.getElementById('prelim-banner').style.display = 'block';
  }

  var VERDICT_LABEL = { good: 'Phù hợp', review: 'Cần xem xét', no: 'Không phù hợp' };

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function etsyUrl(shop) { return 'https://www.etsy.com/shop/' + encodeURIComponent(shop); }

  function rowHtml(r) {
    var isPrelim = r.status === 'prelim';
    var nListings = r.n_listings == null ? '–' : r.n_listings.toLocaleString('en-US');
    var pctKwCell = r.pct_gift_kw == null
      ? '<span title="Chưa crawl listing — không tính được">–</span>'
      : ('<div class="bar-cell">' + r.pct_gift_kw.toFixed(1) + '%<span class="bar-track">' +
         '<span class="bar-fill" style="width:' + Math.min(r.pct_gift_kw, 100) + '%"></span></span></div>');
    var pctCustCell = r.pct_customizable == null ? '–' : r.pct_customizable.toFixed(1) + '%';
    var price = r.avg_price == null ? '–' : ('$' + r.avg_price.toFixed(2));
    var samples = (r.sample_titles || []).slice(0, 2).map(function (t) {
      return '<span class="t">' + escapeHtml(t) + '</span>';
    }).join('');
    var verdictCell = '<span class="pill ' + r.verdict + '">' + VERDICT_LABEL[r.verdict] + '</span>' +
      (isPrelim
        ? '<span class="pill prelim" title="Chưa có dữ liệu listing — verdict sơ bộ chỉ dựa category/mô tả shop, cần crawl thêm listing để chấm chính xác">sơ bộ</span>'
        : '');
    return '' +
      '<tr data-shop="' + escapeHtml(r.shop.toLowerCase()) + '" data-blob="' +
        escapeHtml((r.shop + ' ' + r.top_category + ' ' + r.shop_category + ' ' + r.shop_description).toLowerCase()) +
      '" data-verdict="' + r.verdict + '">' +
      '<td class="shop"><a href="' + etsyUrl(r.shop) + '" target="_blank" rel="noopener">' + escapeHtml(r.shop) + '</a></td>' +
      '<td class="cat">' + escapeHtml(r.top_category || r.shop_category || '—') + '</td>' +
      '<td class="num">' + nListings + '</td>' +
      '<td class="num">' + pctKwCell + '</td>' +
      '<td class="num">' + pctCustCell + '</td>' +
      '<td class="num">' + price + '</td>' +
      '<td>' + verdictCell + '</td>' +
      '<td class="sample">' + samples + '</td>' +
      '</tr>';
  }

  var tbody = document.getElementById('tbody');
  tbody.innerHTML = present
    .slice()
    .sort(function (a, b) {
      var order = { good: 0, review: 1, no: 2 };
      if (order[a.verdict] !== order[b.verdict]) return order[a.verdict] - order[b.verdict];
      return (b.pct_gift_kw || 0) - (a.pct_gift_kw || 0);
    })
    .map(rowHtml)
    .join('');

  var filteredGrid = document.getElementById('filtered-grid');
  filteredGrid.innerHTML = filtered
    .slice()
    .sort(function (a, b) { return a.shop.localeCompare(b.shop); })
    .map(function (r) {
      return '<div class="filtered-item"><span class="name">' + escapeHtml(r.shop) + '</span>' +
        '<span class="cat">' + escapeHtml(r.shop_category || '—') + (r.shop_description ? ' · ' + escapeHtml(r.shop_description) : '') + '</span></div>';
    })
    .join('');

  var activeFilter = 'all';
  var searchTerm = '';

  function applyFilters() {
    var rows = tbody.querySelectorAll('tr');
    var visible = 0;
    rows.forEach(function (tr) {
      var matchesVerdict = activeFilter === 'all' || tr.getAttribute('data-verdict') === activeFilter;
      var matchesSearch = !searchTerm || tr.getAttribute('data-blob').indexOf(searchTerm) !== -1;
      var show = matchesVerdict && matchesSearch;
      tr.style.display = show ? '' : 'none';
      if (show) visible++;
    });
    document.getElementById('count-line').textContent = 'Hiển thị ' + visible + ' / ' + present.length + ' shop đã chấm điểm';
    document.getElementById('empty-state').style.display = visible === 0 ? 'block' : 'none';
  }

  document.querySelectorAll('.chip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      document.querySelectorAll('.chip').forEach(function (c) { c.setAttribute('aria-pressed', 'false'); });
      chip.setAttribute('aria-pressed', 'true');
      activeFilter = chip.getAttribute('data-filter');
      applyFilters();
    });
  });

  document.getElementById('search').addEventListener('input', function (e) {
    searchTerm = e.target.value.trim().toLowerCase();
    applyFilters();
  });

  applyFilters();

  // ── Đối chiếu ngược (113426 → 083803) ──────────────────────────────────
  var reverseData = JSON.parse(document.getElementById('reverse-data').textContent);
  document.getElementById('tab-count-main').textContent = present.length;
  document.getElementById('tab-count-reverse').textContent = reverseData.length;

  function reverseRowHtml(r) {
    var price = r.avg_price == null ? '–' : ('$' + r.avg_price.toFixed(2));
    var samples = (r.sample_titles || []).slice(0, 2).map(function (t) {
      return '<span class="t">' + escapeHtml(t) + '</span>';
    }).join('');
    return '' +
      '<tr>' +
      '<td class="shop"><a href="' + etsyUrl(r.shop) + '" target="_blank" rel="noopener">' + escapeHtml(r.shop) + '</a></td>' +
      '<td class="cat">' + escapeHtml(r.top_category || r.shop_category || '—') + '</td>' +
      '<td class="num">' + r.n_listings.toLocaleString('en-US') + '</td>' +
      '<td class="num"><div class="bar-cell">' + r.pct_gift_kw.toFixed(1) + '%<span class="bar-track"><span class="bar-fill" style="width:' + Math.min(r.pct_gift_kw, 100) + '%"></span></span></div></td>' +
      '<td class="num">' + r.pct_customizable.toFixed(1) + '%</td>' +
      '<td class="num">' + r.pct_made_to_order.toFixed(1) + '%</td>' +
      '<td class="num">' + price + '</td>' +
      '<td><span class="pill ' + r.verdict + '">' + VERDICT_LABEL[r.verdict] + '</span></td>' +
      '<td class="note">' + escapeHtml(r.note) + (samples ? '<div style="margin-top:6px;">' + samples + '</div>' : '') + '</td>' +
      '</tr>';
  }

  document.getElementById('tbody-reverse').innerHTML = reverseData.map(reverseRowHtml).join('');

  // ── Chỉ số kinh doanh — shop phù hợp ────────────────────────────────────
  var bizData = JSON.parse(document.getElementById('biz-data').textContent);
  document.getElementById('tab-count-biz').textContent = bizData.length;
  document.getElementById('biz-n-shops').textContent = bizData.length;

  function avgOf(arr, key) {
    var vals = arr.map(function (r) { return r[key]; }).filter(function (v) { return v != null && !isNaN(v); });
    if (!vals.length) return null;
    return vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
  }
  function sumOf(arr, key) {
    var vals = arr.map(function (r) { return r[key]; }).filter(function (v) { return v != null && !isNaN(v); });
    return vals.reduce(function (a, b) { return a + b; }, 0);
  }
  document.getElementById('biz-stat-shops').textContent = bizData.length;
  document.getElementById('biz-stat-revenue30').textContent = '$' + Math.round(sumOf(bizData, 'shop_revenue_30d')).toLocaleString('en-US');
  document.getElementById('biz-stat-sales30').textContent = Math.round(sumOf(bizData, 'shop_sales_30d')).toLocaleString('en-US');
  var avgConv = avgOf(bizData, 'shop_conversion_rate');
  document.getElementById('biz-stat-conv').textContent = avgConv == null ? '–' : avgConv.toFixed(2) + '%';
  var avgRev = avgOf(bizData, 'shop_review_avg');
  document.getElementById('biz-stat-review').textContent = avgRev == null ? '–' : avgRev.toFixed(2) + ' ★';

  function fmtNum(v, opts) {
    if (v == null || isNaN(v)) return '–';
    opts = opts || {};
    if (opts.currency) return '$' + v.toLocaleString('en-US', { maximumFractionDigits: 0 });
    if (opts.pct) return v.toFixed(2) + '%';
    if (opts.decimals != null) return v.toFixed(opts.decimals);
    return Math.round(v).toLocaleString('en-US');
  }

  // Cột kinh doanh — schema-driven: chỉ hiện cột thực có trong file shop-level
  // vừa upload (bizColumns do Python tính, không hardcode danh sách cột).
  var bizColumns = JSON.parse(document.getElementById('biz-columns').textContent);

  var theadBiz = document.getElementById('thead-biz');
  theadBiz.innerHTML = '<tr>' +
    '<th class="sortable" data-key="shop_name" data-type="str">Shop <span class="arrow">↕</span></th>' +
    '<th class="sortable" data-key="shop_category" data-type="str">Ngành hàng <span class="arrow">↕</span></th>' +
    bizColumns.map(function (c, i) {
      return '<th class="sortable" data-key="' + c.key + '" data-type="num"' +
        (i === 0 ? ' data-sort-dir="desc"' : '') + '>' + escapeHtml(c.label) + ' <span class="arrow">↕</span></th>';
    }).join('') +
    '</tr>';

  function bizRowHtml(r) {
    var cells = bizColumns
      .map(function (c) { return '<td class="num">' + fmtNum(r[c.key], c.format) + '</td>'; })
      .join('');
    return '' +
      '<tr data-blob="' + escapeHtml((r.shop_name + ' ' + r.shop_category).toLowerCase()) + '">' +
      '<td class="shop"><a href="' + (r.shop_url || etsyUrl(r.shop_name)) + '" target="_blank" rel="noopener">' + escapeHtml(r.shop_name) + '</a></td>' +
      '<td class="cat">' + escapeHtml(r.shop_category || '—') + '</td>' +
      cells +
      '</tr>';
  }

  var tbodyBiz = document.getElementById('tbody-biz');
  var bizSortKey = bizColumns.length ? bizColumns[0].key : 'shop_name';
  var bizSortDir = 'desc';
  var bizSearchTerm = '';

  function renderBiz() {
    var rows = bizData.filter(function (r) {
      return !bizSearchTerm || (r.shop_name + ' ' + r.shop_category).toLowerCase().indexOf(bizSearchTerm) !== -1;
    });
    rows.sort(function (a, b) {
      var av = a[bizSortKey], bv = b[bizSortKey];
      if (typeof av === 'string' || typeof bv === 'string') {
        av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase();
        return bizSortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      av = av == null ? -Infinity : av; bv = bv == null ? -Infinity : bv;
      return bizSortDir === 'asc' ? av - bv : bv - av;
    });
    tbodyBiz.innerHTML = rows.map(bizRowHtml).join('');
    document.getElementById('biz-count-line').textContent = 'Hiển thị ' + rows.length + ' / ' + bizData.length + ' shop phù hợp';
  }
  renderBiz();

  document.getElementById('biz-search').addEventListener('input', function (e) {
    bizSearchTerm = e.target.value.trim().toLowerCase();
    renderBiz();
  });

  document.querySelectorAll('#biz-table th.sortable').forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.getAttribute('data-key');
      if (bizSortKey === key) {
        bizSortDir = bizSortDir === 'asc' ? 'desc' : 'asc';
      } else {
        bizSortKey = key;
        bizSortDir = th.getAttribute('data-type') === 'str' ? 'asc' : 'desc';
      }
      document.querySelectorAll('#biz-table th.sortable').forEach(function (t) { t.removeAttribute('data-sort-dir'); });
      th.setAttribute('data-sort-dir', bizSortDir);
      renderBiz();
    });
  });

  // ── Top bán chạy & tiềm năng (Heyetsy) ──────────────────────────────────
  var top10Data = JSON.parse(document.getElementById('top10-data').textContent);
  var top10Meta = JSON.parse(document.getElementById('top10-meta').textContent);
  document.getElementById('tab-count-top10').textContent = top10Data.length;
  if (!top10Meta.available) {
    document.getElementById('top10-empty-state').style.display = 'block';
    document.getElementById('top10-content').style.display = 'none';
  }

  var tooltipEl = document.getElementById('chart-tooltip');
  function showTooltip(evt, html) {
    tooltipEl.innerHTML = html;
    tooltipEl.classList.add('visible');
    positionTooltip(evt);
  }
  function positionTooltip(evt) {
    var pad = 14;
    var x = evt.clientX + pad;
    var y = evt.clientY + pad;
    var rect = tooltipEl.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
    if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
    tooltipEl.style.left = x + 'px';
    tooltipEl.style.top = y + 'px';
  }
  function hideTooltip() { tooltipEl.classList.remove('visible'); }

  function fmtInt(v) { return v == null ? '–' : Math.round(v).toLocaleString('en-US'); }
  function fmtPct(v) { return v == null ? '–' : (v > 0 ? '+' : '') + v.toFixed(1) + '%'; }

  var VOLATILITY_LABEL = {
    stable: 'Ổn định', moderate: 'Dao động vừa', volatile: 'Đột biến',
    no_sales: 'Không có sales', no_data: 'Không đủ dữ liệu',
  };
  var VOLATILITY_PILL = {
    stable: 'good', moderate: 'review', volatile: 'no', no_sales: 'filtered', no_data: 'filtered',
  };

  function trendHtml(r) {
    if (r.volatility === 'no_sales') return '<span style="color:var(--muted)">–</span>';
    if (r.trend_pct == null) return '<span style="color:var(--muted)">–</span>';
    var color = r.trend_pct > 5 ? 'var(--good)' : r.trend_pct < -5 ? 'var(--critical)' : 'var(--ink-soft)';
    var arrow = r.trend_pct > 5 ? '▲' : r.trend_pct < -5 ? '▼' : '→';
    return '<span style="color:' + color + '; font-weight:600;">' + arrow + ' ' + fmtPct(r.trend_pct) + '</span>';
  }

  function sparklineSvg(shop, values) {
    if (!values || !values.length) return '<span style="color:var(--muted)">–</span>';
    var w = 100, h = 26, pad = 2;
    var maxV = Math.max.apply(null, values.concat([0]));
    var n = values.length;
    var pts = values.map(function (v, idx) {
      var x = n === 1 ? w / 2 : pad + (idx / (n - 1)) * (w - 2 * pad);
      var y = maxV ? h - pad - (v / maxV) * (h - 2 * pad) : h - pad;
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    return '<svg class="spark" data-shop="' + escapeHtml(shop) + '" width="' + w + '" height="' + h +
      '" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
      '<polyline points="' + pts + '" fill="none" stroke="var(--chart-1)" stroke-width="1.5" ' +
      'vector-effect="non-scaling-stroke" /></svg>';
  }

  function top10RowHtml(r) {
    return '' +
      '<tr>' +
      '<td class="num">' + r.rank + '</td>' +
      '<td class="shop"><a href="' + (r.shop_url || etsyUrl(r.shop)) + '" target="_blank" rel="noopener">' + escapeHtml(r.shop) + '</a></td>' +
      '<td class="cat">' + escapeHtml(r.shop_category || '—') + '</td>' +
      '<td class="num">' + sparklineSvg(r.shop, r.daily_series) + '</td>' +
      '<td class="num">' + trendHtml(r) + '</td>' +
      '<td><span class="pill ' + (VOLATILITY_PILL[r.volatility] || 'filtered') + '">' +
        (VOLATILITY_LABEL[r.volatility] || '–') + '</span></td>' +
      '<td class="num">' + fmtInt(r.avg_weekly_sold) + '</td>' +
      '<td class="num">' + fmtInt(r.sold_this_month) + '</td>' +
      '<td class="num">' + fmtInt(r.sold_last_month) + '</td>' +
      '<td class="num">' + fmtInt(r.this_week_sold) + '</td>' +
      '<td class="num">' + fmtInt(r.last_week_sold) + '</td>' +
      '<td class="num">' + fmtInt(r.today_sold) + '</td>' +
      '<td class="num">' + (r.avg_daily_sold == null ? '–' : r.avg_daily_sold.toFixed(1)) + '</td>' +
      '<td class="num">' + (r.avg_weekly_favorites == null ? '–' : r.avg_weekly_favorites.toFixed(1)) + '</td>' +
      '<td class="num">' + fmtInt(r.reviews_this_month) + '</td>' +
      '</tr>';
  }
  document.getElementById('tbody-top10').innerHTML = top10Data.map(top10RowHtml).join('');

  document.querySelectorAll('.spark').forEach(function (el) {
    var r = top10Data.filter(function (d) { return d.shop === el.getAttribute('data-shop'); })[0];
    el.addEventListener('pointermove', function (evt) {
      var n = (r.daily_series || []).length;
      showTooltip(evt, '' +
        '<span class="tt-title">' + escapeHtml(r.shop) + ' — report1m</span>' +
        '<div class="tt-row"><span>Số điểm dữ liệu</span><strong>' + n + '</strong></div>' +
        '<div class="tt-row"><span>Xu hướng</span><strong>' + (r.trend_pct == null ? '–' : fmtPct(r.trend_pct)) + '</strong></div>' +
        '<div class="tt-row"><span>Biến động</span><strong>' + (VOLATILITY_LABEL[r.volatility] || '–') + '</strong></div>');
    });
    el.addEventListener('pointerleave', hideTooltip);
  });

  // Chart 1 — nhịp bán trung bình mỗi tuần (single series bar)
  (function () {
    var container = document.getElementById('chart-top10-bars');
    var maxVal = Math.max.apply(null, top10Data.map(function (r) { return r.avg_weekly_sold || 0; }));
    container.innerHTML = top10Data.map(function (r) {
      var pct = maxVal ? (r.avg_weekly_sold / maxVal * 100) : 0;
      return '' +
        '<div class="hbar-row">' +
        '<div class="hbar-label"><span class="hbar-rank">' + r.rank + '</span>' + escapeHtml(r.shop) + '</div>' +
        '<div class="hbar-track"><div class="hbar-fill" data-shop="' + escapeHtml(r.shop) + '" style="width:' + pct.toFixed(1) + '%"></div></div>' +
        '<div class="hbar-value">' + fmtInt(r.avg_weekly_sold) + '/tuần</div>' +
        '</div>';
    }).join('');

    container.querySelectorAll('.hbar-fill').forEach(function (el) {
      var r = top10Data.filter(function (d) { return d.shop === el.getAttribute('data-shop'); })[0];
      el.addEventListener('pointermove', function (evt) {
        showTooltip(evt, '' +
          '<span class="tt-title">' + escapeHtml(r.shop) + '</span>' +
          '<div class="tt-row"><span>TB/tuần</span><strong>' + fmtInt(r.avg_weekly_sold) + '</strong></div>' +
          '<div class="tt-row"><span>Tháng này / tháng trước</span><strong>' + fmtInt(r.sold_this_month) + ' / ' + fmtInt(r.sold_last_month) + '</strong></div>' +
          '<div class="tt-row"><span>TB/ngày</span><strong>' + (r.avg_daily_sold == null ? '–' : r.avg_daily_sold.toFixed(1)) + '</strong></div>');
      });
      el.addEventListener('pointerleave', hideTooltip);
    });
  })();

  // Chart 2 — tuần trước vs tuần này (grouped bars)
  (function () {
    var container = document.getElementById('chart-top10-growth');
    var maxVal = Math.max.apply(null, top10Data.map(function (r) {
      return Math.max(r.this_week_sold || 0, r.last_week_sold || 0);
    }));
    container.innerHTML = top10Data.map(function (r) {
      var pctPrev = maxVal ? ((r.last_week_sold || 0) / maxVal * 100) : 0;
      var pctCurr = maxVal ? ((r.this_week_sold || 0) / maxVal * 100) : 0;
      return '' +
        '<div class="growth-group">' +
        '<div class="hbar-label"><span class="hbar-rank">' + r.rank + '</span>' + escapeHtml(r.shop) + '</div>' +
        '<div class="growth-track">' +
        '<div class="growth-bar s1" data-shop="' + escapeHtml(r.shop) + '" style="width:' + pctPrev.toFixed(1) + '%"></div>' +
        '<div class="growth-bar s2" data-shop="' + escapeHtml(r.shop) + '" style="width:' + pctCurr.toFixed(1) + '%"></div>' +
        '</div>' +
        '<div class="growth-value">' + fmtInt(r.last_week_sold) + ' → ' + fmtInt(r.this_week_sold) + ' (' + fmtPct(r.week_growth_pct) + ')</div>' +
        '</div>';
    }).join('');

    container.querySelectorAll('.growth-bar').forEach(function (el) {
      var r = top10Data.filter(function (d) { return d.shop === el.getAttribute('data-shop'); })[0];
      el.addEventListener('pointermove', function (evt) {
        showTooltip(evt, '' +
          '<span class="tt-title">' + escapeHtml(r.shop) + '</span>' +
          '<div class="tt-row"><span>Tuần trước</span><strong>' + fmtInt(r.last_week_sold) + '</strong></div>' +
          '<div class="tt-row"><span>Tuần này (chưa hết tuần)</span><strong>' + fmtInt(r.this_week_sold) + '</strong></div>' +
          '<div class="tt-row"><span>TB/ngày (ổn định)</span><strong>' + (r.avg_daily_sold == null ? '–' : r.avg_daily_sold.toFixed(1)) + '</strong></div>' +
          '<div class="tt-row"><span>Hôm nay</span><strong>' + fmtInt(r.today_sold) + '</strong></div>');
      });
      el.addEventListener('pointerleave', hideTooltip);
    });
  })();

  // ── Top 50 sản phẩm (listing-level, không phải shop) ────────────────────
  var productsData = JSON.parse(document.getElementById('products-data').textContent);
  // review_per_age không có sẵn trong data server trả về — suy ra 1 lần ở đây
  // (est_reviews / listing_age, tính bằng tháng) để renderProductsTab dùng
  // chung logic r[metricKey] như 3 metric gốc còn lại.
  productsData.forEach(function (r) {
    r.review_per_age = (r.est_reviews != null && r.listing_age != null && r.listing_age > 0)
      ? r.est_reviews / r.listing_age : null;
  });
  var PRODUCT_METRIC_LABELS = {
    views: 'Views', est_reviews: 'Ước tính reviews', favorites: 'Favorites', review_per_age: 'Review/Age',
  };
  var PRODUCT_METRIC_FORMAT = {
    review_per_age: function (v) { return v == null ? '–' : v.toFixed(2); },
  };
  function fmtMetric(v, metricKey) {
    return (PRODUCT_METRIC_FORMAT[metricKey] || fmtInt)(v);
  }
  // Thứ tự cố định (categorical, KHÔNG cycle) — khớp palette đã validate trong
  // dataviz skill; quá 7 ngành gộp chung vào "Khác" (màu --muted) thay vì sinh
  // thêm hue mới.
  var CATEGORY_PALETTE = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)',
                          'var(--chart-5)', 'var(--chart-6)', 'var(--chart-7)'];
  var CATEGORY_OTHER_COLOR = 'var(--muted)';
  var CATEGORY_MAX_SLICES = 7;
  var PRODUCT_TOP_N = 50;

  function productRowHtml(r, idx, metricKey) {
    return '' +
      '<tr>' +
      '<td class="num">' + (idx + 1) + '</td>' +
      '<td class="shop"><a href="' + (r.etsy_url || '#') + '" target="_blank" rel="noopener">' + escapeHtml(r.title || '—') + '</a></td>' +
      '<td class="shop"><a href="' + etsyUrl(r.shop) + '" target="_blank" rel="noopener">' + escapeHtml(r.shop) + '</a></td>' +
      '<td class="cat">' + escapeHtml(r.category || '—') + '</td>' +
      '<td class="num">' + fmtMetric(r[metricKey], metricKey) + '</td>' +
      '</tr>';
  }

  function renderCategoryPie(topN) {
    var counts = {};
    topN.forEach(function (r) {
      var cat = r.category || 'Không rõ';
      counts[cat] = (counts[cat] || 0) + 1;
    });
    var entries = Object.keys(counts).map(function (k) { return { category: k, count: counts[k] }; });
    entries.sort(function (a, b) { return b.count - a.count; });

    var main = entries.slice(0, CATEGORY_MAX_SLICES);
    var rest = entries.slice(CATEGORY_MAX_SLICES);
    if (rest.length) {
      var restCount = rest.reduce(function (s, e) { return s + e.count; }, 0);
      main.push({ category: 'Khác (' + rest.length + ' ngành)', count: restCount, isOther: true });
    }

    var total = main.reduce(function (s, e) { return s + e.count; }, 0);
    var svg = document.getElementById('chart-product-pie');
    var legend = document.getElementById('product-pie-legend');
    if (!total) {
      svg.innerHTML = '';
      legend.innerHTML = '';
      return;
    }

    var r = 80, cx = 100, cy = 100, strokeW = 32, gap = 3;
    var circumference = 2 * Math.PI * r;
    var offset = 0;
    var parts = [];
    main.forEach(function (e, i) {
      var color = e.isOther ? CATEGORY_OTHER_COLOR : (CATEGORY_PALETTE[i] || CATEGORY_OTHER_COLOR);
      var frac = e.count / total;
      var segLen = Math.max(frac * circumference - gap, 0);
      parts.push(
        '<circle cx="' + cx + '" cy="' + cy + '" r="' + r + '" fill="none" stroke="' + color + '" ' +
        'stroke-width="' + strokeW + '" stroke-dasharray="' + segLen.toFixed(2) + ' ' + (circumference - segLen).toFixed(2) + '" ' +
        'stroke-dashoffset="' + (-offset).toFixed(2) + '" data-cat="' + escapeHtml(e.category) + '" data-count="' + e.count + '" class="pie-slice" />'
      );
      offset += frac * circumference;
    });
    // rotate -90deg: bắt đầu lát cắt đầu tiên ở đỉnh (12h) thay vì mặc định 3h.
    svg.innerHTML = '<g transform="rotate(-90 ' + cx + ' ' + cy + ')">' + parts.join('') + '</g>';

    svg.querySelectorAll('.pie-slice').forEach(function (el) {
      el.addEventListener('pointermove', function (evt) {
        var cat = el.getAttribute('data-cat');
        var count = +el.getAttribute('data-count');
        var pct = (count / total * 100).toFixed(1);
        showTooltip(evt, '' +
          '<span class="tt-title">' + escapeHtml(cat) + '</span>' +
          '<div class="tt-row"><span>Số sản phẩm</span><strong>' + count + '</strong></div>' +
          '<div class="tt-row"><span>Tỉ trọng</span><strong>' + pct + '%</strong></div>');
      });
      el.addEventListener('pointerleave', hideTooltip);
    });

    legend.innerHTML = main.map(function (e, i) {
      var color = e.isOther ? CATEGORY_OTHER_COLOR : (CATEGORY_PALETTE[i] || CATEGORY_OTHER_COLOR);
      var pct = (e.count / total * 100).toFixed(1);
      return '<span class="legend-item"><span class="swatch" style="background:' + color + '"></span>' +
        escapeHtml(e.category) + ' — ' + e.count + ' (' + pct + '%)</span>';
    }).join('');
  }

  function renderProductsTab(metricKey) {
    var ranked = productsData
      .filter(function (r) { return r[metricKey] != null; })
      .slice()
      .sort(function (a, b) { return b[metricKey] - a[metricKey]; });
    var topN = ranked.slice(0, PRODUCT_TOP_N);
    document.getElementById('tab-count-products').textContent = topN.length;
    document.getElementById('product-metric-th').textContent = PRODUCT_METRIC_LABELS[metricKey] || metricKey;
    document.getElementById('tbody-products').innerHTML = topN.map(function (r, idx) {
      return productRowHtml(r, idx, metricKey);
    }).join('');
    renderCategoryPie(topN);
  }

  (function () {
    var select = document.getElementById('product-metric');
    if (!productsData.length) {
      document.getElementById('products-empty-state').style.display = 'block';
      document.getElementById('products-content').style.display = 'none';
      document.getElementById('tab-count-products').textContent = 0;
      return;
    }
    renderProductsTab(select.value);
    select.addEventListener('change', function () { renderProductsTab(select.value); });
  })();

  // ── Tìm theo từ khoá (toàn bộ listing, mọi shop — không lọc theo verdict) ──
  var keywordRows = JSON.parse(document.getElementById('keyword-listing-data').textContent);
  var keywordBizRows = JSON.parse(document.getElementById('keyword-biz-data').textContent);
  var keywordShopTotals = {};
  keywordRows.forEach(function (r) {
    keywordShopTotals[r.shop] = (keywordShopTotals[r.shop] || 0) + 1;
  });
  var keywordBizByShop = {};
  keywordBizRows.forEach(function (r) { keywordBizByShop[r.shop_name] = r; });

  function keywordRowMatches(r, kw) {
    return (
      String(r.title || '').toLowerCase().indexOf(kw) !== -1 ||
      String(r.tags || '').toLowerCase().indexOf(kw) !== -1 ||
      String(r.category || '').toLowerCase().indexOf(kw) !== -1
    );
  }

  // Cột kinh doanh dùng LẠI đúng bizColumns (schema-driven, chỉ hiện cột thực
  // có trong file shop-level vừa upload) đã tính cho tab "Chỉ số kinh doanh" —
  // join theo shop_name từ keywordBizRows (không giới hạn nhóm "Phù hợp").
  // shop_age_month/shop_total_sold được kéo lên ngay cột 3-4 (sau Shop/Link
  // shop) theo yêu cầu ưu tiên riêng của tab này — không đụng thứ tự
  // BUSINESS_COLUMNS dùng chung với tab "Chỉ số kinh doanh".
  function bizCol(c) { return { key: c.key, label: c.label, type: 'num', format: c.format }; }
  var KEYWORD_PRIORITY_KEYS = ['shop_age_month', 'shop_total_sold'];
  var keywordPriorityCols = KEYWORD_PRIORITY_KEYS
    .map(function (key) { return bizColumns.filter(function (c) { return c.key === key; })[0]; })
    .filter(Boolean)
    .map(bizCol);
  var keywordOtherBizCols = bizColumns
    .filter(function (c) { return KEYWORD_PRIORITY_KEYS.indexOf(c.key) === -1; })
    .map(bizCol);

  // 'shopUrl' (link shop) CHỈ xuất hiện trong file CSV, không hiện trên bảng
  // trang web (cột Shop trên bảng đã là link rồi) — nên KHÔNG có trong
  // keywordFixedCols/keywordAllCols (dùng để build thead + render bảng),
  // chỉ được chèn thêm ở keywordCsvCols bên dưới.
  var keywordFixedCols = [
    { key: 'shop', label: 'Shop', type: 'str' },
  ].concat(keywordPriorityCols).concat([
    { key: 'topCat', label: 'Ngành hàng chủ đạo', type: 'str' },
    { key: 'count', label: 'Sản phẩm khớp', type: 'num' },
    { key: 'total', label: 'Tổng listing shop', type: 'num' },
    { key: 'pct', label: '% khớp', type: 'num' },
  ]);
  var keywordAllCols = keywordFixedCols.concat(keywordOtherBizCols);
  var keywordCsvCols = [keywordAllCols[0], { key: 'shopUrl', label: 'Link shop', type: 'str' }]
    .concat(keywordAllCols.slice(1));

  // Header tiếng Anh riêng cho file CSV xuất ra (bảng trên trang vẫn giữ nhãn
  // tiếng Việt) — key khớp với keywordCsvCols ở trên.
  var CSV_HEADER_EN = {
    shop: 'Shop', shopUrl: 'Shop URL', topCat: 'Top Category',
    count: 'Matched Products', total: 'Total Listings', pct: 'Match %',
    shop_review_count: 'Reviews', shop_review_avg: 'Avg Review',
    shop_age_month: 'Shop Age (Months)', shop_total_sold: 'Total Sold',
    shop_review_rate: 'Review Rate %', listing_active_count: 'Active Listings',
    shop_favorites: 'Favorites', shop_views: 'Views',
    shop_revenue: 'Lifetime Revenue', shop_revenue_30d: 'Revenue 30D',
    shop_sales_30d: 'Sales 30D', shop_conversion_rate: 'Conversion Rate %',
  };

  var theadKeyword = document.getElementById('thead-keyword');
  theadKeyword.innerHTML = '<tr>' + keywordAllCols.map(function (c) {
    return '<th class="sortable" data-key="' + c.key + '" data-type="' + c.type + '"' +
      (c.key === 'count' ? ' data-sort-dir="desc"' : '') + '>' + escapeHtml(c.label) + ' <span class="arrow">↕</span></th>';
  }).join('') + '</tr>';

  var keywordShopsArr = [];
  var keywordVisibleRows = [];
  var keywordSortKey = 'count';
  var keywordSortDir = 'desc';
  var lastKeyword = '';

  // Render TỪNG cell theo đúng thứ tự keywordAllCols (data-driven) — không
  // hardcode thứ tự cột trong HTML, vì keywordAllCols có thể xếp lại
  // (shop_age_month/shop_total_sold chen giữa Link shop và Ngành hàng).
  function keywordCellHtml(r, c) {
    switch (c.key) {
      case 'shop':
        return '<td class="shop"><a href="' + r.shopUrl + '" target="_blank" rel="noopener">' + escapeHtml(r.shop) + '</a></td>';
      case 'topCat':
        return '<td class="cat">' + escapeHtml(r.topCat) + '</td>';
      case 'count':
        return '<td class="num">' + r.count + '</td>';
      case 'total':
        return '<td class="num">' + r.total + '</td>';
      case 'pct':
        return '<td class="num">' + (r.pct == null ? '–' : r.pct.toFixed(2) + '%') + '</td>';
      default:
        return '<td class="num">' + fmtNum(r[c.key], c.format) + '</td>';
    }
  }

  function keywordRowHtml(r) {
    return '<tr>' + keywordAllCols.map(function (c) { return keywordCellHtml(r, c); }).join('') + '</tr>';
  }

  function renderKeywordTable() {
    var rows = keywordShopsArr.slice();
    rows.sort(function (a, b) {
      var av = a[keywordSortKey], bv = b[keywordSortKey];
      if (typeof av === 'string' || typeof bv === 'string') {
        av = (av || '').toLowerCase(); bv = (bv || '').toLowerCase();
        return keywordSortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
      }
      av = av == null ? -Infinity : av; bv = bv == null ? -Infinity : bv;
      return keywordSortDir === 'asc' ? av - bv : bv - av;
    });
    keywordVisibleRows = rows;
    document.getElementById('tbody-keyword').innerHTML = rows.map(keywordRowHtml).join('');
  }

  document.querySelectorAll('#keyword-table th.sortable').forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.getAttribute('data-key');
      if (keywordSortKey === key) {
        keywordSortDir = keywordSortDir === 'asc' ? 'desc' : 'asc';
      } else {
        keywordSortKey = key;
        keywordSortDir = th.getAttribute('data-type') === 'str' ? 'asc' : 'desc';
      }
      document.querySelectorAll('#keyword-table th.sortable').forEach(function (t) { t.removeAttribute('data-sort-dir'); });
      th.setAttribute('data-sort-dir', keywordSortDir);
      renderKeywordTable();
    });
  });

  function renderKeywordTab(rawKeyword) {
    lastKeyword = rawKeyword.trim();
    var kw = lastKeyword.toLowerCase();
    var idleState = document.getElementById('keyword-idle-state');
    var emptyState = document.getElementById('keyword-empty-state');
    var content = document.getElementById('keyword-content');

    if (!kw) {
      idleState.style.display = 'block';
      emptyState.style.display = 'none';
      content.style.display = 'none';
      keywordShopsArr = [];
      keywordVisibleRows = [];
      document.getElementById('tab-count-keyword').textContent = '–';
      return;
    }
    idleState.style.display = 'none';

    var matched = keywordRows.filter(function (r) { return keywordRowMatches(r, kw); });
    document.getElementById('tab-count-keyword').textContent = matched.length;

    if (!matched.length) {
      emptyState.style.display = 'block';
      content.style.display = 'none';
      keywordShopsArr = [];
      keywordVisibleRows = [];
      return;
    }
    emptyState.style.display = 'none';
    content.style.display = 'block';

    var groups = {};
    matched.forEach(function (r) {
      var g = groups[r.shop];
      if (!g) g = groups[r.shop] = { shop: r.shop, count: 0, catCounts: {} };
      g.count++;
      var cat = r.category || '—';
      g.catCounts[cat] = (g.catCounts[cat] || 0) + 1;
    });

    keywordShopsArr = Object.keys(groups).map(function (k) {
      var g = groups[k];
      var topCat = Object.keys(g.catCounts).sort(function (a, b) { return g.catCounts[b] - g.catCounts[a]; })[0] || '—';
      var total = keywordShopTotals[g.shop] || g.count;
      var row = {
        shop: g.shop, shopUrl: etsyUrl(g.shop), count: g.count, total: total,
        pct: total ? (g.count / total * 100) : null,
        topCat: topCat,
      };
      var biz = keywordBizByShop[g.shop];
      bizColumns.forEach(function (c) { row[c.key] = biz ? biz[c.key] : null; });
      return row;
    });

    document.getElementById('keyword-summary-line').textContent =
      'Tìm thấy ' + matched.length + ' sản phẩm khớp "' + lastKeyword + '" trên ' + keywordShopsArr.length +
      ' shop (trong tổng ' + keywordRows.length + ' listing đã upload).';

    renderKeywordTable();
  }

  function csvEscape(v) {
    var s = v == null ? '' : String(v);
    if (/[",\n\r]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  // Làm tròn tối đa 2 chữ số thập phân — số đã tròn (nguyên) giữ nguyên,
  // không bị thêm ".00" (Number bỏ số 0 thừa khi String() hoá).
  function roundTo2(v) {
    return (typeof v === 'number' && isFinite(v)) ? Math.round(v * 100) / 100 : v;
  }

  function downloadCsv(filename, cols, rows) {
    var lines = [cols.map(function (c) { return csvEscape(CSV_HEADER_EN[c.key] || c.label); }).join(',')];
    rows.forEach(function (r) {
      lines.push(cols.map(function (c) { return csvEscape(roundTo2(r[c.key])); }).join(','));
    });
    var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  document.getElementById('keyword-export-btn').addEventListener('click', function () {
    if (!keywordVisibleRows.length) return;
    var safeKw = lastKeyword.replace(/[^a-z0-9]+/gi, '_').toLowerCase() || 'export';
    downloadCsv('keyword_' + safeKw + '_shops.csv', keywordCsvCols, keywordVisibleRows);
  });

  (function () {
    var input = document.getElementById('keyword-input');
    if (!keywordRows.length) {
      document.getElementById('keyword-idle-state').textContent =
        'Chưa có dữ liệu listing (cần upload kèm file listing-level) nên chưa thể tìm theo từ khoá.';
    }
    renderKeywordTab(input.value);
    input.addEventListener('input', function () { renderKeywordTab(input.value); });
  })();

  // ── Tab switching ────────────────────────────────────────────────────────
  var tabButtons = document.querySelectorAll('.tab-btn');
  var panels = {
    main: 'tab-panel-main', reverse: 'tab-panel-reverse', biz: 'tab-panel-biz',
    top10: 'tab-panel-top10', products: 'tab-panel-products', keyword: 'tab-panel-keyword',
  };
  tabButtons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = btn.getAttribute('data-tab');
      tabButtons.forEach(function (b) { b.setAttribute('aria-selected', b === btn ? 'true' : 'false'); });
      Object.keys(panels).forEach(function (key) {
        document.getElementById(panels[key]).setAttribute('data-active', key === target ? 'true' : 'false');
      });
    });
  });
})();
</script>
"""


if __name__ == "__main__":
    main()
