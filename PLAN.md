# PLAN — analyze-shop-pipeline

File này ghi hướng build và tiến độ của project. Cập nhật mỗi khi xong 1 việc
hoặc đổi quyết định thiết kế.

Cập nhật lần cuối: 2026-10-14

---

## 1. Mục tiêu

Tách 2 script từ `topcv-data-pipeline/product-scraper/` ra thành 1 project độc lập,
chạy bằng Docker để dùng giống nhau trên **Linux (ở nhà)** và **Windows (ở công ty)**:

| Nguồn (repo cũ)                                | Vai trò trong project mới                        |
|------------------------------------------------|--------------------------------------------------|
| `analyze_shop_build_scraper.py`                | Crawl shop/listing từ Everbee API → Excel + DuckDB |
| `everbee_duckdb.py`                            | Schema + ghi snapshot vào DuckDB                 |
| `analyze_shop_gift_fit.py` (+ `listing_plan.py`) | Web app phân tích gift-fit, build vs conflict, lịch sử, kế hoạch listing |

Không mang theo các phần khác của repo cũ (job-scraper, mockup-generator, rembg, torch…).

## 2. Quyết định thiết kế

| # | Quyết định | Lý do |
|---|------------|-------|
| D1 | Python 3.12, quản lý dependency bằng `uv` + `uv.lock` | Giống repo cũ, cài nhanh, lock giống nhau mọi máy |
| D2 | 1 image Docker, 2 service trong `docker-compose.yaml`: `scraper` (chạy 1 lần) và `analyzer` (web, cổng 8765) | Dùng chung code và dependency, image nhẹ |
| D3 | Toàn bộ dữ liệu nằm trong `./data` mount vào `/app/data` | Code và dữ liệu tách bạch; xoá container không mất dữ liệu |
| D4 | Token Everbee lấy từ `.env` (`AUTH_TOKEN`) | Trong container không có trình duyệt Edge để Selenium tự lấy token |
| D5 | Danh sách shop (`SHOP_NAMES`, `SHOP_BUILD`) chuyển từ code ra `config/shop_lists.json` | Sửa list không cần build lại image |
| D6 | DuckDB lưu tại `data/everbee.duckdb`, schema snapshot như đã chốt | Giữ được lịch sử để xem tăng trưởng |
| D7 | Web server bind `0.0.0.0` trong container, chỉ publish ra `127.0.0.1:8765` trên host | Container cần bind mọi interface; host vẫn chỉ mở nội bộ |
| D8 | `.gitattributes` ép `eol=lf` cho `*.sh`, `Dockerfile`, `*.py` | Tránh lỗi CRLF khi clone trên Windows rồi build image |
| D9 | Mọi đường dẫn trong code dùng `pathlib` + biến môi trường, không hard-code `~/Downloads/...` | Chạy được cả trong container lẫn trực tiếp trên host |

## 3. Cấu trúc thư mục dự kiến

```
analyze-shop-pipeline/
├── README.md
├── PLAN.md
├── pyproject.toml / uv.lock / .python-version
├── Dockerfile
├── docker-compose.yaml
├── .env.example            # mẫu AUTH_TOKEN, cổng, đường dẫn
├── .gitignore / .dockerignore / .gitattributes
├── config/
│   └── shop_lists.json     # {"shop_names": [...], "shop_build": [...]}
├── src/analyze_shop/
│   ├── settings.py         # đọc env, đường dẫn data/
│   ├── log.py              # log có màu
│   ├── scraper/
│   │   ├── everbee_client.py   # session, retry, token, fetch page
│   │   ├── scrape.py           # entry point crawl (CLI)
│   │   └── storage_duckdb.py   # từ everbee_duckdb.py
│   └── analyzer/
│       ├── gift_fit.py         # từ analyze_shop_gift_fit.py
│       └── listing_plan.py
├── scripts/
│   └── get_token.py        # chạy trên host (có Edge) để lấy AUTH_TOKEN
└── data/                   # mount volume, KHÔNG commit
    ├── everbee.duckdb
    ├── input/              # Shop build.csv, CSV upload tay
    ├── output/             # Excel/CSV mỗi lần crawl (theo timestamp)
    ├── checkpoints/
    ├── shop_data/          # CSV cho tab Build vs Conflict + listing_plan.json
    └── analysis_output/    # report HTML của analyzer
```

## 4. Lệnh chạy dự kiến

