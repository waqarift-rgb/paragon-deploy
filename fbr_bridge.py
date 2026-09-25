#!/usr/bin/env python3
"""
===============================================================
  FBR BRIDGE  (multi-client)  —  one server, many businesses
===============================================================

  Each client's Invoice Manager sends its own client id and its
  own password. The bridge looks up that client, uses that
  client's FBR token, and files the invoice under that client's
  NTN. No client can use another client's token.

  Tokens live here on the server, never in the HTML file the
  client holds. If a client's file is copied or emailed, their
  FBR token does not go with it.

---------------------------------------------------------------
  WHAT YOU GIVE A CLIENT
---------------------------------------------------------------
      Bridge URL      http://YOUR.SERVER.IP:8080/
      Client ID       e.g.  martindow
      Shared secret   their own password, different for each

  They enter those three on the FBR link screen. That's all.

---------------------------------------------------------------
  ADDING A CLIENT
---------------------------------------------------------------
      python3 bridge.py --add

  It asks for the name, NTN, and token, invents a password, and
  writes it into clients.json. No restart needed.

  ---------------------------------------------------------------
  ADDING COMPANIES  --  the easy way
  ---------------------------------------------------------------

  Open a browser on this server and go to:

      http://localhost:8080/admin

  The first visit asks you to set a password. After that you can
  add, edit, switch off and remove companies from that screen.
  Nothing needs to be typed into a file by hand.

  From another computer, use the server's address instead:

      http://YOUR.SERVER.IP:8080/admin

  Each company gets a CLIENT ID and a SHARED SECRET. Those two go
  into that company's Invoice Manager, on its FBR link screen,
  along with this server's address.

  The command line still works if you prefer it:
      python fbr_bridge_multi.py --add
      python fbr_bridge_multi.py --list
"""

PORT = 5000          # FBR bridge — 8090 Windows block, 5000 free
CLIENTS_FILE = "clients.json"

FBR_POST_SANDBOX     = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata_sb"
FBR_POST_PRODUCTION  = "https://gw.fbr.gov.pk/di_data/v1/di/postinvoicedata"
FBR_CHECK_SANDBOX    = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata_sb"
FBR_CHECK_PRODUCTION = "https://gw.fbr.gov.pk/di_data/v1/di/validateinvoicedata"

FBR_HSCODE_URL   = "https://gw.fbr.gov.pk/pdi/v1/itemdesccode"
FBR_UOM_URL      = "https://gw.fbr.gov.pk/pdi/v1/uom"
FBR_PROVINCE_URL = "https://gw.fbr.gov.pk/pdi/v1/provinces"
FBR_REGTYPE_URL  = "https://gw.fbr.gov.pk/dist/v1/Get_Reg_Type"
FBR_STATL_URL    = "https://gw.fbr.gov.pk/dist/v1/statl"

# Which SRO applies to a given tax rate, and which item serial numbers that SRO
# allows. FBR insists on both whenever the rate is not the standard 18%, and it
# will not name the valid values in the rejection message.
BUILD = "2026-09-20 lookups + units + shared data + admin"
EXTRA_ACTIONS = ["transtypes", "rates", "sro", "sroitems", "hsuom"]
SHARED_ACTIONS = ["number", "push", "pull", "forget", "stats"]

FBR_TRANSTYPE_URL = "https://gw.fbr.gov.pk/pdi/v1/transtypecode"
FBR_HSUOM_URL     = "https://gw.fbr.gov.pk/pdi/v2/HS_UOM"
FBR_SRO_URL       = "https://gw.fbr.gov.pk/pdi/v1/SroSchedule"
FBR_SROITEM_URL   = "https://gw.fbr.gov.pk/pdi/v2/SROItem"
FBR_RATE_URL      = "https://gw.fbr.gov.pk/pdi/v2/SaleTypeToRate"

import json, ssl, socket, os, sys, time, hmac, secrets, datetime
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)) or "."
CLIENTS_PATH = os.path.join(HERE, CLIENTS_FILE)
LOG_PATH = os.path.join(HERE, "bridge.log")

_fails = {}          # client id -> [count, locked until]


def note(*bits):
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + " ".join(str(b) for b in bits)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- clients

def load_clients():
    try:
        with open(CLIENTS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        note("clients.json could not be read:", e)
        return {}


def save_clients(clients):
    tmp = CLIENTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clients, fh, indent=2)
    os.replace(tmp, CLIENTS_PATH)
    try:
        os.chmod(CLIENTS_PATH, 0o600)      # owner only — it holds tokens
    except OSError:
        pass


def find_client(cid, secret):
    """Returns (client dict, error message). Constant-time secret check."""
    if not cid:
        return None, "No client id was sent. Set it on the FBR link screen."

    lock = _fails.get(cid)
    if lock and lock[1] > time.time():
        return None, "Too many wrong attempts. Try again in %d seconds." % int(lock[1] - time.time())

    c = load_clients().get(cid)
    if not c:
        note("unknown client id:", cid)
        return None, "That client id is not set up on this bridge."
    if not c.get("active", True):
        return None, "This client has been switched off. Contact your supplier."

    if not hmac.compare_digest(str(c.get("secret", "")), str(secret or "")):
        f = _fails.get(cid, [0, 0])
        f[0] += 1
        if f[0] >= 5:
            f[1] = time.time() + 60 * min(2 ** (f[0] - 5), 32)
        _fails[cid] = f
        note("wrong secret for client:", cid, "attempt", f[0])
        return None, "The shared secret does not match."

    _fails.pop(cid, None)
    return c, None


def token_for(c, env):
    live = (env == "production")
    tok = c.get("production_token" if live else "sandbox_token") or ""
    return tok, live


# ---------------------------------------------------------------- FBR

def call_fbr(url, token, payload=None, method="POST"):
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as res:
            body, status = res.read().decode("utf-8", "replace"), res.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode("utf-8", "replace"), e.code
    except (urllib.error.URLError, socket.timeout) as e:
        return 0, {"_unreachable": str(e)}

    try:
        return status, json.loads(body)
    except ValueError:
        pass

    # FBR sometimes sends its JSON wrapped in stray tabs and newlines, which is
    # not valid JSON. Salvage it rather than losing the reason inside a string.
    cleaned = body.strip()
    for attempt in (cleaned,
                    cleaned.replace("\t", "").replace("\r", "").replace("\n", ""),
                    cleaned[cleaned.find("{"): cleaned.rfind("}") + 1] if "{" in cleaned else ""):
        if not attempt:
            continue
        try:
            return status, json.loads(attempt)
        except ValueError:
            continue

    # still not JSON: pull the validation block out by hand so the reason shows
    at = cleaned.find('"validationResponse"')
    if at > -1:
        start = cleaned.find("{", at)
        depth, i = 0, start
        while i < len(cleaned):
            if cleaned[i] == "{": depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        try:
            return status, {"validationResponse": json.loads(cleaned[start:i + 1])}
        except ValueError:
            pass

    return status, {"_raw": body[:4000]}
def line_errors(vr):
    """Every complaint FBR made, line by line, including the code it used."""
    out = []
    if isinstance(vr, dict):
        for s in (vr.get("invoiceStatuses") or []):
            if not isinstance(s, dict):
                continue
            bits = []
            if s.get("errorCode"):
                bits.append(str(s["errorCode"]))
            if s.get("error"):
                bits.append(str(s["error"]))
            if not bits and s.get("status") and str(s["status"]).lower() != "valid":
                bits.append(str(s["status"]))
            if bits:
                out.append("Line %s: %s" % (s.get("itemSNo", "?"), " — ".join(bits)))
    return out


def why_rejected(data, vr):
    """FBR is not consistent about where it puts the reason. Look everywhere,
    and if it truly said nothing useful, hand back what it did send so the
    person is not left guessing."""
    for src in (vr, data):
        if not isinstance(src, dict):
            continue
        for key in ("error", "errorMessage", "message", "title", "status"):
            v = src.get(key)
            if v and str(v).strip().lower() not in ("invalid", "valid", "success", ""):
                code = src.get("errorCode") or ""
                return (str(code) + " — " if code else "") + str(v)
    code = (vr or {}).get("errorCode") or (data or {}).get("errorCode")
    if code:
        return "FBR error " + str(code)
    lines = line_errors(vr)
    if lines:
        return lines[0]
    raw = json.dumps(data)[:400] if data else ""
    return "FBR rejected it without saying why. It sent: " + raw if raw \
           else "FBR rejected the invoice"


