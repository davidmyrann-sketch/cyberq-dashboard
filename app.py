#!/usr/bin/env python3
"""
CyberQ Dashboard — SaaS edition
Multi-tenant: each user connects their own BBQ Guru CyberQ / FlameBoss device.
"""
import json, time, threading, ssl, os, uuid, base64, sqlite3, hashlib, secrets
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, g
from collections import deque
from datetime import datetime
import urllib.request
import paho.mqtt.client as mqtt
try:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
except ImportError:
    stripe = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_PATH             = os.environ.get("DB_PATH", "cyberq_saas.db")
STRIPE_WH_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_MONTH  = os.environ.get("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_YEAR   = os.environ.get("STRIPE_PRICE_ANNUAL", "")
APP_URL             = os.environ.get("APP_URL", "https://web-production-77bd1.up.railway.app")
API_BASE    = "https://myflameboss.com/api/v1"
MQTT_HOST   = "s2.myflameboss.com"
MQTT_PORT   = 8084
POLL_SECS   = 12

# ── Per-device runtime state ───────────────────────────────────────────────────
# device_id (str) → { state, history, ctrl, settings, mqttc, threads_started }
DEVICES = {}
DEVICES_LOCK = threading.Lock()

# ── Database ───────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        email                  TEXT UNIQUE NOT NULL,
        password_hash          TEXT NOT NULL,
        name                   TEXT,
        plan                   TEXT DEFAULT 'trial',
        trial_ends             TEXT,
        stripe_customer_id     TEXT,
        stripe_subscription_id TEXT,
        subscription_status    TEXT DEFAULT 'trial',
        created_at             TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS devices (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL REFERENCES users(id),
        device_id     TEXT NOT NULL,
        device_name   TEXT DEFAULT 'My CyberQ',
        fb_user       TEXT NOT NULL,
        fb_pass       TEXT NOT NULL,
        created_at    TEXT DEFAULT (datetime('now'))
    );
    """)
    # Migrate existing DB if columns missing
    try:
        db.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    except: pass
    try:
        db.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
    except: pass
    try:
        db.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'trial'")
    except: pass
    db.commit()
    db.close()

# ── Password helpers ───────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def check_pw(pw, h):
    return hash_pw(pw) == h

# ── Auth helpers ───────────────────────────────────────────────────────────────
def current_user():
    uid = session.get("user_id")
    if not uid: return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def subscription_ok(user):
    """Returns True if user has active trial or paid subscription."""
    status = user["subscription_status"] or "trial"
    if status == "active":
        return True
    if status == "trial":
        trial_ends = user["trial_ends"]
        if trial_ends and datetime.strptime(trial_ends, "%Y-%m-%d") >= datetime.now():
            return True
    return False

def subscription_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if not subscription_ok(user):
            return redirect(url_for("pricing_page"))
        return f(*args, **kwargs)
    return decorated

# ── Per-device state factory ───────────────────────────────────────────────────
def make_device_state():
    return {
        "state": {
            "connected": False, "mqtt_connected": False,
            "last_data": 0, "temps": {}, "set_temp": None,
            "user_set_temp": None, "food_alarms": {}, "blower": 0,
            "labels": {}, "ts": "--", "raw_msgs": deque(maxlen=20),
            "cook_id": None, "last_cnt": 0,
            "cook_timers": {
                1: {"duration_s":0.0,"start":0.0,"spent_s":0.0,"running":False,"fired":False},
                2: {"duration_s":0.0,"start":0.0,"spent_s":0.0,"running":False,"fired":False},
                3: {"duration_s":0.0,"start":0.0,"spent_s":0.0,"running":False,"fired":False},
            },
        },
        "history":  deque(maxlen=240),
        "ctrl": {
            "food_override": False, "food_override_probe": None,
            "ramping": False, "ramp_factor": 0.0,
            "ramp_active_set_c": None, "open_lid": False,
            "open_lid_since": 0.0, "pit_history": deque(maxlen=8),
            "ramp_locked_until": 0.0,
        },
        "settings": {
            "opendetect": 1, "cook_ramp": 0,
            "propband": 5, "cyctime": 20, "timeout_action": "Hold",
        },
        "mqttc": None,
        "last_poll_error": "",
        "threads_started": False,
    }

def get_device_ctx(device_id):
    with DEVICES_LOCK:
        if device_id not in DEVICES:
            DEVICES[device_id] = make_device_state()
        return DEVICES[device_id]

# ── Helpers ────────────────────────────────────────────────────────────────────
def tdc_to_c(tdc):
    try:
        v = int(tdc)
        return None if v <= -1000 else round(v / 10.0, 1)
    except: return None

def c_to_tdc(c):
    return int(float(c) * 10)

def compute_probe_status(c, target_c, alarm_c=None):
    if c is None: return "ERROR"
    if alarm_c is not None and c >= alarm_c: return "DONE"
    if target_c is not None:
        dev = c - target_c
        if dev > 8:  return "HIGH"
        if dev < -8: return "LOW"
    return "OK"

def api_get(path, fb_user, fb_pass):
    auth = "Basic " + base64.b64encode(f"{fb_user}:{fb_pass}".encode()).decode()
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        headers={"Authorization": auth, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ── Background threads per device ─────────────────────────────────────────────
def start_device_threads(device_id, fb_user, fb_pass):
    ctx = get_device_ctx(device_id)
    if ctx["threads_started"]:
        return
    ctx["threads_started"] = True

    def poll():
        while True:
            try:
                s = ctx["state"]
                device = api_get(f"devices/{device_id}", fb_user, fb_pass)
                s["connected"] = device.get("online", False)
                if s["set_temp"] is None:
                    raw_st = device.get("set_temp")
                    if raw_st:
                        s["set_temp"] = raw_st
                        if s["user_set_temp"] is None:
                            s["user_set_temp"] = raw_st

                meat_alarms = device.get("meat_alarms", [])
                if len(meat_alarms) >= 6:
                    for i in range(1, 4):
                        if i not in s["food_alarms"]:
                            tdc = meat_alarms[2 + i]
                            if tdc and tdc > 0:
                                s["food_alarms"][i] = tdc

                cook_info = device.get("most_recent_cook", {}) or {}
                for i in range(1, 4):
                    if str(i) not in s["labels"]:
                        name = cook_info.get(f"probe_name_{i}")
                        if name: s["labels"][str(i)] = name

                cook_id = cook_info.get("id") if cook_info else None
                if cook_id: s["cook_id"] = cook_id

                if cook_id and s["connected"]:
                    cook_data = api_get(f"cooks/{cook_id}", fb_user, fb_pass)
                    points = cook_data.get("data", [])
                    if points:
                        new_pts = [p for p in points if p["cnt"] > s["last_cnt"]]
                        if new_pts: s["last_cnt"] = new_pts[-1]["cnt"]
                        latest = points[-1]
                        s["ts"] = datetime.now().strftime("%H:%M:%S")
                        s["last_data"] = time.time()
                        s["set_temp"] = latest["set_temp"]
                        raw_temps = [latest["pit_temp"], latest["meat_temp1"], latest["meat_temp2"], latest["meat_temp3"]]
                        s["temps"] = {}
                        for i, raw in enumerate(raw_temps):
                            s["temps"][i] = {"c": tdc_to_c(raw), "raw": raw}
                        s["blower"] = latest["fan_dc"] // 100
                        for p in new_pts:
                            ctx["history"].append({
                                "ts":     datetime.fromtimestamp(p["sec"]).strftime("%H:%M"),
                                "blower": p["fan_dc"] // 100,
                                "probe0": tdc_to_c(p["pit_temp"]),
                                "probe1": tdc_to_c(p["meat_temp1"]),
                                "probe2": tdc_to_c(p["meat_temp2"]),
                                "probe3": tdc_to_c(p["meat_temp3"]),
                            })
            except Exception as e:
                ctx["last_poll_error"] = f"{type(e).__name__}: {e}"
                ctx["state"]["connected"] = False
            time.sleep(POLL_SECS)

    def mqtt_run():
        delay = 5
        topic = f"flameboss/{device_id}/recv"
        while True:
            try:
                cid = f"cyberq-{uuid.uuid4().hex[:8]}"
                mc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, transport="websockets")
                mc.ws_set_options(path="/mqtt")
                mc.username_pw_set(fb_user, fb_pass)
                mc.tls_set(cert_reqs=ssl.CERT_NONE)
                mc.tls_insecure_set(True)
                mc.on_connect    = lambda c,u,f,rc,p=None: ctx["state"].__setitem__("mqtt_connected", rc==0)
                mc.on_disconnect = lambda c,u,*a: ctx["state"].__setitem__("mqtt_connected", False)
                ctx["mqttc"] = mc
                mc.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
                mc.loop_forever()
                delay = 5
            except Exception as e:
                print(f"MQTT [{device_id}] {e}")
                time.sleep(delay)
                delay = min(delay * 2, 120)

    threading.Thread(target=poll,     daemon=True).start()
    threading.Thread(target=mqtt_run, daemon=True).start()

def mqtt_send(device_id, payload):
    ctx = get_device_ctx(device_id)
    mc  = ctx.get("mqttc")
    if mc and ctx["state"]["mqtt_connected"]:
        mc.publish(f"flameboss/{device_id}/recv", json.dumps(payload))
        return True
    return False

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def landing():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("landing.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name  = request.form.get("name", "").strip()
        pw    = request.form.get("password", "")
        if len(pw) < 6:
            error = "Passordet må være minst 6 tegn."
        else:
            db = get_db()
            if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                error = "E-posten er allerede registrert."
            else:
                from datetime import datetime, timedelta
                trial_ends = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
                db.execute("INSERT INTO users (email,password_hash,name,trial_ends) VALUES (?,?,?,?)",
                           (email, hash_pw(pw), name, trial_ends))
                db.commit()
                user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                session["user_id"] = user["id"]
                return redirect(url_for("setup"))
    return render_template("auth.html", mode="register", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw    = request.form.get("password", "")
        db    = get_db()
        user  = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_pw(pw, user["password_hash"]):
            session["user_id"] = user["id"]
            # Start threads for their device if configured
            dev = db.execute("SELECT * FROM devices WHERE user_id=?", (user["id"],)).fetchone()
            if dev:
                start_device_threads(dev["device_id"], dev["fb_user"], dev["fb_pass"])
            return redirect(url_for("dashboard"))
        error = "Feil e-post eller passord."
    return render_template("auth.html", mode="login", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))

@app.route("/setup", methods=["GET", "POST"])
@login_required
def setup():
    error = None
    if request.method == "POST":
        device_id   = request.form.get("device_id", "").strip()
        device_name = request.form.get("device_name", "Min CyberQ").strip()
        fb_user     = request.form.get("fb_user", "").strip()
        fb_pass     = request.form.get("fb_pass", "").strip()
        if not device_id or not fb_user or not fb_pass:
            error = "Alle felt må fylles ut."
        else:
            # Verify credentials
            try:
                api_get(f"devices/{device_id}", fb_user, fb_pass)
            except Exception as e:
                error = f"Kunne ikke koble til enheten: {e}"
            if not error:
                uid = session["user_id"]
                db  = get_db()
                db.execute("DELETE FROM devices WHERE user_id=?", (uid,))
                db.execute("INSERT INTO devices (user_id,device_id,device_name,fb_user,fb_pass) VALUES (?,?,?,?,?)",
                           (uid, device_id, device_name, fb_user, fb_pass))
                db.commit()
                start_device_threads(device_id, fb_user, fb_pass)
                return redirect(url_for("dashboard"))
    return render_template("setup.html", error=error)

# ── Payment routes ─────────────────────────────────────────────────────────────

@app.route("/pricing")
def pricing_page():
    user = current_user()
    return render_template("pricing.html", user=user,
                           stripe_key=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""))

@app.route("/checkout")
@login_required
def checkout():
    if not stripe or not stripe.api_key:
        return "Stripe not configured", 500
    plan  = request.args.get("plan", "monthly")
    price = STRIPE_PRICE_YEAR if plan == "annual" else STRIPE_PRICE_MONTH
    if not price:
        return "Stripe prices not configured", 500

    user = current_user()
    customer_id = user["stripe_customer_id"]
    if not customer_id:
        customer = stripe.Customer.create(
            email=user["email"],
            name=user["name"] or user["email"],
            metadata={"user_id": str(user["id"])}
        )
        customer_id = customer.id
        get_db().execute("UPDATE users SET stripe_customer_id=? WHERE id=?",
                         (customer_id, user["id"]))
        get_db().commit()

    session_obj = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price, "quantity": 1}],
        mode="subscription",
        success_url=APP_URL + "/checkout/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=APP_URL + "/pricing",
        allow_promotion_codes=True,
    )
    return redirect(session_obj.url, code=303)

@app.route("/checkout/success")
@login_required
def checkout_success():
    session_id = request.args.get("session_id")
    if session_id and stripe and stripe.api_key:
        try:
            cs = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
            sub = cs.subscription
            if sub:
                get_db().execute(
                    "UPDATE users SET stripe_subscription_id=?, subscription_status='active' WHERE id=?",
                    (sub.id, session["user_id"])
                )
                get_db().commit()
        except Exception as e:
            print(f"Checkout success error: {e}")
    return redirect(url_for("dashboard"))

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WH_SECRET)
    except Exception as e:
        return str(e), 400

    db    = sqlite3.connect(DB_PATH)
    etype = event["type"]

    if etype in ("customer.subscription.updated", "customer.subscription.created"):
        sub    = event["data"]["object"]
        status = "active" if sub["status"] in ("active", "trialing") else sub["status"]
        db.execute("UPDATE users SET subscription_status=?, stripe_subscription_id=? WHERE stripe_customer_id=?",
                   (status, sub["id"], sub["customer"]))
    elif etype == "customer.subscription.deleted":
        sub = event["data"]["object"]
        db.execute("UPDATE users SET subscription_status='cancelled' WHERE stripe_customer_id=?",
                   (sub["customer"],))
    elif etype == "invoice.payment_failed":
        inv = event["data"]["object"]
        db.execute("UPDATE users SET subscription_status='past_due' WHERE stripe_customer_id=?",
                   (inv["customer"],))

    db.commit()
    db.close()
    return "", 200

@app.route("/account")
@login_required
def account():
    user   = current_user()
    dev    = get_db().execute("SELECT * FROM devices WHERE user_id=?", (user["id"],)).fetchone()
    status = user["subscription_status"] or "trial"
    if status == "trial":
        trial_ends = user["trial_ends"]
        if trial_ends and datetime.strptime(trial_ends, "%Y-%m-%d") < datetime.now():
            status = "expired"
    return render_template("account.html", user=user, device=dev, status=status)

@app.route("/account/portal")
@login_required
def billing_portal():
    if not stripe or not stripe.api_key:
        return redirect(url_for("account"))
    user = current_user()
    if not user["stripe_customer_id"]:
        return redirect(url_for("pricing_page"))
    portal = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=APP_URL + "/account"
    )
    return redirect(portal.url, code=303)

@app.route("/dashboard")
@subscription_required
def dashboard():
    uid = session["user_id"]
    dev = get_db().execute("SELECT * FROM devices WHERE user_id=?", (uid,)).fetchone()
    if not dev:
        return redirect(url_for("setup"))
    start_device_threads(dev["device_id"], dev["fb_user"], dev["fb_pass"])
    return render_template("dashboard.html", device=dev)

# ── API routes (same as before, but scoped to user's device) ──────────────────

def user_device():
    uid = session.get("user_id")
    if not uid: return None
    return get_db().execute("SELECT * FROM devices WHERE user_id=?", (uid,)).fetchone()

@app.route("/api/status")
@login_required
def api_status():
    dev = user_device()
    if not dev: return jsonify({"error": "no device"}), 400
    ctx = get_device_ctx(dev["device_id"])
    s   = ctx["state"]

    set_c      = tdc_to_c(s["set_temp"]) if s["set_temp"] else None
    user_set_c = tdc_to_c(s["user_set_temp"]) if s["user_set_temp"] else None
    alarm_c    = {str(k): tdc_to_c(v) for k, v in s["food_alarms"].items() if v}

    probes = []
    for i in range(4):
        td  = s["temps"].get(i, {})
        c   = td.get("c")
        ac  = alarm_c.get(str(i)) if i > 0 else None
        probes.append({
            "index":  i,
            "name":   s["labels"].get(str(i), "Pit" if i==0 else f"Mat {i}"),
            "c":      c,
            "type":   "pit" if i==0 else "food",
            "status": compute_probe_status(c, set_c if i==0 else None, ac),
        })

    now = time.time()
    timers_out = {}
    for k, v in s["cook_timers"].items():
        spent     = v["spent_s"] + (now - v["start"] if v["running"] else 0)
        remaining = max(0.0, v["duration_s"] - spent)
        timers_out[str(k)] = {"running": v["running"], "duration_s": v["duration_s"],
                               "remaining": round(remaining,1), "fired": v["fired"]}

    return jsonify({
        "connected":       s["connected"],
        "mqtt_connected":  s["mqtt_connected"],
        "device_online":   (now - s["last_data"]) < 90,
        "probes":          probes,
        "set_temp_c":      set_c,
        "user_set_temp_c": user_set_c,
        "food_alarm_c":    alarm_c,
        "blower":          s["blower"],
        "ts":              s["ts"],
        "cook_id":         s["cook_id"],
        "settings":        ctx["settings"],
        "cook_timers":     timers_out,
        "ctrl": {
            "food_override":     ctx["ctrl"]["food_override"],
            "ramping":           ctx["ctrl"]["ramping"],
            "ramp_factor":       ctx["ctrl"]["ramp_factor"],
            "open_lid":          ctx["ctrl"]["open_lid"],
        },
    })

@app.route("/api/history")
@login_required
def api_history():
    dev = user_device()
    if not dev: return jsonify([])
    return jsonify(list(get_device_ctx(dev["device_id"])["history"]))

@app.route("/api/set_temp", methods=["POST"])
@login_required
def api_set_temp():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    body   = request.json
    temp_c = float(body.get("temp_c", 0))
    tdc    = c_to_tdc(temp_c)
    ctx    = get_device_ctx(dev["device_id"])
    ctx["state"]["user_set_temp"] = tdc
    ctx["ctrl"]["ramping"] = False
    ctx["ctrl"]["ramp_locked_until"] = time.time() + 120
    sent = mqtt_send(dev["device_id"], {"name": "set_temp", "set_temp": tdc})
    return jsonify({"ok": True, "set_c": temp_c, "mqtt_sent": sent})

@app.route("/api/set_food_temp", methods=["POST"])
@login_required
def api_set_food_temp():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    body   = request.json
    idx    = int(body.get("index", 1))
    temp_c = float(body.get("temp_c", 0))
    tdc    = c_to_tdc(temp_c)
    ctx    = get_device_ctx(dev["device_id"])
    ctx["state"]["food_alarms"][idx] = tdc
    sent = mqtt_send(dev["device_id"], {"name": "set_food", "food": idx, "set_temp": tdc})
    return jsonify({"ok": True, "index": idx, "set_c": temp_c, "mqtt_sent": sent})

@app.route("/api/set_label", methods=["POST"])
@login_required
def api_set_label():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    body  = request.json
    idx   = int(body.get("index", 0))
    label = body.get("label", "")[:16]
    ctx   = get_device_ctx(dev["device_id"])
    ctx["state"]["labels"][str(idx)] = label
    labels = [ctx["state"]["labels"].get(str(i), "") for i in range(4)]
    mqtt_send(dev["device_id"], {"name": "labels", "labels": labels})
    return jsonify({"ok": True})

@app.route("/api/timer", methods=["POST"])
@login_required
def api_timer():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    body   = request.json
    idx    = int(body.get("index", 1))
    action = body.get("action", "start")
    t      = get_device_ctx(dev["device_id"])["state"]["cook_timers"].get(idx)
    if t is None: return jsonify({"ok": False})
    now = time.time()
    if action == "set":
        h = int(body.get("hours", 0)); m = int(body.get("minutes", 0)); sc = int(body.get("seconds", 0))
        t["duration_s"] = float(h*3600 + m*60 + sc)
        t["spent_s"] = 0.0; t["start"] = 0.0; t["running"] = False; t["fired"] = False
    elif action == "start":
        if not t["running"] and t["duration_s"] > 0 and not t["fired"]:
            t["start"] = now; t["running"] = True
    elif action == "pause":
        if t["running"]: t["spent_s"] += now - t["start"]; t["running"] = False
    elif action == "reset":
        t["spent_s"] = 0.0; t["start"] = 0.0; t["running"] = False; t["fired"] = False
    spent = t["spent_s"] + (now - t["start"] if t["running"] else 0)
    return jsonify({"ok": True, "remaining": round(max(0.0, t["duration_s"]-spent),1), "running": t["running"]})

@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    body     = request.json
    ctx      = get_device_ctx(dev["device_id"])
    sett     = ctx["settings"]
    for key in ("opendetect", "cook_ramp", "propband", "cyctime"):
        if key in body: sett[key] = int(body[key])
    if "timeout_action" in body: sett["timeout_action"] = body["timeout_action"]
    return jsonify({"ok": True, "settings": sett})

@app.route("/api/sync")
@login_required
def api_sync():
    dev = user_device()
    if not dev: return jsonify({"ok": False}), 400
    mqtt_send(dev["device_id"], {"name": "sync"})
    return jsonify({"ok": True})

@app.route("/api/debug")
@login_required
def api_debug():
    dev = user_device()
    if not dev: return jsonify({"error": "no device"})
    ctx = get_device_ctx(dev["device_id"])
    s   = ctx["state"]
    return jsonify({
        "connected": s["connected"], "mqtt_connected": s["mqtt_connected"],
        "last_data_ago_s": round(time.time() - s["last_data"], 1),
        "cook_id": s["cook_id"], "last_poll_error": ctx["last_poll_error"],
    })

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8089))
    print(f"CyberQ SaaS → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
