# analyze-shop-pipeline

Thu thập và phân tích dữ liệu shop Etsy (qua Everbee API) để theo dõi các shop
build và đánh giá mức phù hợp với dòng **custom gift**.

Chạy bằng Docker, dùng giống nhau trên Linux và Windows. Hướng build và tiến độ:
xem [PLAN.md](PLAN.md).

## Thành phần

| Service    | Làm gì |
|------------|--------|
| `scraper`  | Crawl thông tin shop + toàn bộ listing của các shop trong 1 list → xuất CSV/Excel và lưu snapshot vào DuckDB |
| `analyzer` | Web app tại `http://localhost:8765`: phân tích gift-fit, so sánh 2 lần phân tích, Build vs Conflict, lịch sử shop build, kế hoạch listing |

## Yêu cầu

- Docker Compose v2.24 trở lên (Linux) hoặc Docker Desktop với WSL2 (Windows)
- Token Everbee (`AUTH_TOKEN`)

## Cài đặt

```bash
git clone <repo-url> analyze-shop-pipeline
cd analyze-shop-pipeline
cp .env.example .env        # Windows PowerShell: copy .env.example .env
# Điền AUTH_TOKEN vào .env (xem mục "Lấy token Everbee")
docker compose build
```

Đặt file danh sách shop build (cột `Shop name`, `PIC`, `Conflict shop`) tại
`data/input/shop_build.csv` để dùng tab Build vs Conflict.

## Sử dụng

**Crawl dữ liệu**

```bash
docker compose run --rm scraper --list shop_build
docker compose run --rm scraper --list shop_names
docker compose run --rm scraper --list shop_build --fresh   # bỏ checkpoint, crawl lại từ đầu
```

- Danh sách shop sửa trong `config/shop_lists.json`, không cần build lại image. Thêm list
  mới chỉ cần thêm 1 key.
- Mặc định scraper **tự resume** từ checkpoint nếu lần trước bị dừng (Ctrl+C, token hết hạn).
- Token hết hạn → scraper dừng với exit code 2 và giữ checkpoint. Cập nhật `.env` rồi chạy lại cùng lệnh.

**Mở web phân tích**

```bash
docker compose up -d analyzer     # mở http://localhost:8765
docker compose down               # khi dùng xong
```

**Phân tích 1 lần crawl không cần mở web**

```bash
docker compose run --rm analyzer analyzer --analyze-dir /app/data/output/20261014_102314_shop_build
```

Report lưu vào `data/analysis_output/<tên thư mục>/` và hiện trong tab So sánh.

**Sau khi sửa code** chạy `docker compose build` để cập nhật image.

## Dữ liệu

Mọi dữ liệu nằm trong thư mục `data/` (không commit lên git):

| Đường dẫn                          | Nội dung |
|------------------------------------|----------|
| `data/everbee.duckdb`              | Database: shops, listings và snapshot chỉ số theo từng lần crawl |
| `data/output/<thời gian>_<list>/`  | `shops.csv/.xlsx`, `listings.csv/.xlsx` của mỗi lần crawl |
| `data/checkpoints/`                | Checkpoint khi crawl dở (tự xoá khi crawl xong) |
| `data/input/shop_build.csv`        | Danh sách shop build + shop conflict |
| `data/shop_data/`                  | CSV listing cho tab Build vs Conflict / Lịch sử, và `listing_plan.json` |
| `data/analysis_output/`            | Report HTML của analyzer |

### Schema DuckDB

| Bảng / View         | Nội dung |
|---------------------|----------|
| `crawl_runs`        | Mỗi lần chạy scraper |
| `shops`             | Thông tin ít thay đổi của shop |
| `shop_snapshots`    | Chỉ số shop theo từng lần crawl |
| `listings`          | Thông tin ít thay đổi của listing (title, tags, category…) |
| `listing_snapshots` | Chỉ số listing theo từng lần crawl (price, views, favorites…) |
| `shop_lists`        | Shop thuộc list nào |
| `v_shop_latest`     | Chỉ số mới nhất của mỗi shop |
| `v_listing_growth`  | Mức tăng views/favorites giữa 2 lần crawl |

## Lấy token Everbee

Trong container không có trình duyệt nên phải lấy token trên máy host. Có 2 cách:

1. **Tự động** (cần [uv](https://docs.astral.sh/uv/) và Edge hoặc Chrome trên máy):

   ```bash
   uv run --extra token python scripts/get_token.py                  # Edge
   uv run --extra token python scripts/get_token.py --browser chrome
   ```

   Đăng nhập Everbee trên cửa sổ vừa mở, search bất kỳ shop nào; script tự ghi `AUTH_TOKEN` vào `.env`.

2. **Thủ công**: đăng nhập `app.everbee.com`, mở DevTools → Network, chọn 1 request tới
   `api.everbee.com`, copy giá trị header `X-Access-Token` vào `AUTH_TOKEN` trong `.env`.

## Chạy không cần Docker

```bash
uv sync
uv run scrape --list shop_build
uv run analyzer                  # tự mở trình duyệt
```

Trên host, `.env` không được nạp tự động: đặt biến `AUTH_TOKEN` trong shell trước khi chạy.

## Nguồn gốc

Tách từ `topcv-data-pipeline/product-scraper/`:
`analyze_shop_build_scraper.py`, `everbee_duckdb.py`, `analyze_shop_gift_fit.py`, `listing_plan.py`.