def send_invoice(c, cid, req, check_only):
    env = req.get("env", "sandbox")
    tok, live = token_for(c, env)
    if not tok:
        return {"ok": False,
                "msg": "No %s token stored for this client. Ask your supplier to add it."
                       % ("production" if live else "sandbox")}

    url = (FBR_CHECK_PRODUCTION if live else FBR_CHECK_SANDBOX) if check_only else \
          (FBR_POST_PRODUCTION if live else FBR_POST_SANDBOX)

    status, data = call_fbr(url, tok, req.get("invoice"))

    if status == 0:
        return {"ok": False, "msg": "Could not reach FBR: " + data.get("_unreachable", "")}
    if status in (401, 403):
        return {"ok": False, "http": status,
                "msg": "FBR refused the token. It may have expired, or this server's IP "
                       "is not whitelisted against this NTN in IRIS."}

    vr = data.get("validationResponse") if isinstance(data, dict) else None
    good = (status == 200 and isinstance(vr, dict) and
            (vr.get("statusCode") == "00" or vr.get("status") in ("Valid", "Success")))
    irn = data.get("invoiceNumber", "") if isinstance(data, dict) else ""
    reason = "" if good else why_rejected(data, vr)

    note("%-10s %-6s %-14s -> %s %s" % (
        cid, "CHECK" if check_only else "POST",
        (req.get("meta") or {}).get("no", "?"), status,
        irn if good else reason))
    for ln in ([] if good else line_errors(vr)):
        note("            " + ln)

    return {
        "ok": bool(good),
        "http": status,
        "irn": irn,
        "msg": ("Passed the FBR check" if check_only else "Accepted by FBR") if good
               else reason,
        "errors": line_errors(vr),
        "raw": data,
    }


def reference(c, url, key, env):
    tok, _ = token_for(c, env)
    tok = tok or c.get("production_token") or c.get("sandbox_token") or ""
    if not tok:
        return {"ok": False, "msg": "No token stored for this client."}
    status, data = call_fbr(url, tok, method="GET")
    if status == 0:
        return {"ok": False, "msg": "Could not reach FBR."}
    if status != 200:
        return {"ok": False, "http": status, "msg": "FBR replied with %s." % status}
    items = data if isinstance(data, list) else (data.get("data") or data.get("items") or [])
    out = {"ok": True, "count": len(items)}
    out[key] = items
    return out


def _get(c, url, params, env, label):
    """GET a reference endpoint and hand back whatever list it gives."""
    token, _live = token_for(c, env)
    if not token:
        return {"ok": False, "msg": "No %s token is set in the bridge." % env}
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    full = url + ("?" + qs if qs else "")
    req = urllib.request.Request(full, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "msg": "FBR replied %s. %s" % (e.code, detail)}
    except Exception as e:
        return {"ok": False, "msg": "Could not reach FBR: %s" % e}
    try:
        data = json.loads(body)
    except Exception:
        return {"ok": False, "msg": "FBR sent something that is not JSON."}
    if isinstance(data, dict):
        data = data.get("data") or data.get("items") or data.get(label) or []
    note("%s -> %d row(s)" % (label, len(data) if isinstance(data, list) else 0))
    return {"ok": True, label: data, "url": full}


def _iso_date(s):
    """FBR is inconsistent: SaleTypeToRate takes 17-Sep-2026, SROItem takes
    2026-09-17. Convert whichever came in."""
    s = str(s or "").strip()
    if not s:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _try_dates(c, url, params, env, label, date_key="date"):
    """FBR is inconsistent about date shape between its own endpoints, and
    answers 500 or an empty list rather than saying so. Try both shapes."""
    given = params.get(date_key) or ""
    iso = _iso_date(given)
    shapes = []
    if iso: shapes.append(iso)
    if given and given != iso: shapes.append(given)
    if not shapes: shapes = [""]

    last = None
    for d in shapes:
        q = dict(params); q[date_key] = d
        r = _get(c, url, q, env, label)
        rows = r.get(label) if isinstance(r, dict) else None
        if r.get("ok") and isinstance(rows, list) and rows:
            return r
        last = r
        note("%s with date=%s gave nothing, trying the other shape" % (label, d))
    return last or {"ok": False, "msg": "FBR did not answer."}


def sale_rates(c, req, env):
    """Valid rates for a sale type, on a date. transTypeId comes from FBR's
    sale-type list; 18 is 'Goods at standard rate'."""
    return _try_dates(c, FBR_RATE_URL, {
        "date": req.get("date") or "",
        "transTypeId": req.get("transTypeId") or "",
        "originationSupplier": req.get("province") or "",
    }, env, "rates")


def sro_list(c, req, env):
    """Which SRO / schedule numbers are valid for a given rate id."""
    return _try_dates(c, FBR_SRO_URL, {
        "rate_id": req.get("rateId") or "",
        "date": req.get("date") or "",
        "origination_supplier_csv": req.get("province") or "",
    }, env, "sros")


def sro_items(c, req, env):
    """Which item serial numbers a given SRO allows. This is the one FBR
    rejects invoices over, and the only place to find the answer.

    FBR answers 500 rather than a useful message when the date is in the
    wrong shape, so both shapes are tried before giving up."""
    sro = req.get("sroId") or ""
    given = req.get("date") or ""
    tries = []
    iso = _iso_date(given)
    if iso:
        tries.append(iso)
    if given and given != iso:
        tries.append(given)
    if not tries:
        tries = [""]

    last = None
    for d in tries:
        r = _get(c, FBR_SROITEM_URL, {"date": d, "sro_id": sro}, env, "items")
        if r.get("ok"):
            return r
        last = r
        note("SROItem with date=%s failed, trying the other shape" % d)
    return last or {"ok": False, "msg": "FBR did not answer."}



def reg_type(c, ntn, env):
    tok, _ = token_for(c, env)
    tok = tok or c.get("production_token") or c.get("sandbox_token") or ""
    if not tok:
        return {"ok": False, "msg": "No token stored for this client."}
    if not ntn:
        return {"ok": False, "msg": "No registration number given."}
    status, data = call_fbr(FBR_REGTYPE_URL, tok, {"Registration_No": str(ntn)})
    if status != 200:
        return {"ok": False, "http": status, "msg": "FBR replied with %s." % status}
    # STATL - Active/Inactive status
    import datetime as _dt
    atl_status = ""
    try:
        st2, atl = call_fbr(FBR_STATL_URL, tok, {"regno": str(ntn), "date": _dt.date.today().isoformat()})
        if st2 == 200 and atl:
            atl_status = atl.get("status") or ""
    except Exception:
        pass
    # jo bhi FBR de - naam, address, province - sab wapas
    return {"ok": True,
            "ntn": data.get("REGISTRATION_NO", ntn),
            "type": data.get("REGISTRATION_TYPE", ""),
            "active": atl_status,
            "name": data.get("BUSINESS_NAME") or data.get("NAME") or data.get("TAXPAYER_NAME") or "",
            "address": data.get("ADDRESS") or data.get("BUSINESS_ADDRESS") or "",
            "province": data.get("PROVINCE") or data.get("PROVINCE_NAME") or "",
            "raw": data}


# ---------------------------------------------------------------- routing


# ================================================================
#  ADMIN SCREEN
#  ----------------------------------------------------------------
#  Open http://<this server>:8080/admin in a browser to add clients.
#  The password is set the first time the page is opened and is kept
#  as a salted hash in admin.json, never in plain text.
# ================================================================

import hashlib

ADMIN_PATH = os.path.join(HERE, "admin.json")
_sessions = {}                 # token -> expires at
_admin_fails = [0, 0]          # [count, locked until]


def admin_load():
    try:
        with open(ADMIN_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def admin_save(d):
    tmp = ADMIN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)
    os.replace(tmp, ADMIN_PATH)
    try:
        os.chmod(ADMIN_PATH, 0o600)
    except OSError:
        pass


def admin_hash(pw, salt):
    """Slow on purpose, so a stolen admin.json is not worth much."""
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               salt.encode("utf-8"), 120000).hex()


def admin_is_set():
    return bool(admin_load().get("hash"))


def admin_set_password(pw):
    if len(pw or "") < 8:
        return {"ok": False, "msg": "Choose a password of at least 8 characters."}
    salt = secrets.token_hex(16)
    admin_save({"salt": salt, "hash": admin_hash(pw, salt),
                "set_at": datetime.datetime.now().isoformat(timespec="seconds")})
    note("admin password set")
    return {"ok": True, "msg": "Password set."}


def admin_check(pw):
    d = admin_load()
    if not d.get("hash"):
        return False
    return hmac.compare_digest(admin_hash(pw or "", d.get("salt", "")), d["hash"])


