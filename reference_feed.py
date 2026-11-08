"""Prix de référence Chainlink via PolyBolt (API temps réel de Polymarket).

Polymarket tranche les marchés Up/Down avec un TWAP Chainlink : « The TWAP at the
start sets the price to beat. When the duration ends, the TWAP at that time sets
the final price » (docs.polymarket.com, concepts/resolution). Binance (USDT) en
diffère de 55 à 175 $ en niveau et de 21 $ en médiane sur la variation d'une
fenêtre (mesuré le 2026-11-02) : suivre le flux Chainlink lui-même supprime
cette erreur.

Canaux suivis sur wss://ws-live-v2.polymarket.com/ws (authentification requise,
avec les identifiants API CLOB du wallet) :
  - price.crypto       {"symbol": "btcusd"}                      → "spot"
  - price.crypto.twap  {"symbol": "btcusd", "window_seconds": 60} → "twap"

Format documenté (enveloppe) :
  {"v":1,"channel":"price.crypto","seq":2,"ts":...,"payload":{"symbol":"btcusd",
   "value":64126.0,"full_accuracy_value":"64126.00000000","timestamp":...,
   "source":"chainlink"}}
Les snapshots portent "snapshot": true et payload.data = [{timestamp, value,
full_accuracy_value}, ...] (deux minutes d'historique).
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque

import websockets

from logger import log

POLYBOLT_URL = "wss://ws-live-v2.polymarket.com/ws"
HISTORY_SECONDS = 420
CHANNELS = {
    "spot": {"channel": "price.crypto", "filter": {"symbol": "btcusd"}},
    "twap": {"channel": "price.crypto.twap", "filter": {"symbol": "btcusd", "window_seconds": 60}},
}
CHANNEL_TO_KIND = {spec["channel"]: kind for kind, spec in CHANNELS.items()}


def _point(item: dict) -> tuple[float, float] | None:
    """(timestamp en secondes, valeur) d'un point de prix, ou None s'il est invalide."""
    try:
        raw = item.get("full_accuracy_value", item.get("value"))
        ts = float(item["timestamp"])
        if ts > 1e11:  # millisecondes
            ts /= 1000.0
        return ts, float(raw)
    except (KeyError, TypeError, ValueError):
        return None


class ReferenceFeed:
    """Historique des prix Chainlink (spot et TWAP 60 s) reçus de PolyBolt."""

    def __init__(self, api_creds: dict | None):
        self._creds = api_creds
        self._history: dict[str, deque[tuple[float, float]]] = {k: deque() for k in CHANNELS}
        self._running = False
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._creds)

    # ─── Données ──────────────────────────────────────────────

    def add_point(self, kind: str, ts: float, value: float) -> None:
        hist = self._history[kind]
        if hist and ts <= hist[-1][0]:
            if ts == hist[-1][0]:
                hist[-1] = (ts, value)
            return  # point en retard ou doublon
        hist.append((ts, value))
        cutoff = ts - HISTORY_SECONDS
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def latest(self, kind: str, max_age: float = 10.0, now: float | None = None) -> float | None:
        hist = self._history[kind]
        if not hist:
            return None
        now = time.time() if now is None else now
        ts, value = hist[-1]
        return value if now - ts <= max_age else None

    def value_at(self, kind: str, ts: float, max_gap: float = 3.0) -> float | None:
        """Dernière valeur publiée à l'instant `ts` ou juste avant (≤ max_gap s)."""
        hist = self._history[kind]
        if not hist or hist[0][0] > ts:
            return None
        found = None
        for t, v in hist:
            if t > ts:
                break
            found = (t, v)
        if found is None or ts - found[0] > max_gap:
            return None
        return found[1]

    def average(self, kind: str, start: float, end: float) -> float | None:
        """Moyenne des valeurs publiées entre `start` et `end` (secondes)."""
        vals = [v for t, v in self._history[kind] if start <= t <= end]
        return sum(vals) / len(vals) if vals else None

    def volatility(self, kind: str = "spot", lookback: float = 300.0,
                   now: float | None = None) -> float | None:
        """Écart-type des variations de prix par √seconde, en dollars.

        Calculé sur des points rééchantillonnés à la seconde. None s'il y a
        moins de 60 secondes de données exploitables.
        """
        hist = self._history[kind]
        now = time.time() if now is None else now
        pts = [(t, v) for t, v in hist if t >= now - lookback]
        if len(pts) < 2:
            return None
        per_second: dict[int, float] = {}
        for t, v in pts:
            per_second[int(t)] = v
        secs = sorted(per_second)
        diffs = []
        for a, b in zip(secs, secs[1:]):
            gap = b - a
            if 0 < gap <= 5:
                diffs.append((per_second[b] - per_second[a]) / math.sqrt(gap))
        if len(diffs) < 60:
            return None
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / (len(diffs) - 1)
        return math.sqrt(var)

    # ─── Connexion ────────────────────────────────────────────

    async def start(self) -> None:
        if not self.enabled:
            log.warning("ReferenceFeed désactivé : pas d'identifiants API CLOB")
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="reference_feed")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                log.warning("ReferenceFeed: %s — reconnexion dans %.0fs", e, backoff)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        async with websockets.connect(POLYBOLT_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({"op": "auth", "rid": "a1", "auth": self._creds}))
            await ws.send(json.dumps({
                "op": "subscribe", "rid": "s1",
                "subscriptions": list(CHANNELS.values()),
            }))
            while self._running:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                self.handle_message(raw)

    def handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        op = msg.get("op")
        if op == "authed":
            log.info("ReferenceFeed: authentifié sur PolyBolt")
            return
        if op == "subscribed":
            self.connected = True
            log.info("ReferenceFeed: abonné à %s", msg.get("channel"))
            return
        if op == "error":
            self.last_error = f"{msg.get('code')} ({msg.get('channel')})"
            log.error("ReferenceFeed: erreur PolyBolt %s", self.last_error)
            return
        kind = CHANNEL_TO_KIND.get(msg.get("channel", ""))
        payload = msg.get("payload") or {}
        if kind is None or payload.get("symbol", "btcusd") != "btcusd":
            return
        items = payload.get("data") if msg.get("snapshot") else [payload]
        for item in items or []:
            pt = _point(item)
            if pt:
                self.add_point(kind, *pt)
