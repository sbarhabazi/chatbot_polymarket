"""Outcome Recorder — résultats officiels des fenêtres observées.

Pour chaque fenêtre présente dans shadow_log.jsonl, enregistre une fois publiés
(7 minutes après la clôture) le priceToBeat et le finalPrice officiels et le
côté gagnant, dans window_outcomes.jsonl. Avec le journal fantôme, cela permet
de vérifier :
  - quel flux (TWAP Chainlink, spot Chainlink, Binance) reproduit exactement le
    priceToBeat officiel ;
  - si les probabilités du modèle sont bien calibrées, et quel MIN_EDGE aurait
    été rentable, sur toutes les fenêtres et pas seulement celles tradées.
Rapport : python scripts/shadow_report.py
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from logger import log
from market_manager import MarketManager
from paths import data_path

SHADOW_LOG_FILE = data_path("shadow_log.jsonl")
OUTCOMES_FILE = data_path("window_outcomes.jsonl")
POLL_INTERVAL = 60
PUBLISH_DELAY = 7 * 60       # finalPrice publié 1 à 6 min après la clôture
GIVE_UP_AFTER = 24 * 3600


def _read_jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


class OutcomeRecorder:
    def __init__(self, market_manager: MarketManager):
        self._mm = market_manager
        self._done = {r["window_ts"] for r in _read_jsonl(OUTCOMES_FILE)}
        self._abandoned: set[int] = set()

    async def run(self) -> None:
        log.info("OutcomeRecorder started — %d fenêtres déjà résolues", len(self._done))
        while True:
            try:
                await self.record_pending()
            except Exception as e:
                log.error("OutcomeRecorder error: %s", e, exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def record_pending(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        windows = {r["window_ts"] for r in _read_jsonl(SHADOW_LOG_FILE)}
        recorded = 0
        for ts in sorted(windows - self._done - self._abandoned):
            end = ts + 300
            if now < end + PUBLISH_DELAY:
                continue
            if now > end + GIVE_UP_AFTER:
                self._abandoned.add(ts)
                continue
            result = await self._mm.get_window_result(ts)
            if result is None:
                continue
            with open(OUTCOMES_FILE, "a") as f:
                f.write(json.dumps(result) + "\n")
            self._done.add(ts)
            recorded += 1
        return recorded
