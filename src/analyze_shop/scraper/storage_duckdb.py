"""
Lưu dữ liệu Everbee shop/listing vào DuckDB theo dạng snapshot.

- shops / listings                 : thông tin ít thay đổi (upsert, giữ bản mới nhất)
- shop_snapshots / listing_snapshots: chỉ số theo từng lần crawl (1 run = 1 snapshot)
- crawl_runs                       : mỗi lần chạy scraper
- shop_lists                       : shop thuộc list nào (shop_build / shop_names)
"""
from datetime import datetime
from pathlib import Path
from typing import List

import duckdb
import pandas as pd

from analyze_shop.settings import DB_PATH

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS run_id_seq START 1;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id        INTEGER PRIMARY KEY DEFAULT nextval('run_id_seq'),
    shop_list     VARCHAR NOT NULL,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    shop_count    INTEGER,
    listing_count INTEGER
);

CREATE TABLE IF NOT EXISTS shops (
    shop_id        BIGINT PRIMARY KEY,
    shop_name      VARCHAR NOT NULL,
    shop_url       VARCHAR,
    shop_logo_url  VARCHAR,
    ship_from      VARCHAR,
    shop_type      VARCHAR,
    shop_category  VARCHAR,
    is_deactivated BOOLEAN,
    first_seen_at  TIMESTAMP,
    last_seen_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop_snapshots (
    run_id                 INTEGER,
    shop_id                BIGINT,
    scraped_at             TIMESTAMP NOT NULL,
    shop_age_month         INTEGER,
    listing_active_count   INTEGER,
    shop_review_count      INTEGER,
    shop_review_avg        DECIMAL(3,2),
    shop_review_rate       DOUBLE,
    shop_total_sold        INTEGER,
    shop_favorites         INTEGER,
    shop_views             BIGINT,
    shop_revenue           DECIMAL(14,2),
    shop_revenue_30d       DECIMAL(14,2),
    shop_sales_30d         INTEGER,
    shop_conversion_rate   DOUBLE,
    shop_avg_listing_price DECIMAL(10,2),
    shop_sale_per_listing  DOUBLE,
    PRIMARY KEY (run_id, shop_id)
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id      BIGINT PRIMARY KEY,
    shop_id         BIGINT,
    title           VARCHAR,
    etsy_url        VARCHAR,
    image_url       VARCHAR,
    tags            VARCHAR[],
    category        VARCHAR,
    sub_category    VARCHAR,
    listing_type    VARCHAR,
    ship_from       VARCHAR,
    when_made       VARCHAR,
    who_made        VARCHAR,
    is_customizable BOOLEAN,
    has_variations  BOOLEAN,
    first_seen_at   TIMESTAMP,
    last_seen_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    run_id           INTEGER,
    listing_id       BIGINT,
    scraped_at       TIMESTAMP NOT NULL,
    price            DECIMAL(10,2),
    state            VARCHAR,
    listing_age      INTEGER,
    views            INTEGER,
    favorites        INTEGER,
    avg_reviews      DOUBLE,
    est_reviews      INTEGER,
    visibility_score INTEGER,
    mo_sales         INTEGER,
    mo_revenue       DECIMAL(14,2),
    total_sales_est  INTEGER,
    growth_rate      DOUBLE,
    PRIMARY KEY (run_id, listing_id)
);

CREATE TABLE IF NOT EXISTS shop_lists (
    list_name VARCHAR,
    shop_name VARCHAR,
    added_at  TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (list_name, shop_name)
);

CREATE OR REPLACE VIEW v_shop_latest AS
SELECT s.*, ss.* EXCLUDE (shop_id)
FROM shops s
JOIN shop_snapshots ss USING (shop_id)
QUALIFY row_number() OVER (PARTITION BY s.shop_id ORDER BY ss.scraped_at DESC) = 1;

CREATE OR REPLACE VIEW v_listing_growth AS
SELECT listing_id, run_id, scraped_at, views, favorites,
       views     - lag(views)     OVER w AS views_delta,
       favorites - lag(favorites) OVER w AS fav_delta,
       date_diff('day', lag(scraped_at) OVER w, scraped_at) AS days_between
FROM listing_snapshots
WINDOW w AS (PARTITION BY listing_id ORDER BY scraped_at);
"""

# Cột cần có trong DataFrame trước khi nạp (thiếu thì thêm rỗng)
_SHOP_COLS = [
    "shop_name", "shop_id", "shop_url", "shop_logo_url", "ship_from", "shop_type",
    "shop_category", "is_deactivated", "shop_age_month", "listing_active_count",
    "shop_review_count", "shop_review_avg", "shop_review_rate", "shop_total_sold",
    "shop_favorites", "shop_views", "shop_revenue", "shop_revenue_30d", "shop_sales_30d",
    "shop_conversion_rate", "shop_avg_listing_price", "shop_sale_per_listing",
]
_LISTING_COLS = [
    "shop_name", "listing_id", "title", "etsy_url", "image_url", "tags", "category",
    "sub_category", "listing_type", "ship_from", "when_made", "who_made",
    "is_customizable", "has_variations", "price", "state", "listing_age", "views",
    "favorites", "avg_reviews", "est_reviews", "visibility_score",
    "mo_sales", "mo_revenue", "total_sales_est", "growth_rate",
]


def _c(col: str, typ: str) -> str:
    """'' / 'nan' → NULL, ép kiểu an toàn."""
    return f"TRY_CAST(NULLIF(NULLIF(trim({col}), ''), 'nan') AS {typ})"


def _prepare(df: pd.DataFrame, cols: List[str]) -> pd.DataFrame:
    df = df.copy() if not df.empty else pd.DataFrame(columns=cols)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols].fillna("").astype(str)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_SQL)


def save_to_duckdb(
    df_shops: pd.DataFrame,
    df_listings: pd.DataFrame,
    shop_list: str,
    list_shop_names: List[str],
    started_at: datetime,
    db_path: Path = DB_PATH,
) -> int:
    """Ghi 1 lần crawl vào DuckDB. Trả về run_id."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    finished_at = datetime.now()

    shops_df    = _prepare(df_shops, _SHOP_COLS).drop_duplicates("shop_id", keep="last")
    listings_df = _prepare(df_listings, _LISTING_COLS).drop_duplicates("listing_id", keep="last")
    lists_df    = pd.DataFrame({"list_name": shop_list, "shop_name": list_shop_names})

    con = duckdb.connect(str(db_path))
    try:
        init_schema(con)
        con.register("src_shops", shops_df)
        con.register("src_listings", listings_df)
        con.register("src_lists", lists_df)
        con.execute("BEGIN TRANSACTION")

        run_id = con.execute(
            """
            INSERT INTO crawl_runs (shop_list, started_at, finished_at, shop_count, listing_count)
            VALUES (?, ?, ?, ?, ?) RETURNING run_id
            """,
            [shop_list, started_at, finished_at, len(shops_df), len(listings_df)],
        ).fetchone()[0]
        params = {"run_id": run_id, "ts": started_at}
        ts_only = {"ts": started_at}

        con.execute(
            "INSERT INTO shop_lists (list_name, shop_name) SELECT * FROM src_lists ON CONFLICT DO NOTHING"
        )

        # ── shops (upsert) ────────────────────────────────────────────────────
        con.execute(
            f"""
            INSERT INTO shops
            SELECT {_c('shop_id', 'BIGINT')}, shop_name, NULLIF(shop_url, ''),
                   NULLIF(shop_logo_url, ''), NULLIF(ship_from, ''), NULLIF(shop_type, ''),
                   NULLIF(shop_category, ''), {_c('is_deactivated', 'BOOLEAN')},
                   $ts, $ts
            FROM src_shops
            WHERE {_c('shop_id', 'BIGINT')} IS NOT NULL
            ON CONFLICT (shop_id) DO UPDATE SET
                shop_name      = excluded.shop_name,
                shop_url       = excluded.shop_url,
                shop_logo_url  = excluded.shop_logo_url,
                ship_from      = excluded.ship_from,
                shop_type      = excluded.shop_type,
                shop_category  = excluded.shop_category,
                is_deactivated = excluded.is_deactivated,
                last_seen_at   = excluded.last_seen_at
            """,
            ts_only,
        )

        con.execute(
            f"""
            INSERT INTO shop_snapshots
            SELECT $run_id, {_c('shop_id', 'BIGINT')}, $ts,
                   {_c('shop_age_month', 'INTEGER')}, {_c('listing_active_count', 'INTEGER')},
                   {_c('shop_review_count', 'INTEGER')}, {_c('shop_review_avg', 'DECIMAL(3,2)')},
                   {_c('shop_review_rate', 'DOUBLE')}, {_c('shop_total_sold', 'INTEGER')},
                   {_c('shop_favorites', 'INTEGER')}, {_c('shop_views', 'BIGINT')},
                   {_c('shop_revenue', 'DECIMAL(14,2)')}, {_c('shop_revenue_30d', 'DECIMAL(14,2)')},
                   {_c('shop_sales_30d', 'INTEGER')}, {_c('shop_conversion_rate', 'DOUBLE')},
                   {_c('shop_avg_listing_price', 'DECIMAL(10,2)')}, {_c('shop_sale_per_listing', 'DOUBLE')}
            FROM src_shops
            WHERE {_c('shop_id', 'BIGINT')} IS NOT NULL
            """,
            params,
        )

        # ── listings (upsert) — shop_id lấy theo shop_name (không phân biệt hoa thường)
        con.execute(
            f"""
            INSERT INTO listings
            SELECT {_c('l.listing_id', 'BIGINT')}, s.shop_id,
                   NULLIF(l.title, ''), NULLIF(l.etsy_url, ''), NULLIF(l.image_url, ''),
                   list_filter(regexp_split_to_array(l.tags, '\\s*[;,]\\s*'), t -> trim(t) <> ''),
                   NULLIF(l.category, ''), NULLIF(l.sub_category, ''), NULLIF(l.listing_type, ''),
                   NULLIF(l.ship_from, ''), NULLIF(l.when_made, ''), NULLIF(l.who_made, ''),
                   {_c('l.is_customizable', 'BOOLEAN')}, {_c('l.has_variations', 'BOOLEAN')},
                   $ts, $ts
            FROM src_listings l
            LEFT JOIN shops s ON lower(s.shop_name) = lower(l.shop_name)
            WHERE {_c('l.listing_id', 'BIGINT')} IS NOT NULL
            ON CONFLICT (listing_id) DO UPDATE SET
                shop_id         = excluded.shop_id,
                title           = excluded.title,
                etsy_url        = excluded.etsy_url,
                image_url       = excluded.image_url,
                tags            = excluded.tags,
                category        = excluded.category,
                sub_category    = excluded.sub_category,
                listing_type    = excluded.listing_type,
                ship_from       = excluded.ship_from,
                when_made       = excluded.when_made,
                who_made        = excluded.who_made,
                is_customizable = excluded.is_customizable,
                has_variations  = excluded.has_variations,
                last_seen_at    = excluded.last_seen_at
            """,
            ts_only,
        )

        con.execute(
            f"""
            INSERT INTO listing_snapshots
            SELECT $run_id, {_c('listing_id', 'BIGINT')}, $ts,
                   {_c('price', 'DECIMAL(10,2)')}, NULLIF(state, ''),
                   {_c('listing_age', 'INTEGER')}, {_c('views', 'INTEGER')},
                   {_c('favorites', 'INTEGER')}, {_c('avg_reviews', 'DOUBLE')},
                   {_c('est_reviews', 'INTEGER')}, {_c('visibility_score', 'INTEGER')},
                   {_c('mo_sales', 'INTEGER')}, {_c('mo_revenue', 'DECIMAL(14,2)')},
                   {_c('total_sales_est', 'INTEGER')}, {_c('growth_rate', 'DOUBLE')}
            FROM src_listings
            WHERE {_c('listing_id', 'BIGINT')} IS NOT NULL
            """,
            params,
        )

        con.execute("COMMIT")
        return run_id
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    finally:
        con.close()
