#!/usr/bin/env python3
"""
CyberQ Local Agent
Polls myflameboss.com on your local network and forwards data to your dashboard.

Usage:
  CYBERQ_TOKEN="your-token" CYBERQ_USER="T-252541" CYBERQ_PASS="your-pass" python3 cyberq_agent.py

Or edit the CONFIG section below directly.
"""
import json, time, base64, os, sys
import urllib.request
from datetime import datetime

# ── CONFIG (set via env vars or edit here) ────────────────────────────────────
DASHBOARD_URL = os.environ.get("CYBERQ_URL",   "{{ app_url }}")
AGENT_TOKEN   = os.environ.get("CYBERQ_TOKEN", "")
FB_USER       = os.environ.get("CYBERQ_USER",  "")   # e.g. T-252541
FB_PASS       = os.environ.get("CYBERQ_PASS",  "")   # MQTT password / API token
POLL_SECS     = 12

# ── API ───────────────────────────────────────────────────────────────────────
API_BASE = "https://myflameboss.com/api/v1"

def api_get(path):
    user = FB_USER if FB_USER.upper().startswith("T-") else f"T-{FB_USER}"
    auth = "Basic " + base64.b64encode(f"{user}:{FB_PASS}".encode()).decode()
    req  = urllib.request.Request(
        f"{API_BASE}/{path}",
        headers={"Authorization": auth, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def tdc_to_c(tdc):
    try:
        v = int(tdc)
        return None if v <= -1000 else round(v / 10.0, 1)
    except Exception:
        return None

def send(payload):
    payload["token"] = AGENT_TOKEN
    body = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{DASHBOARD_URL}/api/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def poll():
    try:
        cooks_data = api_get("cooks")
        cooks_list = cooks_data if isinstance(cooks_data, list) else cooks_data.get("cooks", [])
        cook_id    = max((c["id"] for c in cooks_list if "id" in c), default=None) if cooks_list else None

        if not cook_id:
            send({"connected": False, "cook_id": None})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Ingen aktiv cook funnet")
            return

        cook   = api_get(f"cooks/{cook_id}")
        points = cook.get("data", [])
        if not points:
            send({"connected": False, "cook_id": cook_id})
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Cook {cook_id} — ingen datapunkter")
            return

        latest    = points[-1]
        raw_temps = [latest["pit_temp"], latest["meat_temp1"],
                     latest["meat_temp2"], latest["meat_temp3"]]
        temps = {str(i): {"c": tdc_to_c(t), "raw": t} for i, t in enumerate(raw_temps)}

        food_alarms = {}
        labels      = {}
        for i in range(1, 4):
            alarm = cook.get(f"meat_alarm_{i}")
            if alarm and int(alarm) > 0:
                food_alarms[str(i)] = int(alarm)
            name = cook.get(f"probe_name_{i}")
            if name:
                labels[str(i)] = name

        payload = {
            "connected":    True,
            "cook_id":      cook_id,
            "set_temp":     latest["set_temp"],
            "temps":        temps,
            "blower":       latest["fan_dc"] // 100,
            "food_alarms":  food_alarms,
            "labels":       labels,
            "history_point": {
                "ts":     datetime.now().strftime("%H:%M"),
                "blower": latest["fan_dc"] // 100,
                "probe0": tdc_to_c(latest["pit_temp"]),
                "probe1": tdc_to_c(latest["meat_temp1"]),
                "probe2": tdc_to_c(latest["meat_temp2"]),
                "probe3": tdc_to_c(latest["meat_temp3"]),
            }
        }

        send(payload)
        pit = tdc_to_c(latest["pit_temp"])
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅  Pit: {pit}°C  Blower: {latest['fan_dc']//100}%  Cook: {cook_id}")

    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️  {e}")
        try:
            send({"connected": False, "error": str(e)})
        except Exception:
            pass

def main():
    if not AGENT_TOKEN:
        print("❌  CYBERQ_TOKEN ikke satt. Se setup-siden i dashboardet.")
        sys.exit(1)
    if not FB_USER or not FB_PASS:
        print("❌  CYBERQ_USER og CYBERQ_PASS må settes.")
        sys.exit(1)
    print(f"CyberQ Agent startet → {DASHBOARD_URL}")
    print(f"Bruker: {FB_USER}  |  Poll: {POLL_SECS}s")
    print("Trykk Ctrl+C for å stoppe\n")
    while True:
        poll()
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    main()