def admin_signin(pw):
    now = time.time()
    if _admin_fails[1] > now:
        return {"ok": False,
                "msg": "Too many wrong attempts. Wait %d seconds."
                       % int(_admin_fails[1] - now)}
    if not admin_check(pw):
        _admin_fails[0] += 1
        if _admin_fails[0] >= 5:
            _admin_fails[1] = now + 60 * min(2 ** (_admin_fails[0] - 5), 32)
        note("wrong admin password, attempt", _admin_fails[0])
        return {"ok": False, "msg": "Wrong password."}
    _admin_fails[0] = 0
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = now + 3600                 # an hour
    return {"ok": True, "token": tok}


def admin_session_ok(tok):
    exp = _sessions.get(tok or "")
    if not exp:
        return False
    if exp < time.time():
        _sessions.pop(tok, None)
        return False
    _sessions[tok] = time.time() + 3600         # keep it alive while in use
    return True


# Features jo admin de sakta (client ke liye)
ADMIN_FEATURES = [
    ("reports",       "Reports"),
    ("returnSummary", "Tax Return Summary"),
    ("clients",       "Buyers"),
    ("items",         "Products"),
    ("hs",            "HS Code Search"),
    ("autoscenario",  "Auto Scenarios (FBR testing)"),
    ("downloadAll",   "Download All (Excel/PDF)"),
    ("bulkImport",    "Bulk Import"),
    ("emailInvoice",  "Email Invoices"),
    ("fbrProof",      "FBR Tax Proof"),
    ("autoTax",       "Automatic Tax Rate"),
    ("commercial",    "Commercial Invoice"),
    ("data",          "Backup & Restore"),
    ("users",         "User Management"),
    ("audit",         "Activity Log"),
]


def admin_clients_view():
    """The list for the page. Tokens are never sent back in full."""
    out = []
    for cid, c in sorted(load_clients().items()):
        def tail(v):
            v = str(v or "")
            return ("set, ending " + v[-6:]) if len(v) > 6 else ("set" if v else "")
        # client ke users (full.json se)
        users_list = []
        try:
            fpath = os.path.join(DATA_DIR, cid + "_full.json")
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as fh:
                    fdata = json.load(fh)
                for u in (fdata.get("users") or []):
                    users_list.append({
                        "user": u.get("user", ""),
                        "name": u.get("name", ""),
                        "role": u.get("role", ""),
                        "active": bool(u.get("active", True)),
                        "last": u.get("last", ""),
                    })
        except Exception:
            pass
        out.append({
            "id": cid,
            "name": c.get("name", ""),
            "ntn": c.get("ntn", ""),
            "strn": c.get("strn", ""),
            "addr": c.get("addr", ""),
            "phone": c.get("phone", ""),
            "prov": c.get("prov", "SINDH"),
            "email": c.get("email", ""),
            "web": c.get("web", ""),
            "active": bool(c.get("active", True)),
            "login_user": c.get("login_user", ""),
            "sandbox": tail(c.get("sandbox_token")),
            "production": tail(c.get("production_token")),
            "secret": tail(c.get("secret")),
            "added": c.get("added", ""),
            "users": users_list,
            "userCount": len(users_list),
            "features": c.get("features", {}),
        })
    return out


def admin_save_features(d):
    """Admin: client ke features set (jo do wahi client use kare)."""
    cid = str(d.get("id") or "").strip().lower()
    feats = d.get("features")
    if not isinstance(feats, dict):
        return {"ok": False, "msg": "Invalid features."}
    clients = load_clients()
    if cid not in clients:
        return {"ok": False, "msg": "Company not found."}
    clients[cid]["features"] = feats
    save_clients(clients)
    return {"ok": True}


def admin_toggle_active(d):
    """Admin: company block/unblock (active on/off)."""
    cid = str(d.get("id") or "").strip().lower()
    clients = load_clients()
    if cid not in clients:
        return {"ok": False, "msg": "Company not found."}
    clients[cid]["active"] = bool(d.get("active"))
    save_clients(clients)
    return {"ok": True, "active": clients[cid]["active"]}


def admin_toggle_user(d):
    """Admin: client ke ek user ko block/unblock."""
    cid = str(d.get("id") or "").strip().lower()
    uname = str(d.get("user") or "").strip().lower()
    active = bool(d.get("active"))
    fpath = os.path.join(DATA_DIR, cid + "_full.json")
    try:
        with open(fpath, "r", encoding="utf-8") as fh:
            fdata = json.load(fh)
    except Exception:
        return {"ok": False, "msg": "No data for this company."}
    found = False
    for u in (fdata.get("users") or []):
        if str(u.get("user", "")).lower() == uname:
            u["active"] = active
            found = True
            break
    if not found:
        return {"ok": False, "msg": "User not found."}
    tmp = fpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(fdata, fh, separators=(",", ":"))
    os.replace(tmp, fpath)
    try:
        os.chmod(fpath, 0o600)
    except OSError:
        pass
    return {"ok": True, "active": active}


def admin_save_client(d):
    cid = str(d.get("id") or "").strip().lower()
    if not cid or not cid.replace("-", "").replace("_", "").isalnum():
        return {"ok": False, "msg": "The client id must be letters, digits, - or _ only."}
    if not str(d.get("name") or "").strip():
        return {"ok": False, "msg": "Give the company a name."}

    clients = load_clients()
    c = clients.get(cid, {})
    fresh = cid not in clients

    c["name"] = str(d.get("name")).strip()
    c["ntn"] = str(d.get("ntn") or "").strip()
    c["strn"] = str(d.get("strn") or "").strip()
    c["addr"] = str(d.get("addr") or "").strip()
    c["phone"] = str(d.get("phone") or "").strip()
    c["prov"] = str(d.get("prov") or "SINDH").strip()
    c["email"] = str(d.get("email") or "").strip()
    c["web"] = str(d.get("web") or "").strip()
    c["active"] = bool(d.get("active", True))

    # blank means "leave the stored one alone", so a token is never wiped by accident
    for field, key in (("sandbox", "sandbox_token"),
                       ("production", "production_token")):
        v = str(d.get(field) or "").strip()
        if v:
            c[key] = v

    secret = str(d.get("secret") or "").strip()
    made = ""
    if secret:
        if len(secret) < 8:
            return {"ok": False, "msg": "The shared secret must be at least 8 characters."}
        c["secret"] = secret
    elif not c.get("secret"):
        made = secrets.token_urlsafe(18)
        c["secret"] = made

    # Client ka SOFTWARE LOGIN (username + password) - admin set kare
    # Features - admin jo diye (client sirf wahi use kare)
    if "features" in d and isinstance(d.get("features"), dict):
        c["features"] = d.get("features")

    lu = str(d.get("loginUser") or "").strip().lower()
    lp = str(d.get("loginPass") or "").strip()
    if lu:
        c["login_user"] = lu
    if lp:
        if len(lp) < 4:
            return {"ok": False, "msg": "Login password must be at least 4 characters."}
        salt = secrets.token_hex(8)
        c["login_salt"] = salt
        c["login_hash"] = admin_hash(lp, salt)
        c["login_mustchange"] = bool(d.get("mustChange", True))

    if fresh:
        c["added"] = datetime.datetime.now().strftime("%Y-%m-%d")

    clients[cid] = c
    save_clients(clients)
    note(("added" if fresh else "updated") + " client:", cid)
    return {"ok": True,
            "msg": ("Added " if fresh else "Saved ") + c["name"],
            "madeSecret": made}


def admin_delete_client(cid):
    cid = str(cid or "").strip().lower()
    clients = load_clients()
    if cid not in clients:
        return {"ok": False, "msg": "No such client."}
    name = clients[cid].get("name", cid)
    del clients[cid]
    save_clients(clients)
    note("removed client:", cid)
    return {"ok": True, "msg": "Removed " + name}


def admin_handle(path, req):
    """Everything under /admin/. Only sign-in and setup work without a session."""
    if path == "/admin/status":
        return {"ok": True, "passwordSet": admin_is_set(), "clients": len(load_clients())}
    if path == "/admin/setup":
        if admin_is_set():
            return {"ok": False, "msg": "A password is already set."}
        return admin_set_password(str(req.get("password") or ""))
    if path == "/admin/signin":
        return admin_signin(str(req.get("password") or ""))

    if not admin_session_ok(req.get("token")):
        return {"ok": False, "msg": "Sign in again.", "signedOut": True}

    if path == "/admin/list":    return {"ok": True, "clients": admin_clients_view()}
    if path == "/admin/save":    return admin_save_client(req.get("client") or {})
    if path == "/admin/delete":  return admin_delete_client(req.get("id"))
    if path == "/admin/toggle":  return admin_toggle_active(req)
    if path == "/admin/toggleuser": return admin_toggle_user(req)
    if path == "/admin/features": return admin_save_features(req)
    if path == "/admin/lock":
        _sessions.pop(req.get("token"), None)
        return {"ok": True, "msg": "Locked."}
    if path == "/admin/password":
        if not admin_check(str(req.get("current") or "")):
            return {"ok": False, "msg": "The current password is wrong."}
        r = admin_set_password(str(req.get("new") or ""))
        if r.get("ok"):
            _sessions.clear()
        return r
    return {"ok": False, "msg": "Unknown admin action."}


