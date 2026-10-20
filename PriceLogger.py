import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONFIG_FILE = "config.json"
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def load_prices_config(path=CONFIG_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Config file '{path}' not found.")
    except json.JSONDecodeError as e:
        sys.exit(f"'{path}' is not valid JSON: {e}")

    prices = cfg.get("prices")
    if not prices or "request" not in prices:
        sys.exit(f"'{path}' needs a 'prices' section with a 'request'.")
    prices.setdefault("file", "output/prices.csv")
    prices.setdefault("fields", {})
    return prices


def resolve_env(value):
    """'env:NAME' -> value of environment variable NAME."""
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        found = os.environ.get(name)
        if found is None:
            sys.exit(f"Environment variable {name} is not set.")
        return found
    return value


def fetch_json(req_cfg):
    headers = {"User-Agent": "NewsScrapper/0.1"}
    data = None
    post = req_cfg.get("post_data")
    if post:
        body = {k: resolve_env(v) for k, v in post.items()}
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    method = req_cfg.get("method", "POST" if data else "GET").upper()
    req = urllib.request.Request(req_cfg["url"], data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def dig(obj, path):
    """Follow a dotted path; numeric parts index into lists."""
    for part in path.split("."):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def to_number(value):
    if isinstance(value, (int, float)):
        return value
    text = str(value).translate(DIGITS).replace(",", "").strip()
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def take_snapshot(prices):
    data = fetch_json(prices["request"])
    row = {"utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for column, path in prices["fields"].items():
        try:
            row[column] = to_number(dig(data, path))
        except (KeyError, IndexError, ValueError, TypeError):
            print(f"[!] '{column}': path '{path}' not found in the response")
            row[column] = ""
    return row


def append_row(path, row):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    prices = load_prices_config()

    if "--probe" in args:  # print the raw response so you can pick field paths
        try:
            print(json.dumps(fetch_json(prices["request"]), ensure_ascii=False, indent=2))
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            print(f"Request failed: {e}")
        return

    if not prices["fields"]:
        sys.exit("No 'fields' configured. Run with --probe first, then fill them in.")

    interval = None
    if "--loop" in args:
        try:
            interval = float(args[args.index("--loop") + 1])
        except (IndexError, ValueError):
            sys.exit("Usage: python PriceLogger.py --loop MINUTES")

    while True:
        try:
            row = take_snapshot(prices)
            append_row(prices["file"], row)
            values = "  ".join(f"{k}={v}" for k, v in row.items() if k != "utc_time")
            print(f"{row['utc_time']}  {values}")
        except Exception as e:
            print(f"[!] snapshot failed: {e}")
        if interval is None:
            break
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()