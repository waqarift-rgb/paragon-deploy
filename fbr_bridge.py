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
            # Business nature / activity (Importer, Exporter, General Order Supplier, etc.)
            "businessNature": data.get("BUSINESS_NATURE") or data.get("NATURE_OF_BUSINESS") or data.get("BUSINESS_ACTIVITY") or "",
            "registrationStatus": data.get("REGISTRATION_STATUS") or data.get("STATUS") or "",
            "businessActivity": data.get("PRINCIPAL_ACTIVITY") or data.get("ACTIVITY") or data.get("SECTOR") or "",
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
    ("quotation",     "Quotations"),
    ("data",          "Backup & Restore"),
    ("users",         "User Management"),
    ("audit",         "Activity Log"),
    # ---- POS & Retail ----
    ("pos",           "POS Counter"),
    ("loyalty",       "Loyalty / Store Card"),
    ("inventory",     "Inventory"),
    ("stores",        "Multi-Store / Branches"),
    # ---- Tax Authorities (Services) ----
    ("tax_srb",       "SRB (Sindh services)"),
    ("tax_pra",       "PRA (Punjab services)"),
    ("tax_kpra",      "KPRA (KP services)"),
    ("tax_bra",       "BRA (Balochistan services)"),
    # ---- ERP Extras ----
    ("erp",           "ERP System (master)"),
    ("crm",           "CRM"),
    ("hr",            "HR & Payroll"),
    ("purchase",      "Purchases"),
    ("ledgers",       "Ledgers"),
    ("expenses",      "Expenses"),
    ("payments",      "Payments"),
    ("accounting",    "Accounting"),
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
            "planName": c.get("plan_name", ""),
            "planFee": c.get("plan_fee", ""),
            "expiry": c.get("expiry", ""),
            "paidTill": c.get("paid_till", ""),
            "paymentNote": c.get("payment_note", ""),
        })
    return out


SIGNUPS_PATH = os.path.join(HERE, "signups.json")

def load_signups():
    try:
        with open(SIGNUPS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return []

def save_signups(s):
    tmp = SIGNUPS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2)
    os.replace(tmp, SIGNUPS_PATH)
    try:
        os.chmod(SIGNUPS_PATH, 0o600)
    except OSError:
        pass

def public_signup(req):
    """Client online form submit (public - no auth). Pending list mein jaye."""
    d = req or {}
    name = str(d.get("name") or "").strip()
    if not name:
        return {"ok": False, "msg": "Company name is required."}
    entry = {
        "id": secrets.token_hex(8),
        "name": name,
        "ntn": str(d.get("ntn") or "").strip(),
        "strn": str(d.get("strn") or "").strip(),
        "address": str(d.get("address") or "").strip(),
        "phone": str(d.get("phone") or "").strip(),
        "email": str(d.get("email") or "").strip(),
        "province": str(d.get("province") or "").strip(),
        "website": str(d.get("website") or "").strip(),
        "businessNature": str(d.get("businessNature") or "").strip(),
        "contactPerson": str(d.get("contactPerson") or "").strip(),
        "preferredUser": str(d.get("preferredUser") or "").strip(),
        "logo": str(d.get("logo") or ""),
        "token": str(d.get("token") or "").strip(),
        "prodToken": str(d.get("prodToken") or "").strip(),
        "submitted": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "pending"
    }
    signups = load_signups()
    signups.insert(0, entry)
    save_signups(signups)
    return {"ok": True, "msg": "Thank you! Your details have been received. We will set up your account and share your login shortly."}

def admin_signups_view():
    return load_signups()

def admin_signup_delete(sid):
    signups = [s for s in load_signups() if s.get("id") != sid]
    save_signups(signups)
    return {"ok": True}


