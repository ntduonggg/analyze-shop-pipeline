"""Đường dẫn và cấu hình chung — đọc từ biến môi trường.

Trong Docker: DATA_DIR=/app/data, CONFIG_DIR=/app/config (đặt sẵn trong Dockerfile).
Chạy trực tiếp trên host: mặc định là data/ và config/ ở gốc project.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR   = Path(os.getenv("DATA_DIR",   PROJECT_ROOT / "data"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", PROJECT_ROOT / "config"))

DB_PATH          = Path(os.getenv("EVERBEE_DB", DATA_DIR / "everbee.duckdb"))
OUTPUT_DIR       = DATA_DIR / "output"
CHECKPOINT_DIR   = DATA_DIR / "checkpoints"
INPUT_DIR        = DATA_DIR / "input"
SHOP_LISTS_JSON  = CONFIG_DIR / "shop_lists.json"

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()
