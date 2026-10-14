"""
Mở trình duyệt, chờ bạn đăng nhập Everbee, bắt header X-Access-Token rồi ghi vào .env.

Chạy trên máy host (KHÔNG chạy trong Docker — container không có trình duyệt):
    uv run --extra token python scripts/get_token.py              # Edge (mặc định)
    uv run --extra token python scripts/get_token.py --browser chrome
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from selenium import webdriver

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
TOKEN_TIMEOUT = 180


def open_driver(browser: str):
    if browser == "chrome":
        from selenium.webdriver.chrome.options import Options
        options = Options()
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    else:
        from selenium.webdriver.edge.options import Options
        options = Options()
        options.set_capability("ms:loggingPrefs", {"performance": "ALL"})
    options.add_argument("--window-size=1280,800")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=options) if browser == "chrome" else webdriver.Edge(options=options)


def capture_token(driver) -> str:
    driver.execute_cdp_cmd("Network.enable", {})
    driver.get("https://app.everbee.com/shop-analyzer")
    print("Trình duyệt đã mở. Đăng nhập Everbee rồi search bất kỳ keyword/shop nào.")
    print(f"Đang chờ request API (tối đa {TOKEN_TIMEOUT}s)...")

    deadline = time.time() + TOKEN_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            logs = driver.get_log("performance")
        except Exception:
            continue
        for entry in logs:
            try:
                msg = json.loads(entry.get("message", "{}")).get("message", {})
                if msg.get("method") != "Network.requestWillBeSent":
                    continue
                req = msg.get("params", {}).get("request", {})
                if "everbee." not in req.get("url", ""):
                    continue
                headers = req.get("headers", {})
                token = headers.get("X-Access-Token") or headers.get("x-access-token", "")
                if token:
                    return token
            except (json.JSONDecodeError, AttributeError, TypeError):
                continue
    return ""


def write_env(token: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    line = f"AUTH_TOKEN={token}"
    if re.search(r"(?m)^\s*AUTH_TOKEN\s*=.*$", text):
        text = re.sub(r"(?m)^\s*AUTH_TOKEN\s*=.*$", lambda _: line, text, count=1)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    args = parser.parse_args()

    driver = open_driver(args.browser)
    try:
        token = capture_token(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not token:
        print(f"Không lấy được token sau {TOKEN_TIMEOUT}s.")
        sys.exit(1)
    write_env(token)
    print(f"Đã ghi AUTH_TOKEN vào {ENV_PATH}")


if __name__ == "__main__":
    main()
