"""
PC Parts Price Tracker — Companion API Server
Runs on http://localhost:5001
Allows the HTML report to add/remove items from config.json without editing files manually.

Start it: python server.py
Or:       start_server.bat
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
RELEASES_FILE = BASE_DIR / "releases.json"
REPORT_FILE = BASE_DIR / "morning_report.html"

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Background scrape process (started via /api/run-tracker)
_scan_proc = None


def read_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def write_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def read_releases():
    if RELEASES_FILE.exists():
        with open(RELEASES_FILE, "r") as f:
            return json.load(f)
    return {"releases": []}


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def auto_search_terms(name, category=""):
    """Generate per-retailer search terms from a product name."""
    name_clean = name.strip()
    terms = {}

    # Detect GPU patterns
    rtx_match = re.search(r"RTX\s*(\d+(?:\s*\w+)?)", name_clean, re.I)
    rx_match = re.search(r"RX\s*(\d+(?:\s*\w+)?)", name_clean, re.I)
    arc_match = re.search(r"Arc\s+([A-Z]\d+)", name_clean, re.I)
    # CPU patterns
    ryzen_match = re.search(r"Ryzen\s*(?:\d+\s*)?(\w+)", name_clean, re.I)
    core_match = re.search(r"(?:Core\s+(?:Ultra\s+)?|i[3579]-?)(\d+\w*)", name_clean, re.I)

    retailers = ["newegg", "amazon", "best_buy", "micro_center", "ebay", "facebook"]

    if rtx_match:
        model = rtx_match.group(0).replace(" ", " ").strip()  # e.g. "RTX 4070 Super"
        terms = {
            "newegg": model,
            "amazon": f"NVIDIA GeForce {model}",
            "best_buy": model,
            "micro_center": model,
            "ebay": model,
            "facebook": model,
        }
    elif rx_match:
        model = rx_match.group(0).strip()
        terms = {
            "newegg": f"Radeon {model}",
            "amazon": f"AMD Radeon {model}",
            "best_buy": model,
            "micro_center": model,
            "ebay": f"Radeon {model}",
            "facebook": model,
        }
    elif arc_match:
        model = f"Arc {arc_match.group(1)}"
        terms = {
            "newegg": f"Intel {model}",
            "amazon": f"Intel {model}",
            "best_buy": model,
            "micro_center": model,
            "ebay": f"Intel {model}",
            "facebook": model,
        }
    elif ryzen_match:
        terms = {r: (f"AMD {name_clean}" if r == "amazon" else name_clean) for r in retailers}
    elif core_match:
        terms = {r: (f"Intel {name_clean}" if r == "amazon" else name_clean) for r in retailers}
    else:
        terms = {r: name_clean for r in retailers}
    return terms


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if REPORT_FILE.exists():
        return send_file(REPORT_FILE)
    return "<h1>No report yet</h1><p>Run <code>python price_tracker.py</code> to generate one.</p>"


@app.route("/api/config")
def get_config():
    return jsonify(read_config())


@app.route("/api/releases")
def get_releases():
    return jsonify(read_releases())


@app.route("/api/add-item", methods=["POST"])
def add_item():
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"status": "error", "message": "Missing name"}), 400

    config = read_config()
    name = data["name"].strip()
    item_id = slugify(name)

    # Prevent duplicates
    existing_ids = {item["id"] for item in config["items"]}
    if item_id in existing_ids:
        return jsonify({"status": "exists", "message": f"'{name}' is already being tracked."})

    spec_query = data.get("spec_query")  # set when it's a spec/category search
    retailers = ["newegg", "amazon", "best_buy", "micro_center", "ebay", "facebook"]
    if data.get("custom") or data.get("exact_query"):
        # Free-typed product: use the exact string on every retailer
        search_terms = {r: name for r in retailers}
    elif spec_query:
        # Spec search: same generic query across all retailers
        search_terms = {r: spec_query for r in retailers}
    else:
        search_terms = data.get("search_terms") or auto_search_terms(name, data.get("category", ""))

    min_price = data.get("min_price")
    try:
        min_price = float(min_price) if min_price is not None and min_price != "" else None
    except (TypeError, ValueError):
        min_price = None

    new_item = {
        "id": item_id,
        "name": name,
        "category": data.get("category", "Other"),
        "target_price": data.get("target_price") or None,
        "min_price": min_price,
        "search_terms": search_terms,
    }
    config["items"].append(new_item)
    write_config(config)
    return jsonify({"status": "added", "item": new_item})


@app.route("/api/update-item", methods=["POST"])
def update_item():
    """Update fields on an existing watchlist item (min_price, target_price, …)."""
    data = request.get_json()
    if not data or not data.get("id"):
        return jsonify({"status": "error", "message": "Missing id"}), 400
    config = read_config()
    item = next((i for i in config["items"] if i["id"] == data["id"]), None)
    if not item:
        return jsonify({"status": "error", "message": "Item not found"}), 404

    if "min_price" in data:
        raw = data["min_price"]
        if raw is None or raw == "":
            item["min_price"] = None
        else:
            try:
                item["min_price"] = float(raw)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "Invalid min_price"}), 400

    if "target_price" in data:
        raw = data["target_price"]
        if raw is None or raw == "":
            item["target_price"] = None
        else:
            try:
                item["target_price"] = float(raw)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "Invalid target_price"}), 400

    write_config(config)
    return jsonify({"status": "updated", "item": item})


@app.route("/api/remove-item", methods=["POST"])
def remove_item():
    data = request.get_json()
    if not data or not data.get("id"):
        return jsonify({"status": "error", "message": "Missing id"}), 400
    config = read_config()
    before = len(config["items"])
    config["items"] = [i for i in config["items"] if i["id"] != data["id"]]
    write_config(config)
    removed = before - len(config["items"])
    return jsonify({"status": "removed" if removed else "not_found", "count": removed})


@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    """Read/update tracker settings (Facebook ZIP, radius, …)."""
    config = read_config()
    settings = config.setdefault("settings", {})
    if request.method == "GET":
        return jsonify(settings)

    data = request.get_json() or {}
    if "facebook_zipcode" in data:
        zipcode = str(data.get("facebook_zipcode") or "").strip()
        if zipcode and not re.fullmatch(r"\d{5}(-\d{4})?", zipcode):
            return jsonify({"status": "error", "message": "ZIP must be 5 digits"}), 400
        settings["facebook_zipcode"] = zipcode
    if "facebook_radius_miles" in data:
        try:
            radius = int(data["facebook_radius_miles"])
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Invalid radius"}), 400
        if radius not in (1, 2, 5, 10, 20, 40, 60, 80, 100, 250, 500):
            return jsonify({"status": "error", "message": "Radius must be a Facebook-supported mile value"}), 400
        settings["facebook_radius_miles"] = radius
    write_config(config)
    return jsonify({"status": "updated", "settings": settings})


@app.route("/api/run-tracker", methods=["POST"])
def run_tracker():
    """Kick off a fresh price scrape in the background."""
    global _scan_proc
    if _scan_proc is not None and _scan_proc.poll() is None:
        return jsonify({"status": "already_running"})
    python = sys.executable
    script = str(BASE_DIR / "price_tracker.py")
    _scan_proc = subprocess.Popen([python, script], cwd=str(BASE_DIR))
    return jsonify({"status": "started", "pid": _scan_proc.pid})


@app.route("/api/scan-status")
def scan_status():
    """Whether a scrape is running + report mtime (for UI refresh)."""
    running = _scan_proc is not None and _scan_proc.poll() is None
    report_mtime = None
    if REPORT_FILE.exists():
        report_mtime = REPORT_FILE.stat().st_mtime
    return jsonify({
        "running": running,
        "report_mtime": report_mtime,
        "report_exists": REPORT_FILE.exists(),
    })


if __name__ == "__main__":
    print(f"\n  PC Parts Price Tracker — Companion Server")
    print(f"  Running at http://localhost:5001")
    print(f"  Open that URL in your browser for the report.\n")
    app.run(host="localhost", port=5001, debug=False, use_reloader=False)