# ================================================================
#  SHARED DATA
#  ----------------------------------------------------------------
#  Invoices, buyers and products for every computer in the business.
#  Each one keeps its own copy and syncs with this, so work carries
#  on when the network does not.
#
#  Invoice numbers are handed out here and nowhere else.
# ================================================================

import threading

_store_lock = threading.Lock()
_BASE = globals().get("HERE") or os.path.dirname(os.path.abspath(__file__)) or "."
DATA_DIR = os.path.join(_BASE, "data")


def _data_path(cid):
    """One file per business. A single-business bridge uses 'main'."""
    safe = "".join(ch for ch in str(cid or "main")
                   if ch.isalnum() or ch in "-_") or "main"
    return os.path.join(DATA_DIR, safe + ".json")


def _blank():
    return {"invoices": {}, "clients": {}, "items": {},
            "counter": {}, "rev": 0}


def store_load(cid):
    try:
        with open(_data_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        for k in ("invoices", "clients", "items", "counter"):
            d.setdefault(k, {})
        d.setdefault("rev", 0)
        return d
    except (FileNotFoundError, ValueError, OSError):
        return _blank()


def store_save(cid, d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        pass
    path = _data_path(cid)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, separators=(",", ":"))
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def next_number(cid, req):
    """Hand out the next invoice number for a prefix, and move it on.

    The whole point of the server: two computers can never be given the
    same one. The prefix, separator and padding come from the asking
    computer so its Company settings still decide the shape."""
    prefix = str(req.get("prefix") or "")
    sep    = req.get("sep", "-")
    pad    = int(req.get("pad") or 4)
    start  = int(req.get("start") or 1)
    count  = max(1, min(int(req.get("count") or 1), 500))

    with _store_lock:
        d = store_load(cid)
        key = prefix + "|" + str(sep) + "|" + str(pad)
        n = d["counter"].get(key)
        if n is None:
            n = start                       # first ever: honour their setting
        nums = list(range(n, n + count))
        d["counter"][key] = n + count
        d["rev"] += 1
        store_save(cid, d)

    made = [(prefix + str(sep) if prefix else "") + str(i).zfill(pad) for i in nums]
    note("%s issued %s" % (cid, ", ".join(made[:3]) +
                           ("\u2026" if len(made) > 3 else "")))
    return {"ok": True, "numbers": made, "next": nums[-1] + 1}


# Client login sessions - ADMIN _sessions se ALAG (conflict fix)
_client_sessions = {}   # token -> {cid, at}

def client_login(cid, req):
    """Client login verify. Username SE client dhoondo (client id ki zaroorat nahi).
    Shared secret ki bhi zaroorat nahi - login hi kaafi."""
    lu = str(req.get("user") or "").strip().lower()
    lp = str(req.get("pass") or "")
    if not lu or not lp:
        return {"ok": False, "msg": "Enter your username and password."}

    # SAARE clients mein se login_user match karo (client id ki zaroorat nahi)
    clients = load_clients()
    found_cid, c = None, None
    for xcid, xc in clients.items():
        if str(xc.get("login_user", "")).lower() == lu:
            found_cid, c = xcid, xc
            break

    if not c:
        return {"ok": False, "msg": "Wrong username or password."}
    if not c.get("active", True):
        return {"ok": False, "msg": "This account is switched off. Contact your provider."}
    stored_hash = c.get("login_hash", "")
    stored_salt = c.get("login_salt", "")
    if not stored_hash:
        return {"ok": False, "msg": "No login has been set up by the administrator."}
    if admin_hash(lp, stored_salt) != stored_hash:
        return {"ok": False, "msg": "Wrong username or password."}

    # login sahi - session token banao (shared secret ki jagah)
    tok = secrets.token_urlsafe(24)
    _client_sessions[tok] = {"cid": found_cid, "at": time.time()}
    return {"ok": True, "user": lu, "client": found_cid,
            "session": tok,
            "mustChange": bool(c.get("login_mustchange", False)),
            "company": c.get("name", ""),
            "features": c.get("features", {}),
            "companyDetail": {
                "name": c.get("name", ""),
                "ntn": c.get("ntn", ""),
                "strn": c.get("strn", ""),
                "addr": c.get("addr", ""),
                "phone": c.get("phone", ""),
                "prov": c.get("prov", "SINDH"),
                "email": c.get("email", ""),
                "web": c.get("web", "")
            }}


def session_client(tok):
    """Client session token se cid nikaalo (har client ALAG)."""
    s = _client_sessions.get(tok)
    if not s:
        return None
    return s.get("cid")


def client_change_pass(cid, req):
    """Client apna password change kare (login ke baad)."""
    c = load_clients().get(cid)
    if not c:
        return {"ok": False, "msg": "Account not found."}
    old = str(req.get("old") or "")
    new = str(req.get("new") or "")
    if len(new) < 4:
        return {"ok": False, "msg": "New password must be at least 4 characters."}
    if admin_hash(old, c.get("login_salt", "")) != c.get("login_hash", ""):
        return {"ok": False, "msg": "Current password is wrong."}
    clients = load_clients()
    salt = secrets.token_hex(8)
    clients[cid]["login_salt"] = salt
    clients[cid]["login_hash"] = admin_hash(new, salt)
    clients[cid]["login_mustchange"] = False
    save_clients(clients)
    return {"ok": True}


def test_client_smtp(cid, req):
    """SMTP connection test - sirf login check (email na bheje)."""
    import smtplib, ssl
    smtp_host = str(req.get("smtpHost") or "").strip()
    smtp_port = int(req.get("smtpPort") or 587)
    smtp_user = str(req.get("smtpUser") or "").strip()
    smtp_pass = str(req.get("smtpPass") or "")
    if not (smtp_host and smtp_user and smtp_pass):
        return {"ok": False, "msg": "Fill your email, password and mail server first."}
    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as s:
                s.login(smtp_user, smtp_pass)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.starttls(context=ctx)
                s.login(smtp_user, smtp_pass)
        return {"ok": True, "msg": "Connected! Your email is set up correctly."}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "msg": "Login failed — check your email and app password."}
    except (smtplib.SMTPConnectError, OSError):
        return {"ok": False, "msg": "Could not reach the mail server — check the host and port."}
    except Exception as e:
        return {"ok": False, "msg": "Connection failed: " + str(e)[:100]}


def send_client_email(cid, req):
    """Client ki apni email (SMTP) se invoice bheje."""
    import smtplib, ssl
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = str(req.get("smtpHost") or "").strip()
    smtp_port = int(req.get("smtpPort") or 587)
    smtp_user = str(req.get("smtpUser") or "").strip()
    smtp_pass = str(req.get("smtpPass") or "")
    to = str(req.get("to") or "").strip()
    cc = str(req.get("cc") or "").strip()
    subject = str(req.get("subject") or "Invoice")
    html = str(req.get("html") or "")
    from_name = str(req.get("fromName") or smtp_user)

    if not (smtp_host and smtp_user and smtp_pass and to):
        return {"ok": False, "msg": "Email is not set up. Add your email settings on the Company page."}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_name + " <" + smtp_user + ">"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(html, "html"))

    recipients = [to] + ([cc] if cc else [])
    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as s:
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.starttls(context=ctx)
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_user, recipients, msg.as_string())
        return {"ok": True, "sent": to}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "msg": "Email login failed. Check your email and app password."}
    except Exception as e:
        return {"ok": False, "msg": "Could not send: " + str(e)[:120]}


def fulldata_save(cid, req):
    """Client ka POORA data server par save (users, company, invoices, sab)."""
    data = req.get("data")
    if not isinstance(data, dict):
        return {"ok": False, "msg": "data must be an object"}
    with _store_lock:
        path = os.path.join(DATA_DIR, cid + "_full.json")
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except OSError:
            pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return {"ok": True, "saved": True}