SIGNUP_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Register Your Business — Paragon Business Solution</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',-apple-system,Arial,sans-serif;background:#eef1f6;color:#1a2332;line-height:1.5;padding:24px 16px}
.wrap{max-width:680px;margin:0 auto}
.card{background:#fff;border-radius:16px;box-shadow:0 8px 40px rgba(28,46,74,.12);overflow:hidden}
.hd{background:linear-gradient(135deg,#1C2E4A 0%,#2C4A73 100%);color:#fff;padding:38px 40px;position:relative;overflow:hidden}
.hd::after{content:"";position:absolute;top:-40px;right:-40px;width:180px;height:180px;background:rgba(255,255,255,.06);border-radius:50%}
.hd .badge{display:inline-block;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);border-radius:20px;padding:5px 14px;font-size:11px;letter-spacing:1px;margin-bottom:14px}
.hd h1{font-size:26px;font-weight:800;margin-bottom:8px;position:relative}
.hd p{font-size:14px;opacity:.92;position:relative}
.body{padding:34px 40px}
.intro{background:linear-gradient(100deg,#EDF4FF,#F5F9FF);border-radius:12px;padding:18px 22px;font-size:13.5px;color:#334155;line-height:1.75;margin-bottom:28px;border:1px solid #DCE7F5}
.intro b{color:#1C2E4A}
.sec-title{font-size:12px;font-weight:700;color:#1C2E4A;letter-spacing:.05em;text-transform:uppercase;margin:26px 0 14px;padding-bottom:8px;border-bottom:2px solid #EFF4FE}
.sec-title:first-child{margin-top:0}
label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;color:#334155}
label .req{color:#DC2626}
label .opt{color:#94a3b8;font-weight:400;font-size:12px}
input,select,textarea{width:100%;padding:11px 14px;border:1.5px solid #d8dfe8;border-radius:9px;font-size:14px;font-family:inherit;transition:border .15s;background:#fdfdfe}
input:focus,select:focus,textarea:focus{outline:none;border-color:#2563EB;background:#fff}
.help{font-size:11.5px;color:#94a3b8;margin-top:5px;line-height:1.5}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.fld{margin-bottom:16px}
.btn{margin-top:30px;width:100%;padding:15px;background:linear-gradient(135deg,#2563EB,#1d4ed8);color:#fff;border:none;border-radius:11px;font-size:15.5px;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(37,99,235,.3);transition:transform .1s}
.btn:hover{transform:translateY(-1px)}
.btn:active{transform:translateY(0)}
#msg{margin-top:18px;padding:16px 18px;border-radius:11px;font-size:14px;display:none;line-height:1.6}
#msg.ok{background:#EDFBF3;color:#15803d;border:1px solid #A7E5C4;display:block}
#msg.err{background:#FEF2F2;color:#DC2626;border:1px solid #FBCFCF;display:block}
.tokwrap{background:#FEFCF5;border:1px solid #F0E4C4;border-radius:12px;padding:18px 20px;margin-bottom:16px}
.foot{text-align:center;font-size:12.5px;color:#8794a3;margin-top:24px;line-height:1.8}
.foot b{color:#64748b}
@media(max-width:560px){.row{grid-template-columns:1fr;gap:0}.hd,.body{padding-left:24px;padding-right:24px}}
</style></head><body>
<div class="wrap"><div class="card">
<div class="hd">
  <div class="badge">FBR DIGITAL INVOICING</div>
  <h1>Register Your Business</h1>
  <p>Get set up for effortless, FBR-compliant invoicing &mdash; by Paragon Business Solution</p>
</div>
<div class="body">
<div class="intro">
  <b>Welcome, and thank you for choosing us.</b><br>
  You are just one short form away from real-time FBR invoicing, professional quotations, and complete peace of mind on compliance. Simply share your business details below and our team will set up your account and send you a secure login &mdash; typically within one working day. Fields marked <b style="color:#DC2626">*</b> are required; everything else is optional. If anything is unclear, we are only a phone call away.
</div>

<div class="sec-title">Business Details</div>
<div class="fld"><label>Company / Business name <span class="req">*</span></label>
  <input id="s_name" type="text" placeholder="Exactly as registered with FBR">
  <div class="help">This name appears on your invoices and FBR submissions.</div></div>
<div class="row">
  <div><label>NTN <span class="req">*</span></label><input id="s_ntn" type="text" placeholder="e.g. 1234567">
    <div class="help">Found on your FBR registration certificate.</div></div>
  <div><label>STRN <span class="opt">(optional)</span></label><input id="s_strn" type="text" placeholder="Sales tax registration no.">
    <div class="help">If you are registered for sales tax.</div></div>
</div>
<div class="fld"><label>Business address <span class="opt">(optional)</span></label>
  <input id="s_address" type="text" placeholder="Complete business address"></div>
<div class="row">
  <div><label>Province <span class="req">*</span></label>
    <select id="s_province"><option value="">Select province...</option>
      <option>SINDH</option><option>PUNJAB</option><option>KHYBER PAKHTUNKHWA</option>
      <option>BALOCHISTAN</option><option>CAPITAL TERRITORY</option>
      <option>AZAD JAMMU AND KASHMIR</option><option>GILGIT BALTISTAN</option></select></div>
  <div><label>Nature of business <span class="opt">(optional)</span></label><input id="s_nature" type="text" placeholder="e.g. Trading, Services">
    <div class="help">e.g. Importer, General Order Supplier.</div></div>
</div>

<div class="sec-title">Contact &amp; Access</div>
<div class="row">
  <div><label>Phone <span class="req">*</span></label><input id="s_phone" type="text" placeholder="021-XXXXXXX"></div>
  <div><label>Email <span class="req">*</span></label><input id="s_email" type="email" placeholder="you@company.com">
    <div class="help">Your login and updates are sent here.</div></div>
</div>
<div class="row">
  <div><label>Website <span class="opt">(optional)</span></label><input id="s_website" type="text" placeholder="www.company.com"></div>
  <div><label>Contact person <span class="opt">(optional)</span></label><input id="s_contact" type="text" placeholder="Who we should speak to"></div>
</div>
<div class="fld"><label>Preferred login username <span class="opt">(optional)</span></label>
  <input id="s_user" type="text" placeholder="e.g. yourcompany">
  <div class="help">Choose what you would like to sign in with. We will confirm it and send your password.</div></div>

<div class="sec-title">Branding &amp; FBR Tokens</div>
<div class="fld"><label>Company logo <span class="opt">(optional)</span></label>
  <input id="s_logo" type="file" accept="image/*" onchange="pickLogo(this)">
  <div id="s_logoPreview" style="margin-top:8px"></div>
  <div class="help">Appears on your invoices &mdash; you can add or change it any time later.</div></div>

<div class="tokwrap">
  <div style="font-size:13px;color:#8A6420;line-height:1.7;margin-bottom:14px">
    <b>&#128273; FBR Tokens &mdash; only if you already have them.</b><br>
    Not sure where to find your tokens, or don&rsquo;t have them yet? Please leave these blank.
    Our team will guide you step by step, or securely obtain them for you from FBR using your IRIS login &mdash;
    whatever is easiest for you.
  </div>
  <div class="fld" style="margin-bottom:12px"><label>FBR Sandbox Token <span class="opt">(optional)</span></label>
    <textarea id="s_token" rows="2" placeholder="Paste your sandbox token here, if you have it"></textarea>
    <div class="help">Used for testing before you go live.</div></div>
  <div class="fld" style="margin-bottom:0"><label>FBR Production Token <span class="opt">(optional)</span></label>
    <textarea id="s_prodtoken" rows="2" placeholder="Paste your production token here, if you have it"></textarea>
    <div class="help">Used for live invoicing once testing is complete.</div></div>
</div>

<button class="btn" onclick="submitSignup()">Submit My Details</button>
<div id="msg"></div>
</div></div>
<div class="foot">
  <b>Paragon Business Solution</b><br>
  sales@pbsolution.com.pk &nbsp;&middot;&nbsp; 021-34536010 &nbsp;&middot;&nbsp; www.pbsolution.com.pk
</div>
</div>
<script>
function pickLogo(input){
  var file=input.files&&input.files[0]; if(!file) return;
  var r=new FileReader();
  r.onload=function(e){ window._signupLogo=e.target.result;
    document.getElementById('s_logoPreview').innerHTML='<img src="'+e.target.result+'" style="max-height:52px;border:1px solid #d8dfe8;border-radius:6px;padding:3px;background:#fff">'; };
  r.readAsDataURL(file);
}
function submitSignup(){
  var g=function(id){var e=document.getElementById(id);return e?e.value.trim():'';};
  var name=g('s_name'), ntn=g('s_ntn'), prov=g('s_province'), phone=g('s_phone'), email=g('s_email');
  var m=document.getElementById('msg');
  if(!name||!ntn||!prov||!phone||!email){ m.className='err'; m.textContent='Please complete all required fields marked with a red asterisk (*).'; m.scrollIntoView({behavior:'smooth',block:'center'}); return; }
  if(email.indexOf('@')<1){ m.className='err'; m.textContent='Please enter a valid email address.'; return; }
  m.className=''; m.style.display='none';
  var data={name:name,ntn:ntn,strn:g('s_strn'),address:g('s_address'),province:prov,
    businessNature:g('s_nature'),phone:phone,email:email,website:g('s_website'),
    contactPerson:g('s_contact'),preferredUser:g('s_user'),
    logo:window._signupLogo||'',token:g('s_token'),prodToken:g('s_prodtoken')};
  var btn=document.querySelector('.btn'); btn.textContent='Submitting...'; btn.disabled=true;
  fetch('/signup/submit',{method:'POST',headers:{'Content-Type':'text/plain'},body:JSON.stringify(data)})
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.ok){ m.className='ok'; m.innerHTML='&#10003; <b>Thank you!</b> '+d.msg;
        document.querySelectorAll('input,select,textarea').forEach(function(x){x.disabled=true;});
        btn.style.display='none'; m.scrollIntoView({behavior:'smooth',block:'center'});
      } else { m.className='err'; m.textContent=d.msg||'Something went wrong. Please try again.'; btn.textContent='Submit My Details'; btn.disabled=false; }
    })
    .catch(function(){ m.className='err'; m.textContent='Could not submit right now. Please check your connection and try again.'; btn.textContent='Submit My Details'; btn.disabled=false; });
}
</script></body></html>"""


PLANS_PATH = os.path.join(HERE, "plans.json")

def load_plans():
    try:
        with open(PLANS_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}

def save_plans(plans):
    tmp = PLANS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plans, fh, indent=2)
    os.replace(tmp, PLANS_PATH)
    try:
        os.chmod(PLANS_PATH, 0o600)
    except OSError:
        pass

def admin_plans_view():
    return load_plans()

def admin_save_plan(d):
    """Admin: plan banao/edit (features bundle)."""
    name = str(d.get("name") or "").strip()
    if not name:
        return {"ok": False, "msg": "Give the plan a name."}
    plans = load_plans()
    plans[name] = {
        "name": name,
        "fee": str(d.get("fee") or "").strip(),
        "features": d.get("features") if isinstance(d.get("features"), dict) else {},
    }
    save_plans(plans)
    return {"ok": True}

def admin_delete_plan(name):
    plans = load_plans()
    name = str(name or "").strip()
    if name in plans:
        del plans[name]
        save_plans(plans)
    return {"ok": True}


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

    # Plan assign (plan ke features auto + complementary)
    if d.get("assignedPlan") is not None: c["assigned_plan"] = str(d.get("assignedPlan") or "").strip()
    if d.get("complementary") is not None: c["complementary"] = bool(d.get("complementary"))
    if d.get("complementaryFeatures") is not None and isinstance(d.get("complementaryFeatures"), list):
        c["complementary_features"] = d.get("complementaryFeatures")

    # Payment / Subscription tracking
    if d.get("planName") is not None: c["plan_name"] = str(d.get("planName") or "").strip()
    if d.get("planFee") is not None: c["plan_fee"] = str(d.get("planFee") or "").strip()
    if d.get("expiry") is not None: c["expiry"] = str(d.get("expiry") or "").strip()
    if d.get("paidTill") is not None: c["paid_till"] = str(d.get("paidTill") or "").strip()
    if d.get("paymentNote") is not None: c["payment_note"] = str(d.get("paymentNote") or "").strip()

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


# ---- Public Documents (brochure/guides — client link se PDF khule, no login) ----
PUBDOCS_DIR = os.path.join(HERE, "public_docs")

def pubdocs_list():
    try:
        os.makedirs(PUBDOCS_DIR, exist_ok=True)
        out = []
        for fn in sorted(os.listdir(PUBDOCS_DIR)):
            p = os.path.join(PUBDOCS_DIR, fn)
            if os.path.isfile(p):
                out.append({"name": fn, "size": os.path.getsize(p),
                            "date": datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
        return out
    except OSError:
        return []

def pubdoc_save(d):
    name = str(d.get("name") or "").strip()
    data = d.get("data") or ""
    if not name:
        return {"ok": False, "msg": "No file name."}
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_. ()") or "file"
    try:
        os.makedirs(PUBDOCS_DIR, exist_ok=True)
        import base64
        if data.startswith("data:"):
            b64 = data.split(",", 1)[1] if "," in data else ""
            raw = base64.b64decode(b64)
        else:
            raw = data.encode("utf-8")
        with open(os.path.join(PUBDOCS_DIR, safe), "wb") as fh:
            fh.write(raw)
        return {"ok": True, "msg": "Saved " + safe, "name": safe}
    except Exception as e:
        return {"ok": False, "msg": "Could not save: " + str(e)[:100]}

def pubdoc_delete(name):
    safe = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_. ()")
    p = os.path.join(PUBDOCS_DIR, safe)
    try:
        os.remove(p)
        return {"ok": True}
    except OSError:
        return {"ok": False, "msg": "Could not remove."}

# ---- Admin Documents (AI handover file + notes) ----
DOCS_DIR = os.path.join(HERE, "admin_docs")

def admin_docs_list():
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
        out = []
        for fn in sorted(os.listdir(DOCS_DIR)):
            p = os.path.join(DOCS_DIR, fn)
            if os.path.isfile(p):
                out.append({"name": fn, "size": os.path.getsize(p),
                            "date": datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
        return out
    except OSError:
        return []

def admin_doc_save(d):
    name = str(d.get("name") or "").strip()
    data = d.get("data") or ""
    if not name:
        return {"ok": False, "msg": "No file name."}
    # safe name only
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_. ()") or "file"
    try:
        os.makedirs(DOCS_DIR, exist_ok=True)
        import base64
        # data may be a data URL (base64) or plain text
        if data.startswith("data:"):
            b64 = data.split(",", 1)[1] if "," in data else ""
            raw = base64.b64decode(b64)
        else:
            raw = data.encode("utf-8")
        with open(os.path.join(DOCS_DIR, safe), "wb") as fh:
            fh.write(raw)
        try: os.chmod(os.path.join(DOCS_DIR, safe), 0o600)
        except OSError: pass
        return {"ok": True, "msg": "Saved " + safe}
    except Exception as e:
        return {"ok": False, "msg": "Could not save: " + str(e)[:100]}

def admin_doc_get(name):
    safe = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_. ()")
    p = os.path.join(DOCS_DIR, safe)
    try:
        with open(p, "rb") as fh:
            import base64
            return {"ok": True, "name": safe, "data": base64.b64encode(fh.read()).decode("ascii")}
    except OSError:
        return {"ok": False, "msg": "File not found."}

def admin_doc_delete(name):
    safe = "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_. ()")
    p = os.path.join(DOCS_DIR, safe)
    try:
        os.remove(p)
        return {"ok": True}
    except OSError:
        return {"ok": False, "msg": "Could not remove."}


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
    if path == "/admin/signups":  return {"ok": True, "signups": admin_signups_view()}
    if path == "/admin/signupdel": return admin_signup_delete(req.get("id"))
    if path == "/admin/plans":    return {"ok": True, "plans": admin_plans_view()}
    if path == "/admin/saveplan": return admin_save_plan(req)
    if path == "/admin/delplan":  return admin_delete_plan(req.get("name"))
    if path == "/admin/lock":
        _sessions.pop(req.get("token"), None)
        return {"ok": True, "msg": "Locked."}
    if path == "/admin/docs":      return {"ok": True, "docs": admin_docs_list()}
    if path == "/admin/pubdocs":   return {"ok": True, "docs": pubdocs_list()}
    if path == "/admin/pubdocsave":return pubdoc_save(req)
    if path == "/admin/pubdocdel": return pubdoc_delete(req.get("name"))
    if path == "/admin/docsave":   return admin_doc_save(req)
    if path == "/admin/docget":    return admin_doc_get(req.get("name"))
    if path == "/admin/docdel":    return admin_doc_delete(req.get("name"))
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
    # Subscription expiry check (auto-block agar expire ho gaya)
    exp = c.get("expiry", "")
    if exp:
        try:
            exp_date = datetime.datetime.strptime(exp, "%Y-%m-%d").date()
            if datetime.date.today() > exp_date:
                return {"ok": False, "msg": "Your subscription has expired. Please contact your provider to renew."}
        except (ValueError, TypeError):
            pass
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
            "planInfo": {
                "name": c.get("assigned_plan", ""),
                "complementary": bool(c.get("complementary", False)),
                "complementaryFeatures": c.get("complementary_features", []),
                "expiry": c.get("expiry", "")
            },
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
    <button class="ghost" onclick="openSignups()">Registrations</button> <button class="ghost" onclick="downloadTemplate()">Excel Template</button> <button class="ghost" onclick="importExcel()">Import Excel</button> <button class="ghost" onclick="openPlans()">Manage Plans</button> <button onclick="openForm()">+ Add a company</button><input type="file" id="excelImportFile" accept=".csv,.xlsx" style="display:none" onchange="doImportExcel(this)">
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

   <div style="border-top:1px solid #e2e8f0;margin:16px 0 8px;padding-top:14px;font-weight:600;color:#1C2E4A">Plan &amp; Access</div>
   <div class="field"><label>Assign a plan</label>
    <select id="fAssignedPlan" onchange="planAssigned()"><option value="">— no plan —</option></select>
    <div class="help">The plan's features are applied automatically. You can still add extra features below.</div></div>
   <div class="field"><label style="display:flex;align-items:center;gap:8px;font-weight:400;font-size:13.5px">
     <input type="checkbox" id="fComplementary" style="width:auto">
     Complementary account — no monthly charges (for friends/partners)</label></div>

   <div style="border-top:1px solid #e2e8f0;margin:16px 0 8px;padding-top:14px;font-weight:600;color:#1C2E4A">Subscription &amp; Payment</div>
   <div class="row">
    <div class="field"><label>Plan name</label>
     <input type="text" id="fPlanName" placeholder="e.g. Premium, Basic"></div>
    <div class="field"><label>Monthly fee</label>
     <input type="text" id="fPlanFee" placeholder="e.g. 5000"></div>
   </div>
   <div class="row">
    <div class="field"><label>Paid till</label>
     <input type="date" id="fPaidTill"></div>
    <div class="field"><label>Expiry date <span class="small">— access blocked after this</span></label>
     <input type="date" id="fExpiry">
     <div class="help">Leave blank for no expiry. Client is auto-blocked after this date.</div></div>
   </div>
   <div class="field"><label>Payment note</label>
    <input type="text" id="fPaymentNote" placeholder="e.g. Paid via bank, ref 12345"></div>

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
   <h2>Documents <span class="small" style="font-weight:400">(admin only — clients never see these)</span></h2>
   <p class="muted" style="margin:4px 0 12px">Keep your AI handover report, notes and any files here. Only you, signed in to this admin page, can see them.</p>
   <div style="margin-bottom:12px">
     <input type="file" id="docFile" onchange="uploadDoc(this)">
   </div>
   <div id="docsList" class="small">Loading...</div>
  </div>

  <div class="card">
   <h2>Public Documents <span class="small" style="font-weight:400">(share link with clients — PDF opens directly, no login)</span></h2>
   <p class="muted" style="margin:4px 0 12px">Upload brochures, key features, guides here. Copy the link and send it to a client — they open the PDF directly, no sign-in needed.</p>
   <div style="margin-bottom:12px">
     <input type="file" id="pubDocFile" onchange="uploadPubDoc(this)">
   </div>
   <div id="pubDocsList" class="small">Loading...</div>
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
    loadDocs();
    loadPubDocs();
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
      '<th>Sandbox</th><th>Production</th><th>Status</th><th>Subscription</th><th></th></tr>' +
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
          '<td>' + (function(){
            if(!x.expiry) return '<span style="color:#94a3b8;font-size:11px">no expiry</span>';
            var exp = new Date(x.expiry); var today = new Date(); today.setHours(0,0,0,0);
            var days = Math.ceil((exp - today) / 86400000);
            if(days < 0) return '<span class="pill off">expired</span>';
            if(days <= 7) return '<span style="color:#D97706;font-weight:600;font-size:11px">' + days + ' days left</span>';
            return '<span style="color:#059669;font-size:11px">' + x.expiry + '</span>';
          })() + (x.planName ? '<div style="font-size:10px;color:#94a3b8">' + esc(x.planName) + '</div>' : '') + '</td>' +
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
  ['commercial','Commercial Invoice'],['quotation','Quotations'],['data','Backup & Restore'],
  ['users','User Management'],['audit','Activity Log'],
  ['pos','POS Counter'],['loyalty','Loyalty / Store Card'],['inventory','Inventory'],
  ['stores','Multi-Store / Branches'],
  ['tax_srb','SRB (Sindh services)'],['tax_pra','PRA (Punjab services)'],
  ['tax_kpra','KPRA (KP services)'],['tax_bra','BRA (Balochistan services)'],
  ['erp','ERP System (master)'],['crm','CRM'],['hr','HR & Payroll'],['purchase','Purchases'],
  ['ledgers','Ledgers'],['expenses','Expenses'],['payments','Payments'],['accounting','Accounting']
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

// Registrations (online form submissions)
function openSignups(){
  post('/admin/signups').then(function(d){
    var s = (d && d.signups) || [];
    var rows = s.length ? s.map(function(x){
      return '<div style="border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:10px">'+
        '<div style="display:flex;justify-content:space-between"><b>'+esc(x.name)+'</b>'+
        '<span style="font-size:11px;color:#888">'+esc(x.submitted||'')+'</span></div>'+
        '<div style="font-size:12.5px;color:#55606d;margin-top:6px;line-height:1.7">'+
          'NTN: '+esc(x.ntn||'-')+' · STRN: '+esc(x.strn||'-')+'<br>'+
          esc(x.address||'')+(x.province?', '+esc(x.province):'')+'<br>'+
          'Phone: '+esc(x.phone||'-')+' · Email: '+esc(x.email||'-')+'<br>'+
          (x.businessNature?'Nature: '+esc(x.businessNature)+'<br>':'')+
          (x.contactPerson?'Contact: '+esc(x.contactPerson)+'<br>':'')+
          (x.preferredUser?'Wants username: <b>'+esc(x.preferredUser)+'</b><br>':'')+
          (x.logo?'&#10003; Logo uploaded  ':'')+
          (x.token?'&#10003; Sandbox token  ':'')+
          (x.prodToken?'&#10003; Production token':'')+
          (!x.token&&!x.prodToken?'<span style="color:#D97706">No FBR tokens \u2014 will guide/fetch</span>':'')+
        '</div>'+
        '<div style="margin-top:10px;text-align:right">'+
          '<button class="primary sm" onclick=\'createFromSignup('+JSON.stringify(JSON.stringify(x))+')\'>Create company</button> '+
          '<button class="danger sm" onclick="delSignup(\''+esc(x.id)+'\')">Delete</button></div>'+
      '</div>';
    }).join('') : '<div style="color:#888;padding:16px;text-align:center">No registrations yet. Share your form link: <b>'+location.origin+'/signup</b></div>';
    showModal('<h3 style="margin-bottom:6px">Registrations</h3>'+
      '<p style="font-size:12.5px;color:#666;margin-bottom:14px">Businesses who filled your online form. Share the link: <b style="color:#2563EB">'+location.origin+'/signup</b></p>'+
      rows+'<div style="margin-top:14px;text-align:right"><button class="ghost" onclick="closeModalX()">Close</button></div>');
  });
}
function createFromSignup(json){
  var x = JSON.parse(json);
  closeModalX();
  openForm();
  setTimeout(function(){
    var set=function(id,v){var e=document.getElementById(id);if(e)e.value=v||'';};
    set('fName',x.name); set('fNtn',x.ntn); set('fStrn',x.strn); set('fAddr',x.address);
    set('fPhone',x.phone); set('fEmail',x.email); set('fWeb',x.website);
    set('fLoginUser',x.preferredUser);
    if(document.getElementById('fProv')&&x.province){ document.getElementById('fProv').value=x.province; }
    // FBR tokens (agar client ne diye)
    if(document.getElementById('fSandbox')&&x.token){ document.getElementById('fSandbox').value=x.token; }
    if(document.getElementById('fProd')&&x.prodToken){ document.getElementById('fProd').value=x.prodToken; }
    // id suggest
    if(document.getElementById('fId')&&x.preferredUser){ document.getElementById('fId').value=x.preferredUser.toLowerCase().replace(/[^a-z0-9]/g,''); }
    var extra = x.token ? ' FBR token was provided.' : ' No FBR token yet \u2014 fetch it from FBR or guide the client.';
    say('Details loaded from registration.' + extra + ' Set a login password and features, then Save.','good');
  }, 300);
}
function delSignup(id){
  if(!confirm('Delete this registration?')) return;
  post('/admin/signupdel',{id:id}).then(function(){ openSignups(); });
}

// Excel template download
function downloadTemplate(){
  var headers = ['Company Name','NTN','STRN','Address','Province','Business Nature','Phone','Email','Website','Contact Person','Preferred Username'];
  var example = ['ABC Traders','1234567','3277876180532','Karachi','SINDH','Importer','021-1234567','info@abc.com','www.abc.com','Ali Khan','abctraders'];
  var csv = headers.join(',')+'\n'+example.join(',')+'\n';
  var blob = new Blob([csv],{type:'text/csv'});
  var a = document.createElement('a'); a.href=URL.createObjectURL(blob);
  a.download='company-template.csv'; a.click();
  say('Template downloaded. Fill it and use Import Excel.','good');
}
// Excel import
function importExcel(){ document.getElementById('excelImportFile').click(); }
function doImportExcel(input){
  var file=input.files&&input.files[0]; if(!file) return;
  var r=new FileReader();
  r.onload=function(e){
    var text=e.target.result;
    var lines=text.split(/\r?\n/).filter(function(l){return l.trim();});
    if(lines.length<2){ say('File is empty or has no data row.','bad'); return; }
    var vals=lines[1].split(',');
    // headers order: name,ntn,strn,address,province,nature,phone,email,website,contact,user
    closeModalX();
    openForm();
    setTimeout(function(){
      var set=function(id,v){var el=document.getElementById(id);if(el)el.value=(v||'').trim();};
      set('fName',vals[0]); set('fNtn',vals[1]); set('fStrn',vals[2]); set('fAddr',vals[3]);
      if(document.getElementById('fProv')&&vals[4]) document.getElementById('fProv').value=vals[4].trim();
      set('fPhone',vals[6]); set('fEmail',vals[7]); set('fWeb',vals[8]);
      set('fLoginUser',vals[10]);
      if(document.getElementById('fId')&&vals[10]) document.getElementById('fId').value=vals[10].trim().toLowerCase().replace(/[^a-z0-9]/g,'');
      say('Details loaded from Excel. Set a login password and features, then Save.','good');
    },300);
  };
  r.readAsText(file);
  input.value='';
}

// Plans management
var ALL_FEATURES_LIST = [
  ['reports','Reports'],['returnSummary','Tax Return Summary'],['clients','Buyers'],
  ['items','Products'],['hs','HS Code Search'],['autoscenario','Auto Scenarios'],
  ['downloadAll','Download All'],['bulkImport','Bulk Import'],['emailInvoice','Email Invoices'],
  ['fbrProof','FBR Tax Proof'],['autoTax','Automatic Tax Rate'],['commercial','Commercial Invoice'],
  ['quotation','Quotation'],['creditnote','Credit/Debit Notes'],['challan','Delivery Challan'],
  ['recurring','Recurring Invoices'],['payment','Payment Tracking'],['statement','Buyer Statements'],
  ['data','Backup & Restore'],['users','User Management'],['audit','Activity Log']
];
function openPlans(){
  post('/admin/plans').then(function(d){
    var plans = (d && d.plans) || {};
    window._plans = plans;
    var list = Object.keys(plans).map(function(name){
      var p = plans[name];
      var fcount = Object.keys(p.features||{}).filter(function(k){return p.features[k];}).length;
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:10px;border:1px solid #e2e8f0;border-radius:6px;margin-bottom:8px">'+
        '<div><b>'+esc(name)+'</b>'+(p.fee?' <span style="color:#666">— '+esc(p.fee)+'/mo</span>':'')+
        '<div style="font-size:11px;color:#888">'+fcount+' features</div></div>'+
        '<div><button class="ghost sm" onclick="editPlan(\''+esc(name)+'\')">Edit</button> '+
        '<button class="danger sm" onclick="delPlan(\''+esc(name)+'\')">Delete</button></div></div>';
    }).join('') || '<div style="color:#888;padding:12px">No plans yet. Create one below.</div>';
    showModal('<h3 style="margin-bottom:12px">Plans</h3>'+
      '<p style="font-size:12.5px;color:#666;margin-bottom:14px">Create feature bundles. Assign a plan to a company and its features are set automatically.</p>'+
      list +
      '<div style="margin-top:14px;text-align:right">'+
        '<button class="ghost" onclick="closeModalX()">Close</button> '+
        '<button class="primary" onclick="newPlan()">+ New plan</button></div>');
  });
}
function newPlan(){ editPlan(null); }
function editPlan(name){
  var p = name ? (window._plans[name]||{}) : {name:'',fee:'',features:{}};
  var feats = p.features || {};
  var rows = ALL_FEATURES_LIST.map(function(f){
    return '<label style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12.5px">'+
      '<input type="checkbox" data-planfeat="'+f[0]+'" '+(feats[f[0]]?'checked':'')+' style="width:auto"> '+esc(f[1])+'</label>';
  }).join('');
  showModal('<h3 style="margin-bottom:12px">'+(name?'Edit plan':'New plan')+'</h3>'+
    '<div style="margin-bottom:10px"><label style="font-size:12px;font-weight:600">Plan name</label>'+
      '<input id="planName" type="text" value="'+esc(p.name||'')+'"'+(name?' readonly':'')+' style="width:100%"></div>'+
    '<div style="margin-bottom:10px"><label style="font-size:12px;font-weight:600">Monthly fee (optional)</label>'+
      '<input id="planFee" type="text" value="'+esc(p.fee||'')+'" placeholder="e.g. 5000" style="width:100%"></div>'+
    '<div style="font-size:12px;font-weight:600;margin:10px 0 6px">Features in this plan</div>'+
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 14px;max-height:220px;overflow:auto">'+rows+'</div>'+
    '<div style="margin-top:14px;text-align:right">'+
      '<button class="ghost" onclick="openPlans()">Back</button> '+
      '<button class="primary" onclick="savePlan()">Save plan</button></div>');
}
function savePlan(){
  var name = document.getElementById('planName').value.trim();
  if(!name){ say('Give the plan a name','bad'); return; }
  var feats = {};
  document.querySelectorAll('[data-planfeat]').forEach(function(inp){ feats[inp.getAttribute('data-planfeat')] = inp.checked; });
  var fee = document.getElementById('planFee').value.trim();
  post('/admin/saveplan', {name:name, fee:fee, features:feats}).then(function(d){
    if(d.ok){ say('Plan saved','good'); openPlans(); } else say(d.msg||'Failed','bad');
  });
}
function delPlan(name){
  if(!confirm('Delete plan '+name+'?')) return;
  post('/admin/delplan', {name:name}).then(function(d){ if(d.ok){ say('Plan deleted','good'); openPlans(); } });
}
// Generic modal helpers (agar nahi hain)
function showModal(html){
  var box = document.getElementById('genModal');
  if(!box){ box=document.createElement('div'); box.id='genModal';
    box.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:1000';
    document.body.appendChild(box); }
  box.innerHTML='<div style="background:#fff;border-radius:10px;padding:24px;max-width:560px;width:90%;max-height:82vh;overflow:auto">'+html+'</div>';
  box.style.display='flex';
}
function closeModalX(){ var b=document.getElementById('genModal'); if(b) b.style.display='none'; }

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
  setTimeout(loadPlansDropdown, 100);
  $('formTitle').textContent = 'Add a company';
  ['fName','fId','fNtn','fStrn','fAddr','fPhone','fProv','fEmail','fWeb','fSandbox','fProd','fSecret','fLoginUser','fLoginPass','fPlanName','fPlanFee','fPaidTill','fExpiry','fPaymentNote','fAssignedPlan','fComplementary'].forEach(function(f){
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

/* Plan assign - features auto */
function loadPlansDropdown(){
  post('/admin/plans').then(function(d){
    var plans = (d && d.plans) || {};
    window._plans = plans;
    var sel = document.getElementById('fAssignedPlan');
    if(sel){
      var cur = sel.value;
      sel.innerHTML = '<option value="">— no plan —</option>' +
        Object.keys(plans).map(function(n){ return '<option value="'+esc(n)+'">'+esc(n)+(plans[n].fee?' ('+esc(plans[n].fee)+'/mo)':'')+'</option>'; }).join('');
      sel.value = cur;
    }
  });
}
function planAssigned(){
  var name = document.getElementById('fAssignedPlan').value;
  var plans = window._plans || {};
  var plan = plans[name];
  if(plan && plan.features){
    // plan ke features company ko auto-set (form mein features section agar ho)
    // note: features Features button se manage hote, plan assign se base set
    if(plan.fee && document.getElementById('fPlanFee') && !document.getElementById('fPlanFee').value){
      document.getElementById('fPlanFee').value = plan.fee;
    }
    if(document.getElementById('fPlanName') && !document.getElementById('fPlanName').value){
      document.getElementById('fPlanName').value = name;
    }
  }
}

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
  if($('fPlanName')) $('fPlanName').value = x.planName || '';
  if($('fPlanFee')) $('fPlanFee').value = x.planFee || '';
  if($('fPaidTill')) $('fPaidTill').value = x.paidTill || '';
  if($('fExpiry')) $('fExpiry').value = x.expiry || '';
  if($('fPaymentNote')) $('fPaymentNote').value = x.paymentNote || '';
  loadPlansDropdown();
  setTimeout(function(){ if($('fAssignedPlan')) $('fAssignedPlan').value = x.planName || ''; if($('fComplementary')) $('fComplementary').checked = !!x.complementary; }, 300);
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
    secret: $('fSecret').value, strn: $('fStrn')?$('fStrn').value:'', addr: $('fAddr')?$('fAddr').value:'', phone: $('fPhone')?$('fPhone').value:'', prov: $('fProv')?$('fProv').value:'', email: $('fEmail')?$('fEmail').value:'', web: $('fWeb')?$('fWeb').value:'', loginUser: $('fLoginUser')?$('fLoginUser').value:'', loginPass: $('fLoginPass')?$('fLoginPass').value:'', planName: $('fPlanName')?$('fPlanName').value:'', assignedPlan: $('fAssignedPlan')?$('fAssignedPlan').value:'', complementary: $('fComplementary')?$('fComplementary').checked:false, planFee: $('fPlanFee')?$('fPlanFee').value:'', paidTill: $('fPaidTill')?$('fPaidTill').value:'', expiry: $('fExpiry')?$('fExpiry').value:'', paymentNote: $('fPaymentNote')?$('fPaymentNote').value:'',
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
    // Welcome sheet - agar login username+password diya
    var lu = $('fLoginUser') ? $('fLoginUser').value.trim() : '';
    var lp = $('fLoginPass') ? $('fLoginPass').value.trim() : '';
    if(lu && lp){
      showWelcomeSheet($('fName').value, lu, lp);
    }
    say(d.msg, 'good'); loadList();
  });
}

/* Welcome sheet - client ko share karne ke liye (login detail + steps) */
function showWelcomeSheet(company, user, pass){
  var appUrl = location.origin + '/';
  var sheet =
    'Welcome to FBR Digital Invoicing\n'+
    'Powered by Paragon Business Solution\n'+
    '=====================================\n\n'+
    'Company: ' + company + '\n\n'+
    'Login here:\n' + appUrl + '\n\n'+
    'Username: ' + user + '\n'+
    'Password: ' + pass + '\n\n'+
    'First time sign-in:\n'+
    '1. Open the link above in any browser\n'+
    '2. Enter your username and password\n'+
    '3. You will be asked to set your own new password\n'+
    '4. Check your company details and start invoicing\n\n'+
    'Need help?\n'+
    'Paragon Business Solution\n'+
    'sales@pbsolution.com.pk  |  021-34536010\n';

  var html =
    '<div id="welcomeSheetPrint" style="font-family:Arial;max-width:520px;margin:0 auto">'+
      '<div style="background:linear-gradient(135deg,#1C2E4A,#2C4A73);color:#fff;padding:24px;border-radius:10px 10px 0 0;text-align:center">'+
        '<div style="font-size:20px;font-weight:800">Welcome to FBR Digital Invoicing</div>'+
        '<div style="font-size:12px;opacity:.9;margin-top:4px">Powered by Paragon Business Solution</div></div>'+
      '<div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;padding:24px">'+
        '<div style="font-size:15px;font-weight:700;color:#1C2E4A;margin-bottom:14px">' + esc(company) + '</div>'+
        '<div style="background:#EDF4FF;border-radius:8px;padding:16px;margin-bottom:16px">'+
          '<div style="font-size:11px;color:#5A7290;text-transform:uppercase">Login here</div>'+
          '<div style="font-size:14px;color:#2563EB;font-weight:600;word-break:break-all">' + esc(appUrl) + '</div>'+
          '<div style="margin-top:12px;display:flex;gap:20px">'+
            '<div><div style="font-size:11px;color:#5A7290;text-transform:uppercase">Username</div><div style="font-size:15px;font-weight:700">' + esc(user) + '</div></div>'+
            '<div><div style="font-size:11px;color:#5A7290;text-transform:uppercase">Password</div><div style="font-size:15px;font-weight:700;font-family:monospace">' + esc(pass) + '</div></div>'+
          '</div></div>'+
        '<div style="font-size:13px;color:#3a4656;line-height:1.8">'+
          '<b>First time sign-in:</b><br>'+
          '1. Open the link above in any browser<br>'+
          '2. Enter your username and password<br>'+
          '3. Set your own new password when asked<br>'+
          '4. Check your details and start invoicing</div>'+
        '<div style="margin-top:16px;padding-top:12px;border-top:1px solid #eef1f5;font-size:12px;color:#8794a3">'+
          'Need help? sales@pbsolution.com.pk &middot; 021-34536010</div>'+
      '</div></div>';

  var box = document.getElementById('welcomeBox');
  if(!box){ box=document.createElement('div'); box.id='welcomeBox';
    box.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:1100;padding:20px;overflow:auto';
    document.body.appendChild(box); }
  box.innerHTML = '<div style="background:#fff;border-radius:12px;padding:24px;max-width:600px;width:100%;max-height:90vh;overflow:auto">'+
    '<div style="font-size:13px;color:#059669;background:#EDFBF3;padding:10px 14px;border-radius:8px;margin-bottom:16px">&#10003; Company created. Share these login details with your client. The password is shown only now.</div>'+
    html +
    '<div style="margin-top:20px;text-align:center;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">'+
      '<button class="primary" onclick=\'copyWelcome('+JSON.stringify(JSON.stringify(sheet))+')\'>Copy for WhatsApp/Email</button>'+
      '<button class="ghost" onclick="printWelcome()">Print</button>'+
      '<button class="ghost" onclick="closeWelcome()">Close</button></div></div>';
  box.style.display='flex';
}
function copyWelcome(json){
  var text = JSON.parse(json);
  if(navigator.clipboard){ navigator.clipboard.writeText(text).then(function(){ say('Login details copied \u2014 paste into WhatsApp or email','good'); }); }
  else { var ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); say('Copied','good'); }
}
function printWelcome(){
  var content = document.getElementById('welcomeSheetPrint');
  if(!content) return;
  var w = window.open('', '_blank');
  if(!w){ say('Please allow pop-ups to print','bad'); return; }
  w.document.write('<html><head><title>Login details</title></head><body style="margin:30px">'+content.outerHTML+'</body></html>');
  w.document.close(); setTimeout(function(){ w.print(); }, 300);
}
function closeWelcome(){ var b=document.getElementById('welcomeBox'); if(b) b.style.display='none'; hide('form'); }

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

/* ---------- Admin Documents ---------- */
function loadDocs(){
  post('/admin/docs').then(function(d){
    var docs = (d && d.docs) || [];
    var el = document.getElementById('docsList');
    if(!el) return;
    if(!docs.length){ el.innerHTML = '<span style="color:#8794a3">No files yet. Upload your AI handover report or notes above.</span>'; return; }
    el.innerHTML = docs.map(function(f){
      var kb = Math.round(f.size/1024);
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #eef1f5">'+
        '<span>&#128196; <b>'+esc(f.name)+'</b> <span style="color:#8794a3">('+kb+' KB · '+esc(f.date)+')</span></span>'+
        '<span><button class="ghost sm" onclick="downloadDoc(\''+esc(f.name)+'\')">Download</button> '+
        '<button class="danger sm" onclick="delDoc(\''+esc(f.name)+'\')">Remove</button></span></div>';
    }).join('');
  });
}
function uploadDoc(input){
  var file = input.files && input.files[0]; if(!file) return;
  var r = new FileReader();
  r.onload = function(e){
    post('/admin/docsave', { name: file.name, data: e.target.result }).then(function(d){
      if(d.ok){ say('Uploaded '+file.name, 'good'); loadDocs(); }
      else say(d.msg || 'Upload failed', 'bad');
      input.value='';
    });
  };
  r.readAsDataURL(file);
}
function downloadDoc(name){
  post('/admin/docget', { name: name }).then(function(d){
    if(!d.ok){ say(d.msg||'Not found','bad'); return; }
    var a = document.createElement('a');
    a.href = 'data:application/octet-stream;base64,' + d.data;
    a.download = d.name; a.click();
  });
}
function delDoc(name){
  if(!confirm('Remove '+name+'?')) return;
  post('/admin/docdel', { name: name }).then(function(d){
    if(d.ok){ say('Removed', 'good'); loadDocs(); } else say(d.msg||'Failed','bad');
  });
}
/* ---- Public Documents (client link) ---- */
function loadPubDocs(){
  post('/admin/pubdocs').then(function(d){
    var docs = (d && d.docs) || [];
    var el = document.getElementById('pubDocsList');
    if(!el) return;
    if(!docs.length){ el.innerHTML = '<span style="color:#8794a3">No public files yet. Upload a brochure or guide above.</span>'; return; }
    var base = window.location.origin;
    el.innerHTML = docs.map(function(f){
      var kb = Math.round(f.size/1024);
      var link = base + '/doc/' + encodeURIComponent(f.name);
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #eef1f5">'+
        '<span>&#127760; <b>'+esc(f.name)+'</b> <span style="color:#8794a3">('+kb+' KB)</span></span>'+
        '<span><button class="ghost sm" onclick="copyPubLink(this,\''+esc(f.name)+'\')">Copy Link</button> '+
        '<button class="ghost sm" onclick="window.open(\''+link+'\',\'_blank\')">Open</button> '+
        '<button class="danger sm" onclick="delPubDoc(\''+esc(f.name)+'\')">Remove</button></span></div>';
    }).join('');
  });
}
function uploadPubDoc(input){
  var file = input.files && input.files[0]; if(!file) return;
  var r = new FileReader();
  r.onload = function(e){
    post('/admin/pubdocsave', { name: file.name, data: e.target.result }).then(function(d){
      if(d.ok){ say('Uploaded '+file.name, 'good'); loadPubDocs(); }
      else say(d.msg || 'Upload failed', 'bad');
      input.value='';
    });
  };
  r.readAsDataURL(file);
}
function copyPubLink(btn, name){
  var link = window.location.origin + '/doc/' + encodeURIComponent(name);
  function done(){ var o=btn.textContent; btn.textContent='Copied!'; setTimeout(function(){btn.textContent=o;},1500); }
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(link).then(done).catch(function(){ prompt('Copy this link:', link); });
  } else { prompt('Copy this link:', link); }
}
function delPubDoc(name){
  if(!confirm('Remove '+name+'?')) return;
  post('/admin/pubdocdel', { name: name }).then(function(d){
    if(d.ok){ say('Removed', 'good'); loadPubDocs(); } else say(d.msg||'Failed','bad');
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

        if path == "/signup":
            body = SIGNUP_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/admin":
            body = ADMIN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(body)
                return
            # file nahi to JSON status
            self._send({"ok": True, "msg": "FBR bridge running. Invoice Manager file not found on server.",
                        "clients": len(load_clients())})
            return

        # PUBLIC document — /doc/<filename> — client link se PDF khule (no login)
        if path.startswith("/doc/"):
            fname = path[len("/doc/"):]
            import urllib.parse
            fname = urllib.parse.unquote(fname)
            safe = "".join(ch for ch in fname if ch.isalnum() or ch in "-_. ()")
            fp = os.path.join(PUBDOCS_DIR, safe)
            if os.path.isfile(fp):
                data = open(fp, "rb").read()
                low = safe.lower()
                ctype = "application/pdf" if low.endswith(".pdf") else (
                        "image/png" if low.endswith(".png") else (
                        "image/jpeg" if (low.endswith(".jpg") or low.endswith(".jpeg")) else "application/octet-stream"))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition", 'inline; filename="' + safe + '"')
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Document not found.")
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
            if path == "/signup/submit":
                self._send(public_signup(req))
            elif path.startswith("/admin"):
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