```bash
docker compose build
docker compose run --rm scraper --list shop_build          # crawl list shop_build (tự resume)
docker compose run --rm scraper --list shop_names --fresh  # bỏ checkpoint, crawl lại
docker compose up -d analyzer                               # mở http://localhost:8765
```

Lệnh giống nhau trên Linux và Windows (PowerShell / Docker Desktop).

## 5. Các việc cần làm

### Giai đoạn 1 — Khung project
- [x] Viết PLAN.md, README.md
- [x] `pyproject.toml` chỉ gồm dependency cần thiết: `pandas`, `requests`, `duckdb`, `openpyxl` (+ `selenium` trong extra `token`, chỉ cho `scripts/get_token.py`)
- [x] `uv.lock`
- [x] `Dockerfile` (python:3.12-slim + uv 0.13.0), `docker-compose.yaml`, `.env.example`
- [x] `.gitignore`, `.dockerignore`, `.gitattributes`
- [x] `git init`

### Giai đoạn 2 — Chuyển scraper
- [x] Tách `analyze_shop_build_scraper.py` thành `everbee_client.py` + `scrape.py`
- [x] Đọc list shop từ `config/shop_lists.json`, thay cờ `--shop-build` bằng `--list <tên>`
- [x] Output ghi vào `data/output/<YYYYmmdd_HHMMSS>_<list>/` gồm `shops.csv/.xlsx` + `listings.csv/.xlsx` (2 CSV dùng thẳng được cho `analyzer --analyze-dir`)
- [x] Checkpoint ghi vào `data/checkpoints/<list>_*.csv`; mặc định resume, `--fresh` để crawl lại
- [x] Bỏ Selenium khỏi luồng chính; token hết hạn → flush checkpoint, exit code 2
- [x] Bắt cả SIGTERM (`docker stop`) để flush checkpoint
- [x] Chuyển `everbee_duckdb.py` → `storage_duckdb.py`
- [x] `scripts/get_token.py` (Edge/Chrome) tự ghi `AUTH_TOKEN` vào `.env`

### Giai đoạn 3 — Chuyển analyzer
- [x] Chuyển `analyze_shop_gift_fit.py` → `analyzer/gift_fit.py`, `listing_plan.py` giữ nguyên
- [x] Thêm `--host` (Docker dùng `0.0.0.0`), compose chạy với `--no-browser`
- [x] Đổi `BUILD_LIST_CSV` (`~/Downloads/Shop build.csv`) → `data/input/shop_build.csv`
- [x] Analyzer `chdir` vào `DATA_DIR` nên `analysis_output/`, `shop_data/`, `listing_plan.json` đều nằm trong `data/`
- [x] Giữ nguyên giao diện và các tab (upload, compare, build-conflict, build-history, plan.xlsx)

### Giai đoạn 4 — Kiểm thử trên 2 máy
- [x] Chạy scraper + analyzer trực tiếp bằng Python trên Linux (API giả lập bằng dữ liệu cũ)
- [ ] `docker compose build` + chạy scraper và analyzer trên Linux
- [ ] Build + chạy trên Windows (Docker Desktop, WSL2 backend)
- [ ] Đối chiếu kết quả với bản chạy ở repo cũ

### Sau này (chưa làm)
- [ ] Analyzer đọc thẳng từ DuckDB thay vì phải upload CSV
- [ ] Tab build-history dùng `listing_snapshots` thay cho so sánh 2 file CSV
- [ ] Lịch crawl tự động (cron trong container hoặc Task Scheduler/cron trên host)

## 6. Lưu ý chạy trên Windows

- Dùng Docker Desktop với WSL2 backend.
- Nên clone project vào filesystem của WSL (`\\wsl$\...`) hoặc ổ NTFS thường; tránh OneDrive vì
  file DuckDB bị khoá khi đồng bộ.
- DuckDB chỉ cho **1 tiến trình ghi** tại một thời điểm: không chạy scraper khi đang mở
  file `.duckdb` bằng DBeaver/DuckDB UI.
- Không chép thẳng file `.duckdb` giữa 2 máy khi đang có container chạy; dừng container trước.

## 7. Nhật ký

| Ngày | Việc |
|------|------|
| 2026-10-14 | Tạo project, viết PLAN.md + README.md |
| 2026-10-14 | Xong giai đoạn 1–3. Kiểm thử không qua Docker: crawl giả lập 3 shop / 2683 listing (gồm token hết hạn giữa chừng → resume), ghi DuckDB, `--analyze-dir`, mọi route web trả 200. Máy dev chưa cài Docker nên chưa build image |