def fulldata_load(cid, req):
    """Client ka poora data server se load."""
    path = os.path.join(DATA_DIR, cid + "_full.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {"ok": True, "data": data}
    except (FileNotFoundError, ValueError, OSError):
        return {"ok": True, "data": None}   # koi data nahi (naya client)


def store_push(cid, req):
    """Take what this computer has changed. Newest write wins per record."""
    sent = req.get("records") or {}
    if not isinstance(sent, dict):
        return {"ok": False, "msg": "records must be an object"}

    kept, skipped = 0, 0
    with _store_lock:
        d = store_load(cid)
        for kind in ("invoices", "clients", "items"):
            for rid, rec in (sent.get(kind) or {}).items():
                if not isinstance(rec, dict):
                    continue
                have = d[kind].get(rid)
                # a record carries the moment it was last touched
                mine  = str((have or {}).get("_at") or "")
                theirs = str(rec.get("_at") or "")
                if have and mine and theirs and mine >= theirs:
                    skipped += 1
                    continue
                d[kind][rid] = rec
                kept += 1
        if kept:
            d["rev"] += 1
            store_save(cid, d)
    return {"ok": True, "saved": kept, "skipped": skipped, "rev": d["rev"]}


def store_pull(cid, req):
    """Give back everything, or only what changed since their last rev."""
    since = req.get("since")
    d = store_load(cid)
    if since is not None and int(since) == d["rev"]:
        return {"ok": True, "rev": d["rev"], "unchanged": True}
    return {"ok": True, "rev": d["rev"],
            "invoices": d["invoices"], "clients": d["clients"],
            "items": d["items"],
            "counts": {k: len(d[k]) for k in ("invoices", "clients", "items")}}


def store_forget(cid, req):
    """Remove records this computer deleted."""
    ids = req.get("ids") or {}
    gone = 0
    with _store_lock:
        d = store_load(cid)
        for kind in ("invoices", "clients", "items"):
            for rid in (ids.get(kind) or []):
                if rid in d[kind]:
                    del d[kind][rid]
                    gone += 1
        if gone:
            d["rev"] += 1
            store_save(cid, d)
    return {"ok": True, "removed": gone, "rev": d["rev"]}


def store_stats(cid):
    d = store_load(cid)
    try:
        size = os.path.getsize(_data_path(cid))
    except OSError:
        size = 0
    return {"ok": True, "rev": d["rev"], "bytes": size,
            "counts": {k: len(d[k]) for k in ("invoices", "clients", "items")},
            "counters": d["counter"]}



def handle(req):
    action = req.get("action", "")
    env = req.get("env", "sandbox")

    # clientlogin - auth se pehle (login karne ke liye)
    if action == "clientlogin":
        return client_login(req.get("client", ""), req)

    # Session token ho to us se client (shared secret ki zaroorat NAHI)
    sess_tok = req.get("session") or ""
    cid = ""
    c = None
    if sess_tok:
        scid = session_client(sess_tok)
        if scid:
            cid = scid
            c = load_clients().get(scid)
            if not c or not c.get("active", True):
                return {"ok": False, "msg": "Session invalid. Please sign in again."}
    # session nahi - purana tareeqa (client id + secret) - backward compat
    if not c:
        cid = str(req.get("client") or req.get("clientId") or "").strip().lower()
        c, err = find_client(cid, req.get("key"))
        if err:
            return {"ok": False, "msg": err}

    if action == "ping":
        return {"ok": True,
                "msg": "Bridge is running for " + c.get("name", cid),
                "client": cid,
                "sandboxToken": bool(c.get("sandbox_token")),
                "productionToken": bool(c.get("production_token"))}
    if action == "validate":  return send_invoice(c, cid, req, True)
    if action == "invoice":   return send_invoice(c, cid, req, False)
    if action == "hscodes":   return reference(c, FBR_HSCODE_URL, "codes", env)
    if action == "uom":       return reference(c, FBR_UOM_URL, "list", env)
    if action == "provinces": return reference(c, FBR_PROVINCE_URL, "list", env)
    if action == "regtype":   return reg_type(c, req.get("ntn"), env)
    if action == "number":    return next_number(cid, req)
    if action == "push":      return store_push(cid, req)
    if action == "pull":      return store_pull(cid, req)
    if action == "forget":    return store_forget(cid, req)
    if action == "stats":     return store_stats(cid)
    if action == "savedata":  return fulldata_save(cid, req)
    if action == "loaddata":  return fulldata_load(cid, req)
    if action == "sendemail": return send_client_email(cid, req)
    if action == "testsmtp": return test_client_smtp(cid, req)
    if action == "clientlogin": return client_login(cid, req)
    if action == "clientchangepass": return client_change_pass(cid, req)
    if action == "transtypes": return _get(c, FBR_TRANSTYPE_URL, {}, env, "types")
    if action == "rates":     return sale_rates(c, req, env)
    if action == "sro":       return sro_list(c, req, env)
    if action == "sroitems":  return sro_items(c, req, env)
    if action == "hsuom":     return _get(c, FBR_HSUOM_URL,
                                  {"hs_code": req.get("hsCode") or "",
                                   "annexure_id": req.get("annexure") or "3"},
                                  env, "units")
    if action == "backup":
        return {"ok": False,
                "msg": "This bridge does not do Google Drive backups. Keep the Apps Script "
                       "bridge for backups and use this one for FBR."}
    return {"ok": False, "msg": "Unknown action: " + str(action)}



ADMIN_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FBR Bridge &mdash; Companies</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
 --ink:#16202B;--ink-2:#55606D;--ink-3:#8794A3;--line:#D8DDE4;--soft:#F5F7F9;
 --blue:#1F5FCC;--blue-lt:#E7EFFC;--green:#17794B;--green-lt:#E8F3EC;
 --amber:#94620F;--amber-lt:#FCF6E9;--red:#C0322C;--red-lt:#FBEEED;--bg:#EEF1F5;
}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 background:var(--bg);color:var(--ink);font-size:14px;line-height:1.55}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 80px}
.top{display:flex;align-items:center;justify-content:space-between;
 padding-bottom:16px;border-bottom:2px solid var(--ink);margin-bottom:26px}
.brand{display:flex;align-items:center;gap:12px}
.mark{width:38px;height:38px;border-radius:8px;background:var(--blue);
 display:flex;align-items:center;justify-content:center;flex-shrink:0}
.mark i{display:block;width:17px;height:3px;background:#fff;border-radius:1px;
 box-shadow:0 6px 0 rgba(255,255,255,.62),0 12px 0 rgba(255,255,255,.34)}
h1{font-size:17px;font-weight:600;letter-spacing:-.2px}
h1 span{display:block;font-size:11px;font-weight:400;color:var(--ink-3);
 letter-spacing:.1em;margin-top:2px}
h2{font-size:15px;font-weight:600;margin-bottom:4px}
.card{background:#fff;border:1px solid var(--line);border-radius:4px;
 padding:22px 24px;margin-bottom:18px}
.muted{color:var(--ink-2);font-size:13px}
.small{color:var(--ink-3);font-size:12px}
label{display:block;font-size:12px;font-weight:600;margin-bottom:5px}
input[type=text],input[type=password]{width:100%;padding:9px 11px;font-size:14px;
 border:1px solid var(--line);border-radius:3px;font-family:inherit;background:#fff}
input:focus{outline:0;border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-lt)}
.field{margin-bottom:15px}
.help{font-size:11.5px;color:var(--ink-3);margin-top:4px}
.row{display:flex;gap:14px}.row>*{flex:1}
button{font-family:inherit;font-size:13.5px;font-weight:600;padding:9px 17px;
 border:1px solid var(--blue);background:var(--blue);color:#fff;border-radius:3px;
 cursor:pointer}
