"""
Crawl shop + listing từ Everbee cho 1 list shop trong config/shop_lists.json.

    scrape --list shop_build            # resume nếu có checkpoint
    scrape --list shop_names --fresh    # bỏ checkpoint cũ, crawl lại từ đầu

Kết quả:
    data/output/<YYYYmmdd_HHMMSS>_<list>/shops.csv|xlsx, listings.csv|xlsx
    data/everbee.duckdb (1 run = 1 snapshot)
"""
import argparse
import json
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

from analyze_shop import settings
from analyze_shop.log import ckpt, done, err, info, stop, warn
from analyze_shop.scraper.everbee_client import (
    LISTING_COLUMN_ORDER, SHOP_COLUMN_ORDER, TokenExpired, build_session, scrape_shop,
)
from analyze_shop.scraper.storage_duckdb import save_to_duckdb

CHECKPOINT_EVERY = 5      # flush mỗi N shops

EXIT_TOKEN_EXPIRED = 2


# ── Checkpoint ────────────────────────────────────────────────────────────────
class Checkpoint:
    def __init__(self, list_name: str):
        settings.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        self.shops_path    = settings.CHECKPOINT_DIR / f"{list_name}_shops.csv"
        self.listings_path = settings.CHECKPOINT_DIR / f"{list_name}_listings.csv"
        self.done_shops: List[str] = []
        self.shop_rows: List[Dict] = []
        self.listing_rows: List[Dict] = []

    def exists(self) -> bool:
        return self.shops_path.exists() or self.listings_path.exists()

    def load(self) -> None:
        if self.shops_path.exists():
            df_s = pd.read_csv(self.shops_path, dtype=str, encoding="utf-8-sig").fillna("")
            self.shop_rows = df_s.to_dict("records")
        if self.listings_path.exists():
            df_l = pd.read_csv(self.listings_path, dtype=str, encoding="utf-8-sig").fillna("")
            self.listing_rows = df_l.to_dict("records")
        # Shop đã crawl = có dòng shop hoặc có listing
        names = [r.get("shop_name", "") for r in self.shop_rows + self.listing_rows]
        self.done_shops = list(dict.fromkeys(n for n in names if n))
        ckpt(f"Loaded: {len(self.done_shops)} shops, {len(self.listing_rows)} listings.")

    def clear(self) -> None:
        for p in (self.shops_path, self.listings_path):
            p.unlink(missing_ok=True)

    @staticmethod
    def _flush(rows: List[Dict], path: Path) -> None:
        if not rows:
            return
        tmp = path.with_suffix(".tmp")
        pd.DataFrame(rows).to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(path)
        ckpt(f"Flush {len(rows)} records → {path}")

    def flush(self) -> None:
        self._flush(self.shop_rows, self.shops_path)
        self._flush(self.listing_rows, self.listings_path)


def _install_signal_handlers(cp: Checkpoint) -> None:
    # SIGINT: Ctrl+C. SIGTERM: docker stop / docker compose down.
    def _handler(sig, frame):
        stop("Bị dừng — flush checkpoint rồi thoát...")
        cp.flush()
        sys.exit(130)
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


# ── Crawl ─────────────────────────────────────────────────────────────────────
def _ordered(rows: List[Dict], order: List[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    cols = [c for c in order if c in df.columns]
    return df[cols + [c for c in df.columns if c not in cols]]


def crawl(list_name: str, shop_names: List[str], resume: bool,
          delay_between_shops: tuple = (0.5, 1)) -> int:
    started_at = datetime.now()

    if not settings.AUTH_TOKEN:
        err("Chưa có AUTH_TOKEN. Điền vào file .env (xem README, mục 'Lấy token Everbee').")
        return 1

    cp = Checkpoint(list_name)
    if cp.exists():
        if resume:
            cp.load()
        else:
            cp.clear()
            info("Bắt đầu mới — đã xoá checkpoint cũ.")
    _install_signal_handlers(cp)

    session   = build_session(settings.AUTH_TOKEN)
    remaining = [s for s in shop_names if s not in cp.done_shops]
    info(f"List '{list_name}': {len(shop_names)} shops — còn lại {len(remaining)} shops cần crawl.")

    shops_since_flush = 0
    for i, shop_name in enumerate(remaining, 1):
        info(f"[{i}/{len(remaining)}] Crawl shop: '{shop_name}'")
        try:
            shop_row, listing_rows = scrape_shop(session, shop_name)
        except TokenExpired:
            cp.flush()
            err("Token hết hạn (401). Cập nhật AUTH_TOKEN trong .env rồi chạy lại cùng lệnh để resume.")
            return EXIT_TOKEN_EXPIRED

        if shop_row:
            cp.shop_rows.append(shop_row)
        cp.listing_rows.extend(listing_rows)
        cp.done_shops.append(shop_name)
        done(f"  '{shop_name}': {len(listing_rows)} listings")

        shops_since_flush += 1
        if shops_since_flush >= CHECKPOINT_EVERY:
            cp.flush()
            shops_since_flush = 0

        time.sleep(random.uniform(*delay_between_shops))

    cp.flush()

    df_shops    = _ordered(cp.shop_rows,    SHOP_COLUMN_ORDER)
    df_listings = _ordered(cp.listing_rows, LISTING_COLUMN_ORDER)

    # ── Output: CSV + Excel (2 file CSV dùng được ngay cho analyzer --analyze-dir)
    out_dir = settings.OUTPUT_DIR / f"{started_at:%Y%m%d_%H%M%S}_{list_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, df in (("shops", df_shops), ("listings", df_listings)):
        df.to_csv(out_dir / f"{name}.csv", index=False, encoding="utf-8-sig")
        df.to_excel(out_dir / f"{name}.xlsx", index=False)
    done(f"Output → {out_dir} ({len(df_shops)} shops, {len(df_listings)} listings)")

    # ── DuckDB — lỗi thì giữ checkpoint để chạy lại
    run_id = save_to_duckdb(df_shops, df_listings, list_name, shop_names, started_at)
    done(f"DuckDB → {settings.DB_PATH} (run_id={run_id})")

    cp.clear()
    ckpt("Checkpoint đã xoá.")
    return 0


def load_shop_lists() -> Dict[str, List[str]]:
    with open(settings.SHOP_LISTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    lists = load_shop_lists()
    parser = argparse.ArgumentParser(description="Crawl shop + listing từ Everbee")
    parser.add_argument("--list", dest="list_name", required=True, choices=sorted(lists),
                        help=f"Tên list trong {settings.SHOP_LISTS_JSON.name}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--resume", action="store_true", default=True,
                       help="Tiếp tục từ checkpoint nếu có (mặc định)")
    group.add_argument("--fresh", action="store_true",
                       help="Bỏ checkpoint cũ, crawl lại từ đầu")
    args = parser.parse_args()

    shop_names = [s.strip() for s in lists[args.list_name] if s.strip()]
    sys.exit(crawl(args.list_name, shop_names, resume=not args.fresh))


if __name__ == "__main__":
    main()
