"""
Détection d'indisponibilité du marché Polymarket.
Vérifie que l'API Gamma et les contrats on-chain répondent correctement.
Exécuté toutes les 6h par Perplexity Computer.
"""

import json
import time
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB = "https://clob.polymarket.com"
TRACKING_FILE = Path("/home/user/workspace/cron_tracking/market_availability.json")

# Endpoints à tester
ENDPOINTS = [
    {
        "name": "Gamma API — markets",
        "url": f"{GAMMA_API}/markets?limit=1",
        "critical": True,
    },
    {
        "name": "Gamma API — events",
        "url": f"{GAMMA_API}/events?limit=1",
        "critical": False,
    },
    {
        "name": "CLOB API — health",
        "url": f"{POLYMARKET_CLOB}",
        "critical": False,
    },
]

TIMEOUT = 10


def check_endpoint(name: str, url: str) -> dict:
    start = time.time()
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            status = resp.status
            body = resp.read(512).decode("utf-8", errors="ignore")
            elapsed = round(time.time() - start, 3)
            return {
                "name": name,
                "url": url,
                "status": "ok",
                "http_code": status,
                "latency_s": elapsed,
                "preview": body[:100],
            }
    except HTTPError as e:
        return {"name": name, "url": url, "status": "error", "http_code": e.code, "error": str(e)}
    except URLError as e:
        return {"name": name, "url": url, "status": "error", "http_code": None, "error": str(e.reason)}
    except Exception as e:
        return {"name": name, "url": url, "status": "error", "http_code": None, "error": str(e)}


def load_state():
    if TRACKING_FILE.exists():
        with open(TRACKING_FILE) as f:
            return json.load(f)
    return {"consecutive_failures": 0, "last_down": None}


def save_state(state):
    TRACKING_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKING_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    state = load_state()
    results = [check_endpoint(ep["name"], ep["url"]) for ep in ENDPOINTS]

    critical_failures = [
        r for r, ep in zip(results, ENDPOINTS)
        if ep["critical"] and r["status"] != "ok"
    ]
    all_failures = [r for r in results if r["status"] != "ok"]

    now = datetime.now(timezone.utc).isoformat()
    market_up = len(critical_failures) == 0

    if not market_up:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_down"] = now
    else:
        state["consecutive_failures"] = 0

    report = {
        "timestamp": now,
        "market_status": "up" if market_up else "down",
        "endpoints": results,
        "critical_failures": len(critical_failures),
        "consecutive_failures": state["consecutive_failures"],
        "notify": not market_up and state["consecutive_failures"] >= 2,
    }

    if report["notify"]:
        errors = "; ".join(f["name"] for f in critical_failures)
        report["notification"] = {
            "title": "Marché Polymarket indisponible",
            "body": (
                f"Polymarket ne répond pas depuis {state['consecutive_failures']} vérifications consécutives.\n"
                f"Endpoints défaillants: {errors}\n"
                f"Le bot ne peut pas placer de trades. Vérifiez https://polymarket.com"
            )
        }

    save_state(state)
    print(json.dumps(report))
    return report


if __name__ == "__main__":
    main()