button:hover{opacity:.9}
button.ghost{background:#fff;color:var(--ink);border-color:var(--line)}
button.danger{background:#fff;color:var(--red);border-color:#E6C4C2}
button.sm{padding:5px 11px;font-size:12.5px;font-weight:500}
table{width:100%;border-collapse:collapse;margin-top:6px}
th{text-align:left;font-size:11px;letter-spacing:.06em;color:var(--ink-3);
 padding:9px 10px;border-bottom:1px solid var(--line);font-weight:600}
td{padding:11px 10px;border-bottom:1px solid #EDF0F3;vertical-align:middle}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;
 border-radius:10px}
.pill.on{background:var(--green-lt);color:var(--green)}
.pill.off{background:var(--soft);color:var(--ink-3)}
.pill.no{background:var(--amber-lt);color:var(--amber)}
.msg{padding:11px 14px;border-radius:3px;font-size:13px;margin-bottom:16px;
 border-left:3px solid}
.msg.good{background:var(--green-lt);border-color:var(--green);color:var(--green)}
.msg.bad{background:var(--red-lt);border-color:var(--red);color:var(--red)}
.msg.warn{background:var(--amber-lt);border-color:var(--amber);color:var(--amber)}
.empty{text-align:center;padding:36px 20px;color:var(--ink-3);font-size:13.5px}
.gate{max-width:390px;margin:60px auto}
.hide{display:none}
code{font-family:Consolas,Menlo,monospace;font-size:12.5px;background:var(--soft);
 padding:1px 5px;border-radius:2px}
.sec{background:var(--soft);border:1px solid var(--line);border-radius:3px;
 padding:11px 13px;font-family:Consolas,Menlo,monospace;font-size:13px;
 word-break:break-all;margin-top:8px}
.foot{margin-top:26px;padding-top:14px;border-top:1px solid var(--line);
 font-size:11.5px;color:var(--ink-3);display:flex;justify-content:space-between}
@media(max-width:640px){.row{display:block}.row>*{margin-bottom:15px}
 td,th{padding:8px 6px;font-size:12.5px}}
</style></head><body>
<div class="wrap">

 <div class="top">
  <div class="brand"><div class="mark"><i></i></div>
   <h1>FBR Bridge<span>COMPANIES ON THIS SERVER</span></h1></div>
  <div id="topRight"></div>
 </div>

 <div id="msg"></div>

 <!-- first run: choose a password -->
 <div id="setup" class="card gate hide">
  <h2>Set a password</h2>
  <p class="muted" style="margin-bottom:16px">This screen holds every
   company's FBR token, so it needs a password before anything else.</p>
  <div class="field"><label>Password</label>
   <input type="password" id="pw1" autocomplete="new-password">
   <div class="help">At least 8 characters. Write it down &mdash; it cannot be
    recovered.</div></div>
  <div class="field"><label>Type it again</label>
   <input type="password" id="pw2" autocomplete="new-password"></div>
  <button onclick="doSetup()">Set password</button>
 </div>

 <!-- sign in -->
 <div id="gate" class="card gate hide">
  <h2>Sign in</h2>
  <p class="muted" style="margin-bottom:16px">Enter the bridge password.</p>
  <div class="field"><label>Password</label>
   <input type="password" id="pw" autocomplete="current-password"
    onkeydown="if(event.key==='Enter')doSignin()"></div>
  <button onclick="doSignin()">Sign in</button>
 </div>

 <!-- the list -->
 <div id="main" class="hide">
  <div class="card">
   <div style="display:flex;justify-content:space-between;align-items:center;
    margin-bottom:10px">
    <div><h2>Companies</h2>
     <div class="small" id="count"></div></div>
    <button onclick="openForm()">+ Add a company</button>
   </div>
   <div id="list"></div>
  </div>

  <!-- add or edit -->
  <div id="form" class="card hide">
   <h2 id="formTitle">Add a company</h2>
   <p class="muted" style="margin-bottom:18px">The client id and the shared
    secret go into that company's Invoice Manager, on its FBR link screen.</p>

   <div class="row">
    <div class="field"><label>Company name</label>
     <input type="text" id="fName" placeholder="Martindow Pvt Ltd"></div>
    <div class="field"><label>Client id</label>
     <input type="text" id="fId" placeholder="martindow">
     <div class="help">Letters, digits, - and _ only. Cannot be changed later.</div>
    </div>
   </div>

   <div class="row">
    <div class="field"><label>NTN</label>
     <input type="text" id="fNtn" placeholder="8963611"></div>
    <div class="field"><label>STRN <span class="small">optional</span></label>
     <input type="text" id="fStrn" placeholder="3277876180532"></div>
   </div>
   <div class="field"><label>Address</label>
    <input type="text" id="fAddr" placeholder="Company address"></div>
   <div class="row">
    <div class="field"><label>Phone</label>
     <input type="text" id="fPhone" placeholder="021-1234567"></div>
    <div class="field"><label>Province</label>
     <select id="fProv">
      <option>SINDH</option><option>PUNJAB</option><option>KHYBER PAKHTUNKHWA</option>
      <option>BALOCHISTAN</option><option>CAPITAL TERRITORY</option>
      <option>AZAD JAMMU AND KASHMIR</option><option>GILGIT BALTISTAN</option><option>FATA/PATA</option>
     </select></div>
   </div>
   <div class="row">
    <div class="field"><label>Email <span class="small">optional</span></label>
     <input type="text" id="fEmail" placeholder="info@company.com"></div>
    <div class="field"><label>Website <span class="small">optional</span></label>
     <input type="text" id="fWeb" placeholder="www.company.com"></div>
   </div>

   <div class="field"><label>Sandbox token</label>
    <input type="text" id="fSandbox" placeholder="paste from IRIS">
    <div class="help" id="hSandbox">From IRIS &rarr; API Integration &rarr;
     Sandbox Environment.</div></div>

   <div class="field"><label>Production token</label>
    <input type="text" id="fProd" placeholder="paste once testing has passed">
    <div class="help" id="hProd">Only after all the company's scenarios
     pass.</div></div>

   <div class="field"><label>Shared secret <span class="small">optional — older setups only</span></label>
    <input type="text" id="fSecret" placeholder="leave blank and one is made for you">
    <div class="help">This must match what is typed on that company's FBR link
     screen.</div></div>

   <div style="border-top:1px solid #e2e8f0;margin:16px 0 8px;padding-top:14px;font-weight:600;color:#1C2E4A">Software Login (client uses this to sign in)</div>
   <div class="field"><label>Login username</label>
    <input type="text" id="fLoginUser" placeholder="e.g. matts">
    <div class="help">The username the client types to sign in to the software.</div></div>
   <div class="field"><label>Login password</label>
    <input type="text" id="fLoginPass" placeholder="set an initial password">
    <div class="help">The client signs in with this, then changes it. Without a login set here, the client cannot access the software.</div></div>

   <div class="field"><label style="display:flex;align-items:center;gap:8px;
     font-weight:400;font-size:13.5px">
     <input type="checkbox" id="fActive" checked style="width:auto">
     Switched on &mdash; untick to stop this company filing without deleting
     anything</label></div>

   <div id="madeSecret" class="hide"></div>

   <div style="display:flex;gap:10px;margin-top:20px">
    <button onclick="saveClient()">Save</button>
    <button class="ghost" onclick="closeForm()">Cancel</button>
   </div>
  </div>

  <div class="card">
   <h2>Password</h2>
   <div class="row" style="margin-top:12px">
    <div class="field"><label>Current</label>
     <input type="password" id="cpOld"></div>
    <div class="field"><label>New</label>
     <input type="password" id="cpNew"></div>
   </div>
   <button class="ghost" onclick="changePw()">Change password</button>
  </div>

  <div class="foot">
   <span>Tokens are kept in <code>clients.json</code> beside the bridge. Back
    that file up.</span>
   <span><a href="#" onclick="doLock();return false">Lock this screen</a></span>
  </div>
 </div>

</div>

<script>
var TOKEN = '', EDITING = null;

function $(id){ return document.getElementById(id); }
function show(id){ $(id).classList.remove('hide'); }
function hide(id){ $(id).classList.add('hide'); }
function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

function say(text, kind){
  $('msg').innerHTML = text ? '<div class="msg ' + (kind || 'good') + '">' +
    esc(text) + '</div>' : '';
  if(text) window.scrollTo(0, 0);
}

function post(path, body){
  return fetch(path, { method:'POST', headers:{'Content-Type':'text/plain'},
    body: JSON.stringify(Object.assign({ token: TOKEN }, body || {})) })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d && d.signedOut){ TOKEN = ''; showGate(); say('Signed out. Sign in again.','warn'); }
      return d;
    });
}

/* ---------- getting in ---------- */
function boot(){
  post('/admin/status').then(function(d){
    if(!d.passwordSet){ hide('gate'); show('setup'); $('pw1').focus(); }
    else showGate();
  }).catch(function(){ say('The bridge is not answering. Is it still running?','bad'); });
}

function showGate(){
  hide('setup'); hide('main'); show('gate');
  $('topRight').innerHTML = '';
  $('pw').value = ''; $('pw').focus();
}

function doSetup(){
  var a = $('pw1').value, b = $('pw2').value;
  if(a !== b) return say('The two passwords do not match.','bad');
  post('/admin/setup', { password:a }).then(function(d){
    if(!d.ok) return say(d.msg, 'bad');
    say('Password set. Sign in with it.','good');
    showGate();
  });
}

function doSignin(){
  post('/admin/signin', { password: $('pw').value }).then(function(d){
    if(!d.ok) return say(d.msg, 'bad');
    TOKEN = d.token; say('');
    hide('gate'); show('main');
    $('topRight').innerHTML = '<button class="ghost sm" onclick="doLock()">Lock</button>';
    loadList();
  });
}

