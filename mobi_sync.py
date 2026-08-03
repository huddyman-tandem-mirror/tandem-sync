#!/usr/bin/env python3
"""
mobi_sync.py — Tandem Source → Nightscout sync for guardian/dependent accounts
===============================================================================
tconnectsync's device-selection step calls an endpoint that returns 404 for
personal_guardian accounts (parents of under-13 dependents). This script
bypasses that step by using the Tandem Source web BFF API directly, which
works for any account type — the same endpoints the source.tandemdiabetes.com
web UI uses.

Auth still goes through tconnectsync's OIDC machinery (installed as a dep),
so there's nothing extra to set up.

Required env vars (same secrets you already have in GitHub Actions):
  TCONNECT_EMAIL      Tandem Source login email
  TCONNECT_PASSWORD   Tandem Source login password
  NS_URL              Nightscout base URL  (no trailing slash)
  NS_SECRET           Nightscout API_SECRET (master, for write access)
  PUMP_SERIAL_NUMBER  Hudson's pump serial  (default: 1562014)
  HOURS_BACK          How many hours of history to sync each run (default: 3)
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
EMAIL       = os.environ["TCONNECT_EMAIL"]
PASSWORD    = os.environ["TCONNECT_PASSWORD"]
NS_URL      = os.environ["NS_URL"].rstrip("/")
NS_SECRET   = os.environ["NS_SECRET"]
PUMP_SERIAL = os.environ.get("PUMP_SERIAL_NUMBER", "1562014")
HOURS_BACK  = int(os.environ.get("HOURS_BACK", "24"))

# Event IDs observed in the web UI's network calls for Tandem Mobi + Control-IQ+
# (superset of tconnectsync's DEFAULT_EVENT_IDS — includes newer CIQ+ types)
BFF_EVENT_IDS = (
    "229,5,28,4,26,99,279,3,16,59,21,55,20,280,64,65,66,61,33,371,171,"
    "369,460,172,370,461,372,480,399,256,213,406,477,394,212,404,214,405,"
    "486,447,313,60,14,6,90,230,140,12,11,53,13,63,203,307,191"
)

# ── Authenticate via tconnectsync ─────────────────────────────────────────────
log.info("Authenticating with Tandem Source…")
try:
    from tconnectsync.api.tandemsource import TandemSourceApi
except ImportError:
    log.error("tconnectsync not installed — run: pip install tconnectsync")
    sys.exit(1)

api = TandemSourceApi(EMAIL, PASSWORD)
log.info(f"  pumperId  = {api.pumperId}")
log.info(f"  accountId = {api.accountId}")

# ── Device discovery (BFF pumpers endpoint) ───────────────────────────────────
log.info("Fetching pumper/device info…")
pumper = api.pumper_info()
log.info(
    f"  pumper: {pumper.get('firstName')} {pumper.get('lastName')} "
    f"| role={pumper.get('account', {}).get('role')}"
)

devices = pumper.get("devices", [])
if not devices:
    log.error("No devices found in pumper info — cannot continue")
    sys.exit(1)

device = next(
    (d for d in devices if str(d.get("serialNumber")) == str(PUMP_SERIAL)),
    devices[0],
)
assignment_id = device["assignmentId"]
log.info(
    f"  device: serial={device.get('serialNumber')} "
    f"model={device.get('modelName')} "
    f"assignmentId={assignment_id}"
)

# ── Fetch BFF pump-logs ───────────────────────────────────────────────────────
now_utc   = datetime.now(timezone.utc)
start_utc = now_utc - timedelta(hours=HOURS_BACK)

bff_url = (
    f"https://source.tandemdiabetes.com"
    f"/api/reports/bff/pump-logs/{assignment_id}"
)
bff_headers = {
    **api.api_headers(),
    "Origin":  "https://source.tandemdiabetes.com",
    "Referer": "https://source.tandemdiabetes.com/",
    "Accept":  "application/json",
}
bff_params = {
    "pumperId":  api.pumperId,
    "startDate": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "endDate":   now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "eventIds":  BFF_EVENT_IDS,
}

log.info(
    f"Fetching BFF pump-logs "
    f"{start_utc.strftime('%Y-%m-%d %H:%M')} → {now_utc.strftime('%H:%M')} UTC …"
)
r = requests.get(bff_url, params=bff_params, headers=bff_headers, timeout=90)
if r.status_code != 200:
    log.error(f"BFF returned HTTP {r.status_code}: {r.text[:600]}")
    sys.exit(1)

raw = r.json()
log.info(f"BFF response: {len(r.content) // 1024} KB")

# ── Inspect response structure (diagnostic — helps tune the parser) ───────────
def _desc(obj, prefix="", depth=0):
    if depth > 3:
        return
    if isinstance(obj, dict):
        log.info(f"{prefix}dict  keys={list(obj.keys())}")
        for k, v in list(obj.items())[:6]:
            _desc(v, prefix=f"{prefix}  [{k}] ", depth=depth + 1)
    elif isinstance(obj, list):
        log.info(f"{prefix}list  len={len(obj)}")
        if obj:
            _desc(obj[0], prefix=f"{prefix}  [0] ", depth=depth + 1)
    else:
        log.info(f"{prefix}{type(obj).__name__} = {str(obj)[:80]}")

log.info("─── BFF RESPONSE STRUCTURE ───────────────────────────")
_desc(raw)
log.info("──────────────────────────────────────────────────────")

# ── Locate the events list ────────────────────────────────────────────────────
events = []
if isinstance(raw, list):
    events = raw
    log.info(f"Response is a top-level list of {len(events)} items")
elif isinstance(raw, dict):
    for key in ("events", "pumpEvents", "pumpLogs", "logs", "data", "items", "results"):
        if key in raw and isinstance(raw[key], list):
            events = raw[key]
            log.info(f"Found events under key '{key}': {len(events)} items")
            break
    if not events:
        # Fall back: use the largest list value
        best = max(
            ((k, v) for k, v in raw.items() if isinstance(v, list)),
            key=lambda kv: len(kv[1]),
            default=(None, []),
        )
        if best[0]:
            events = best[1]
            log.info(f"Using largest list under key '{best[0]}': {len(events)} items")

if not events:
    log.warning("Could not locate an events list in the response.")
    log.warning("Full response (first 2000 chars):")
    log.warning(json.dumps(raw)[:2000])
else:
    # Print first 3 unique event shapes
    seen_shapes = set()
    for evt in events[:50]:
        if not isinstance(evt, dict):
            continue
        shape = tuple(sorted(evt.keys()))
        if shape not in seen_shapes:
            seen_shapes.add(shape)
            log.info(f"EVENT SAMPLE: {json.dumps(evt, default=str)[:400]}")

log.info(f"Events to process: {len(events)}")

# ── Parse events → Nightscout treatments ─────────────────────────────────────
#
# Field names aren't publicly documented for the BFF API.
# We try every plausible spelling. The EVENT SAMPLE logs above will show the
# actual field names on the first run so we can tighten this up if needed.
#
# Nightscout treatment types used:
#   "Bolus"           — insulin-only bolus
#   "Meal Bolus"      — bolus + carbs together
#   "Carb Correction" — carbs logged without a simultaneous bolus

def _get_any(d, *keys):
    """Return the first truthy value found among the given keys (supports dot-paths)."""
    for k in keys:
        parts = k.split(".")
        v = d
        for p in parts:
            if isinstance(v, dict):
                v = v.get(p)
            else:
                v = None
                break
        if v is not None and v != "" and v != 0:
            return v
    return None

treatments = []
seen_shapes = set()

for evt in events:
    if not isinstance(evt, dict):
        continue

    # ── Timestamp ──────────────────────────────────────────────────────────
    ts_raw = _get_any(
        evt,
        "pumpDateTime", "estimatedDateTime",
        "eventDateTime", "dateTime", "timestamp", "created_at",
        "time", "bolusDateTime", "eventDate",
    )
    if not ts_raw:
        continue
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except Exception:
        continue

    created_at = ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Event type ID ──────────────────────────────────────────────────────
    type_id_raw = _get_any(
        evt,
        "eventCode", "eventTypeId", "eventId", "typeId", "type_id",
        "eventType", "pumpEventTypeId",
    )
    try:
        type_id = int(type_id_raw) if type_id_raw is not None else None
    except (ValueError, TypeError):
        type_id = None

    # Log full eventProperties for bolus event codes so we can see field names
    BOLUS_CODES = {4, 5, 26, 28, 27, 434}
    if type_id in BOLUS_CODES:
        props = evt.get("eventProperties", {})
        log.info(f"BOLUS_EVENT typeId={type_id} props={json.dumps(props)}")

    # ── Insulin delivered ──────────────────────────────────────────────────
    # Tandem stores volumes in milliunits (0.001 U), so divide by 1000
    ep = evt.get("eventProperties", {}) or {}
    insulin_raw = _get_any(
        evt,
        "eventProperties.deliveredVolume", "eventProperties.deliveredAmount",
        "eventProperties.bolusAmount", "eventProperties.totalDelivered",
        "eventProperties.volume", "eventProperties.insulin",
        "deliveredAmount", "bolusAmount", "insulin", "delivered",
        "totalDelivered", "insulinDelivered", "bolusDelivered",
        "bolus.deliveredAmount", "bolus.amount",
        "details.deliveredAmount",
    )
    try:
        insulin_val = float(insulin_raw) if insulin_raw is not None else None
        # If value looks like milliunits (>20 for a realistic bolus), divide by 1000
        if insulin_val is not None and insulin_val > 20:
            insulin_val = insulin_val / 1000
        insulin = round(insulin_val, 3) if insulin_val is not None else None
    except (ValueError, TypeError):
        insulin = None

    # ── Carbs ──────────────────────────────────────────────────────────────
    carbs_raw = _get_any(
        evt,
        "eventProperties.carbAmount", "eventProperties.carbs",
        "eventProperties.carbsGrams", "eventProperties.mealCarbs",
        "carbAmount", "carbs", "carbsGrams", "carbIntake",
        "totalCarbs", "mealCarbs", "carbGrams",
        "meal.carbs", "meal.carbAmount",
        "bolusData.carbAmount",
    )
    try:
        carbs = round(float(carbs_raw), 1) if carbs_raw is not None else None
    except (ValueError, TypeError):
        carbs = None

    # Log new event shapes so we can refine the parser
    shape_key = (type_id, tuple(sorted(evt.keys())))
    if shape_key not in seen_shapes:
        seen_shapes.add(shape_key)
        log.info(
            f"NEW SHAPE  typeId={type_id}  "
            f"insulin={insulin}  carbs={carbs}  "
            f"keys={sorted(evt.keys())}"
        )

    # ── Build treatment ────────────────────────────────────────────────────
    if insulin and insulin > 0:
        t = {
            "created_at": created_at,
            "enteredBy":  "mobi-sync",
        }
        if carbs and carbs > 0:
            t["eventType"] = "Meal Bolus"
            t["insulin"]   = insulin
            t["carbs"]     = carbs
        else:
            t["eventType"] = "Bolus"
            t["insulin"]   = insulin

        treatments.append(t)
        log.info(
            f"  BOLUS  {insulin}U"
            + (f" + {carbs}g carbs" if carbs else "")
            + f"  @ {created_at}"
        )

    elif carbs and carbs > 0:
        treatments.append({
            "created_at": created_at,
            "eventType":  "Carb Correction",
            "carbs":      carbs,
            "enteredBy":  "mobi-sync",
        })
        log.info(f"  CARBS  {carbs}g  @ {created_at}")

log.info(f"Parsed {len(treatments)} treatments from {len(events)} events")

# ── Upload to Nightscout ──────────────────────────────────────────────────────
def ns_headers():
    secret_hash = hashlib.sha1(NS_SECRET.encode()).hexdigest()
    return {
        "api-secret":   secret_hash,
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }

if treatments:
    log.info(f"Uploading {len(treatments)} treatment(s) to Nightscout…")
    resp = requests.post(
        f"{NS_URL}/api/v1/treatments",
        json=treatments,
        headers=ns_headers(),
        timeout=30,
    )
    if resp.status_code in (200, 201):
        log.info(f"  ✓ Nightscout accepted: HTTP {resp.status_code}")
    else:
        log.warning(
            f"  ✗ Nightscout upload: HTTP {resp.status_code}: {resp.text[:300]}"
        )
else:
    log.info(
        "No treatments to upload — either nothing happened in the last "
        f"{HOURS_BACK}h, or the parser needs field-name tuning (check EVENT "
        "SAMPLE / NEW SHAPE lines above)."
    )

# ── Keep Nightscout awake ─────────────────────────────────────────────────────
try:
    ping = requests.get(
        f"{NS_URL}/api/v1/status.json",
        headers=ns_headers(),
        timeout=10,
    )
    log.info(f"Nightscout ping: HTTP {ping.status_code}")
except Exception as exc:
    log.warning(f"Nightscout ping failed: {exc}")

log.info("Done.")