function doLock(){
  post('/admin/lock').then(function(){ TOKEN = ''; showGate(); say('Locked.','good'); });
}

/* ---------- the list ---------- */
function loadList(){
  post('/admin/list').then(function(d){
    if(!d.ok) return;
    var c = d.clients || [];
    $('count').textContent = c.length === 0 ? 'None yet' :
      c.length + (c.length === 1 ? ' company' : ' companies') + ' on this bridge';
    if(!c.length){
      $('list').innerHTML = '<div class="empty">No companies yet.<br>' +
        'Add one, then give it the client id and shared secret.</div>';
      return;
    }
    $('list').innerHTML = '<table><tr><th>Company</th><th>Client id</th>' +
      '<th>Sandbox</th><th>Production</th><th>Status</th><th></th></tr>' +
      c.map(function(x){
        return '<tr><td><b>' + esc(x.name) + '</b>' +
          (x.ntn ? '<div class="small">NTN ' + esc(x.ntn) + '</div>' : '') + '</td>' +
          '<td><code>' + esc(x.id) + '</code></td>' +
          '<td>' + (x.sandbox ? '<span class="pill on">set</span>'
                              : '<span class="pill no">none</span>') + '</td>' +
          '<td>' + (x.production ? '<span class="pill on">set</span>'
                                 : '<span class="pill no">none</span>') + '</td>' +
          '<td>' + (x.active ? '<span class="pill on">on</span>'
                             : '<span class="pill off">off</span>') + '</td>' +
          '<td style="text-align:right;white-space:nowrap">' +
            (x.userCount ? '<button class="ghost sm" onclick="showUsers(\'' + esc(x.id) + '\')">Users (' + x.userCount + ')</button> ' : '') +
            '<button class="ghost sm" onclick="showFeatures(\'' + esc(x.id) + '\')">Features</button> ' +
            '<button class="' + (x.active ? 'ghost' : 'primary') + ' sm" onclick="toggleActive(\'' + esc(x.id) + '\',' + (x.active ? 'false' : 'true') + ')">' +
              (x.active ? 'Block' : 'Unblock') + '</button> ' +
            '<button class="ghost sm" onclick="editClient(\'' + esc(x.id) +
              '\')">Edit</button> ' +
            '<button class="danger sm" onclick="delClient(\'' + esc(x.id) + '\',\'' +
              esc(x.name) + '\')">Remove</button></td></tr>';
      }).join('') + '</table>';
    window._clients = c;
  });
}

/* ---------- block/unblock + users ---------- */
// ADMIN_FEATURES list (JS - server se match)
var ADMIN_FEATURES = [
  ['reports','Reports'],['returnSummary','Tax Return Summary'],['clients','Buyers'],
  ['items','Products'],['hs','HS Code Search'],['autoscenario','Auto Scenarios'],
  ['downloadAll','Download All (Excel/PDF)'],['bulkImport','Bulk Import'],
  ['emailInvoice','Email Invoices'],['fbrProof','FBR Tax Proof'],['autoTax','Automatic Tax Rate'],
  ['commercial','Commercial Invoice'],['data','Backup & Restore'],['users','User Management'],
  ['audit','Activity Log']
];
function showFeatures(id){
  var c = (window._clients || []).find(function(x){ return x.id === id; });
  if(!c) return;
  var feats = c.features || {};
  var rows = ADMIN_FEATURES.map(function(f){
    var on = feats[f[0]] !== false;  // default on
    return '<div style="display:flex;align-items:center;justify-content:space-between;'+
      'padding:10px 0;border-bottom:1px solid #eef1f5">'+
      '<span style="font-size:13.5px">' + esc(f[1]) + '</span>'+
      '<label style="position:relative;display:inline-block;width:42px;height:23px">'+
        '<input type="checkbox" data-feat="' + f[0] + '"' + (on?' checked':'') + ' style="opacity:0;width:0;height:0">'+
        '<span class="ftgl"></span></label></div>';
  }).join('');
  var html = '<h3 style="margin-bottom:6px">Features — ' + esc(c.name) + '</h3>'+
    '<p style="font-size:12.5px;color:#666;margin-bottom:14px">Turn features on or off for this company. '+
    'The client only sees what is turned on here.</p>'+
    '<div id="featList">' + rows + '</div>'+
    '<div style="margin-top:16px;text-align:right">'+
      '<button class="ghost" onclick="closeFeatures()">Cancel</button> '+
      '<button class="primary" onclick="saveFeatures(\'' + esc(id) + '\')">Save features</button></div>';
  var box = document.getElementById('featBox');
  if(!box){
    box = document.createElement('div'); box.id = 'featBox';
    box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
    document.body.appendChild(box);
    var st = document.createElement('style');
    st.textContent = '.ftgl{position:absolute;cursor:pointer;inset:0;background:#ccc;border-radius:23px;transition:.2s}.ftgl:before{content:"";position:absolute;height:17px;width:17px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.2s}input:checked+.ftgl{background:#2563EB}input:checked+.ftgl:before{transform:translateX(19px)}';
    document.head.appendChild(st);
  }
  box.innerHTML = '<div style="background:#fff;border-radius:10px;padding:24px;max-width:520px;width:90%;max-height:80vh;overflow:auto">' + html + '</div>';
  box.style.display = 'flex';
}
function closeFeatures(){ var b=document.getElementById('featBox'); if(b) b.style.display='none'; }
function saveFeatures(id){
  var feats = {};
  document.querySelectorAll('#featList input[data-feat]').forEach(function(inp){
    feats[inp.getAttribute('data-feat')] = inp.checked;
  });
  post('/admin/features', { id: id, features: feats }).then(function(d){
    if(d.ok){ say('Features saved', 'good'); closeFeatures(); loadList(); }
    else say(d.msg || 'Failed', 'bad');
  });
}

function toggleActive(id, active){
  var word = active ? 'unblock' : 'block';
  if(!confirm('Are you sure you want to ' + word + ' this company?')) return;
  post('/admin/toggle', { id: id, active: active }).then(function(d){
    if(d.ok){ say(active ? 'Company unblocked' : 'Company blocked', "good"); loadList(); }
    else say(d.msg || 'Failed', "bad");
  });
}
function showUsers(id){
  var c = (window._clients || []).find(function(x){ return x.id === id; });
  if(!c){ return; }
  var users = c.users || [];
  var rows = users.length ? users.map(function(u){
    return '<tr><td><b>' + esc(u.user) + '</b></td>' +
      '<td>' + esc(u.name || '') + '</td>' +
      '<td>' + esc(u.role || '') + '</td>' +
      '<td>' + (u.active ? '<span class="pill on">active</span>' : '<span class="pill off">blocked</span>') + '</td>' +
      '<td style="text-align:right"><button class="' + (u.active ? 'ghost' : 'primary') + ' sm" ' +
        'onclick="toggleUser(\'' + esc(id) + '\',\'' + esc(u.user) + '\',' + (u.active ? 'false' : 'true') + ')">' +
        (u.active ? 'Block' : 'Unblock') + '</button></td></tr>';
  }).join('') : '<tr><td colspan="5" style="color:#888">No users yet</td></tr>';
  var html = '<h3 style="margin-bottom:12px">Users — ' + esc(c.name) + '</h3>' +
    '<table style="width:100%"><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th><th></th></tr>' +
    rows + '</table>' +
    '<div style="margin-top:14px;text-align:right"><button class="primary" onclick="closeUsers()">Close</button></div>';
  var box = document.getElementById('usersBox');
  if(!box){
    box = document.createElement('div');
    box.id = 'usersBox';
    box.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:999';
    document.body.appendChild(box);
  }
  box.innerHTML = '<div style="background:#fff;border-radius:10px;padding:24px;max-width:640px;width:90%;max-height:80vh;overflow:auto">' + html + '</div>';
  box.style.display = 'flex';
}
function closeUsers(){ var b=document.getElementById('usersBox'); if(b) b.style.display='none'; }
function toggleUser(id, user, active){
  post('/admin/toggleuser', { id: id, user: user, active: active }).then(function(d){
    if(d.ok){ say(active ? 'User unblocked' : 'User blocked', "good"); loadList(); setTimeout(function(){ showUsers(id); }, 300); }
    else say(d.msg || 'Failed', "bad");
  });
}

/* ---------- add and edit ---------- */
function openForm(){
  EDITING = null;
  $('formTitle').textContent = 'Add a company';
  ['fName','fId','fNtn','fStrn','fAddr','fPhone','fProv','fEmail','fWeb','fSandbox','fProd','fSecret','fLoginUser','fLoginPass'].forEach(function(f){
    $(f).value = ''; });
  $('fActive').checked = true;
  $('fId').disabled = false;
  $('fSandbox').placeholder = 'paste from IRIS';
  $('fProd').placeholder = 'paste once testing has passed';
  $('fSecret').placeholder = 'leave blank and one is made for you';
  hide('madeSecret'); show('form');
  $('fName').focus();
}

function closeForm(){ hide('form'); say(''); }

function editClient(id){
  var x = (window._clients || []).filter(function(c){ return c.id === id; })[0];
  if(!x) return;
  EDITING = id;
  $('formTitle').textContent = 'Edit ' + x.name;
  $('fName').value = x.name; $('fId').value = x.id; $('fNtn').value = x.ntn || '';
  // Poori detail fill (edit mein khali na ho)
  if($('fStrn')) $('fStrn').value = x.strn || '';
  if($('fAddr')) $('fAddr').value = x.addr || '';
  if($('fPhone')) $('fPhone').value = x.phone || '';
  if($('fProv')) $('fProv').value = x.prov || 'SINDH';
  if($('fEmail')) $('fEmail').value = x.email || '';
  if($('fWeb')) $('fWeb').value = x.web || '';
  if($('fLoginUser')) $('fLoginUser').value = x.login_user || '';
  if($('fLoginPass')){ $('fLoginPass').value = ''; $('fLoginPass').placeholder = x.login_user ? 'set — leave blank to keep' : 'set an initial password'; }
  $('fId').disabled = true;
  $('fActive').checked = !!x.active;
  /* tokens are never sent back in full, so blank means "keep what is stored" */
  $('fSandbox').value = ''; $('fProd').value = ''; $('fSecret').value = '';
  $('fSandbox').placeholder = x.sandbox ? x.sandbox + ' \u2014 leave blank to keep'
                                        : 'paste from IRIS';
  $('fProd').placeholder    = x.production ? x.production + ' \u2014 leave blank to keep'
                                           : 'paste once testing has passed';
  $('fSecret').placeholder  = x.secret ? x.secret + ' \u2014 leave blank to keep'
                                       : 'leave blank and one is made for you';
  hide('madeSecret'); show('form');
  $('fName').focus();
}

function saveClient(){
  post('/admin/save', { client: {
    id: EDITING || $('fId').value,
    name: $('fName').value,
    ntn: $('fNtn').value,
    sandbox: $('fSandbox').value,
    production: $('fProd').value,
    secret: $('fSecret').value, strn: $('fStrn')?$('fStrn').value:'', addr: $('fAddr')?$('fAddr').value:'', phone: $('fPhone')?$('fPhone').value:'', prov: $('fProv')?$('fProv').value:'', email: $('fEmail')?$('fEmail').value:'', web: $('fWeb')?$('fWeb').value:'', loginUser: $('fLoginUser')?$('fLoginUser').value:'', loginPass: $('fLoginPass')?$('fLoginPass').value:'',
    active: $('fActive').checked
  }}).then(function(d){
    if(!d.ok) return say(d.msg, 'bad');
    if(d.madeSecret){
      $('madeSecret').className = '';
      $('madeSecret').innerHTML =
        '<div class="msg warn" style="margin:16px 0 0">' +
        'A shared secret was made for this company. Copy it now \u2014 it is not ' +
        'shown again in full.<div class="sec">' + esc(d.madeSecret) + '</div></div>';
      say(d.msg, 'good');
      loadList();
      return;
    }
    hide('form'); say(d.msg, 'good'); loadList();
  });
}

function delClient(id, name){
  if(!confirm('Remove ' + name + ' from this bridge?\n\n' +
              'Its token and secret are deleted. It will no longer be able to ' +
              'file invoices through this server.')) return;
  post('/admin/delete', { id:id }).then(function(d){
    say(d.msg, d.ok ? 'good' : 'bad'); loadList();
  });
}

function changePw(){
  var o = $('cpOld').value, n = $('cpNew').value;
  if(!o || !n) return say('Fill in both boxes.','bad');
  post('/admin/password', { current:o, new:n }).then(function(d){
    if(!d.ok) return say(d.msg, 'bad');
    $('cpOld').value = ''; $('cpNew').value = '';
    TOKEN = ''; showGate();
    say('Password changed. Sign in with the new one.','good');
  });
}

boot();
</script>
</body></html>
"""



class Handler(BaseHTTPRequestHandler):
    server_version = "FBRBridge/2.0"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({"ok": True})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/admin":
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        # / ya /app par Invoice Manager software serve karo (file se)
        if path in ("/", "/app", "/invoice", "/index.html"):
            app_file = os.path.join(HERE, "invoice_manager.html")
            if os.path.exists(app_file):
                body = open(app_file, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            # file nahi to JSON status
            self._send({"ok": True, "msg": "FBR bridge running. Invoice Manager file not found on server.",
                        "clients": len(load_clients())})
            return

        self._send({"ok": True, "msg": "FBR bridge is running.",
                    "clients": len(load_clients()),
                    "admin": "Open /admin to manage companies. Open / for the software."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 8 * 1024 * 1024:
                return self._send({"ok": False, "msg": "Request too large"}, 413)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            req = json.loads(raw) if raw else {}
        except Exception as e:
            return self._send({"ok": False, "msg": "Could not read the request: %s" % e}, 400)
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path.startswith("/admin"):
                self._send(admin_handle(path, req))
            else:
                self._send(handle(req))
        except Exception as e:
            note("ERROR", repr(e))
            self._send({"ok": False, "msg": "Bridge error: %s" % e}, 500)

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- admin

def cmd_add():
    clients = load_clients()
    print("\nAdd a client\n" + "-" * 40)
    cid = input("Client id (short, no spaces, e.g. martindow): ").strip().lower()
    if not cid or not cid.replace("-", "").replace("_", "").isalnum():
        print("Use letters and numbers only."); return
    if cid in clients:
        print("That id already exists. Pick another."); return

    name = input("Business name: ").strip()
    ntn  = input("NTN (7 digits, no check digit): ").strip()
    sbx  = input("Sandbox token (blank if not yet issued): ").strip()
    prod = input("Production token (blank for now): ").strip()

    secret = secrets.token_urlsafe(12)
    clients[cid] = {"name": name, "ntn": ntn, "secret": secret,
                    "sandbox_token": sbx, "production_token": prod,
                    "active": True,
                    "added": datetime.datetime.now().strftime("%Y-%m-%d")}
    save_clients(clients)

    print("\n" + "=" * 46)
    print("  Give the client these three lines:\n")
    print("  Bridge URL      http://YOUR.SERVER.IP:%d/" % PORT)
    print("  Client ID       %s" % cid)
    print("  Shared secret   %s" % secret)
    print("=" * 46)
    print("\nWrite the secret down now. It is stored in clients.json,")
    print("but it is easier to copy it from here.\n")


def cmd_list():
    clients = load_clients()
    if not clients:
        print("No clients yet. Run:  python3 bridge.py --add"); return
    print("\n%-14s %-28s %-10s %-8s %-8s" % ("ID", "NAME", "NTN", "SANDBOX", "LIVE"))
    print("-" * 72)
    for cid, c in sorted(clients.items()):
        print("%-14s %-28s %-10s %-8s %-8s%s" % (
            cid, (c.get("name") or "")[:28], c.get("ntn", ""),
            "yes" if c.get("sandbox_token") else "-",
            "yes" if c.get("production_token") else "-",
            "" if c.get("active", True) else "   (off)"))
    print()


def cmd_toggle(cid, on):
    clients = load_clients()
    if cid not in clients:
        print("No such client:", cid); return
    clients[cid]["active"] = on
    save_clients(clients)
    print("%s is now %s" % (cid, "on" if on else "off"))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args:
        if args[0] == "--add":  cmd_add();  sys.exit(0)
        if args[0] == "--list": cmd_list(); sys.exit(0)
        if args[0] in ("--off", "--on") and len(args) > 1:
            cmd_toggle(args[1].lower(), args[0] == "--on"); sys.exit(0)
        print(__doc__); sys.exit(0)

    clients = load_clients()
    if not clients:
        note("No companies set up yet.")
    note("FBR bridge listening on port %d, %d company(ies) set up" % (PORT, len(clients)))
    note("Build %s  \u00b7  lookups: %s" % (BUILD, ", ".join(EXTRA_ACTIONS)))
    note("Shared data: %s" % ", ".join(SHARED_ACTIONS))
    note("Manage companies at  http://localhost:%d/admin" % PORT)
    if not admin_is_set():
        note("   (the first visit will ask you to set a password)")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        note("stopped"); sys.exit(0)
