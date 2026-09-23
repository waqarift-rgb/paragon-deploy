#!/usr/bin/env python3
"""
PARAGON SERVER
==============

The server behind the Paragon app: complaints, quotations, tasks, and the
copier counters that rental billing is worked out from.

    Phones and the app  --->  this server  --->  clients' copiers (SNMP)
                                  |
                              the console, on your laptop

WHAT IT DOES

  * holds one set of data every phone syncs with, so work carries on when
    the signal does not
  * hands out invoice and bill numbers, so two computers can never issue
    the same one
  * reads copier counters over SNMP, with nothing to install
  * serves a console at /console for whoever is at a desk

RUNNING IT

    python3 paragon_server.py

Then open  http://localhost:8080/console  in a browser.

SETTING IT UP

  1. change SHARED_SECRET below, and put the same words in the app
  2. leave it running
  3. back up the "data" folder beside this file. It is the only copy.

SECURITY

  Everything is guarded by the shared secret. Keep it off the open internet
  unless it sits behind HTTPS and a firewall: this speaks plain HTTP, which
  is right for an office network and wrong for the public one.
"""

# Apple and Google ask every server that sends pushes for a way to reach
# its owner. Nothing is sent here; it is only named in the signed note.
PUSH_CONTACT = "mailto:sales@pbsolution.com.pk"

SHARED_SECRET = "change-this-please"      # must match the app
PORT = 8080

import json, os, sys, time, socket, hmac, secrets, datetime, threading
import urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__)) or "."
LOG_PATH = os.path.join(HERE, "paragon.log")
_store_lock = threading.Lock()


def note(*bits):
    line = (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " +
            " ".join(str(b) for b in bits))
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass

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


# Everything the phones share. The app keeps the same list; the two have to
# agree, or something made on one phone never reaches the next.
KINDS = ("invoices", "clients", "items", "users", "parts", "stock", "buys",
         "scrap", "quotes", "calls", "bills", "contracts", "models", "tasks",
         "notes", "goals", "habits", "visits", "rights")


def _blank():
    d = {k: {} for k in KINDS}
    d["counter"] = {}
    d["rev"] = 0
    return d




def store_load(cid):
    try:
        with open(_data_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        for k in KINDS + ("counter",):
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
    _daily_backup(cid, d)


BACKUP_DIR = os.path.join(DATA_DIR, "backups")
BACKUP_KEEP = 14      # do hafte ki rozana copies rakho


def _daily_backup(cid, d):
    """Din mein ek dafa poore data ki alag copy rakho, taake kabhi kuch
    khoye to wapas laaya ja sake. Purani copies apne aap saaf ho jati hain."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        day = datetime.date.today().isoformat()
        name = "%s-%s.json" % (cid, day)
        dest = os.path.join(BACKUP_DIR, name)
        # purani copies har dafa saaf karo (chahe aaj ki copy bane ya na bane)
        mine0 = sorted(f for f in os.listdir(BACKUP_DIR)
                       if f.startswith(cid + "-") and f.endswith(".json"))
        for old0 in mine0[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old0))
            except OSError:
                pass
        if os.path.exists(dest):
            return                      # aaj ki copy pehle se hai
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, separators=(",", ":"))
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
        # purani copies (14 din se zyada) hata do
        mine = sorted(f for f in os.listdir(BACKUP_DIR)
                      if f.startswith(cid + "-") and f.endswith(".json"))
        for old in mine[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(BACKUP_DIR, old))
            except OSError:
                pass
    except Exception:
        pass                            # backup fail ho to bhi asal kaam na ruke


def backup_list(cid):
    """Admin ko dikhao kaunsi backup copies mehfooz hain."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        out = []
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.startswith(cid + "-") and f.endswith(".json"):
                full = os.path.join(BACKUP_DIR, f)
                out.append({"day": f[len(cid) + 1:-5],
                            "size": os.path.getsize(full)})
        return {"ok": True, "backups": out[:BACKUP_KEEP]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def backup_get(cid, req):
    """Ek din ki backup copy poori wapas do (admin download kar sake)."""
    day = str(req.get("day") or "")
    if not day:
        return {"ok": False, "msg": "Which day?"}
    f = os.path.join(BACKUP_DIR, "%s-%s.json" % (cid, day))
    if not os.path.exists(f):
        return {"ok": False, "msg": "That backup is not here."}
    try:
        with open(f, encoding="utf-8") as fh:
            return {"ok": True, "day": day, "data": json.load(fh)}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


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


def store_push(cid, req):
    """Take what this computer has changed. Newest write wins per record."""
    sent = req.get("records") or {}
    if not isinstance(sent, dict):
        return {"ok": False, "msg": "records must be an object"}

    kept, skipped = 0, 0
    wake = set()
    with _store_lock:
        d = store_load(cid)
        for kind in KINDS:
            # Rights are set by an admin through a signed-in session, never by
            # a phone's ordinary sync \u2014 or any phone could grant itself anything.
            if kind == "rights" and not req.get("_trusted"):
                continue
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
                # A note is stamped with this server's own clock the first
                # time it arrives. Phones' clocks drift; this one decides what
                # counts as new, so no phone can make a note arrive late or
                # twice by being a few minutes out.
                if kind == "notes":
                    rec["rcvd"] = (have or {}).get("rcvd") or \
                        datetime.datetime.now().isoformat(timespec="seconds")
                    if not have and not rec.get("read") and rec.get("to"):
                        wake.add(str(rec["to"]))
                d[kind][rid] = rec
                kept += 1
        # A person deleted on one phone is removed everywhere. The id is
        # remembered so a phone that still had them, syncing later, does not
        # bring them back; their past work stays under their name in the
        # records that reference them.
        for uid_del in (req.get("deletedUsers") or {}):
            if uid_del in d.get("users", {}):
                d["users"].pop(uid_del, None); kept += 1
            d.setdefault("deletedUsers", {})[str(uid_del)] = \
                datetime.datetime.now().isoformat(timespec="seconds")
        for uid_del in list(d.get("deletedUsers", {})):
            d.get("users", {}).pop(uid_del, None)
        if kept:
            d["rev"] += 1
            store_save(cid, d)
    for who in wake:
        push_for(cid, who)
    return {"ok": True, "saved": kept, "skipped": skipped, "rev": d["rev"]}


def store_pull(cid, req):
    """Give back everything, or only what changed since their last rev."""
    since = req.get("since")
    d = store_load(cid)
    if since is not None and int(since) == d["rev"]:
        return {"ok": True, "rev": d["rev"], "unchanged": True}
    out = {"ok": True, "rev": d["rev"],
           "counts": {k: len(d[k]) for k in KINDS}}
    for k in KINDS:
        out[k] = d[k]
    return out


def notes_for(cid, req):
    """Only one person's notes, only newer than a time.

    A phone checking every fifteen minutes on mobile data should not pull
    every job and every photograph to find out whether anything happened.
    This answers only the question it is asking."""
    who = str(req.get("to") or "")
    since = str(req.get("since") or "")
    if not who:
        return {"ok": False, "msg": "Whose notes?"}
    d = store_load(cid)
    out = [x for x in d.get("notes", {}).values()
           if isinstance(x, dict) and x.get("to") == who
           and not x.get("read") and str(x.get("rcvd", "")) > since]
    out.sort(key=lambda x: str(x.get("rcvd", "")))
    return {"ok": True, "notes": out[-20:],
            "now": datetime.datetime.now().isoformat(timespec="seconds")}


def store_forget(cid, req):
    """Remove records this computer deleted."""
    ids = req.get("ids") or {}
    gone = 0
    with _store_lock:
        d = store_load(cid)
        for kind in KINDS:
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
            "counts": {k: len(d[k]) for k in KINDS},
            "counters": d["counter"]}




# ================================================================
#  READING COUNTERS OVER SNMP
#  ----------------------------------------------------------------
#  No library to install. A copier answers on UDP 161 and reports
#  its page counts, serial number and toner levels through the
#  standard Printer MIB.
# ================================================================

import struct as _struct

SNMP_PORT = 161
SNMP_WAIT = 2.0          # seconds to wait for one reply
SNMP_TRIES = 2

# The values worth having. The first four are the standard Printer MIB
# and answer on almost anything; the Ricoh ones are its private tree.
OIDS = {
    "name":      "1.3.6.1.2.1.1.5.0",          # what the device calls itself
    "descr":     "1.3.6.1.2.1.1.1.0",          # make and model
    "uptime":    "1.3.6.1.2.1.1.3.0",
    "serial":    "1.3.6.1.2.1.43.5.1.1.17.1",  # printer serial number
    "total":     "1.3.6.1.2.1.43.10.2.1.4.1.1",# life count, all pages
    # Ricoh keeps its own per-function counters here
    "ricoh_total": "1.3.6.1.4.1.367.3.2.1.2.19.5.1.9.1",
    "ricoh_bw":    "1.3.6.1.4.1.367.3.2.1.2.19.5.1.9.2",
    "ricoh_colour":"1.3.6.1.4.1.367.3.2.1.2.19.5.1.9.3",
}


def _len(n):
    """BER length: short form under 128, long form above."""
    if n < 0x80:
        return bytes([n])
    out = b""
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _tlv(tag, body):
    return bytes([tag]) + _len(len(body)) + body


def _int(n):
    """BER integer, two's complement, shortest form."""
    if n == 0:
        return _tlv(0x02, b"\x00")
    out = b""
    neg = n < 0
    v = n if not neg else ~n
    while v:
        out = bytes([v & 0xFF]) + out
        v >>= 8
    if neg:
        out = bytes([(~b) & 0xFF for b in out])
    if (not neg and out[0] & 0x80) or (neg and not (out[0] & 0x80)):
        out = bytes([0xFF if neg else 0x00]) + out
    return _tlv(0x02, out)


def _oid(text):
    """BER object identifier: the first two arcs share a byte, the rest
    are base-128 with a continuation bit."""
    parts = [int(p) for p in text.split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            out += bytes([p])
            continue
        stack = []
        while p:
            stack.insert(0, p & 0x7F)
            p >>= 7
        out += bytes([b | 0x80 for b in stack[:-1]] + [stack[-1]])
    return _tlv(0x06, out)


def _get_request(community, oid, req_id):
    varbind = _tlv(0x30, _oid(oid) + _tlv(0x05, b""))     # oid, null
    pdu = _tlv(0xA0,                                       # GetRequest
               _int(req_id) + _int(0) + _int(0) +          # id, error, index
               _tlv(0x30, varbind))
    return _tlv(0x30,
                _int(1) +                                  # version 2c
                _tlv(0x04, community.encode()) +
                pdu)


def _read_tlv(buf, i):
    tag = buf[i]; i += 1
    n = buf[i]; i += 1
    if n & 0x80:
        k = n & 0x7F
        n = int.from_bytes(buf[i:i+k], "big")
        i += k
    return tag, buf[i:i+n], i + n


def _parse_reply(buf):
    """Walk in far enough to reach the value, and give it back as text."""
    try:
        _, seq, _ = _read_tlv(buf, 0)                 # the whole message
        i = 0
        _, _, i = _read_tlv(seq, i)                   # version
        _, _, i = _read_tlv(seq, i)                   # community
        tag, pdu, _ = _read_tlv(seq, i)               # the response pdu
        j = 0
        _, _, j = _read_tlv(pdu, j)                   # request id
        _, err, j = _read_tlv(pdu, j)                 # error
        if err and err[-1] != 0:
            return None
        _, _, j = _read_tlv(pdu, j)                   # error index
        _, binds, _ = _read_tlv(pdu, j)               # the varbind list
        _, bind, _ = _read_tlv(binds, 0)              # the one varbind
        k = 0
        _, _, k = _read_tlv(bind, k)                  # its oid
        vtag, val, _ = _read_tlv(bind, k)             # its value

        if vtag in (0x02, 0x41, 0x42, 0x43, 0x46):    # integer-ish
            return str(int.from_bytes(val, "big"))
        if vtag == 0x04:                              # octet string
            return val.decode("utf-8", "replace").strip()
        if vtag in (0x05, 0x80, 0x81, 0x82):          # null, or "no such"
            return None
        return val.hex()
    except Exception:
        return None


def snmp_get(host, oid, community="public", timeout=SNMP_WAIT):
    """One value from one device. None when it does not answer."""
    import random
    req_id = random.randint(1, 0x7FFFFFFF)
    pkt = _get_request(community, oid, req_id)

    for _ in range(SNMP_TRIES):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(pkt, (host, SNMP_PORT))
            data, _addr = s.recvfrom(4096)
            return _parse_reply(data)
        except (socket.timeout, OSError):
            continue
        finally:
            s.close()
    return None


def read_meter(host, community="public"):
    """Everything worth having from one machine, in one pass.

    A machine that is off would otherwise cost seven questions at two
    seconds each, twice over. So it is asked one short question first,
    and if nothing comes back the rest is not attempted."""
    out = {"host": host, "at": datetime.datetime.now().isoformat(timespec="seconds")}

    alive = snmp_get(host, OIDS["name"], community, timeout=1.0)
    if alive is None:
        out["ok"] = False
        out["why"] = ("No answer on SNMP. The machine may be off, on another "
                      "network, or have SNMP switched off in its settings.")
        return out
    out["name"] = alive

    for key in ("descr", "serial"):
        v = snmp_get(host, OIDS[key], community)
        if v:
            out[key] = v

    # the Ricoh counters where they answer, the standard one otherwise
    total = snmp_get(host, OIDS["ricoh_total"], community)
    if total is None:
        total = snmp_get(host, OIDS["total"], community)
    if total is not None:
        out["total"] = int(total) if str(total).isdigit() else total

    for key, name in (("ricoh_bw", "bw"), ("ricoh_colour", "colour")):
        v = snmp_get(host, OIDS[key], community)
        if v is not None and str(v).isdigit():
            out[name] = int(v)

    out["ok"] = "total" in out
    if not out["ok"]:
        out["why"] = ("No answer on SNMP. The machine may be off, on another "
                      "network, or have SNMP switched off in its settings.")
    return out


def scan_range(first, last, community="public"):
    """Every machine that answers between two addresses on the same /24.

    Done in parallel because a silent address costs the full timeout and
    a subnet of 254 would otherwise take eight minutes."""
    import ipaddress
    from concurrent.futures import ThreadPoolExecutor

    try:
        a = int(ipaddress.IPv4Address(first))
        b = int(ipaddress.IPv4Address(last))
    except ValueError:
        return {"ok": False, "msg": "Those do not look like addresses."}
    if b < a:
        a, b = b, a
    if b - a > 254:
        return {"ok": False, "msg": "That is more than one subnet at a time."}

    hosts = [str(ipaddress.IPv4Address(n)) for n in range(a, b + 1)]

    def one(h):
        name = snmp_get(h, OIDS["name"], community, timeout=1.0)
        if name is None:
            return None
        return read_meter(h, community)

    found = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for r in pool.map(one, hosts):
            if r:
                found.append(r)
    return {"ok": True, "found": found, "looked": len(hosts)}


# ================================================================
#  METERS
#  ----------------------------------------------------------------
#  One reading per machine per day, kept forever. Rental billing is
#  the difference between two of them, so they are never thinned
#  out, rounded, or tidied away.
# ================================================================

def meters_path(cid):
    return os.path.join(DATA_DIR, (str(cid or "main")) + "-meters.json")


def meters_load(cid):
    try:
        with open(meters_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        d.setdefault("machines", {})
        d.setdefault("readings", [])
        return d
    except (FileNotFoundError, ValueError, OSError):
        return {"machines": {}, "readings": []}


def meters_save(cid, d):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        pass
    p = meters_path(cid)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, separators=(",", ":"))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def meter_keep(cid, req):
    """Take a reading. A second one on the same day replaces the first,
    because two readings in one day is a retry, not two days of printing."""
    m = req.get("meter") or {}
    host = str(m.get("host") or "").strip()
    if not host:
        return {"ok": False, "msg": "No address on that reading"}

    key = str(m.get("serial") or host)
    day = str(m.get("at") or "")[:10] or datetime.date.today().isoformat()

    with _store_lock:
        d = meters_load(cid)
        d["machines"][key] = {
            "host": host,
            "name": m.get("name", ""),
            "descr": m.get("descr", ""),
            "serial": m.get("serial", ""),
            "seen": m.get("at", ""),
        }
        kept = [r for r in d["readings"]
                if not (r.get("key") == key and str(r.get("day")) == day)]
        kept.append({
            "key": key, "day": day, "at": m.get("at", ""),
            "total": m.get("total"), "bw": m.get("bw"), "colour": m.get("colour"),
        })
        kept.sort(key=lambda r: (r.get("key", ""), str(r.get("day", ""))))
        d["readings"] = kept
        meters_save(cid, d)

    note("%s meter %s = %s" % (cid, key, m.get("total")))
    return {"ok": True, "key": key, "day": day, "kept": len(d["readings"])}


def meter_list(cid, req):
    """Every machine, its latest reading, and what it has printed since a
    date if one is given."""
    since = str(req.get("since") or "")
    d = meters_load(cid)
    out = []
    for key, info in sorted(d["machines"].items()):
        mine = [r for r in d["readings"] if r.get("key") == key]
        if not mine:
            continue
        last = mine[-1]
        row = dict(info)
        row["key"] = key
        row["last"] = last
        row["readings"] = len(mine)
        if since:
            before = [r for r in mine if str(r.get("day")) <= since]
            base = before[-1] if before else mine[0]
            row["base"] = base
            try:
                row["used"] = int(last.get("total") or 0) - int(base.get("total") or 0)
            except (TypeError, ValueError):
                row["used"] = None
        out.append(row)
    return {"ok": True, "machines": out}


def meter_history(cid, req):
    # "key" is the shared secret on every request, so a machine is named
    # by "machine" instead - the two cannot share a word
    key = str(req.get("machine") or "")
    d = meters_load(cid)
    return {"ok": True, "key": key,
            "readings": [r for r in d["readings"] if r.get("key") == key]}


def meter_read_now(cid, req):
    """Read one machine, or a range, right now and keep what comes back."""
    community = str(req.get("community") or "public")

    if req.get("first") and req.get("last"):
        r = scan_range(str(req["first"]), str(req["last"]), community)
        if r.get("ok"):
            for m in r["found"]:
                meter_keep(cid, {"meter": m})
        return r

    host = str(req.get("host") or "").strip()
    if not host:
        return {"ok": False, "msg": "Give an address, or a range to look through"}
    m = read_meter(host, community)
    if m.get("ok"):
        meter_keep(cid, {"meter": m})
    return {"ok": m.get("ok", False), "meter": m, "msg": m.get("why", "")}


# ================================================================
#  THE DESK VIEW
#  ----------------------------------------------------------------
#  The phones hold one job each. Somebody at a desk wants the whole
#  picture: every client, every machine, what has not been read, and
#  what is owed. That is all worked out here, once, rather than in
#  five places.
# ================================================================

def _days_since(iso):
    if not iso:
        return 9999
    try:
        d = datetime.datetime.fromisoformat(str(iso)[:19])
        return (datetime.datetime.now() - d).days
    except ValueError:
        return 9999


def console_overview(cid):
    """Everything a person needs to decide what to do next."""
    d = store_load(cid)
    meters = meters_load(cid)

    jobs = list(d.get("invoices", {}).values())     # complaints ride here
    clients = d.get("clients", {})
    machines = d.get("items", {})

    open_jobs = [j for j in jobs
                 if j.get("status") not in ("resolved", "closed")]
    unassigned = [j for j in open_jobs if not j.get("tech")]
    waiting = [j for j in open_jobs if j.get("status") == "parts"]

    # a machine nobody has read for a month is the thing that costs money
    stale = []
    for key, info in meters.get("machines", {}).items():
        mine = [r for r in meters.get("readings", []) if r.get("key") == key]
        last = mine[-1] if mine else None
        age = _days_since(last.get("at")) if last else 9999
        if age >= 25:
            stale.append({"key": key, "name": info.get("name", ""),
                          "host": info.get("host", ""),
                          "days": None if age == 9999 else age,
                          "last": last})

    # machines the app knows about that have no address to read
    noip = [m for m in machines.values() if not (m.get("ip") or "").strip()]

    return {"ok": True,
            "jobs": {"open": len(open_jobs), "unassigned": len(unassigned),
                     "waiting": len(waiting), "all": len(jobs)},
            "clients": len(clients), "machines": len(machines),
            "meters": {"known": len(meters.get("machines", {})),
                       "readings": len(meters.get("readings", [])),
                       "stale": stale, "noAddress": len(noip)},
            "rev": d.get("rev", 0)}


def console_machines(cid):
    """Every machine the app knows, with its client, its address, and its
    latest reading if there is one."""
    d = store_load(cid)
    meters = meters_load(cid)
    clients = d.get("clients", {})

    latest = {}
    for r in meters.get("readings", []):
        latest[r.get("key")] = r
    known = meters.get("machines", {})

    out = []
    for m in d.get("items", {}).values():
        serial = (m.get("serial") or "").strip()
        reading = latest.get(serial)
        info = known.get(serial, {})
        c = clients.get(m.get("client"), {})
        out.append({
            "id": m.get("id"), "model": m.get("model", ""),
            "serial": serial, "dept": m.get("dept", ""),
            "place": m.get("place", ""),
            "client": c.get("name", ""), "clientId": m.get("client"),
            "deal": c.get("deal", "cash"),
            "ip": m.get("ip", ""),
            "rate": m.get("rate"),            # rental rate per copy
            "free": m.get("free"),            # copies included each month
            "seenName": info.get("name", ""),
            "last": reading,
            "age": _days_since(reading.get("at")) if reading else None,
        })
    out.sort(key=lambda r: (r["client"], r["model"]))
    return {"ok": True, "machines": out}


def console_read_all(cid, req):
    """Read every machine that has an address, in parallel.

    One at a time would take a minute a machine on a bad day, and nobody
    watches a screen that long."""
    from concurrent.futures import ThreadPoolExecutor

    d = store_load(cid)
    community = str(req.get("community") or "public")
    todo = [m for m in d.get("items", {}).values() if (m.get("ip") or "").strip()]
    if not todo:
        return {"ok": False,
                "msg": "No machine has an address yet. Put one on each machine "
                       "first, or look through a range."}

    def one(m):
        r = read_meter(m["ip"].strip(), community)
        r["machine"] = m.get("id")
        r["model"] = m.get("model", "")
        if r.get("ok"):
            if not r.get("serial") and m.get("serial"):
                r["serial"] = m["serial"]
            meter_keep(cid, {"meter": r})
        return r

    done = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for r in pool.map(one, todo):
            done.append(r)

    good = [r for r in done if r.get("ok")]
    note("%s read %d of %d machines" % (cid, len(good), len(done)))
    return {"ok": True, "read": len(good), "tried": len(done), "results": done}


def console_assign(cid, req):
    """Give a job to a technician from the desk. The phones pick it up on
    their next sync."""
    job_id = str(req.get("job") or "")
    tech = str(req.get("tech") or "")
    with _store_lock:
        d = store_load(cid)
        j = d.get("invoices", {}).get(job_id)
        if not j:
            return {"ok": False, "msg": "No such job"}
        j["tech"] = tech
        j["_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        if j.get("status") == "pending" and tech:
            j["assignedAt"] = j["_at"]
        d["rev"] += 1
        store_save(cid, d)
    note("%s assigned %s" % (cid, job_id))
    return {"ok": True}


def console_set_machine(cid, req):
    """Put an address, a rate or an allowance on a machine."""
    mid = str(req.get("machine") or "")
    with _store_lock:
        d = store_load(cid)
        m = d.get("items", {}).get(mid)
        if not m:
            return {"ok": False, "msg": "No such machine"}
        for field in ("ip", "rate", "free", "serial"):
            if field in req:
                m[field] = req[field]
        m["_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        d["rev"] += 1
        store_save(cid, d)
    return {"ok": True}


# ---------------------------------------------------------------- billing

def console_rental_due(cid, req):
    """What each rental machine owes between two dates.

    The reading on or before the start is the opening figure, and the last
    reading on or before the end is the closing one. Anything else invents
    pages that were never printed."""
    start = str(req.get("from") or "")
    end = str(req.get("to") or datetime.date.today().isoformat())
    if not start:
        return {"ok": False, "msg": "Give a date to bill from"}

    d = store_load(cid)
    meters = meters_load(cid)
    clients = d.get("clients", {})

    by_key = {}
    for r in meters.get("readings", []):
        by_key.setdefault(r.get("key"), []).append(r)

    rows = []
    for m in d.get("items", {}).values():
        c = clients.get(m.get("client"), {})
        deal = c.get("deal", "cash")
        if deal != "rental":
            continue

        serial = (m.get("serial") or "").strip()
        mine = sorted(by_key.get(serial, []), key=lambda r: str(r.get("day")))
        before = [r for r in mine if str(r.get("day")) <= start]
        within = [r for r in mine if str(r.get("day")) <= end]

        row = {"machine": m.get("id"), "model": m.get("model", ""),
               "serial": serial, "client": c.get("name", ""),
               "clientId": m.get("client"),
               "rate": m.get("rate"), "free": m.get("free")}

        if not before or not within or before[-1] is within[-1]:
            row["ok"] = False
            row["why"] = ("Two readings are needed \u2014 one on or before "
                          + start + ", and a later one.")
            rows.append(row)
            continue

        opening, closing = before[-1], within[-1]
        try:
            pages = int(closing.get("total") or 0) - int(opening.get("total") or 0)
        except (TypeError, ValueError):
            row["ok"] = False
            row["why"] = "The readings are not numbers."
            rows.append(row)
            continue

        if pages < 0:
            row["ok"] = False
            row["why"] = ("The counter went backwards. The machine was probably "
                          "replaced or reset \u2014 this one needs a person.")
            rows.append(row)
            continue

        free = int(m.get("free") or 0)
        rate = float(m.get("rate") or 0)
        charged = max(0, pages - free)

        row.update({"ok": True, "openingDay": opening.get("day"),
                    "closingDay": closing.get("day"),
                    "opening": opening.get("total"), "closing": closing.get("total"),
                    "pages": pages, "freeUsed": min(pages, free),
                    "charged": charged,
                    "amount": round(charged * rate, 2)})
        rows.append(row)

    good = [r for r in rows if r.get("ok")]
    return {"ok": True, "from": start, "to": end, "rows": rows,
            "total": round(sum(r["amount"] for r in good), 2),
            "ready": len(good), "stuck": len(rows) - len(good)}


CONSOLE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paragon &mdash; Console</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
 --ink:#16202B;--ink-2:#55606D;--ink-3:#8794A3;--line:#D8DDE4;--soft:#F5F7F9;
 --blue:#1F5FCC;--blue-lt:#E7EFFC;--green:#17794B;--green-lt:#E8F3EC;
 --amber:#94620F;--amber-lt:#FCF6E9;--red:#C0322C;--red-lt:#FBEEED;--bg:#EEF1F5;
}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 background:var(--bg);color:var(--ink);font-size:14px;line-height:1.55}
.hide{display:none!important}
.bar{background:#1C2430;color:#fff;padding:12px 22px;display:flex;
 align-items:center;gap:13px;position:sticky;top:0;z-index:40}
.bar .mk{width:32px;height:32px;border-radius:7px;background:var(--blue);
 display:flex;align-items:center;justify-content:center;flex-shrink:0}
.bar .mk i{display:block;width:14px;height:2px;background:#fff;border-radius:1px;
 box-shadow:0 5px 0 rgba(255,255,255,.62),0 10px 0 rgba(255,255,255,.34)}
.bar .t{font-size:15px;font-weight:600;flex:1}
.bar .t span{display:block;font-size:11px;font-weight:400;color:#8FA6C4}
.bar button{background:transparent;border:1px solid #3A4757;color:#C7D3E3;
 font-family:inherit;font-size:12.5px;padding:6px 12px;border-radius:5px;cursor:pointer}
.bar button:hover{background:#28323F}
.tabs{background:#fff;border-bottom:1px solid var(--line);padding:0 22px;
 display:flex;gap:2px;position:sticky;top:56px;z-index:39;overflow-x:auto}
.tabs button{background:transparent;border:0;border-bottom:2px solid transparent;
 font-family:inherit;font-size:13.5px;color:var(--ink-2);padding:11px 15px;
 cursor:pointer;white-space:nowrap}
.tabs button.on{color:var(--blue);border-bottom-color:var(--blue);font-weight:600}
.wrap{max-width:1180px;margin:0 auto;padding:22px}
.page{display:none}.page.on{display:block}
h2{font-size:17px;font-weight:600;margin-bottom:3px}
.sub{font-size:12.5px;color:var(--ink-3);margin-bottom:16px}
.card{background:#fff;border:1px solid var(--line);border-radius:6px;
 padding:16px 18px;margin-bottom:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
 gap:11px;margin-bottom:16px}
.tile{background:#fff;border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.tile .n{font-size:26px;font-weight:600;letter-spacing:-.6px;line-height:1}
.tile .l{font-size:11.5px;color:var(--ink-3);margin-top:5px}
.tile.b .n{color:var(--blue)} .tile.g .n{color:var(--green)}
.tile.a .n{color:var(--amber)} .tile.r .n{color:var(--red)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;letter-spacing:.05em;color:var(--ink-3);
 padding:9px 10px;border-bottom:1px solid var(--line);font-weight:600;
 white-space:nowrap}
td{padding:10px;border-bottom:1px solid #EDF0F3;vertical-align:middle}
tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;
 border-radius:10px;white-space:nowrap}
.p-on{background:var(--green-lt);color:var(--green)}
.p-off{background:var(--soft);color:var(--ink-3)}
.p-warn{background:var(--amber-lt);color:var(--amber)}
.p-bad{background:var(--red-lt);color:var(--red)}
.btn{font-family:inherit;font-size:13px;font-weight:600;padding:8px 15px;
 border:1px solid var(--blue);background:var(--blue);color:#fff;border-radius:5px;
 cursor:pointer}
.btn:hover{opacity:.92}
.btn.ghost{background:#fff;color:var(--ink);border-color:var(--line)}
.btn.sm{font-size:12px;padding:5px 11px}
.btn.green{background:var(--green);border-color:var(--green)}
input,select{padding:8px 10px;font-size:13.5px;font-family:inherit;
 border:1px solid var(--line);border-radius:5px;background:#fff;color:var(--ink)}
input:focus,select:focus{outline:0;border-color:var(--blue);
 box-shadow:0 0 0 3px var(--blue-lt)}
input.tiny{width:96px}
label{display:block;font-size:11.5px;font-weight:600;margin-bottom:5px}
.row{display:flex;gap:11px;align-items:flex-end;flex-wrap:wrap}
.note{padding:11px 14px;border-radius:5px;font-size:13px;line-height:1.6;
 border-left:3px solid;margin-bottom:14px}
.note.i{background:var(--soft);border-color:var(--blue)}
.note.w{background:var(--amber-lt);border-color:var(--amber);color:var(--amber)}
.note.d{background:var(--red-lt);border-color:var(--red);color:var(--red)}
.note.g{background:var(--green-lt);border-color:var(--green);color:var(--green)}
.empty{text-align:center;padding:40px 20px;color:var(--ink-3);font-size:13.5px}
code{font-family:Consolas,Menlo,monospace;font-size:12.5px;background:var(--soft);
 padding:1px 5px;border-radius:3px}
.gate{max-width:380px;margin:70px auto;background:#fff;border:1px solid var(--line);
 border-radius:8px;padding:26px 24px}
#msg{position:fixed;right:22px;bottom:22px;z-index:90;background:#1C2430;color:#fff;
 padding:12px 16px;border-radius:6px;font-size:13px;max-width:380px;opacity:0;
 transform:translateY(8px);transition:.18s;pointer-events:none}
#msg.on{opacity:1;transform:none}
#msg.bad{background:var(--red)} #msg.good{background:var(--green)}
.spin{display:inline-block;width:13px;height:13px;border:2px solid var(--line);
 border-top-color:var(--blue);border-radius:50%;animation:sp .7s linear infinite;
 vertical-align:-2px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>

<div id="gate" class="gate">
  <h2>Console</h2>
  <div class="sub">The shared secret from the bridge</div>
  <div style="margin-bottom:14px"><label>Secret</label>
    <input id="gKey" type="password" style="width:100%"
           onkeydown="if(event.key==='Enter')signIn()"></div>
  <div id="gErr" style="color:var(--red);font-size:13px;margin-bottom:12px"></div>
  <button class="btn" style="width:100%" onclick="signIn()">Open</button>
  <div style="font-size:11.5px;color:var(--ink-3);margin-top:14px;line-height:1.6">
    This is the same secret the phones use. It stays in this browser only.</div>
</div>

<div id="app" class="hide">
  <div class="bar">
    <div class="mk"><i></i></div>
    <div class="t">Paragon Console<span id="barSub">&mdash;</span></div>
    <button onclick="loadAll()">Refresh</button>
    <button onclick="signOut()">Lock</button>
  </div>

  <div class="tabs">
    <button data-p="over"  class="on" onclick="go('over')">Overview</button>
    <button data-p="jobs"  onclick="go('jobs')">Complaints</button>
    <button data-p="mach"  onclick="go('mach')">Machines</button>
    <button data-p="cnt"   onclick="go('cnt')">Counters</button>
    <button data-p="bill"  onclick="go('bill')">Rental billing</button>
  </div>

  <div class="wrap">
    <div class="page on" id="pg-over"><div id="overBody"></div></div>
    <div class="page" id="pg-jobs"><div id="jobsBody"></div></div>
    <div class="page" id="pg-mach"><div id="machBody"></div></div>
    <div class="page" id="pg-cnt"><div id="cntBody"></div></div>
    <div class="page" id="pg-bill"><div id="billBody"></div></div>
  </div>
</div>

<div id="msg"></div>

<script>
var KEY = '', DATA = {}, PAGE = 'over';

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function num(n){ return (n==null||n==='') ? '' : Number(n).toLocaleString('en-PK'); }
function money(n){ return 'Rs. ' + Math.round(Number(n)||0).toLocaleString('en-PK'); }

function dmy(iso){
  if(!iso) return '';
  var p = String(iso).slice(0,10).split('-');
  return p.length===3 ? p[2]+'-'+p[1]+'-'+p[0] : iso;
}

function say(text, kind){
  var m = $('msg');
  m.textContent = text;
  m.className = 'on' + (kind ? ' ' + kind : '');
  clearTimeout(m._x);
  m._x = setTimeout(function(){ m.className = ''; }, kind==='bad' ? 8000 : 3200);
}

function ask(body){
  return fetch('/', { method:'POST', headers:{'Content-Type':'text/plain'},
    body: JSON.stringify(Object.assign({ key: KEY }, body)) })
    .then(function(r){ return r.json(); });
}

/* ---------- getting in ---------- */
function signIn(){
  KEY = $('gKey').value;
  ask({ action:'overview' }).then(function(d){
    if(!d || !d.ok){
      $('gErr').textContent = (d && d.msg) || 'That secret was refused.';
      KEY = '';
      return;
    }
    try { sessionStorage.setItem('pk', KEY); } catch(e){}
    $('gate').classList.add('hide');
    $('app').classList.remove('hide');
    loadAll();
  }).catch(function(e){
    $('gErr').textContent = 'The bridge did not answer: ' + e.message;
  });
}

function signOut(){
  KEY = '';
  try { sessionStorage.removeItem('pk'); } catch(e){}
  $('app').classList.add('hide');
  $('gate').classList.remove('hide');
  $('gKey').value = '';
}

function go(p){
  PAGE = p;
  Array.prototype.forEach.call(document.querySelectorAll('.page'), function(el){
    el.classList.toggle('on', el.id === 'pg-' + p); });
  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function(b){
    b.classList.toggle('on', b.dataset.p === p); });
  draw();
}

function loadAll(){
  Promise.all([ ask({action:'overview'}), ask({action:'pull'}),
                ask({action:'machines'}) ])
    .then(function(r){
      DATA.over = r[0]; DATA.store = r[1]; DATA.mach = r[2];
      $('barSub').textContent =
        (DATA.over.clients||0) + ' clients \u00b7 ' +
        (DATA.over.machines||0) + ' machines \u00b7 ' +
        (DATA.over.jobs ? DATA.over.jobs.open : 0) + ' open';
      draw();
    })
    .catch(function(e){ say('Could not reach the bridge: ' + e.message, 'bad'); });
}

function draw(){
  if(PAGE==='over') drawOver();
  if(PAGE==='jobs') drawJobs();
  if(PAGE==='mach') drawMach();
  if(PAGE==='cnt')  drawCnt();
  if(PAGE==='bill') drawBill();
}

/* ---------- overview ---------- */
function drawOver(){
  var o = DATA.over || {}, j = o.jobs || {}, m = o.meters || {};
  $('overBody').innerHTML =
    '<h2>Where things stand</h2>'+
    '<div class="sub">Everything the phones have synced up</div>'+

    '<div class="tiles">'+
      tile('a', j.open||0, 'Open complaints')+
      tile('r', j.unassigned||0, 'Nobody assigned')+
      tile('b', j.waiting||0, 'Waiting for parts')+
      tile('g', m.readings||0, 'Counter readings held')+
    '</div>'+

    ((j.unassigned||0)
      ? '<div class="note w"><b>'+j.unassigned+' complaint(s) have nobody on '+
        'them.</b> <a href="#" onclick="go(\'jobs\');return false">Assign '+
        'them</a>.</div>' : '')+

    ((m.noAddress||0)
      ? '<div class="note i"><b>'+m.noAddress+' machine(s) have no address</b>, '+
        'so their counters cannot be read. <a href="#" onclick="go(\'mach\');'+
        'return false">Add them</a>.</div>' : '')+

    ((m.stale||[]).length
      ? '<div class="card"><h2 style="font-size:15px">Not read for a while</h2>'+
        '<div class="sub">Rental billing is the difference between two readings, '+
        'so a gap here is money nobody can invoice.</div>'+
        '<table><tr><th>Machine</th><th>Address</th><th>Last read</th>'+
        '<th></th></tr>'+
        m.stale.map(function(s){
          return '<tr><td><b>'+esc(s.name||s.key)+'</b><div style="font-size:12px;'+
            'color:var(--ink-3)">'+esc(s.key)+'</div></td>'+
            '<td><code>'+esc(s.host||'')+'</code></td>'+
            '<td>'+(s.last ? esc(dmy(s.last.at)) + ' \u00b7 ' +
              (s.days>500?'a long time ago':s.days+' days ago') : 'never')+'</td>'+
            '<td style="text-align:right">'+(s.host
              ? '<button class="btn sm ghost" onclick="readOne(\''+esc(s.host)+
                '\')">Read now</button>' : '')+'</td></tr>'; }).join('')+
        '</table></div>'
      : '<div class="note g">Every machine has been read recently.</div>');
}

function tile(cls, n, label){
  return '<div class="tile '+cls+'"><div class="n">'+n+'</div>'+
         '<div class="l">'+esc(label)+'</div></div>';
}

/* ---------- complaints ---------- */
var JOB_VIEW = 'open';
function jobView(v){ JOB_VIEW = v; drawJobs(); }

function drawJobs(){
  var s = DATA.store || {};
  var jobs = Object.keys(s.invoices||{}).map(function(k){ return s.invoices[k]; });
  var clients = s.clients || {}, machines = s.items || {};

  var shown = jobs.filter(function(j){
    if(JOB_VIEW==='open') return ['resolved','closed'].indexOf(j.status)===-1;
    if(JOB_VIEW==='none') return !j.tech &&
      ['resolved','closed'].indexOf(j.status)===-1;
    if(JOB_VIEW==='parts') return j.status==='parts';
    return true;
  }).sort(function(a,b){ return String(b.opened||'') < String(a.opened||'') ? -1:1; });

  /* who a job can be given to */
  var techs = [];
  try {
    techs = (JSON.parse(localStorage.getItem('consoleTechs')||'[]'));
  } catch(e){}

  $('jobsBody').innerHTML =
    '<h2>Complaints</h2>'+
    '<div class="sub">Assigning here reaches the phone on its next sync</div>'+

    '<div class="row" style="margin-bottom:14px">'+
      ['open','Open','none','Nobody assigned','parts','Waiting for parts','all','All']
        .reduce(function(a,v,i,arr){
          if(i%2) return a;
          return a + '<button class="btn sm '+(JOB_VIEW===v?'':'ghost')+
            '" onclick="jobView(\''+v+'\')">'+arr[i+1]+'</button>'; },'')+
    '</div>'+

    (shown.length
      ? '<div class="card" style="padding:0"><table>'+
        '<tr><th>Job</th><th>Client</th><th>Machine</th><th>Fault</th>'+
        '<th>Status</th><th>Technician</th></tr>'+
        shown.map(function(j){
          var c = clients[j.client] || {}, m = machines[j.machine] || {};
          return '<tr><td><b>'+esc(j.no||'')+'</b><div style="font-size:12px;'+
            'color:var(--ink-3)">'+esc(dmy(j.opened))+'</div></td>'+
            '<td>'+esc(c.name||'')+'</td>'+
            '<td>'+esc(m.model||'')+'<div style="font-size:12px;color:var(--ink-3)">'+
              esc(m.serial||'')+'</div></td>'+
            '<td style="max-width:280px">'+esc(String(j.what||'').slice(0,90))+'</td>'+
            '<td><span class="pill '+
              (j.status==='resolved'?'p-on':j.status==='parts'?'p-warn':'p-off')+
              '">'+esc(j.status||'')+'</span></td>'+
            '<td>'+techPicker(j, techs)+'</td></tr>'; }).join('')+
        '</table></div>'
      : '<div class="empty">Nothing here.</div>')+

    '<div class="card"><label>Technicians this console can assign to</label>'+
      '<div class="sub">The console does not hold your staff list \u2014 the app '+
      'does. Put their names and ids here once so this screen can offer them.</div>'+
      '<textarea id="techList" rows="4" style="width:100%;font-family:Consolas,'+
      'Menlo,monospace;font-size:12.5px;padding:9px;border:1px solid var(--line);'+
      'border-radius:5px" placeholder="id, name&#10;tc1, Farhan&#10;tc2, Hassan Ali">'+
      esc(techs.map(function(t){ return t.id+', '+t.name; }).join('\n'))+'</textarea>'+
      '<button class="btn sm ghost" style="margin-top:9px" onclick="saveTechs()">'+
      'Save</button></div>';
}

function techPicker(j, techs){
  if(!techs.length)
    return j.tech ? '<code>'+esc(j.tech)+'</code>'
                  : '<span class="pill p-bad">nobody</span>';
  return '<select onchange="assign(\''+esc(j.id)+'\',this.value)">'+
    '<option value="">\u2014 nobody \u2014</option>'+
    techs.map(function(t){
      return '<option value="'+esc(t.id)+'"'+(j.tech===t.id?' selected':'')+'>'+
             esc(t.name)+'</option>'; }).join('')+'</select>';
}

function saveTechs(){
  var lines = ($('techList').value||'').split('\n');
  var out = [];
  lines.forEach(function(l){
    var bits = l.split(',');
    if(bits.length < 2) return;
    var id = bits[0].trim(), name = bits.slice(1).join(',').trim();
    if(id && name) out.push({ id:id, name:name });
  });
  try { localStorage.setItem('consoleTechs', JSON.stringify(out)); } catch(e){}
  drawJobs();
  say(out.length + ' technician(s) saved on this computer','good');
}

function assign(job, tech){
  ask({ action:'assign', job:job, tech:tech }).then(function(d){
    if(!d.ok) return say(d.msg||'It was refused','bad');
    say(tech ? 'Assigned. The phone will pick it up.' : 'Unassigned','good');
    loadAll();
  });
}

/* ---------- machines ---------- */
function drawMach(){
  var list = (DATA.mach && DATA.mach.machines) || [];
  $('machBody').innerHTML =
    '<h2>Machines</h2>'+
    '<div class="sub">An address here is what lets the counter be read. '+
    'The rate and the free allowance are what turn a counter into a bill.</div>'+

    '<div class="row" style="margin-bottom:14px">'+
      '<button class="btn" onclick="readAll()">Read every counter now</button>'+
      '<div><label>Or look through a range</label>'+
        '<input id="rgFrom" class="tiny" placeholder="192.168.1.1"> '+
        '<input id="rgTo" class="tiny" placeholder="192.168.1.254"> '+
        '<button class="btn ghost" onclick="scanRange()">Look</button></div>'+
    '</div>'+

    (list.length
      ? '<div class="card" style="padding:0"><table>'+
        '<tr><th>Client</th><th>Machine</th><th>Address</th>'+
        '<th class="num">Rate / copy</th><th class="num">Free copies</th>'+
        '<th>Last reading</th><th></th></tr>'+
        list.map(function(m){
          var stale = m.age == null || m.age >= 25;
          return '<tr><td>'+esc(m.client)+
            '<div style="font-size:12px;color:var(--ink-3)">'+
              esc(dealName(m.deal))+'</div></td>'+
            '<td><b>'+esc(m.model)+'</b><div style="font-size:12px;'+
              'color:var(--ink-3)">'+esc(m.serial||'no serial')+'</div></td>'+
            '<td><input class="tiny" value="'+esc(m.ip||'')+'" '+
              'onchange="setMach(\''+esc(m.id)+'\',\'ip\',this.value)" '+
              'placeholder="192.168.1.50"></td>'+
            '<td class="num"><input class="tiny" style="width:76px;text-align:right" '+
              'value="'+esc(m.rate==null?'':m.rate)+'" '+
              'onchange="setMach(\''+esc(m.id)+'\',\'rate\',this.value)"></td>'+
            '<td class="num"><input class="tiny" style="width:76px;text-align:right" '+
              'value="'+esc(m.free==null?'':m.free)+'" '+
              'onchange="setMach(\''+esc(m.id)+'\',\'free\',this.value)"></td>'+
            '<td>'+(m.last
              ? num(m.last.total)+'<div style="font-size:12px;color:'+
                (stale?'var(--amber)':'var(--ink-3)')+'">'+esc(dmy(m.last.at))+
                (m.age!=null?' \u00b7 '+m.age+'d':'')+'</div>'
              : '<span class="pill p-off">never</span>')+'</td>'+
            '<td style="text-align:right">'+(m.ip
              ? '<button class="btn sm ghost" onclick="readOne(\''+esc(m.ip)+
                '\')">Read</button>' : '')+'</td></tr>'; }).join('')+
        '</table></div>'
      : '<div class="empty">No machines yet. They come from the app.</div>');
}

function dealName(d){
  return { rental:'Rental', ascWith:'ASC \u2014 with parts',
           ascWithout:'ASC \u2014 without parts', cash:'Cash client' }[d] || d || '';
}

function setMach(id, field, value){
  var body = { action:'setmachine', machine:id };
  body[field] = field === 'ip' ? value.trim() :
                (value === '' ? null : parseFloat(value));
  ask(body).then(function(d){
    if(!d.ok) return say(d.msg||'It was refused','bad');
    say('Saved','good');
  });
}

function readOne(host){
  say('Reading ' + host + '\u2026');
  ask({ action:'readnow', host:host }).then(function(d){
    if(!d.ok) return say(d.msg || 'No answer from ' + host, 'bad');
    say(host + ' \u2014 ' + num(d.meter.total) + ' pages','good');
    loadAll();
  });
}

function readAll(){
  say('Reading every machine that has an address\u2026');
  ask({ action:'readall' }).then(function(d){
    if(!d.ok) return say(d.msg || 'It was refused','bad');
    say(d.read + ' of ' + d.tried + ' answered', d.read ? 'good' : 'bad');
    loadAll();
  });
}

function scanRange(){
  var a = $('rgFrom').value.trim(), b = $('rgTo').value.trim();
  if(!a || !b) return say('Put both addresses in','bad');
  say('Looking through the range\u2026 this takes a moment');
  ask({ action:'readnow', first:a, last:b }).then(function(d){
    if(!d.ok) return say(d.msg||'It was refused','bad');
    say('Found ' + (d.found||[]).length + ' machine(s) out of ' + d.looked +
        ' addresses', (d.found||[]).length ? 'good' : 'bad');
    loadAll();
  });
}

/* ---------- counters ---------- */
function drawCnt(){
  var list = (DATA.mach && DATA.mach.machines || []).filter(function(m){
    return m.last; });
  $('cntBody').innerHTML =
    '<h2>Counters</h2>'+
    '<div class="sub">The latest reading on every machine that has answered</div>'+
    (list.length
      ? '<div class="card" style="padding:0"><table>'+
        '<tr><th>Client</th><th>Machine</th><th class="num">Total</th>'+
        '<th class="num">Black</th><th class="num">Colour</th>'+
        '<th>Read</th></tr>'+
        list.map(function(m){
          return '<tr><td>'+esc(m.client)+'</td>'+
            '<td><b>'+esc(m.model)+'</b><div style="font-size:12px;'+
              'color:var(--ink-3)">'+esc(m.serial||'')+'</div></td>'+
            '<td class="num"><b>'+num(m.last.total)+'</b></td>'+
            '<td class="num">'+num(m.last.bw)+'</td>'+
            '<td class="num">'+num(m.last.colour)+'</td>'+
            '<td>'+esc(dmy(m.last.at))+'</td></tr>'; }).join('')+
        '</table></div>'
      : '<div class="empty">Nothing read yet. Put addresses on the machines and '+
        'press <b>Read every counter now</b>.</div>');
}

/* ---------- rental billing ---------- */
function drawBill(){
  var first = new Date(); first.setMonth(first.getMonth()-1);
  var from = (DATA.billFrom || first.toISOString().slice(0,10));
  var to   = (DATA.billTo   || new Date().toISOString().slice(0,10));

  $('billBody').innerHTML =
    '<h2>Rental billing</h2>'+
    '<div class="sub">What each rental machine printed between two readings</div>'+
    '<div class="card"><div class="row">'+
      '<div><label>From</label><input id="bFrom" type="date" value="'+from+'"></div>'+
      '<div><label>To</label><input id="bTo" type="date" value="'+to+'"></div>'+
      '<button class="btn" onclick="runBill()">Work it out</button>'+
    '</div></div>'+
    '<div id="billOut"></div>';
}

function runBill(){
  DATA.billFrom = $('bFrom').value; DATA.billTo = $('bTo').value;
  $('billOut').innerHTML = '<div class="card"><span class="spin"></span>'+
    'Working it out\u2026</div>';
  ask({ action:'rental', from:DATA.billFrom, to:DATA.billTo }).then(function(d){
    if(!d.ok){ $('billOut').innerHTML = '<div class="note d">'+esc(d.msg)+
      '</div>'; return; }

    var good = d.rows.filter(function(r){ return r.ok; });
    var bad  = d.rows.filter(function(r){ return !r.ok; });

    $('billOut').innerHTML =
      '<div class="tiles">'+
        tile('g', good.length, 'Ready to bill')+
        tile('a', bad.length, 'Need a person')+
        tile('b', money(d.total).replace('Rs. ',''), 'Total, rupees')+
      '</div>'+

      (good.length
        ? '<div class="card" style="padding:0"><table>'+
          '<tr><th>Client</th><th>Machine</th><th>Opening</th><th>Closing</th>'+
          '<th class="num">Pages</th><th class="num">Free</th>'+
          '<th class="num">Charged</th><th class="num">Rate</th>'+
          '<th class="num">Amount</th></tr>'+
          good.map(function(r){
            return '<tr><td>'+esc(r.client)+'</td>'+
              '<td><b>'+esc(r.model)+'</b><div style="font-size:12px;'+
                'color:var(--ink-3)">'+esc(r.serial)+'</div></td>'+
              '<td>'+num(r.opening)+'<div style="font-size:12px;color:var(--ink-3)">'+
                esc(dmy(r.openingDay))+'</div></td>'+
              '<td>'+num(r.closing)+'<div style="font-size:12px;color:var(--ink-3)">'+
                esc(dmy(r.closingDay))+'</div></td>'+
              '<td class="num">'+num(r.pages)+'</td>'+
              '<td class="num">'+num(r.freeUsed)+'</td>'+
              '<td class="num"><b>'+num(r.charged)+'</b></td>'+
              '<td class="num">'+(r.rate||0)+'</td>'+
              '<td class="num"><b>'+money(r.amount)+'</b></td></tr>'; }).join('')+
          '<tr><td colspan="8" style="text-align:right;font-weight:600">Total</td>'+
          '<td class="num" style="font-weight:600">'+money(d.total)+'</td></tr>'+
          '</table></div>'+
          '<button class="btn green" onclick="printBill()">Print this</button>'
        : '')+

      (bad.length
        ? '<div class="card"><h2 style="font-size:15px">These need a person</h2>'+
          '<div class="sub">Nothing here is billed automatically. A guess on a '+
          'client invoice is worse than a blank.</div>'+
          '<table><tr><th>Client</th><th>Machine</th><th>Why</th></tr>'+
          bad.map(function(r){
            return '<tr><td>'+esc(r.client)+'</td><td>'+esc(r.model)+'</td>'+
              '<td>'+esc(r.why)+'</td></tr>'; }).join('')+'</table></div>'
        : '');
  });
}

function printBill(){
  var w = window.open('', '_blank');
  w.document.write('<!doctype html><html><head><meta charset="utf-8">'+
    '<title>Rental billing</title><style>'+
    'body{font-family:-apple-system,Arial,sans-serif;padding:16mm;font-size:11pt}'+
    'h1{font-size:15pt;margin-bottom:2mm}'+
    'table{width:100%;border-collapse:collapse;font-size:9.5pt;margin-top:5mm}'+
    'th{text-align:left;background:#F5F7F9;padding:2mm;border-bottom:1px solid #D8DDE4}'+
    'td{padding:2mm;border-bottom:1px solid #EDF0F3}'+
    '.num{text-align:right}</style></head><body>'+
    '<h1>Rental billing</h1><div>'+esc(dmy(DATA.billFrom))+' to '+
    esc(dmy(DATA.billTo))+'</div>'+
    $('billOut').querySelector('table').outerHTML +
    '</body></html>');
  w.document.close();
  setTimeout(function(){ w.print(); }, 300);
}

/* ---------- start ---------- */
try {
  var saved = sessionStorage.getItem('pk');
  if(saved){ $('gKey').value = saved; signIn(); }
} catch(e){}
</script>
</body></html>
"""


# ================================================================
#  THE CLIENT'S FORM
#  ----------------------------------------------------------------
#  Reached by scanning the sticker on a machine. No login, because
#  a client who has to make an account will phone instead.
# ================================================================

_rate = {}
_rate_lock = threading.Lock()


def rate_ok(serial, seconds=300):
    """One submission per machine per five minutes.

    Not to stop a determined person \u2014 nothing here would \u2014 but
    to stop a bored one, and to stop four people reporting the same jam
    in the same minute."""
    now_s = time.time()
    with _rate_lock:
        for k in [k for k, v in _rate.items() if now_s - v > 3600]:
            _rate.pop(k, None)
        last = _rate.get(serial)
        if last and now_s - last < seconds:
            return False
        _rate[serial] = now_s
        return True


def find_machine(cid, serial):
    d = store_load(cid)
    want = str(serial or "").strip().lower()
    for m in d.get("items", {}).values():
        if str(m.get("serial", "")).strip().lower() == want:
            return m, d
    return None, d


def open_complaint_on(d, machine_id):
    """An open complaint on that machine, if there is one."""
    for j in d.get("invoices", {}).values():
        if j.get("machine") == machine_id and \
           j.get("status") not in ("resolved", "closed"):
            return j
    return None


def scan_look(cid, serial):
    """What the form needs to draw itself. Nothing more."""
    m, d = find_machine(cid, serial)
    if not m:
        return {"ok": False, "msg": "That code does not match any machine."}

    c = d.get("clients", {}).get(m.get("client"), {})
    open_one = open_complaint_on(d, m.get("id"))

    return {"ok": True,
            "machine": {"model": m.get("model", ""),
                        "serial": m.get("serial", ""),
                        "where": " \u2014 ".join(
                            [x for x in (m.get("place"), m.get("dept")) if x])},
            "client": c.get("name", ""),
            "brand": c.get("brand", "paragon"),
            "already": ({"no": open_one.get("no"),
                         "on": str(open_one.get("opened", ""))[:10]}
                        if open_one else None)}


def scan_report(cid, req):
    """Take a fault report from a client."""
    serial = str(req.get("serial") or "")
    m, _ = find_machine(cid, serial)
    if not m:
        return {"ok": False, "msg": "That code does not match any machine."}

    what = str(req.get("what") or "").strip()
    who = str(req.get("who") or "").strip()
    mobile = "".join(ch for ch in str(req.get("mobile") or "") if ch.isdigit())

    if not what:
        return {"ok": False, "msg": "Please say what is wrong."}
    if not who:
        return {"ok": False, "msg": "Please give your name."}
    if len(mobile) not in (10, 11):
        return {"ok": False, "msg": "That mobile number does not look right."}

    with _store_lock:
        d = store_load(cid)

        # A second person walking past the same broken machine is asked
        # first, before any rate limit. Telling him to wait five minutes
        # when what he needs is "there is already one, add a note" is the
        # wrong answer to the right situation.
        open_one = open_complaint_on(d, m.get("id"))
        if open_one and not req.get("addToIt"):
            return {"ok": False, "already": True,
                    "no": open_one.get("no"),
                    "on": str(open_one.get("opened", ""))[:10],
                    "msg": "This machine already has an open complaint."}

        # The limit is on new complaints only. Adding a note to one that is
        # already open is somebody being helpful, not somebody flooding us.
        if not open_one and not rate_ok(serial):
            return {"ok": False, "retry": True,
                    "msg": "A report was just sent for this machine. Please "
                           "wait a few minutes, or call us."}

        stamp = datetime.datetime.now().isoformat(timespec="seconds")

        if open_one and req.get("addToIt"):
            notes = open_one.get("clientNotes") or []
            notes.append({"what": what, "who": who, "mobile": mobile,
                          "at": stamp})
            open_one["clientNotes"] = notes
            open_one["_at"] = stamp
            d["rev"] += 1
            store_save(cid, d)
            note("%s note added to %s by %s" % (cid, open_one.get("no"), who))
            return {"ok": True, "added": True, "no": open_one.get("no")}

        seq = d.setdefault("seq", {})
        seq["scan"] = int(seq.get("scan", 0)) + 1
        now_d = datetime.datetime.now()
        no = "PBS-%02d-%04d-%02d-%02d%02d" % (
            now_d.month, 1400 + seq["scan"], now_d.day,
            now_d.month, now_d.year % 100)

        job_id = "s" + secrets.token_hex(6)
        d.setdefault("invoices", {})[job_id] = {
            "id": job_id, "no": no, "status": "pending",
            "client": m.get("client"), "machine": m.get("id"),
            "what": what,
            "pri": "Normal",                 # the client does not set this
            "tech": "",
            "source": "scan",
            "who": who, "phone": mobile,
            "reportedDept": str(req.get("dept") or "").strip(),
            "clientRef": str(req.get("ref") or "").strip(),
            "scannedAt": stamp,
            "opened": stamp, "openedBy": "",
            "shots": [req["shot"]] if req.get("shot") else [],
            "_at": stamp,
        }

        # the contact learns itself
        c = d.get("clients", {}).get(m.get("client"))
        if c is not None:
            people = c.setdefault("people", [])
            known = any("".join(ch for ch in str(p.get("phone", ""))
                                if ch.isdigit()) == mobile for p in people)
            if not known:
                people.append({"name": who, "role": "Reports faults",
                               "phone": mobile})
                c["_at"] = stamp

        d["rev"] += 1
        store_save(cid, d)

    note("%s scan complaint %s from %s" % (cid, no, who))
    return {"ok": True, "no": no}


def machine_beat(cid, req):
    """Agent har machine ka counter/toner regularly bhejta hai \u2014 bina
    kisi kharabi ke. Sirf machine record update hota hai."""
    serial = str(req.get("serial") or "")
    if not serial:
        return {"ok": False, "msg": "No serial."}
    m, _ = find_machine(cid, serial)
    if not m:
        return {"ok": False, "msg": "unknown-machine", "serial": serial}
    with _store_lock:
        d = store_load(cid)
        if m.get("id") in d.get("items", {}):
            mm = d["items"][m["id"]]
            if req.get("counter") is not None:
                try: mm["autoCounter"] = int(req["counter"])
                except (TypeError, ValueError): pass
            if req.get("toner") is not None:
                mm["autoToner"] = req["toner"]
            mm["autoAt"] = datetime.datetime.now().isoformat(timespec="seconds")
            d["rev"] += 1
            store_save(cid, d)
    return {"ok": True}


def machine_report(cid, req):
    """The Paragon Agent, watching the machines over SNMP, sends a fault it
    found: a serial, an error, and the counter and toner at the time. We turn
    it into a complaint under that machine \u2014 the same as a client\u2019s
    report, but marked as coming from the machine itself. One open complaint
    per machine still holds: a machine that is already being seen to does not
    raise a second."""
    serial = str(req.get("serial") or "")
    if not serial:
        return {"ok": False, "msg": "No serial."}
    m, _ = find_machine(cid, serial)
    if not m:
        return {"ok": False, "msg": "unknown-machine", "serial": serial}

    err = str(req.get("error") or "").strip() or "Machine reported a fault"
    code = str(req.get("code") or "").strip()          # SC code, if any
    counter = req.get("counter")
    toner = req.get("toner")

    with _store_lock:
        d = store_load(cid)

        # counter aur toner machine record par rakho (khud aate rehte hain)
        if m.get("id") in d.get("items", {}):
            mm = d["items"][m["id"]]
            if counter is not None:
                try: mm["autoCounter"] = int(counter)
                except (TypeError, ValueError): pass
            if toner is not None:
                mm["autoToner"] = toner
            mm["autoAt"] = datetime.datetime.now().isoformat(timespec="seconds")

        # pehle se khuli complaint? to dobara na banao (bas note)
        open_one = open_complaint_on(d, m.get("id"))
        if open_one:
            d["rev"] += 1
            store_save(cid, d)
            return {"ok": True, "already": True, "no": open_one.get("no")}

        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        seq = d.setdefault("seq", {})
        seq["machine"] = int(seq.get("machine", 0)) + 1
        now_d = datetime.datetime.now()
        no = "PBS-M%02d-%04d" % (now_d.month, 1600 + seq["machine"])

        # importance: repeat error ya serious -> High
        recent = [c for c in d.get("invoices", {}).values()
                  if c.get("machine") == m.get("id") and c.get("status") == "resolved"]
        pri = "High" if (code and len(recent) >= 1) else "Normal"

        job_id = "m" + secrets.token_hex(6)
        d.setdefault("invoices", {})[job_id] = {
            "id": job_id, "no": no, "status": "pending",
            "client": m.get("client"), "machine": m.get("id"),
            "what": err + ((" (" + code + ")") if code else ""),
            "pri": pri, "tech": "",
            "source": "machine",
            "scCode": code,
            "atCounter": counter,
            "opened": stamp, "openedBy": "", "shots": [],
            "_at": stamp,
        }
        d["rev"] += 1
        store_save(cid, d)

    note("%s machine complaint %s (%s)" % (cid, no, code or err))
    return {"ok": True, "no": no, "made": True}


SCAN_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#1C2E4A">
<title>Report a fault</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{--ink:#16202B;--ink-2:#55606D;--ink-3:#8794A3;--line:#D8DDE4;
 --soft:#F5F7F9;--blue:#1F5FCC;--green:#17794B;--red:#C0322C;--bg:#EEF1F5;
 --brand:#1C2E4A}
body{font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 background:var(--bg);color:var(--ink);font-size:16px;line-height:1.55;
 padding-bottom:env(safe-area-inset-bottom,0px)}
.hide{display:none!important}
.top{background:var(--brand);color:#fff;padding:calc(18px + env(safe-area-inset-top,0px)) 20px 18px}
.top .co{font-size:12px;letter-spacing:.14em;opacity:.8}
.top h1{font-size:22px;font-weight:600;margin-top:4px}
.wrap{max-width:520px;margin:0 auto;padding:18px 16px 40px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;
 padding:16px 18px;margin-bottom:14px}
.mch{font-size:17px;font-weight:600}
.mch span{display:block;font-size:14px;font-weight:400;color:var(--ink-2);
 margin-top:3px}
.wrong{font-size:13px;color:var(--ink-3);margin-top:10px;line-height:1.5}
label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}
label .opt{font-weight:400;color:var(--ink-3)}
input,textarea{width:100%;padding:13px 14px;font-size:16px;font-family:inherit;
 border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink)}
input:focus,textarea:focus{outline:0;border-color:var(--blue);
 box-shadow:0 0 0 3px #E7EFFC}
textarea{min-height:96px;resize:vertical}
.field{margin-bottom:16px}
.btn{display:block;width:100%;font-family:inherit;font-size:16px;font-weight:600;
 padding:15px;border:0;border-radius:8px;background:var(--brand);color:#fff;
 cursor:pointer}
.btn:active{opacity:.88}
.btn.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
.shot{width:100%;border-radius:8px;margin-bottom:10px}
.addshot{border:1px dashed var(--ink-3);background:var(--soft);border-radius:8px;
 padding:22px;text-align:center;color:var(--ink-2);font-size:14px;cursor:pointer}
.addshot b{display:block;font-size:26px;margin-bottom:4px;color:var(--ink-3)}
.note{padding:13px 15px;border-radius:8px;font-size:14px;line-height:1.55;
 border-left:3px solid;margin-bottom:14px}
.note.i{background:var(--soft);border-color:var(--blue)}
.note.w{background:#FCF6E9;border-color:#94620F;color:#94620F}
.note.d{background:#FBEEED;border-color:var(--red);color:var(--red)}
.note.g{background:#E8F3EC;border-color:var(--green);color:var(--green)}
.done{text-align:center;padding:30px 20px}
.done .tick{width:60px;height:60px;border-radius:50%;background:var(--green);
 color:#fff;font-size:30px;display:flex;align-items:center;justify-content:center;
 margin:0 auto 18px}
.ref{font-size:20px;font-weight:600;letter-spacing:.02em;margin:14px 0 6px;
 font-family:Consolas,Menlo,monospace}
.help{font-size:13px;color:var(--ink-3);margin-top:7px;line-height:1.5}
a{color:var(--blue)}
</style></head><body>

<div class="top">
  <div class="co" id="coName">PARAGON COPIER SOLUTION</div>
  <h1 id="head">Report a fault</h1>
</div>

<div class="wrap">
  <div id="body"><div class="card">Loading&hellip;</div></div>
</div>

<script>
var SERIAL = location.pathname.split('/c/')[1] || '';
var INFO = null, SHOT = '';
var BRANDS = { paragon:{name:'PARAGON COPIER SOLUTION',c:'#1C2E4A'},
               house:{name:'COPIER HOUSE',c:'#C2202E'},
               star:{name:'COPIER STAR',c:'#B8860B'} };

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

function ask(body){
  return fetch('/scan', { method:'POST',
    headers:{'Content-Type':'text/plain'},
    body: JSON.stringify(Object.assign({ serial: SERIAL }, body)) })
    .then(function(r){ return r.json(); });
}

function start(){
  ask({ action:'look' }).then(function(d){
    if(!d.ok){
      $('body').innerHTML = '<div class="note d">'+esc(d.msg)+'</div>'+
        '<div class="card"><p>Please call us on <b>021-34536010</b> and we '+
        'will take the details.</p></div>';
      return;
    }
    INFO = d;
    var b = BRANDS[d.brand] || BRANDS.paragon;
    document.documentElement.style.setProperty('--brand', b.c);
    $('coName').textContent = b.name;
    draw();
  }).catch(function(){
    $('body').innerHTML = '<div class="note d">No connection. Please try '+
      'again, or call <b>021-34536010</b>.</div>';
  });
}

function draw(){
  var m = INFO.machine;
  $('body').innerHTML =
    '<div class="card">'+
      '<div class="mch">'+esc(m.model)+' &middot; '+esc(m.serial)+
        '<span>'+esc(INFO.client)+(m.where?' &middot; '+esc(m.where):'')+
        '</span></div>'+
      '<div class="wrong">Not this machine? Check the label on the machine you '+
      'are reporting.</div>'+
    '</div>'+

    (INFO.already
      ? '<div class="note w"><b>This machine already has an open '+
        'complaint.</b><br>Reference '+esc(INFO.already.no)+', reported on '+
        esc(dmy(INFO.already.on))+'.</div>'+
        '<div class="card"><p style="margin-bottom:14px">Would you like to add '+
        'to it, or report something different?</p>'+
        '<button class="btn" onclick="form(true)">Add a note</button>'+
        '<button class="btn ghost" style="margin-top:9px" '+
        'onclick="form(false)">Report something different</button></div>'
      : formHTML(false));
}

function dmy(iso){
  var p = String(iso||'').slice(0,10).split('-');
  return p.length===3 ? p[2]+'-'+p[1]+'-'+p[0] : iso;
}

function form(addToIt){
  $('body').innerHTML = formHTML(addToIt);
}

function formHTML(addToIt){
  return '<div class="card">'+
    (addToIt ? '<div class="note i">Adding to '+esc(INFO.already.no)+'.</div>' : '')+
    '<input type="hidden" id="addToIt" value="'+(addToIt?'1':'')+'">'+

    '<div class="field"><label>Photograph of the error '+
      '<span class="opt">&mdash; helps a lot</span></label>'+
      '<div id="shotBox"><div class="addshot" onclick="takeShot()">'+
      '<b>+</b>Tap to take a photo<br><span style="font-size:12.5px">'+
      'The error code on the screen tells us what to bring</span></div></div>'+
    '</div>'+

    '<div class="field"><label>What is wrong?</label>'+
      '<textarea id="what" placeholder="Paper jam, error on screen, poor print '+
      'quality&hellip;"></textarea></div>'+

    '<div class="field"><label>Your name</label>'+
      '<input id="who" autocomplete="name"></div>'+

    '<div class="field"><label>Your mobile</label>'+
      '<input id="mobile" type="tel" inputmode="numeric" autocomplete="tel" '+
      'placeholder="0300 1234567">'+
      '<div class="help">The technician will ring this when he arrives.</div></div>'+

    '<div class="field"><label>Department or floor '+
      '<span class="opt">&mdash; optional</span></label>'+
      '<input id="dept" value="'+esc(INFO.machine.where||'')+'"></div>'+

    '<div class="field"><label>Contract number '+
      '<span class="opt">&mdash; optional</span></label>'+
      '<input id="ref"></div>'+

    '<div id="err"></div>'+
    '<button class="btn" id="go" onclick="send()">Send it</button>'+
  '</div>';
}

function takeShot(){
  var inp = document.createElement('input');
  inp.type='file'; inp.accept='image/*'; inp.capture='environment';
  inp.onchange = function(){
    var f = inp.files && inp.files[0];
    if(!f) return;
    var r = new FileReader();
    r.onload = function(){
      var img = new Image();
      img.onload = function(){
        var max=1100, w=img.width, h=img.height;
        if(w>max||h>max){ if(w>h){h=Math.round(h*max/w);w=max;}
                          else {w=Math.round(w*max/h);h=max;} }
        var cv=document.createElement('canvas'); cv.width=w; cv.height=h;
        cv.getContext('2d').drawImage(img,0,0,w,h);
        SHOT = cv.toDataURL('image/jpeg',0.7);
        $('shotBox').innerHTML = '<img class="shot" src="'+SHOT+'">'+
          '<button class="btn ghost" onclick="takeShot()">Take another</button>';
      };
      img.src = r.result;
    };
    r.readAsDataURL(f);
  };
  inp.click();
}

function send(){
  var body = {
    action:'report',
    what: $('what').value.trim(),
    who: $('who').value.trim(),
    mobile: $('mobile').value.trim(),
    dept: $('dept').value.trim(),
    ref: $('ref').value.trim(),
    addToIt: !!$('addToIt').value,
    shot: SHOT
  };
  if(!body.what) return oops('Please say what is wrong.');
  if(!body.who) return oops('Please give your name.');
  var digits = body.mobile.replace(/[^0-9]/g,'');
  if(digits.length !== 10 && digits.length !== 11)
    return oops('That mobile number does not look right.');

  $('go').textContent = 'Sending\u2026';
  $('go').disabled = true;

  ask(body).then(function(d){
    if(d.already){
      INFO.already = { no:d.no, on:d.on };
      draw();
      return;
    }
    if(!d.ok){
      $('go').textContent='Send it'; $('go').disabled=false;
      return oops(d.msg || 'It could not be sent.');
    }
    thanks(d.no, d.added);
  }).catch(function(){
    /* Never show success for something that has not been sent. */
    $('go').textContent='Send it'; $('go').disabled=false;
    hold(body);
  });
}

function oops(msg){
  $('err').innerHTML = '<div class="note d">'+esc(msg)+'</div>';
  $('err').scrollIntoView({behavior:'smooth',block:'center'});
}

/* a basement has no signal; hold it and keep trying */
var HELD = null, tries = 0;
function hold(body){
  HELD = body;
  $('err').innerHTML = '<div class="note w"><b>No connection.</b> Your report '+
    'has not been sent yet. It will go automatically as soon as you are back '+
    'online \u2014 please keep this page open.</div>';
  retry();
}

function retry(){
  if(!HELD) return;
  tries++;
  setTimeout(function(){
    if(!HELD) return;
    ask(HELD).then(function(d){
      if(d && d.ok){ HELD = null; thanks(d.no, d.added); }
      else if(d && d.already){ HELD = null; INFO.already={no:d.no,on:d.on}; draw(); }
      else retry();
    }).catch(retry);
  }, Math.min(5000 * tries, 30000));
}

function thanks(no, added){
  $('body').innerHTML = '<div class="card done">'+
    '<div class="tick">\u2713</div>'+
    '<div style="font-size:18px;font-weight:600">Thank you \u2014 your '+
    (added?'note has been added':'complaint has been logged')+'.</div>'+
    (no ? '<div class="ref">'+esc(no)+'</div>'+
      '<div style="font-size:13px;color:var(--ink-3)">Please keep this '+
      'reference</div>' : '')+
    '<p style="margin-top:18px">We will contact you shortly.</p>'+
    '<p style="margin-top:6px">For anything urgent, call '+
    '<b><a href="tel:02134536010">021-34536010</a></b>.</p>'+
  '</div>';
  window.scrollTo(0,0);
}

start();
</script>
</body></html>
"""


# ================================================================
#  A NEW PHONE JOINING
#  ----------------------------------------------------------------
#  A phone joins with an admin's user name and password, and is
#  handed the shared secret only once those are right. So nobody
#  but an admin can connect a phone, and the secret itself never
#  has to be read out, typed in, or known by anyone else.
# ================================================================

import hashlib as _jh

_join_fail = {}
_join_lock = threading.Lock()


def _join_allowed(ip):
    """Five wrong tries in fifteen minutes and that address waits.
    Enough for a mistyped password; far too few to guess one."""
    now_s = time.time()
    with _join_lock:
        tries = [t for t in _join_fail.get(ip, []) if now_s - t < 900]
        _join_fail[ip] = tries
        return len(tries) < 5


def _join_failed(ip):
    with _join_lock:
        _join_fail.setdefault(ip, []).append(time.time())


def store_join(cid, req, ip):
    if not _join_allowed(ip):
        return {"ok": False, "msg": "Too many wrong tries. Wait fifteen minutes "
                                    "and try again."}
    name = str(req.get("user") or "").strip().lower()
    plain = str(req.get("pass") or "")
    d = store_load(cid)
    admins = [u for u in d.get("users", {}).values()
              if isinstance(u, dict) and u.get("role") == "admin"
              and u.get("active", True) and u.get("passHash")]
    if not admins:
        return {"ok": False, "msg": "No admin account has reached this server "
                "yet. On the admin's phone, choose your own password (not "
                "admin123) and let it sync, then try again."}
    for u in admins:
        if str(u.get("email", "")).strip().lower() != name:
            continue
        # the same hash the app keeps: nothing here ever sees a stored password
        h = _jh.sha256(("paragon:%s:%s" % (u.get("id"), plain)).encode("utf-8")).hexdigest()
        if hmac.compare_digest(h, str(u.get("passHash"))):
            note("%s phone joined by admin %s from %s" % (cid, u.get("name") or name, ip))
            return {"ok": True, "key": SHARED_SECRET, "admin": u.get("name") or name}
        break
    _join_failed(ip)
    return {"ok": False, "msg": "That is not an admin's user name and password."}


# ================================================================
#  RIGHTS
#  ----------------------------------------------------------------
#  Who may do what. Each role starts with the defaults below; the
#  admin can change them for a role, or give or take a right from one
#  person. The phones follow the same list, and so does this server,
#  so a right taken away stops working at once, whatever a phone shows.
#  An admin always has every right \u2014 nobody can lock the firm out.
# ================================================================

PERM_DEFAULTS = {
    "users": ["admin"], "settings": ["admin"],
    "stock.edit": ["admin", "store"], "stock.see": ["admin", "supervisor", "store"],
    "complaint.add": ["admin", "supervisor", "store"], "complaint.assign": ["admin", "supervisor", "store"],
    "complaint.all": ["admin", "supervisor", "store"],
    "part.approve": ["admin", "supervisor", "store"], "part.issue": ["admin", "supervisor", "store"],
    "reports": ["admin", "supervisor", "store"], "jobs": ["admin", "supervisor", "store", "tech"],
    "fleet": ["admin", "supervisor", "store"], "quotes": ["admin"],
    "tasks": ["admin", "supervisor"], "attendance.all": ["admin", "supervisor"], "salary": ["admin"],
    "money": ["admin"], "collect": ["admin", "supervisor", "tech"], "delivery": ["admin", "store"],
    "manuals": ["admin"],
}


def allowed(cid, u, what):
    if not u:
        return False
    if u.get("role") == "admin":
        return True
    recs = store_load(cid).get("rights", {})
    r = recs.get("role:" + str(u.get("role") or ""))
    base = (what in (r.get("perms") or [])) if isinstance(r, dict) else (u.get("role") in PERM_DEFAULTS.get(what, []))
    ur = recs.get("user:" + str(u.get("id") or ""))
    if isinstance(ur, dict):
        if what in (ur.get("allow") or []):
            return True
        if what in (ur.get("deny") or []):
            return False
    return base


# ================================================================
#  ATTENDANCE
#  ----------------------------------------------------------------
#  The owner's rules, confirmed:
#    duty 09:00 to 18:00
#    09:30 is on time, 09:31 is late, compared to the minute
#    after 18:00 is overtime
#    Sunday is a paid weekly off
#    holidays are paid, and chosen by the admin by hand
#    a technician sent straight to a client must be there by 09:30 too
#    a missing check-out is flagged for the admin, never guessed
# ================================================================

import datetime as _dt

PK = _dt.timezone(_dt.timedelta(hours=5))      # Pakistan keeps no summer time

HR_POLICY_DEFAULT = {"duty_start": "09:00", "duty_end": "18:00",
                     "late_after": "09:30", "weekly_off": 6}  # Monday=0 .. Sunday=6

_hr_lock = threading.Lock()


def pk_now():
    return _dt.datetime.now(PK)


def _hr_path(cid):
    return os.path.join(DATA_DIR, str(cid or "main") + "-hr.json")


def hr_load(cid):
    try:
        with open(_hr_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        d = {}
    for k in ("attendance", "holidays", "sessions"):
        d.setdefault(k, {})
    d.setdefault("policy", dict(HR_POLICY_DEFAULT))
    return d


def hr_save(cid, d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _hr_path(cid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _hr_path(cid))


# ---------- who is asking ----------
# A session is given for a person's own name and password, checked against
# the same hash the app keeps. After that the phone carries only the session.

def _user_by_name(cid, name):
    name = str(name or "").strip().lower()
    for u in store_load(cid).get("users", {}).values():
        if isinstance(u, dict) and str(u.get("email", "")).strip().lower() == name:
            return u
    return None


def _user_by_id(cid, uid):
    return store_load(cid).get("users", {}).get(uid)


def hr_login(cid, req, ip):
    if not _join_allowed(ip):
        return {"ok": False, "msg": "Too many wrong tries. Wait fifteen minutes."}
    u = _user_by_name(cid, req.get("user"))
    plain = str(req.get("pass") or "")
    if u and u.get("active", True) and u.get("passHash"):
        h = _jh.sha256(("paragon:%s:%s" % (u.get("id"), plain)).encode("utf-8")).hexdigest()
        if hmac.compare_digest(h, str(u.get("passHash"))):
            tok = secrets.token_hex(24)
            with _hr_lock:
                d = hr_load(cid)
                cut = time.time()
                d["sessions"] = {t: s for t, s in d["sessions"].items()
                                 if s.get("exp", 0) > cut}
                d["sessions"][tok] = {"user": u["id"], "exp": cut + 30 * 86400}
                hr_save(cid, d)
            return {"ok": True, "token": tok, "user": u["id"], "role": u.get("role")}
    _join_failed(ip)
    return {"ok": False, "msg": "That user name or password is not right."}


def _session(cid, req):
    """The person behind a request, looked up fresh every time, so a role
    changed or an account switched off takes effect at once."""
    tok = str(req.get("token") or "")
    d = hr_load(cid)
    s = d["sessions"].get(tok)
    if not s or s.get("exp", 0) < time.time():
        return None
    u = _user_by_id(cid, s.get("user"))
    if not u or not u.get("active", True):
        return None
    return u


# ---------- a day ----------

def _hhmm(iso):
    return str(iso or "")[11:16]


def _classify(rec, policy):
    """Late and overtime are worked out here, from the times, and never
    typed. A typed late mark cannot be checked against what it claims."""
    out = dict(rec)
    tin = _hhmm(rec.get("in"))
    out["late"] = bool(tin) and tin > policy["late_after"]
    ot = 0
    tout = _hhmm(rec.get("out"))
    if tout and tout > policy["duty_end"]:
        a = [int(x) for x in policy["duty_end"].split(":")]
        b = [int(x) for x in tout.split(":")]
        ot = (b[0] * 60 + b[1]) - (a[0] * 60 + a[1])
    out["ot_min"] = ot                     # raw minutes; the salary rounds them
    out["no_out"] = bool(rec.get("in")) and not rec.get("out") and \
        rec.get("day", "") < pk_now().strftime("%Y-%m-%d")
    return out


def _stamp(req_time):
    """The server's own clock decides the time of a check-in made live. A
    phone whose clock is wrong, or has been set wrong, cannot move it. A
    check-in that was queued without signal keeps the phone's time, and is
    marked so the admin can see it was not live."""
    now = pk_now()
    try:
        t = _dt.datetime.fromisoformat(str(req_time))
        if t.tzinfo is None:
            t = t.replace(tzinfo=PK)
    except (ValueError, TypeError):
        t = None
    if t is None or abs((now - t).total_seconds()) <= 300:
        return now.isoformat(timespec="seconds"), False
    return t.astimezone(PK).isoformat(timespec="seconds"), True


def _admins(cid):
    return [u for u in store_load(cid).get("users", {}).values()
            if isinstance(u, dict) and u.get("role") == "admin" and u.get("active", True)]


def _tell_admins(cid, what, detail):
    """A note to every admin, through the same notes the app already rings
    the phone for."""
    stamp = pk_now().isoformat(timespec="seconds")
    recs = {}
    for a in _admins(cid):
        nid = "hr" + secrets.token_hex(6)
        recs[nid] = {"id": nid, "to": a["id"], "what": what, "detail": detail,
                     "link": "", "at": stamp, "read": False, "_at": stamp}
    if recs:
        store_push(cid, {"records": {"notes": recs}})


def att_in(cid, req, u):
    when, queued = _stamp(req.get("time"))
    day = when[:10]
    rid = u["id"] + "|" + day
    with _hr_lock:
        d = hr_load(cid)
        have = d["attendance"].get(rid)
        if have and have.get("in"):
            return {"ok": True, "already": True, "rec": _classify(have, d["policy"])}
        rec = have or {"id": rid, "user": u["id"], "day": day}
        rec.update({"in": when, "inPlace": str(req.get("place") or ""),
                    "inLoc": str(req.get("loc") or ""),
                    "inPhoto": str(req.get("photo") or "")[:400000],
                    "inQueued": queued, "status": "present"})
        d["attendance"][rid] = rec
        hr_save(cid, d)
        c = _classify(rec, d["policy"])
    if c["late"]:
        _tell_admins(cid, "Late check-in", "%s \u00b7 %s" % (u.get("name"), _hhmm(when)))
    return {"ok": True, "rec": c}


def att_out(cid, req, u):
    when, queued = _stamp(req.get("time"))
    day = when[:10]
    rid = u["id"] + "|" + day
    with _hr_lock:
        d = hr_load(cid)
        rec = d["attendance"].get(rid)
        if not rec or not rec.get("in"):
            return {"ok": False, "msg": "There is no check-in today to check out from."}
        rec.update({"out": when, "outPlace": str(req.get("place") or ""),
                    "outLoc": str(req.get("loc") or ""),
                    "outPhoto": str(req.get("photo") or "")[:400000],
                    "outQueued": queued})
        hr_save(cid, d)
        return {"ok": True, "rec": _classify(rec, d["policy"])}


def _strip(rec, admin):
    """A person sees their own times; only an admin sees where they were."""
    if admin:
        return rec
    return {k: v for k, v in rec.items() if k not in ("inPlace", "outPlace")}


def att_month(cid, req, u):
    """Every day of a month, for one person or, for an admin, everyone.
    A working day with nothing recorded is absent \u2014 decided here, not
    left as a gap."""
    admin = allowed(cid, u, "attendance.all")
    month = str(req.get("month") or pk_now().strftime("%Y-%m"))[:7]
    who = req.get("user") if admin and req.get("user") else (None if admin else u["id"])
    d = hr_load(cid)
    pol = d["policy"]
    hol = {h["day"]: h.get("name", "") for h in d["holidays"].values()
           if str(h.get("day", "")).startswith(month)}
    y, m = int(month[:4]), int(month[5:7])
    first = _dt.date(y, m, 1)
    nxt = _dt.date(y + (m == 12), m % 12 + 1, 1)
    today = pk_now().date()
    users = [x for x in store_load(cid).get("users", {}).values()
             if isinstance(x, dict) and x.get("active", True) and x.get("role") != "operator"
             and (who is None or x.get("id") == who)]
    people = []
    for p in users:
        rows, sums = [], {"present": 0, "half": 0, "late": 0, "absent": 0, "leave": 0,
                          "holiday": 0, "weekly_off": 0, "ot_min": 0, "no_out": 0}
        day = first
        while day < nxt and day <= today:
            ds = day.isoformat()
            rec = d["attendance"].get(p["id"] + "|" + ds)
            if rec and rec.get("status") == "leave":
                row = dict(rec); row["status"] = "leave"
            elif rec and rec.get("in"):
                row = _classify(rec, pol)
                row["status"] = "half" if rec.get("status") == "half" else "present"
            elif ds in hol:
                row = {"day": ds, "status": "holiday", "name": hol[ds]}
            elif day.weekday() == pol.get("weekly_off", 6):
                row = {"day": ds, "status": "weekly_off"}
            else:
                # an absence keeps whatever the admin decided about it
                row = {k: v for k, v in (rec or {}).items() if k not in ("in", "out")}
                row.update({"day": ds, "status": "absent"})
            sums[row["status"]] = sums.get(row["status"], 0) + 1
            if row.get("late"):
                sums["late"] += 1
            sums["ot_min"] += row.get("ot_min", 0) or 0
            if row.get("no_out"):
                sums["no_out"] += 1
            rows.append(_strip(row, admin))
            day += _dt.timedelta(days=1)
        people.append({"user": p["id"], "name": p.get("name"), "sums": sums, "rows": rows})
    return {"ok": True, "month": month, "people": people,
            "holidays": sorted(hol.items()), "policy": pol}


def att_fix(cid, req, u):
    """An admin correcting a day. The reason is required, and what it said
    before is kept, because a payroll dispute always comes down to "what did
    it say before?"."""
    if not allowed(cid, u, "attendance.all"):
        return {"ok": False, "msg": "Only an admin corrects attendance."}
    note = str(req.get("note") or "").strip()
    if not note:
        return {"ok": False, "msg": "A reason is required for every correction."}
    who, day = str(req.get("user") or ""), str(req.get("day") or "")[:10]
    if not who or len(day) != 10:
        return {"ok": False, "msg": "Choose the person and the day."}
    rid = who + "|" + day
    stamp = pk_now().isoformat(timespec="seconds")
    with _hr_lock:
        d = hr_load(cid)
        rec = d["attendance"].get(rid) or {"id": rid, "user": who, "day": day}
        before = {k: rec.get(k) for k in ("in", "out", "status")}
        status = req.get("status") or "present"
        if status == "leave":
            rec["status"] = "leave"
            rec.pop("in", None); rec.pop("out", None)
        else:
            rec["status"] = "present"
            for k in ("in", "out"):
                v = str(req.get(k) or "").strip()
                if v:
                    rec[k] = day + "T" + v[:5] + ":00+05:00"
                elif k in req:
                    rec.pop(k, None)
        rec.setdefault("history", []).append(
            {"by": u.get("name"), "at": stamp, "note": note, "before": before})
        rec["correctedBy"] = u.get("name")
        rec["correctedAt"] = stamp
        d["attendance"][rid] = rec
        hr_save(cid, d)
        return {"ok": True, "rec": _classify(rec, d["policy"])}


def hol_set(cid, req, u):
    if not allowed(cid, u, "attendance.all"):
        return {"ok": False, "msg": "Only an admin sets holidays."}
    day = str(req.get("day") or "")[:10]
    name = str(req.get("name") or "").strip()
    if len(day) != 10 or not name:
        return {"ok": False, "msg": "A holiday needs a date and a name."}
    with _hr_lock:
        d = hr_load(cid)
        if req.get("remove"):
            d["holidays"].pop(day, None)
        else:
            d["holidays"][day] = {"day": day, "name": name, "paid": True,
                                  "by": u.get("name")}
        hr_save(cid, d)
    return {"ok": True}


def hr_route(cid, req, ip):
    act = req.get("action")
    if act == "login":
        return hr_login(cid, req, ip)
    u = _session(cid, req)
    if not u:
        return {"ok": False, "signin": True, "msg": "Sign in again to use attendance."}
    if act != "presence.bye":
        presence_touch(u["id"])
    if act == "in":      return att_in(cid, req, u)
    if act == "out":     return att_out(cid, req, u)
    if act == "month":   return att_month(cid, req, u)
    if act == "fix":     return att_fix(cid, req, u)
    if act == "holiday": return hol_set(cid, req, u)
    if act == "whoami":  return {"ok": True, "user": u["id"], "role": u.get("role")}
    if act == "rights.set":
        if u.get("role") != "admin":
            return {"ok": False, "msg": "Only an admin sets rights."}
        rec = req.get("rec") or {}
        rid = str(rec.get("id") or "")
        if not (rid.startswith("role:") or rid.startswith("user:")) or rid == "role:admin":
            return {"ok": False, "msg": "That is not a role or a person."}
        clean = {"id": rid, "perms": [x for x in (rec.get("perms") or []) if x in PERM_DEFAULTS],
                 "allow": [x for x in (rec.get("allow") or []) if x in PERM_DEFAULTS],
                 "deny": [x for x in (rec.get("deny") or []) if x in PERM_DEFAULTS],
                 "by": u.get("name"), "_at": pk_now().isoformat(timespec="seconds")}
        if rid.startswith("user:"):
            target = _user_by_id(cid, rid[5:])
            if not target or target.get("role") == "admin":
                return {"ok": False, "msg": "An admin already has every right."}
            clean.pop("perms")
        else:
            clean.pop("allow"); clean.pop("deny")
        store_push(cid, {"records": {"rights": {rid: clean}}, "_trusted": True})
        return {"ok": True}
    if act in ("slips.mine", "overview", "person", "rate", "advance", "bonus",
               "approve", "slips.make", "slips.month", "slips.final", "company",
               "deduct", "deduct.void", "advance.month", "bank.letter"):
        return pay_route(cid, req, u)
    if act in ("pay.collect", "dn.invoices", "dn.make", "photo", "settings", "inv.make",
               "inv.cancel", "inv.list", "rimport.check", "rimport.commit", "namemap.set",
               "pays.list", "pay.confirm", "pay.cert", "pay.bounce", "allocate", "adj.make",
               "recv", "ledger"):
        return money_route(cid, req, u)
    if str(act or "").startswith(("ai.", "man.")):
        return ai_route(cid, req, u)
    if str(act or "").startswith("me."):
        return me_route(cid, req, u)
    if act == "voice.put":
        return voice_put(cid, req, u)
    if str(act or "").startswith(("chat.", "presence.")):
        return chat_route(cid, req, u)
    if str(act or "").startswith("loc."):
        return loc_route(cid, req, u)
    if act == "backup.list":
        if not allowed(cid, u, "settings"): return {"ok": False, "msg": "Admin only"}
        return backup_list(cid)
    if act == "backup.get":
        if not allowed(cid, u, "settings"): return {"ok": False, "msg": "Admin only"}
        return backup_get(cid, req)
    if act == "admin.settings":
        return admin_route(cid, req, u)
    return {"ok": False, "msg": "Unknown action"}


def hr_watch(cid="main"):
    """Once a day, late in the evening: anyone who checked in and has not
    checked out is flagged to the admin, so it is fixed while it is fresh."""
    done = ""
    while True:
        try:
            now = pk_now()
            day = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") >= "22:30" and done != day:
                done = day
                d = hr_load(cid)
                open_ = [r for r in d["attendance"].values()
                         if r.get("day") == day and r.get("in") and not r.get("out")]
                if open_:
                    names = ", ".join((_user_by_id(cid, r["user"]) or {}).get("name", "?")
                                      for r in open_)
                    _tell_admins(cid, "No check-out today", names)
        except Exception as e:
            note("HR WATCH", repr(e))
        time.sleep(300)


# ================================================================
#  SALARY
#  ----------------------------------------------------------------
#  The owner's decisions, every one of them, so nothing is guessed:
#    per day = basic / 30, per hour = per day / 8          (always)
#    3 late marks = 1 day, divided exactly (7 lates = 2.33 days)
#    overtime counts in whole hours only, and only once the admin
#      approves that day (18:47 is nothing; 19:47 is one hour)
#    a half day costs half a day unless the admin makes it paid
#    an absence without leave costs one day, like a leave
#    leaving early costs every minute, unless the admin approves it
#    only public holidays are paid leave; the admin may forgive any
#      deduction before the salary, and each one is printed on the slip
#    a net below zero is shown as it is: the person owes the rest
#    joining or leaving mid-month: basic / 30 x the days employed
#    a final month recovers every advance in full
#  And the rules of the specification:
#    a rate is never edited; a raise is a new rate from a date
#    a slip is a draft until finalised, and then frozen
#    an advance's balance is always counted from its recoveries
#    a recovery is written only when a slip is finalised
# ================================================================


def _money(x):
    return float("%.4f" % x)


def _days_between(a, b):
    return (_dt.date.fromisoformat(b) - _dt.date.fromisoformat(a)).days + 1


def _rate_for(d, uid, month):
    """The rate in force for that month: the latest one agreed on or before
    the month's last day. Never the current one for an old month."""
    y, m = int(month[:4]), int(month[5:7])
    last = (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).isoformat()
    rates = [r for r in d.get("rates", {}).values()
             if r.get("user") == uid and str(r.get("from", "")) <= last]
    rates.sort(key=lambda r: (r.get("from", ""), r.get("at", "")))
    return rates[-1] if rates else None


def _balance(d, adv):
    got = sum(r["amount"] for r in d.get("recoveries", {}).values()
              if r.get("advance") == adv["id"])
    return round(adv["amount"] - got, 2)


def _instalment_due(d, adv, month, final):
    bal = _balance(d, adv)
    if bal <= 0 or adv.get("status") == "written_off":
        return 0
    if str(adv.get("first", "")) > month:
        return 0
    if final:
        return bal                       # the last month takes all of it
    o = _override_due(d, adv, month)
    if o is not None:
        return o
    n = max(1, int(adv.get("instalments") or 1))
    base = int(adv["amount"] // n)
    done = sum(1 for r in d.get("recoveries", {}).values() if r.get("advance") == adv["id"])
    this = adv["amount"] - base * (n - 1) if done + 1 >= n else base
    return min(this, bal)


def iban_ok(iban):
    """A Pakistani IBAN: PK, two check digits, four letters for the bank,
    sixteen digits for the account \u2014 and the check digits must add up,
    so one mistyped figure is caught before a salary goes to a stranger."""
    s = _re.sub(r"\s", "", str(iban or "")).upper()
    if not _re.fullmatch(r"PK\d{2}[A-Z]{4}\d{16}", s):
        return False
    moved = s[4:] + s[:4]
    num = "".join(str(int(ch, 36)) for ch in moved)
    return int(num) % 97 == 1


def _override_due(d, adv, month):
    """What the admin decided to take this month, if anything \u2014 all of it
    in one salary, a smaller part, or nothing this time."""
    o = (adv.get("overrides") or {}).get(month)
    return None if o is None else max(0.0, min(float(o["amount"]), _balance(d, adv)))


def pay_calculate(cid, uid, month):
    """One person's month, every line from the attendance rows themselves."""
    d = hr_load(cid)
    pol = d["policy"]
    person = d.get("people", {}).get(uid, {})
    user = _user_by_id(cid, uid) or {}
    rate = _rate_for(d, uid, month)
    if not rate:
        return {"ok": False, "msg": "%s has no salary rate for this month yet."
                % (user.get("name") or uid)}
    basic = float(rate["basic"])
    per_day = basic / 30.0
    per_hour = per_day / 8.0
    per_min = per_hour / 60.0

    y, m = int(month[:4]), int(month[5:7])
    first = _dt.date(y, m, 1).isoformat()
    last = (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).isoformat()
    start = max(first, str(person.get("joined") or first))
    end = min(last, str(person.get("left") or last))
    if start > end:
        return {"ok": False, "msg": "%s was not employed in this month."
                % (user.get("name") or uid)}
    full_month = start == first and end == last
    days_employed = _days_between(start, end)
    basic_due = basic if full_month else per_day * days_employed
    final = bool(person.get("left")) and str(person["left"])[:7] == month

    operator = user.get("role") == "operator"
    # A partner draws his salary whole: nothing is deducted from it at all.
    partner = person.get("payType") == "partner"
    if operator or partner:
        # An operator works at a client's office: attendance does not touch his
        # pay. Only advances and deductions the admin records come off it.
        rows = []
    else:
        month_view = att_month(cid, {"month": month, "user": uid}, {"role": "admin", "id": uid})
        rows = [r for p in month_view["people"] if p["user"] == uid for r in p["rows"]]
        rows = [r for r in rows if start <= r["day"] <= end]

    de = [int(x) for x in pol["duty_end"].split(":")]
    end_min = de[0] * 60 + de[1]
    lates = absents = leaves = present = holidays = offs = 0
    halves_unpaid = 0.0
    ot_hours = 0
    early_min = 0
    concessions, no_out, table = [], [], []
    for r in rows:
        waive = set(r.get("waive") or [])
        note = r.get("waiveNote") or ""
        st = r["status"]
        line = {"day": r["day"], "status": st, "in": _hhmm(r.get("in")),
                "out": _hhmm(r.get("out")), "late": False, "ot_h": 0, "early": 0}
        if st in ("present", "half"):
            present += 1 if st == "present" else 0
            if r.get("late"):
                if "late" in waive:
                    concessions.append({"day": r["day"], "what": "Late mark forgiven", "note": note})
                else:
                    lates += 1; line["late"] = True
            if r.get("no_out"):
                no_out.append(r["day"])
            if r.get("ot_min", 0) >= 60 and r.get("okOT"):
                h = r["ot_min"] // 60                   # whole hours only
                ot_hours += h; line["ot_h"] = h
            out = _hhmm(r.get("out"))
            if out and st == "present":
                om = int(out[:2]) * 60 + int(out[3:])
                if om < end_min:
                    mins = end_min - om
                    if r.get("okEarly"):
                        concessions.append({"day": r["day"], "what":
                            "Left %d min early \u2014 approved" % mins, "note": note})
                    else:
                        early_min += mins; line["early"] = mins
            if st == "half":
                if r.get("halfPaid"):
                    concessions.append({"day": r["day"], "what": "Half day \u2014 paid", "note": note})
                else:
                    halves_unpaid += 0.5
        elif st == "leave":
            if "leave" in waive:
                concessions.append({"day": r["day"], "what": "Leave forgiven", "note": note})
            else:
                leaves += 1
        elif st == "absent":
            if "absent" in waive:
                concessions.append({"day": r["day"], "what": "Absence forgiven", "note": note})
            else:
                absents += 1
        elif st == "holiday":
            holidays += 1
        elif st == "weekly_off":
            offs += 1
        table.append(line)

    late_days = lates / 3.0                                     # exact, as decided
    ot_amount = ot_hours * per_hour
    leave_ded = leaves * per_day
    absent_ded = absents * per_day
    late_ded = late_days * per_day
    half_ded = halves_unpaid * per_day
    early_ded = early_min * per_min

    advances = []
    adv_total = 0.0
    for a in d.get("advances", {}).values():
        if a.get("user") != uid or partner:
            continue
        due = _instalment_due(d, a, month, final)
        bal = _balance(d, a)
        if due or bal > 0:
            advances.append({"id": a["id"], "purpose": a.get("purpose", ""),
                             "amount": a["amount"], "recovered": round(a["amount"] - bal, 2),
                             "balance": bal, "this_month": due})
        adv_total += due
    bonus = sum(float(b["amount"]) for b in d.get("bonuses", {}).values()
                if b.get("user") == uid and b.get("month") == month)
    # deductions the admin records by hand \u2014 a client's request, anything \u2014 each with its reason
    other = [{"amount": float(x["amount"]), "reason": x.get("reason", ""), "by": x.get("by", "")}
             for x in d.get("deductions", {}).values()
             if x.get("user") == uid and x.get("month") == month and not x.get("void") and not partner]
    other_total = sum(x["amount"] for x in other)

    net = (basic_due + ot_amount + bonus
           - leave_ded - late_ded - absent_ded - half_ded - early_ded - adv_total - other_total)
    net_r = int(round(net))                          # only the final figure is rounded
    return {"ok": True, "slip": {
        "id": uid + "|" + month, "user": uid, "name": user.get("name"),
        "designation": person.get("designation") or ROLE_NAMES.get(user.get("role"), ""),
        "month": month, "final_month": final,
        "period": [start, end], "days_employed": days_employed, "full_month": full_month,
        "basic": basic, "basic_due": _money(basic_due),
        "per_day": _money(per_day), "per_hour": _money(per_hour),
        "present": present, "leave_days": leaves, "absent_days": absents,
        "half_days_unpaid": halves_unpaid, "holidays": holidays, "weekly_offs": offs,
        "late_marks": lates, "late_days": _money(late_days),
        "ot_hours": ot_hours, "ot_amount": _money(ot_amount),
        "early_minutes": early_min, "early_deduction": _money(early_ded),
        "leave_deduction": _money(leave_ded), "absent_deduction": _money(absent_ded),
        "late_deduction": _money(late_ded), "half_deduction": _money(half_ded),
        "advance_deduction": _money(adv_total), "advances": advances,
        "bonus": _money(bonus), "net": net_r, "owes": -net_r if net_r < 0 else 0,
        "operator": operator, "partner": partner, "other_deductions": other, "other_deduction": _money(other_total),
        "concessions": concessions, "no_out": no_out, "table": table,
        "rate_from": rate.get("from"),
    }}


ROLE_NAMES = {"admin": "Admin", "supervisor": "Supervisor", "store": "Store Manager",
              "tech": "Technician", "helper": "Helper", "operator": "Operator"}


# ---------- what the admin records ----------

def _need_admin(u):
    return u.get("role") == "admin"


def _new_id():
    return secrets.token_hex(6)


def pay_route(cid, req, u):
    act = req.get("action")
    stamp = pk_now().isoformat(timespec="seconds")

    # a person's own finalised slips: the only salary anyone else ever gets
    if act == "slips.mine":
        d = hr_load(cid)
        mine = [s for s in d.get("slips", {}).values()
                if s.get("user") == u["id"] and s.get("status") == "final"]
        mine.sort(key=lambda s: s["month"], reverse=True)
        return {"ok": True, "slips": mine}

    if not allowed(cid, u, "salary"):
        return {"ok": False, "msg": "Salary is for the admin."}

    def need(*keys):
        for k in keys:
            if req.get(k) in (None, ""):
                return {"ok": False, "msg": "Missing: " + k}
        return None

    with _hr_lock:
        d = hr_load(cid)
        for k in ("rates", "people", "advances", "recoveries", "bonuses", "slips"):
            d.setdefault(k, {})

        if act == "overview":
            users = [x for x in store_load(cid).get("users", {}).values()
                     if isinstance(x, dict) and x.get("active", True)]
            out = []
            for x in users:
                rs = sorted([r for r in d["rates"].values() if r.get("user") == x["id"]],
                            key=lambda r: (r.get("from", ""), r.get("at", "")))
                advs = [dict(a, balance=_balance(d, a)) for a in d["advances"].values()
                        if a.get("user") == x["id"]]
                out.append({"user": x["id"], "name": x.get("name"), "role": x.get("role"),
                            "person": d["people"].get(x["id"], {}), "rates": rs,
                            "deductions": [y for y in d.get("deductions", {}).values()
                                           if y.get("user") == x["id"] and not y.get("void")],
                            "advances": advs,
                            "bonuses": [b for b in d["bonuses"].values() if b.get("user") == x["id"]]})
            return {"ok": True, "people": out}

        if act == "person":            # joining date, leaving date, designation
            e = need("user")
            if e: return e
            p = d["people"].setdefault(req["user"], {})
            for k in ("joined", "left", "designation", "acTitle", "acBank"):
                if k in req:
                    p[k] = str(req.get(k) or "").strip()
            if "payType" in req:
                p["payType"] = "partner" if req.get("payType") == "partner" else ""
            if "iban" in req:
                ib = _re.sub(r"\s", "", str(req.get("iban") or "")).upper()
                if ib and not iban_ok(ib):
                    return {"ok": False, "msg": "That IBAN is not right \u2014 check it against the bank's. "
                                                "A Pakistani IBAN is 24 characters, PK then 22."}
                p["iban"] = ib
            hr_save(cid, d)
            return {"ok": True}

        if act == "company":           # Paragon's own account, for the bank letter
            if req.get("set"):
                c = d.setdefault("company", {})
                for k in ("acTitle", "acBank", "branch", "branchAddr"):
                    if k in req:
                        c[k] = str(req.get(k) or "").strip()
                if isinstance(req.get("letter"), dict):
                    # the letter's own words and who signs it \u2014 every part the admin's to change
                    L = req["letter"]
                    keep = {}
                    for k in ("to", "subject", "salutation", "body", "closing", "signoff", "forLine", "ref"):
                        if k in L:
                            keep[k] = str(L.get(k) or "")[:2000]
                    sigs = []
                    for s in (L.get("signers") or [])[:4]:
                        if isinstance(s, dict) and str(s.get("name") or "").strip():
                            sigs.append({"name": str(s["name"]).strip()[:80], "title": str(s.get("title") or "").strip()[:60],
                                         "stamp": bool(s.get("stamp", True))})
                    keep["signers"] = sigs
                    c["letter"] = keep
                if "iban" in req:
                    ib = _re.sub(r"\s", "", str(req.get("iban") or "")).upper()
                    if ib and not iban_ok(ib):
                        return {"ok": False, "msg": "That IBAN is not right \u2014 check it against the bank's."}
                    c["iban"] = ib
                hr_save(cid, d)
            return {"ok": True, "company": d.get("company", {})}

        if act == "deduct":            # a deduction by hand, always with its reason
            e = need("user", "month", "amount", "reason")
            if e: return e
            if float(req["amount"]) <= 0:
                return {"ok": False, "msg": "Enter the amount."}
            xid = _new_id()
            d.setdefault("deductions", {})[xid] = {"id": xid, "user": req["user"], "month": str(req["month"])[:7],
                                                   "amount": float(req["amount"]), "reason": str(req["reason"]),
                                                   "by": u.get("name"), "at": stamp}
            hr_save(cid, d)
            return {"ok": True, "id": xid}

        if act == "deduct.void":
            x = d.get("deductions", {}).get(str(req.get("id") or ""))
            if not x:
                return {"ok": False, "msg": "No such deduction."}
            slip = d.get("slips", {}).get(x["user"] + "|" + x["month"])
            if slip and slip.get("status") == "final":
                return {"ok": False, "msg": "That month's slip is finalised; it cannot change now."}
            x["void"] = True; x["voidBy"] = u.get("name"); x["voidAt"] = stamp
            hr_save(cid, d)
            return {"ok": True}

        if act == "advance.month":     # this month: all of it, a part, or nothing
            a = d.get("advances", {}).get(str(req.get("id") or ""))
            if not a:
                return {"ok": False, "msg": "No such advance."}
            month = str(req.get("month") or "")[:7]
            if len(month) != 7:
                return {"ok": False, "msg": "Choose the month."}
            if req.get("clear"):
                (a.get("overrides") or {}).pop(month, None)
            else:
                amt = float(req.get("amount") or 0)
                if amt < 0 or amt - _balance(d, a) > 0.5:
                    return {"ok": False, "msg": "That is more than is left on this advance (%s)." % _balance(d, a)}
                a.setdefault("overrides", {})[month] = {"amount": amt, "by": u.get("name"), "at": stamp,
                                                        "note": str(req.get("note") or "")}
            hr_save(cid, d)
            return {"ok": True}

        if act == "bank.letter":       # every finalised salary of the month, for the bank
            month = str(req.get("month") or "")[:7]
            users = store_load(cid).get("users", {})
            rows, missing, skipped = [], [], []
            for s in sorted(d.get("slips", {}).values(), key=lambda s: str(s.get("name") or "")):
                if s.get("month") != month or s.get("status") != "final":
                    continue
                if s["net"] <= 0:
                    skipped.append({"name": s.get("name"), "net": s["net"]}); continue
                p = d["people"].get(s["user"], {})
                if not (p.get("iban") and p.get("acTitle") and p.get("acBank")):
                    missing.append(s.get("name")); continue
                rows.append({"name": s.get("name"), "title": p["acTitle"], "bank": p["acBank"],
                             "iban": p["iban"], "amount": s["net"]})
            drafts = [s.get("name") for s in d.get("slips", {}).values()
                      if s.get("month") == month and s.get("status") != "final"]
            return {"ok": True, "month": month, "company": d.get("company", {}), "rows": rows,
                    "total": sum(r["amount"] for r in rows), "missing": missing, "skipped": skipped,
                    "drafts": drafts}

        if act == "rate":              # a raise is a new row; nothing is overwritten
            e = need("user", "basic", "from")
            if e: return e
            rid = _new_id()
            d["rates"][rid] = {"id": rid, "user": req["user"], "basic": float(req["basic"]),
                               "from": str(req["from"])[:10], "note": str(req.get("note") or ""),
                               "by": u.get("name"), "at": stamp}
            hr_save(cid, d)
            return {"ok": True, "id": rid}

        if act == "advance":
            e = need("user", "amount", "given", "instalments", "first")
            if e: return e
            aid = _new_id()
            d["advances"][aid] = {"id": aid, "user": req["user"], "amount": float(req["amount"]),
                                  "given": str(req["given"])[:10],
                                  "purpose": str(req.get("purpose") or ""),
                                  "instalments": int(req["instalments"]),
                                  "first": str(req["first"])[:7], "status": "active",
                                  "by": u.get("name"), "at": stamp}
            hr_save(cid, d)
            return {"ok": True, "id": aid}

        if act == "bonus":
            e = need("user", "amount", "month")
            if e: return e
            bid = _new_id()
            d["bonuses"][bid] = {"id": bid, "user": req["user"], "amount": float(req["amount"]),
                                 "month": str(req["month"])[:7],
                                 "reason": str(req.get("reason") or ""),
                                 "by": u.get("name"), "at": stamp}
            hr_save(cid, d)
            return {"ok": True, "id": bid}

        if act == "approve":           # a day: overtime, early, half day, forgiveness
            e = need("user", "day", "note")
            if e: return e
            rid = req["user"] + "|" + str(req["day"])[:10]
            rec = d["attendance"].get(rid) or {"id": rid, "user": req["user"],
                                               "day": str(req["day"])[:10], "status": "absent"}
            before = {k: rec.get(k) for k in ("okOT", "okEarly", "halfPaid", "waive", "status")}
            for k in ("okOT", "okEarly", "halfPaid"):
                if k in req:
                    rec[k] = bool(req[k])
            if "half" in req:
                if req["half"]:
                    rec["status"] = "half"
                elif rec.get("status") == "half":
                    rec["status"] = "present"
            if "waive" in req:
                rec["waive"] = [w for w in (req.get("waive") or [])
                                if w in ("late", "absent", "leave")]
            rec["waiveNote"] = str(req["note"])
            rec.setdefault("history", []).append({"by": u.get("name"), "at": stamp,
                                                  "note": str(req["note"]), "before": before})
            d["attendance"][rid] = rec
            hr_save(cid, d)
            return {"ok": True}

        if act == "slips.make":        # drafts only: nothing is recovered here
            e = need("month")
            if e: return e
            month = str(req["month"])[:7]
            made, problems = [], []
            who = req.get("users") or [x["id"] for x in store_load(cid).get("users", {}).values()
                                       if isinstance(x, dict) and x.get("active", True)]
            hr_save(cid, d)
    # outside the lock: the calculation reads the store itself
    if act == "slips.make":
        for uid in who:
            with _hr_lock:
                dd = hr_load(cid)
                have = dd.get("slips", {}).get(uid + "|" + month)
            if have and have.get("status") == "final":
                continue                  # a finalised slip is never remade
            r = pay_calculate(cid, uid, month)
            if not r["ok"]:
                problems.append(r["msg"]); continue
            s = r["slip"]; s["status"] = "draft"; s["made"] = stamp; s["madeBy"] = u.get("name")
            with _hr_lock:
                dd = hr_load(cid); dd.setdefault("slips", {})[s["id"]] = s; hr_save(cid, dd)
            made.append(s["id"])
        return {"ok": True, "made": made, "problems": problems}

    with _hr_lock:
        d = hr_load(cid)
        d.setdefault("slips", {}); d.setdefault("recoveries", {}); d.setdefault("advances", {})
        if act == "slips.month":
            month = str(req.get("month") or "")[:7]
            return {"ok": True, "slips": [s for s in d["slips"].values() if s.get("month") == month]}

        if act == "slips.final":
            s = d["slips"].get(str(req.get("id") or ""))
            if not s:
                return {"ok": False, "msg": "No such slip."}
            if s.get("status") == "final":
                return {"ok": True, "already": True}
            if s.get("no_out"):
                return {"ok": False, "msg": "Fix the days without a check-out first: " +
                        ", ".join(s["no_out"])}
            # recoveries are written now, and only now
            for a in s.get("advances", []):
                if a.get("this_month", 0) > 0:
                    rid = _new_id()
                    d["recoveries"][rid] = {"id": rid, "advance": a["id"], "slip": s["id"],
                                            "amount": a["this_month"], "on": stamp}
            for a in d["advances"].values():
                if a.get("user") == s["user"] and _balance(d, a) <= 0:
                    a["status"] = "recovered"
            s["status"] = "final"; s["finalBy"] = u.get("name"); s["finalAt"] = stamp
            hr_save(cid, d)
            return {"ok": True}

    return {"ok": False, "msg": "Unknown action"}


# ================================================================
#  INVOICES, PAYMENTS AND RECEIVABLES
#  ----------------------------------------------------------------
#  The nine rules of the specification, all kept here:
#    1 nothing is deleted \u2014 cancelled, adjusted or written off, with
#      a reason, and it stays
#    2 an issued invoice is never edited
#    3 a payment counts only once confirmed, never on collection
#    4 allocation is automatic only where exactly one answer is possible
#    5 tax rates are frozen at creation
#    6 balances are computed from entries, never stored
#    7 a duplicate invoice number is refused on import
#    8 nothing imports without a summary the admin has seen
#    9 every serial on a delivery note, every first counter photographed
#  And the owner's decisions:
#    withholding is its own line on a payment; the certificate comes later
#    each brand has its own series, continuing from the last by hand
#    payment terms are the client's own, changeable on each invoice
#    an advance waits on the client's account until the admin applies it
#    an overpayment is decided by the admin each time
#    only an admin writes off, with a reason, whenever needed
#    a discount after invoicing is an adjustment with a reason
#    a rental client's name is matched once and remembered
# ================================================================

import base64 as _b64m

_money_lock = threading.Lock()
BRAND_KEYS = ("paragon", "house", "star")


def _money_path(cid):
    return os.path.join(DATA_DIR, str(cid or "main") + "-money.json")


def money_load(cid):
    try:
        with open(_money_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        d = {}
    for k in ("sinv", "rinv", "namemap", "pays", "allocs", "adjust", "dnotes"):
        d.setdefault(k, {})
    s = d.setdefault("settings", {})
    s.setdefault("brands", {b: {"prefix": "", "next": None, "bank": ""} for b in BRAND_KEYS})
    s.setdefault("warranty", "")
    s.setdefault("terms", {})            # a client's own default terms
    return d


def money_save(cid, d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _money_path(cid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _money_path(cid))


def _photo_dir(cid):
    p = os.path.join(DATA_DIR, str(cid or "main") + "-money-photos")
    os.makedirs(p, exist_ok=True)
    return p


def _keep_photo(cid, data_url):
    """A photo is kept as its own file; records carry only its name."""
    s = str(data_url or "")
    if "," in s:
        s = s.split(",", 1)[1]
    if not s:
        return ""
    raw = _b64m.b64decode(s)
    if len(raw) > 3 * 1024 * 1024:
        raise ValueError("That photo is too large")
    pid = secrets.token_hex(8)
    with open(os.path.join(_photo_dir(cid), pid + ".jpg"), "wb") as fh:
        fh.write(raw)
    return pid


def _read_photo(cid, pid):
    pid = "".join(ch for ch in str(pid) if ch in "0123456789abcdef")
    try:
        with open(os.path.join(_photo_dir(cid), pid + ".jpg"), "rb") as fh:
            return "data:image/jpeg;base64," + _b64m.b64encode(fh.read()).decode()
    except OSError:
        return ""


def _today():
    return pk_now().strftime("%Y-%m-%d")


def _add_days(day, n):
    return (_dt.date.fromisoformat(day) + _dt.timedelta(days=int(n))).isoformat()


# ---------- what is owed, always counted, never stored ----------

def _live_allocs(d):
    ok = {p["id"] for p in d["pays"].values() if p.get("status") == "confirmed"}
    return [a for a in d["allocs"].values() if not a.get("void") and a.get("pay") in ok]


def _inv_key(kind, ident):
    return ("s:" if kind == "sales" else "r:") + str(ident)


def _outstanding(d, key, total, cancelled=False):
    if cancelled:
        return 0.0
    paid = sum(a["amount"] for a in _live_allocs(d) if a.get("inv") == key)
    adj = sum(x["amount"] for x in d["adjust"].values() if x.get("inv") == key)
    return round(total - paid - adj, 2)


def _open_items(d, client=None):
    """Every invoice with money still owing, sales and rental, oldest first."""
    out = []
    for i in d["sinv"].values():
        if client and i["client"] != client:
            continue
        key = _inv_key("sales", i["id"])
        o = _outstanding(d, key, i["total"], i.get("status") == "cancelled")
        if o > 0.5:
            out.append({"key": key, "kind": "sales", "no": i["no"], "client": i["client"],
                        "date": i["date"], "due": i.get("due") or i["date"],
                        "total": i["total"], "outstanding": o})
    for r in d["rinv"].values():
        if client and r["client"] != client:
            continue
        key = _inv_key("rental", r["no"])
        o = _outstanding(d, key, r["total"])
        if o > 0.5:
            out.append({"key": key, "kind": "rental", "no": r["no"], "client": r["client"],
                        "date": r["date"], "due": r["date"], "total": r["total"], "outstanding": o})
    out.sort(key=lambda x: (x["date"], x["no"]))
    return out


def _credit(d, client):
    """Money received and confirmed that is not yet set against an invoice:
    an advance, or an overpayment the admin has not yet decided on."""
    got = sum(p["amount"] + p.get("withholding", 0) for p in d["pays"].values()
              if p.get("client") == client and p.get("status") == "confirmed")
    used = sum(a["amount"] for a in _live_allocs(d)
               if d["pays"].get(a["pay"], {}).get("client") == client)
    refunded = sum(x["amount"] for x in d["adjust"].values()
                   if x.get("client") == client and x.get("type") == "refund")
    return round(got - used - refunded, 2)


def _unallocated(d, pay):
    used = sum(a["amount"] for a in d["allocs"].values()
               if a.get("pay") == pay["id"] and not a.get("void"))
    return round(pay["amount"] + pay.get("withholding", 0) - used, 2)


def _inv_status(d, i):
    if i.get("status") == "cancelled":
        return "cancelled"
    o = _outstanding(d, _inv_key("sales", i["id"]), i["total"])
    if o <= 0.5:
        return "paid"
    return "partially_paid" if o < i["total"] - 0.5 else "issued"


def _client_name(cid, client):
    c = store_load(cid).get("clients", {}).get(client) or {}
    return c.get("name") or client


# ---------- allocation: automatic only where exactly one answer exists ----------

def _auto_allocate(d, pay, by):
    amt = _unallocated(d, pay)
    if amt <= 0.5:
        return "nothing"
    items = _open_items(d, pay["client"])
    whole = round(sum(x["outstanding"] for x in items), 2)
    stamp = pk_now().isoformat(timespec="seconds")
    if items and abs(amt - whole) <= 0.5:
        for x in items:
            aid = secrets.token_hex(6)
            d["allocs"][aid] = {"id": aid, "pay": pay["id"], "inv": x["key"],
                                "amount": x["outstanding"], "by": by, "at": stamp, "method": "automatic"}
        return "whole balance"
    same = [x for x in items if abs(x["outstanding"] - amt) <= 0.5]
    if len(same) == 1:
        x = same[0]
        aid = secrets.token_hex(6)
        d["allocs"][aid] = {"id": aid, "pay": pay["id"], "inv": x["key"],
                            "amount": x["outstanding"], "by": by, "at": stamp, "method": "automatic"}
        return "one invoice"
    return "needs a person"


# ---------- the routes ----------

def money_route(cid, req, u):
    act = req.get("action")
    role = u.get("role")
    admin = allowed(cid, u, "money")
    stamp = pk_now().isoformat(timespec="seconds")
    who = u.get("name")

    def refuse(msg="Money screens are for the admin."):
        return {"ok": False, "msg": msg}

    with _money_lock:
        d = money_load(cid)

        # --- client profit ke liye: is client ka is mahine ka confirmed paisa ---
        if act == "received":
            if not admin:
                return refuse()
            client = str(req.get("client") or "")
            month = str(req.get("month") or "")
            total = 0.0
            for pay in d.get("pays", {}).values():
                if pay.get("status") != "confirmed":
                    continue
                if pay.get("client") != client:
                    continue
                when = str(pay.get("confirmedAt") or pay.get("at") or "")
                if month and when[:7] != month:
                    continue
                total += float(pay.get("amount") or 0)
            return {"ok": True, "received": round(total)}

        # --- a technician in front of the client, collecting ---
        if act == "pay.collect":
            if not allowed(cid, u, "collect"):
                return refuse("Only a technician or the admin records a collection.")
            client = str(req.get("client") or "")
            amt = float(req.get("amount") or 0)
            if not client or amt <= 0:
                return {"ok": False, "msg": "Choose the client and enter the amount."}
            if req.get("method") not in ("cash", "cheque", "bank_transfer"):
                return {"ok": False, "msg": "Choose how it was paid."}
            if not req.get("photo"):
                return {"ok": False, "msg": "A photo of the cheque or the receipt is required."}
            due = round(sum(x["outstanding"] for x in _open_items(d, client)) - _credit(d, client), 2)
            why = str(req.get("reason") or "")
            if abs(due - amt) > 0.5 and why not in ("part", "withholding", "advance"):
                # caught here, while he is still in front of the client
                return {"ok": False, "mismatch": True, "due": due, "received": amt,
                        "difference": round(due - amt, 2)}
            pid = secrets.token_hex(6)
            d["pays"][pid] = {"id": pid, "client": client, "amount": amt, "withholding": 0,
                              "method": req["method"], "photo": _keep_photo(cid, req["photo"]),
                              "status": "collected", "collectedBy": who, "collectedAt": stamp,
                              "reason": why, "note": str(req.get("note") or "")}
            money_save(cid, d)
            return {"ok": True, "id": pid}

        # --- delivery: the store manager may do this too, and sees no prices ---
        if act in ("dn.invoices", "dn.make"):
            if not allowed(cid, u, "delivery"):
                return refuse("Delivery notes are for the admin and the store.")
            if act == "dn.invoices":
                out = []
                for i in d["sinv"].values():
                    if i.get("status") == "cancelled":
                        continue
                    lines = [{"desc": l["desc"], "qty": l["qty"], "model": l.get("model", ""),
                              "warranty": l.get("warranty")} for l in i["lines"]]
                    out.append({"id": i["id"], "no": i["no"], "client": i["client"],
                                "brand": i["brand"], "date": i["date"], "po": i.get("po", ""),
                                "lines": lines,
                                "delivered": [it for n in d["dnotes"].values() if n["inv"] == i["id"]
                                              for it in n["items"]]})
                return {"ok": True, "invoices": out}
            inv = d["sinv"].get(str(req.get("inv") or ""))
            if not inv or inv.get("status") == "cancelled":
                return {"ok": False, "msg": "Choose an issued invoice."}
            if not req.get("date"):
                return {"ok": False, "msg": "Enter the delivery date."}
            items = req.get("items") or []
            if not items:
                return {"ok": False, "msg": "Add at least one machine."}
            kept = []
            for it in items:
                if not str(it.get("serial") or "").strip():
                    return {"ok": False, "msg": "Every machine needs its serial number."}
                if str(it.get("counter") or "").strip() == "":
                    return {"ok": False, "msg": "Every machine needs its counter at delivery."}
                if not it.get("counterPhoto"):
                    return {"ok": False, "msg": "A photo of the counter is required for "
                                                + str(it.get("serial")) + "."}
            for it in items:
                kept.append({"line": int(it.get("line") or 0), "serial": str(it["serial"]).strip(),
                             "model": str(it.get("model") or ""), "counter": int(float(it["counter"])),
                             "counterPhoto": _keep_photo(cid, it["counterPhoto"]),
                             "condition": str(it.get("condition") or "")})
            n = sum(1 for x in d["dnotes"].values() if x["inv"] == inv["id"]) + 1
            nid = secrets.token_hex(6)
            d["dnotes"][nid] = {"id": nid, "no": "%s-D%d" % (inv["no"], n), "inv": inv["id"],
                                "address": str(req.get("address") or ""), "date": str(req["date"])[:10],
                                "po": inv.get("po", ""), "poDate": inv.get("poDate", ""),
                                "deliveredBy": str(req.get("deliveredBy") or who),
                                "recvName": str(req.get("recvName") or ""),
                                "recvDesig": str(req.get("recvDesig") or ""),
                                "signed": _keep_photo(cid, req.get("signed")) if req.get("signed") else "",
                                "items": kept, "by": who, "at": stamp}
            money_save(cid, d)
            return {"ok": True, "id": nid, "no": d["dnotes"][nid]["no"]}

        if act == "photo":
            if not (allowed(cid, u, "delivery") or admin):
                return refuse()
            return {"ok": True, "data": _read_photo(cid, req.get("id"))}

        if not admin:
            return refuse()

        # --- settings: numbering, bank details, warranty wording, terms ---
        if act == "settings":
            if req.get("set"):
                s = d["settings"]
                for b in BRAND_KEYS:
                    got = (req.get("brands") or {}).get(b)
                    if got:
                        cur = s["brands"][b]
                        if "prefix" in got: cur["prefix"] = str(got["prefix"]).strip()
                        if got.get("next") not in (None, ""): cur["next"] = int(got["next"])
                        for k in ("bank", "addr", "phone", "ntn", "strn"):
                            if k in got: cur[k] = str(got[k])
                if "warranty" in req:
                    s["warranty"] = str(req["warranty"])
                for c, t in (req.get("terms") or {}).items():
                    s["terms"][c] = t
                money_save(cid, d)
            return {"ok": True, "settings": d["settings"]}

        # --- a sales invoice, from the ticked lines of an accepted quotation ---
        if act == "inv.make":
            q = store_load(cid).get("quotes", {}).get(str(req.get("quote") or ""))
            if not q:
                return {"ok": False, "msg": "That quotation was not found on the server."}
            if q.get("state") != "accepted":
                return {"ok": False, "msg": "Only an accepted quotation can be invoiced."}
            if q.get("type") == "rental":
                return {"ok": False, "msg": "Rental invoices are made in the other software and imported."}
            brand = q.get("brand") or "paragon"
            b = d["settings"]["brands"].get(brand) or {}
            if not b.get("prefix") or b.get("next") in (None, ""):
                return {"ok": False, "msg": "Set this brand's invoice prefix and next number first "
                        "(Money \u2192 Settings), so the series continues rather than restarts."}
            if not req.get("date"):
                return {"ok": False, "msg": "Enter the invoice date \u2014 it is the date on the document."}
            pick = [int(x) for x in (req.get("lines") or [])]
            if not pick:
                return {"ok": False, "msg": "Tick at least one line that was ordered."}
            kind = req.get("type") if req.get("type") in ("sales", "service", "cash") else "sales"
            taxed = bool(q.get("tax")) and kind != "cash"
            rate = float(q.get("taxPct") or 0) if taxed else 0.0     # frozen, as quoted
            warranty = set(int(x) for x in (req.get("warranty") or []))
            lines, sub, tax = [], 0.0, 0.0
            ql = q.get("lines") or []
            for idx in pick:
                if idx < 0 or idx >= len(ql):
                    continue
                l = ql[idx]
                qty = float(l.get("qty") or 1)
                price = float(l.get("price") or 0)
                amt = price * qty
                before = amt / (1 + rate / 100) if (l.get("enteredAs") == "after" and rate) else amt
                t = before * rate / 100
                lines.append({"from": idx, "desc": l.get("desc", ""), "model": l.get("model", ""),
                              "qty": qty, "unit": round(before / qty, 2) if qty else before,
                              "before": round(before, 2), "tax": round(t, 2),
                              "after": round(before + t, 2), "warranty": idx in warranty})
                sub += before; tax += t
            terms = req.get("terms") or d["settings"]["terms"].get(q.get("client")) or {"kind": "days", "days": 30}
            day = str(req["date"])[:10]
            due = _add_days(day, terms.get("days", 0)) if terms.get("kind") == "days" else day
            no = "%s%s" % (b["prefix"], b["next"])
            if any(i["no"] == no for i in d["sinv"].values()):
                return {"ok": False, "msg": "Invoice number %s is already used." % no}
            b["next"] = int(b["next"]) + 1
            iid = secrets.token_hex(6)
            d["sinv"][iid] = {"id": iid, "no": no, "brand": brand, "client": q.get("client"),
                              "quote": q.get("id"), "quoteNo": q.get("no"), "type": kind,
                              "date": day, "due": due, "terms": terms,
                              "po": str(req.get("po") or ""), "poDate": str(req.get("poDate") or ""),
                              "poFile": _keep_photo(cid, req.get("poPhoto")) if req.get("poPhoto") else "",
                              "tax": taxed, "rate": rate, "lines": lines,
                              "subtotal": round(sub, 2), "taxAmt": round(tax, 2),
                              "total": round(sub + tax, 2), "status": "issued", "by": who, "at": stamp}
            money_save(cid, d)
            return {"ok": True, "id": iid, "no": no}

        if act == "inv.cancel":
            i = d["sinv"].get(str(req.get("id") or ""))
            why = str(req.get("reason") or "").strip()
            if not i:
                return {"ok": False, "msg": "No such invoice."}
            if not why:
                return {"ok": False, "msg": "A reason is required to cancel an invoice."}
            if any(a["inv"] == _inv_key("sales", i["id"]) for a in _live_allocs(d)):
                return {"ok": False, "msg": "Payments are set against this invoice. Move them first."}
            i["status"] = "cancelled"; i["cancelReason"] = why; i["cancelledBy"] = who; i["cancelledAt"] = stamp
            money_save(cid, d)
            return {"ok": True}

        if act == "inv.list":
            out = []
            for i in d["sinv"].values():
                x = dict(i)
                x["outstanding"] = _outstanding(d, _inv_key("sales", i["id"]), i["total"],
                                                i.get("status") == "cancelled")
                x["state"] = _inv_status(d, i)
                x["clientName"] = _client_name(cid, i["client"])
                x["deliveries"] = [n for n in d["dnotes"].values() if n["inv"] == i["id"]]
                out.append(x)
            out.sort(key=lambda x: (x["date"], x["no"]), reverse=True)
            return {"ok": True, "invoices": out, "settings": d["settings"]}

        # --- rental invoices: checked, summarised, then imported on a tap ---
        if act in ("rimport.check", "rimport.commit"):
            clients = store_load(cid).get("clients", {})
            by_name = {str(c.get("name", "")).strip().lower(): k for k, c in clients.items()}
            seen, ready, dup, unknown, bad = set(), [], [], [], []
            for n, r in enumerate(req.get("rows") or []):
                name = str(r.get("client") or "").strip()
                no = str(r.get("no") or "").strip()
                try:
                    amount = float(r.get("amount") or 0); tax = float(r.get("tax") or 0)
                    total = float(r.get("total") or 0)
                except (TypeError, ValueError):
                    bad.append({"row": r.get("row", n + 2), "no": no, "why": "a figure is not a number"}); continue
                if not no or not name or not r.get("date") or not r.get("month"):
                    bad.append({"row": r.get("row", n + 2), "no": no, "why": "a required column is empty"}); continue
                if no in d["rinv"] or no in seen:
                    dup.append({"row": r.get("row", n + 2), "no": no}); continue
                if abs(amount + tax - total) > 0.5:
                    bad.append({"row": r.get("row", n + 2), "no": no,
                                "why": "amount + tax (%s) is not the total (%s)" % (amount + tax, total)}); continue
                client = d["namemap"].get(name.lower()) or by_name.get(name.lower())
                if not client:
                    unknown.append({"row": r.get("row", n + 2), "no": no, "name": name}); continue
                seen.add(no)
                ready.append({"no": no, "client": client, "date": str(r["date"])[:10],
                              "month": str(r["month"])[:7], "amount": amount, "tax": tax, "total": total})
            report = {"ok": True, "ready": len(ready), "value": round(sum(x["total"] for x in ready), 2),
                      "duplicates": dup, "unknown": unknown, "bad": bad,
                      "names": sorted({x["name"] for x in unknown})}
            if act == "rimport.check":
                return report
            batch = secrets.token_hex(4)
            for x in ready:
                x.update({"batch": batch, "by": who, "at": stamp})
                d["rinv"][x["no"]] = x
            money_save(cid, d)
            report["imported"] = len(ready); report["batch"] = batch
            return report

        if act == "namemap.set":
            name = str(req.get("name") or "").strip().lower()
            if not name or not req.get("client"):
                return {"ok": False, "msg": "Choose the client for that name."}
            d["namemap"][name] = str(req["client"])
            money_save(cid, d)
            return {"ok": True}

        # --- payments: confirmed in the office, with the cheque in hand ---
        if act == "pays.list":
            out = []
            for p in d["pays"].values():
                x = dict(p); x["clientName"] = _client_name(cid, p["client"])
                x["unallocated"] = _unallocated(d, p) if p["status"] == "confirmed" else None
                x["allocs"] = [a for a in d["allocs"].values() if a["pay"] == p["id"] and not a.get("void")]
                out.append(x)
            out.sort(key=lambda x: (x["status"] != "collected", x.get("collectedAt", "")), reverse=False)
            return {"ok": True, "pays": out}

        if act == "pay.confirm":
            p = d["pays"].get(str(req.get("id") or ""))
            if not p or p["status"] != "collected":
                return {"ok": False, "msg": "Only a collected payment can be confirmed."}
            if p["method"] == "cheque" and not (req.get("chequeNo") and req.get("chequeDate") and req.get("bank")):
                return {"ok": False, "msg": "Enter the cheque number, date and bank."}
            p.update({"chequeNo": str(req.get("chequeNo") or ""), "chequeDate": str(req.get("chequeDate") or ""),
                      "bank": str(req.get("bank") or ""), "withholding": float(req.get("withholding") or 0),
                      "status": "confirmed", "confirmedBy": who, "confirmedAt": stamp})
            how = _auto_allocate(d, p, who)
            money_save(cid, d)
            return {"ok": True, "allocation": how, "unallocated": _unallocated(d, p)}

        if act == "pay.cert":
            p = d["pays"].get(str(req.get("id") or ""))
            if not p or not req.get("photo"):
                return {"ok": False, "msg": "Choose the payment and the certificate."}
            p["whCert"] = _keep_photo(cid, req["photo"]); p["whCertNo"] = str(req.get("no") or "")
            p["whCertAt"] = stamp
            money_save(cid, d)
            return {"ok": True}

        if act == "pay.bounce":
            p = d["pays"].get(str(req.get("id") or ""))
            why = str(req.get("reason") or "").strip()
            if not p or p["status"] != "confirmed":
                return {"ok": False, "msg": "Only a confirmed payment can bounce."}
            if not why:
                return {"ok": False, "msg": "A reason is required."}
            p.update({"status": "bounced", "bounceReason": why, "bouncedBy": who, "bouncedAt": stamp})
            for a in d["allocs"].values():            # the receivable goes back up
                if a["pay"] == p["id"]:
                    a["void"] = True
            money_save(cid, d)
            return {"ok": True}

        if act == "allocate":
            p = d["pays"].get(str(req.get("pay") or ""))
            if not p or p["status"] != "confirmed":
                return {"ok": False, "msg": "Only a confirmed payment can be set against invoices."}
            left = _unallocated(d, p)
            open_ = {x["key"]: x for x in _open_items(d, p["client"])}
            want = [(str(x.get("inv")), float(x.get("amount") or 0)) for x in (req.get("lines") or [])]
            if sum(a for _, a in want) - left > 0.5:
                return {"ok": False, "msg": "That is more than is left on this payment (%s)." % left}
            for key, a in want:
                if key not in open_ or a <= 0 or a - open_[key]["outstanding"] > 0.5:
                    return {"ok": False, "msg": "One of those amounts is more than that invoice owes."}
            for key, a in want:
                aid = secrets.token_hex(6)
                d["allocs"][aid] = {"id": aid, "pay": p["id"], "inv": key, "amount": round(a, 2),
                                    "by": who, "at": stamp, "method": "manual"}
            money_save(cid, d)
            return {"ok": True}

        # --- adjustments: never an edit to the invoice ---
        if act == "adj.make":
            kind = req.get("type")
            why = str(req.get("reason") or "").strip()
            amt = float(req.get("amount") or 0)
            if kind not in ("discount", "write_off", "credit_note", "refund", "correction"):
                return {"ok": False, "msg": "Choose what kind of adjustment."}
            if not why or amt <= 0:
                return {"ok": False, "msg": "An adjustment needs an amount and a reason."}
            client = str(req.get("client") or "")
            key = str(req.get("inv") or "")
            if kind == "refund":
                if amt - _credit(d, client) > 0.5:
                    return {"ok": False, "msg": "That is more than the credit on this account."}
                key = ""
            else:
                open_ = {x["key"]: x for x in _open_items(d, client)}
                if key not in open_ or amt - open_[key]["outstanding"] > 0.5:
                    return {"ok": False, "msg": "Choose an open invoice, for no more than it owes."}
            xid = secrets.token_hex(6)
            d["adjust"][xid] = {"id": xid, "client": client, "inv": key, "type": kind, "amount": amt,
                                "reason": why, "by": who, "at": stamp}
            money_save(cid, d)
            return {"ok": True, "id": xid}

        # --- receivables and the ledger, counted afresh every time ---
        if act == "recv":
            items = _open_items(d)
            today = _dt.date.fromisoformat(_today())
            rows = {}
            for x in items:
                r = rows.setdefault((x["kind"], x["client"]), {"client": x["client"], "kind": x["kind"],
                                    "count": 0, "outstanding": 0.0, "oldest": 0, "over60": 0.0})
                age = (today - _dt.date.fromisoformat(x["date"])).days
                r["count"] += 1; r["outstanding"] = round(r["outstanding"] + x["outstanding"], 2)
                r["oldest"] = max(r["oldest"], age)
                if age >= 60:
                    r["over60"] = round(r["over60"] + x["outstanding"], 2)
            out = sorted(rows.values(), key=lambda r: -r["outstanding"])
            for r in out:
                r["name"] = _client_name(cid, r["client"])
            summ = {k: {"outstanding": round(sum(r["outstanding"] for r in out if r["kind"] == k), 2),
                        "over60": round(sum(r["over60"] for r in out if r["kind"] == k), 2)}
                    for k in ("sales", "rental")}
            waiting = sum(1 for p in d["pays"].values() if p["status"] == "collected")
            no_cert = [p["id"] for p in d["pays"].values()
                       if p["status"] == "confirmed" and p.get("withholding", 0) > 0 and not p.get("whCert")]
            return {"ok": True, "rows": out, "summary": summ, "awaiting": waiting, "noCert": len(no_cert)}

        if act == "dash.money":
            # Everything the Money tab shows, worked out here, once, from the
            # same helpers the receivables screens use \u2014 so the dashboard and
            # the lists can never disagree.
            today = _dt.date.fromisoformat(_today()); month = _today()[:7]
            items = _open_items(d)
            summ = {"sales": 0.0, "rental": 0.0, "over60": 0.0}
            per = {}
            for x in items:
                age = (today - _dt.date.fromisoformat(x["date"])).days
                summ[x["kind"]] += x["outstanding"]
                if age > 60:
                    summ["over60"] += x["outstanding"]
                c = per.setdefault(x["client"], {"client": x["client"], "count": 0, "kinds": set(),
                                                 "amount": 0.0, "oldest": 0})
                c["count"] += 1; c["kinds"].add(x["kind"]); c["amount"] += x["outstanding"]
                c["oldest"] = max(c["oldest"], age)
            owed = sorted(per.values(), key=lambda c: (-c["oldest"], -c["amount"]))[:5]
            for c in owed:
                c["name"] = _client_name(cid, c["client"])
                c["kinds"] = " + ".join(k.capitalize() for k in sorted(c["kinds"]))
                c["amount"] = round(c["amount"], 2)
            confirmed = [x for x in d["pays"].values() if x.get("status") == "confirmed"
                         and str(x.get("confirmedAt") or "")[:7] == month]
            waiting = sorted([x for x in d["pays"].values() if x.get("status") == "collected"],
                             key=lambda x: str(x.get("collectedAt") or ""))
            users = store_load(cid).get("users", {})
            inv_m = {"issued": 0, "paid": 0, "part": 0, "unpaid": 0}
            for i in d["sinv"].values():
                if str(i.get("date") or "")[:7] != month or i.get("status") == "cancelled":
                    continue
                o = _outstanding(d, _inv_key("sales", i["id"]), i["total"])
                inv_m["issued"] += 1
                inv_m["paid" if o <= 0.5 else ("part" if o < i["total"] - 0.5 else "unpaid")] += 1
            # slow payers: how long, on average, a client took to pay what was set against an invoice
            dates = {_inv_key("sales", i["id"]): i["date"] for i in d["sinv"].values()}
            dates.update({_inv_key("rental", r["no"]): r["date"] for r in d["rinv"].values()})
            took = {}
            for a in _live_allocs(d):
                pay = d["pays"].get(a["pay"]) or {}
                if pay.get("status") != "confirmed" or a.get("inv") not in dates:
                    continue
                days = (_dt.date.fromisoformat(str(pay.get("confirmedAt"))[:10]) -
                        _dt.date.fromisoformat(dates[a["inv"]])).days
                t = took.setdefault(pay["client"], [0.0, 0.0]); t[0] += days * a["amount"]; t[1] += a["amount"]
            slow = sorted([{"client": k, "name": _client_name(cid, k), "days": round(v[0] / v[1])}
                           for k, v in took.items() if v[1] > 0 and v[0] / v[1] > 60], key=lambda x: -x["days"])
            return {"ok": True,
                    "sales": round(summ["sales"], 2), "rental": round(summ["rental"], 2),
                    "total": round(summ["sales"] + summ["rental"], 2), "over60": round(summ["over60"], 2),
                    "collected": round(sum(x["amount"] for x in confirmed), 2),
                    "awaitingSum": round(sum(x["amount"] for x in waiting), 2), "awaitingCount": len(waiting),
                    "awaiting": [{"id": x["id"], "amount": x["amount"], "method": x.get("method"),
                                  "client": _client_name(cid, x["client"]),
                                  "by": (users.get(x.get("collectedBy")) or {}).get("name") or x.get("collectedBy") or "",
                                  "at": x.get("collectedAt")} for x in waiting],
                    "owed": owed, "invoices": inv_m, "slow": slow[:3],
                    "noCert": sum(1 for x in d["pays"].values() if x.get("status") == "confirmed"
                                  and x.get("withholding", 0) > 0 and not x.get("whCert"))}

        if act == "ledger":
            client = str(req.get("client") or "")
            frm = str(req.get("from") or "0000-00-00"); to = str(req.get("to") or "9999-99-99")
            e = []
            for i in d["sinv"].values():
                if i["client"] == client:
                    e.append((i["date"], 0, "Invoice " + i["no"], i["total"], 0))
                    if i.get("status") == "cancelled":
                        e.append((i.get("cancelledAt", i["date"])[:10], 1,
                                  "Cancelled " + i["no"] + " \u2014 " + i.get("cancelReason", ""), 0, i["total"]))
            for r in d["rinv"].values():
                if r["client"] == client:
                    e.append((r["date"], 0, "Rental " + r["no"] + " (" + r["month"] + ")", r["total"], 0))
            for p in d["pays"].values():
                if p["client"] != client or p["status"] == "collected":
                    continue
                day = (p.get("confirmedAt") or "")[:10]
                label = {"cheque": "Payment \u2014 cheque " + p.get("chequeNo", ""),
                         "cash": "Payment \u2014 cash", "bank_transfer": "Payment \u2014 bank transfer"}[p["method"]]
                e.append((day, 2, label, 0, p["amount"]))
                if p.get("withholding"):
                    e.append((day, 3, "Withholding" + (" \u2014 cert. " + p["whCertNo"] if p.get("whCertNo") else
                              " \u2014 certificate awaited"), 0, p["withholding"]))
                if p["status"] == "bounced":
                    e.append(((p.get("bouncedAt") or day)[:10], 4, "Bounced \u2014 " + p.get("bounceReason", ""),
                              p["amount"] + p.get("withholding", 0), 0))
            for x in d["adjust"].values():
                if x["client"] == client:
                    label = {"discount": "Discount", "write_off": "Written off", "credit_note": "Credit note",
                             "refund": "Refund", "correction": "Correction"}[x["type"]] + " \u2014 " + x["reason"]
                    if x["type"] == "refund":
                        e.append((x["at"][:10], 5, label, x["amount"], 0))
                    else:
                        e.append((x["at"][:10], 5, label, 0, x["amount"]))
            e.sort(key=lambda t: (t[0], t[1]))
            opening = round(sum(t[3] - t[4] for t in e if t[0] < frm), 2)
            bal, rows = opening, []
            for t in e:
                if frm <= t[0] <= to:
                    bal = round(bal + t[3] - t[4], 2)
                    rows.append({"date": t[0], "doc": t[2], "debit": t[3], "credit": t[4], "balance": bal})
            items = _open_items(d, client)
            today = _dt.date.fromisoformat(_today())
            ageing = {"0-29": 0.0, "30-59": 0.0, "60+": 0.0}
            for x in items:
                age = (today - _dt.date.fromisoformat(x["date"])).days
                k = "60+" if age >= 60 else ("30-59" if age >= 30 else "0-29")
                ageing[k] = round(ageing[k] + x["outstanding"], 2)
            return {"ok": True, "client": client, "name": _client_name(cid, client), "opening": opening,
                    "rows": rows, "closing": bal, "open": items, "credit": _credit(d, client),
                    "ageing": ageing}

    return {"ok": False, "msg": "Unknown action"}


# ================================================================
#  SERVICE MANUALS AND THE AI TECHNICIAN ASSISTANT
#  ----------------------------------------------------------------
#  The library works now: manuals are stored, their text read page
#  by page, and searchable by model. No AI is involved in any of it.
#
#  The assistant is built completely and left OFF. The owner chooses
#  the provider, enters the key, sets a spending limit and presses ON.
#  Until then no AI service is ever contacted and nothing is spent,
#  and every AI action here refuses \u2014 the server enforces it, not
#  the screen.
#
#  Every fact in every answer carries where it came from:
#    manual (with its page) \u00b7 this machine's history \u00b7 internet
#    (with the link) \u00b7 general knowledge, labelled as such.
# ================================================================

import subprocess as _sp, math as _math, re as _re

_ai_lock = threading.Lock()


def _ai_dir():
    d = os.path.join(DATA_DIR, "ai")
    os.makedirs(os.path.join(d, "manuals"), exist_ok=True)
    return d


AI_DEFAULTS = {
    "enabled": False, "provider": "", "model": "", "key_enc": "", "key_last4": "",
    "internet": False, "daily_limit_rs": None, "per_tech_limit": None,
    "price_in_rs": None, "price_out_rs": None,        # rupees per million tokens
    "default_allowed": False, "reader_for_techs": False,
    "tested_at": "", "index_built_at": "", "max_words": 180,
}


# The six drafting agents. Each only ever writes a draft; a person reads it
# and sends it. The master switch and the daily limit still apply.
AI_AGENTS = [
    {"id": "enquiry", "label": "Enquiry reply",
     "hint": "A customer\u2019s enquiry \u2014 draft a warm, professional reply.",
     "system": "You draft replies for Paragon Copier Solution, a Ricoh photocopier "
               "sales/rental/service firm in Karachi. Write a warm, professional, concise "
               "reply to the customer enquiry given. Never invent prices or promises; if a "
               "figure is needed, leave a clear [blank] for the office to fill. End with the "
               "firm\u2019s name. This is a DRAFT for a human to check and send."},
    {"id": "quotation", "label": "Quotation wording",
     "hint": "Details of what to quote \u2014 draft the covering wording.",
     "system": "You draft the covering wording of a quotation for Paragon Copier Solution. "
               "From the details given, write a clear, professional quotation note (not the "
               "price table). Leave [blank] where an exact figure or model is uncertain. A "
               "DRAFT for a human to check."},
    {"id": "followup", "label": "Follow-up message",
     "hint": "Who to follow up and about what \u2014 draft the nudge.",
     "system": "You draft a polite follow-up message for Paragon Copier Solution to a client "
               "about a pending quotation, payment or service. Short, courteous, never pushy. "
               "A DRAFT for a human to check and send."},
    {"id": "consumable", "label": "Consumable reminder",
     "hint": "A machine and its usage \u2014 draft a reminder that toner/parts are due.",
     "system": "You draft a friendly reminder from Paragon Copier Solution that a client\u2019s "
               "machine is likely due for toner or a consumable soon, based on the usage given. "
               "Suggest arranging a visit. A DRAFT for a human to check."},
    {"id": "tender", "label": "Tender summary",
     "hint": "Paste a tender notice \u2014 draft a short summary and what is needed.",
     "system": "You summarise a government or corporate tender for Paragon Business Solution. "
               "From the tender text, give: what is being sought, key dates, eligibility, and "
               "what documents Paragon would need to bid. Plain and short. A DRAFT for a human."},
    {"id": "content", "label": "Marketing content",
     "hint": "A topic \u2014 draft a short post or message for social media.",
     "system": "You write short marketing content for Paragon Copier Solution / Paragon Business "
               "Solution (Ricoh copiers, rental, service; and business software). From the topic "
               "given, draft one short, engaging post. No false claims. A DRAFT for a human."},
]


def _agent_keep(cid, u, kind, brief, draft, day):
    """Har draft ka rikaard \u2014 kis ne banaya, kab, kya (kharcha ginti ke liye)."""
    try:
        log = _ai_load("agentlog.json", [])
        log.insert(0, {"at": pk_now().isoformat(timespec="seconds"),
                       "by": u.get("name"), "agent": kind,
                       "brief": brief[:200], "len": len(draft or "")})
        _ai_store("agentlog.json", log[:200])
    except Exception:
        pass


def ai_settings():
    try:
        with open(os.path.join(_ai_dir(), "settings.json"), encoding="utf-8") as fh:
            s = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        s = {}
    out = dict(AI_DEFAULTS); out.update(s)
    return out


def _ai_save_settings(s):
    p = os.path.join(_ai_dir(), "settings.json")
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(s, fh)
    os.replace(p + ".tmp", p)


def _ai_load(name, empty):
    try:
        with open(os.path.join(_ai_dir(), name), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return empty


def _ai_store(name, data):
    p = os.path.join(_ai_dir(), name)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(p + ".tmp", p)


# ---------- the key, kept encrypted, never sent back ----------
# A key made on this server encrypts it; only the last four characters are
# ever shown again. The cipher is HMAC-SHA256 in counter mode with a tag, so
# a changed byte is caught rather than decrypted into rubbish.

def _master():
    p = os.path.join(_ai_dir(), "master.key")
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(secrets.token_bytes(32))
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    with open(p, "rb") as fh:
        return fh.read()


def _stream(mk, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(mk, nonce + i.to_bytes(8, "big"), _hl.sha256).digest(); i += 1
    return out[:n]


def ai_encrypt(plain):
    mk = _master(); nonce = secrets.token_bytes(16)
    data = plain.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _stream(mk, nonce, len(data))))
    tag = hmac.new(mk, b"tag" + nonce + ct, _hl.sha256).digest()
    return _b64p.b64encode(nonce + ct + tag).decode()


def ai_decrypt(blob):
    raw = _b64p.b64decode(blob)
    mk = _master(); nonce, ct, tag = raw[:16], raw[16:-32], raw[-32:]
    if not hmac.compare_digest(tag, hmac.new(mk, b"tag" + nonce + ct, _hl.sha256).digest()):
        raise ValueError("the stored key has been tampered with")
    return bytes(a ^ b for a, b in zip(ct, _stream(mk, nonce, len(ct)))).decode("utf-8")


# ================================================================
#  THE ADAPTER \u2014 the only place a provider's name, address or model
#  appears. Switching provider is a settings change, never a code change.
#  None of this runs until the owner has pressed ON.
# ================================================================

def _post_json(url, headers, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers=dict({"Content-Type": "application/json"}, **headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))
        except Exception:
            msg = {}
        err = (msg.get("error") or {}) if isinstance(msg, dict) else {}
        raise RuntimeError("%s %s" % (e.code, err.get("message") if isinstance(err, dict) else err or e.reason))


def _anthropic_ask(key, model, system, question, image=None, web=False, max_tokens=900):
    content = []
    if image:
        mt, _, b64 = image.partition(";base64,")
        content.append({"type": "image", "source": {"type": "base64",
                        "media_type": mt.replace("data:", "") or "image/jpeg", "data": b64}})
    content.append({"type": "text", "text": question})
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": content}]}
    if web:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    d = _post_json("https://api.anthropic.com/v1/messages",
                   {"x-api-key": key, "anthropic-version": "2023-06-01"}, body)
    text, links = [], []
    for b in d.get("content", []):
        if b.get("type") == "text":
            text.append(b.get("text", ""))
            for c in b.get("citations") or []:
                if c.get("url") and c["url"] not in links:
                    links.append(c["url"])
    u = d.get("usage", {})
    return {"text": "".join(text).strip(), "links": links,
            "tokens_in": int(u.get("input_tokens", 0)), "tokens_out": int(u.get("output_tokens", 0))}


def _openai_ask(key, model, system, question, image=None, web=False, max_tokens=900):
    content = [{"type": "text", "text": question}]
    if image:
        content.append({"type": "image_url", "image_url": {"url": image}})
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}]}
    d = _post_json("https://api.openai.com/v1/chat/completions",
                   {"Authorization": "Bearer " + key}, body)
    u = d.get("usage", {})
    msg = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {"text": msg.strip(), "links": [],
            "tokens_in": int(u.get("prompt_tokens", 0)), "tokens_out": int(u.get("completion_tokens", 0))}


AI_PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "ask": _anthropic_ask,
                  "images": True, "web": True,
                  "models_hint": "for example claude-sonnet-4-5"},
    "openai":    {"label": "OpenAI (ChatGPT)", "ask": _openai_ask,
                  "images": True, "web": False,
                  "models_hint": "for example gpt-4o-mini"},
}


def ai_provider_ask(s, system, question, image=None, web=False):
    """The one door every AI request goes through \u2014 and it will not open
    while the switch is off."""
    if not s.get("enabled"):
        raise RuntimeError("The AI assistant is switched off.")
    p = AI_PROVIDERS.get(s.get("provider"))
    if not p or not s.get("key_enc") or not s.get("model"):
        raise RuntimeError("The AI assistant is not set up.")
    return p["ask"](ai_decrypt(s["key_enc"]), s["model"], system, question, image=image,
                    web=web and p["web"])


# ================================================================
#  THE MANUAL LIBRARY \u2014 works now, no AI in it
# ================================================================

MANUAL_TYPES = ("Service Manual", "Parts Catalogue", "User Guide", "Troubleshooting Guide", "Bulletin")


def _manuals():
    return _ai_load("manuals.json", {})


def _pages_path(mid):
    return os.path.join(_ai_dir(), "manuals", mid + ".json")


def _pdf_path(mid):
    return os.path.join(_ai_dir(), "manuals", mid + ".pdf")


def extract_pages(pdf):
    """Every page's text, with its page number. Pages with nothing readable
    are flagged: a scanned page, or one that is only a drawing."""
    try:
        info = _sp.run(["pdfinfo", pdf], capture_output=True, text=True, timeout=60).stdout
        n = int(_re.search(r"Pages:\s+(\d+)", info).group(1))
    except Exception:
        return None, "The file could not be read as a PDF."
    try:
        raw = _sp.run(["pdftotext", "-layout", pdf, "-"], capture_output=True, text=True,
                      timeout=600).stdout
    except FileNotFoundError:
        return None, "The text reader is not installed on the server (poppler-utils)."
    except Exception as e:
        return None, "Reading the text failed: %r" % e
    parts = raw.split("\f")
    pages = []
    for i in range(n):
        t = parts[i] if i < len(parts) else ""
        t = _re.sub(r"[ \t]+", " ", t).strip()
        pages.append({"page": i + 1, "text": t, "has_text": len(_re.sub(r"\W", "", t)) >= 25})
    return pages, ""


def _norm_model(x):
    return _re.sub(r"[^a-z0-9]", "", str(x or "").lower())


def _catalog(cid):
    return {k: v for k, v in store_load(cid).get("models", {}).items() if isinstance(v, dict)}


def _model_id_for_machine(cid, machine):
    want = _norm_model((machine or {}).get("model"))
    for k, m in _catalog(cid).items():
        if _norm_model(m.get("name")) == want:
            return k
    return None


def manuals_for_model(model_id):
    return [m for m in _manuals().values() if m.get("active", True) and model_id in (m.get("models") or [])]


# ---------- searching a manual: on this server, free ----------
_STOP = set("""a an the is are was be to of in on at for and or it this that what how why when
which do does can i my me we you your with from by as not no yes
kya hai hain ho raha rahi rahe aa gaya gayi karun karo kare kaise kyun ka ki ke ko se me mein
par pe bhi ye yeh wo woh aur ya tha thi batao bataen btao check sirf manual internet""".split())


def _tokens(text):
    t = str(text or "").lower()
    t = _re.sub(r"\b(sc|sp|jc)\s*[-\s]?\s*(\d{2,4})\b", r"\1\2", t)       # SC 542 = SC542
    return [w for w in _re.findall(r"[a-z0-9]+", t) if w not in _STOP and len(w) > 1]


def search_pages(model_id, question, k=6):
    """BM25 over the pages of the manuals for this model \u2014 and only those.
    A Kyocera page applied to a Ricoh machine is worse than no answer."""
    q = _tokens(question)
    if not q:
        return []
    docs = []
    for m in manuals_for_model(model_id):
        try:
            with open(_pages_path(m["id"]), encoding="utf-8") as fh:
                pages = json.load(fh)
        except (FileNotFoundError, ValueError, OSError):
            continue
        for p in pages:
            if p.get("has_text"):
                docs.append((m, p, _tokens(p["text"])))
    if not docs:
        return []
    N = len(docs); avg = sum(len(d[2]) for d in docs) / N or 1
    df = {}
    for _, _, toks in docs:
        for w in set(toks):
            df[w] = df.get(w, 0) + 1
    scored = []
    for m, p, toks in docs:
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in set(q):
            if w in tf:
                idf = _math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * 2.2 / (tf[w] + 1.2 * (0.25 + 0.75 * len(toks) / avg))
        if s > 0:
            scored.append((s, m, p))
    scored.sort(key=lambda x: -x[0])
    return [{"manual": m["id"], "title": m["title"], "page": p["page"], "text": p["text"][:2500],
             "score": round(s, 3)} for s, m, p in scored[:k]]


# ---------- what this machine has been through ----------

def machine_context(cid, complaint):
    st = store_load(cid)
    mid = complaint.get("machine")
    jobs = [c for c in st.get("invoices", {}).values()
            if isinstance(c, dict) and c.get("machine") == mid and c.get("id") != complaint.get("id")]
    jobs.sort(key=lambda c: str(c.get("opened", "")), reverse=True)
    users = st.get("users", {}); stock = st.get("stock", {}); parts = st.get("parts", {})
    lines = []
    for c in jobs[:6]:
        fitted = []
        for p in parts.values():
            if isinstance(p, dict) and p.get("complaint") == c.get("id") and (p.get("installed") or 0) > 0:
                s = stock.get(p.get("stock")) or {}
                fitted.append("-".join(x for x in (s.get("code"), s.get("name"), s.get("quality")) if x) or "a part")
        lines.append("%s \u00b7 %s \u00b7 fault: %s \u00b7 work done: %s%s%s" % (
            str(c.get("resolved") or c.get("opened") or "")[:10],
            (users.get(c.get("tech")) or {}).get("name", "?"),
            str(c.get("what") or "")[:120], str(c.get("workDone") or "not recorded")[:160],
            (" \u00b7 fitted: " + ", ".join(fitted)) if fitted else "",
            (" \u00b7 counter %s" % c["counter"]) if c.get("counter") else ""))
    reads = sorted([(str(c.get("counterAt") or c.get("opened") or ""), c["counter"]) for c in jobs
                    if c.get("counter")] + [(str(v.get("at", "")), v["counter"]) for v in
                    st.get("visits", {}).values() if isinstance(v, dict) and v.get("machine") == mid
                    and v.get("counter")])
    trend = ""
    if len(reads) >= 2:
        try:
            days = (_dt.datetime.fromisoformat(reads[-1][0][:19]) - _dt.datetime.fromisoformat(reads[0][0][:19])).days
            if days >= 7:
                trend = "about %d pages a month" % round((reads[-1][1] - reads[0][1]) / days * 30)
        except ValueError:
            pass
    return {"history": lines, "counter": reads[-1][1] if reads else None, "trend": trend}


def stock_for(cid, text):
    """The store count of any part the manual pages name \u2014 worked out the
    same way the app works it out."""
    st = store_load(cid)
    low = str(text or "").lower()
    out = []
    for sid, s in st.get("stock", {}).items():
        if not isinstance(s, dict) or not s.get("name"):
            continue
        if s["name"].lower() in low or (s.get("code") and str(s["code"]).lower() in low):
            n = float(s.get("opening") or 0)
            for b in st.get("buys", {}).values():
                for l in (b.get("lines") or []) if isinstance(b, dict) else []:
                    if l.get("stock") == sid:
                        n += float(l.get("qty") or 0) - float(l.get("back") or 0)
            for p in st.get("parts", {}).values():
                if isinstance(p, dict) and p.get("stock") == sid:
                    n += -float(p.get("issued") or 0) + float(p.get("rejBack") or 0) + float(p.get("returned") or 0)
            out.append("%s (%s): %d in store" % (s["name"], s.get("quality") or "", n))
    return out[:8]


# ---------- the instructions every answer is held to ----------

def _system_prompt(s, mode, machine_line, sources, ctx, stock, has_manual):
    lines = [
        "You are the technician assistant of Paragon Copier Solution, a photocopier service company in Karachi.",
        "A technician is standing at this machine: " + machine_line + ".",
        "Reply in the language the technician writes in (Roman Urdu, Urdu or English). Keep it short: at most %d words, for a phone screen." % int(s.get("max_words") or 180),
        "Shape: Likely cause \u00b7 Check first (numbered) \u00b7 Parts usually involved \u00b7 This machine before (if relevant) \u00b7 Safety.",
        "EVERY statement of fact must end with its source label, exactly one of:",
        "  [M#] for a manual page given below (use the id exactly as given, e.g. [M2]);",
        "  [HISTORY] for this machine's own records given below;",
        "  [WEB] followed by the link, for an internet source;",
        "  [GENERAL] for your own general knowledge, which is NOT from the manual.",
        "Never cite a manual page that is not in the list below. Never invent a page number.",
        "Part numbers ONLY from a manual page below \u2014 never from general knowledge.",
        "SC code meanings ONLY from a manual page below when a manual exists for this model.",
        "Always include the manual's safety warnings for any procedure: power off, cool-down, high voltage.",
        "Never tell anyone to bypass a safety interlock. For high voltage or anything specialist, say to call the office.",
        "If something would void the warranty, say so plainly.",
        "If you do not have a reliable answer, say so and tell him to call the office. Never guess.",
    ]
    if mode == "manual":
        lines.append("MODE: MANUAL ONLY. Answer ONLY from the manual pages below. If they do not cover it, "
                     "say exactly: 'The manual does not cover this. I don't have a reliable answer. Call the office.' "
                     "Do not use general knowledge or the internet at all.")
    elif mode == "internet":
        lines.append("MODE: INTERNET ONLY. Use web search; every claim carries [WEB] and its link.")
    else:
        lines.append("MODE: MANUAL FIRST. Use the manual pages first. Only for what they do not cover, you may use "
                     "the internet ([WEB] + link) \u2014 kept clearly separate from the manual.")
    if not has_manual:
        lines.append("There is NO manual in the library for this model. Say so, and label everything [GENERAL] or [WEB].")
    if sources:
        lines.append("\nMANUAL PAGES (the only pages you may cite):")
        for i, x in enumerate(sources, 1):
            lines.append("[M%d] %s, page %d:\n%s" % (i, x["title"], x["page"], x["text"]))
    if ctx["history"] or ctx["trend"] or ctx["counter"]:
        lines.append("\nTHIS MACHINE'S HISTORY (cite as [HISTORY]):")
        if ctx["counter"]:
            lines.append("Current counter %s%s." % (ctx["counter"], (", " + ctx["trend"]) if ctx["trend"] else ""))
        lines += ctx["history"]
    if stock:
        lines.append("\nIN THE STORE NOW (cite as [HISTORY]): " + "; ".join(stock))
    return "\n".join(lines)


def attribute(text, sources, links):
    """Turn the labels into what the technician reads, and check every
    manual citation points at a page that was actually given."""
    bad = []
    def m_sub(mt):
        i = int(mt.group(1))
        if 1 <= i <= len(sources):
            x = sources[i - 1]
            return "\U0001F4D8 _%s, p. %d_" % (x["title"], x["page"])
        bad.append(mt.group(0))
        return "\u26A0\uFE0F _(a citation that could not be checked was removed)_"
    out = _re.sub(r"\[M(\d+)\]", m_sub, text)
    out = out.replace("[HISTORY]", "\U0001F527 _this machine's history_")
    out = out.replace("[GENERAL]", "\U0001F4AD _general knowledge \u2014 not from the manual_")
    out = _re.sub(r"\[WEB\]\s*(\(?https?://\S+?\)?)(?=[\s.,;]|$)", lambda m: "\U0001F310 _" + m.group(1).strip("()") + "_", out)
    out = out.replace("[WEB]", "\U0001F310 _internet_")
    used = sorted({int(n) for n in _re.findall(r"\[M(\d+)\]", text) if 1 <= int(n) <= len(sources)})
    cited = [{"kind": "manual", "title": sources[i - 1]["title"], "page": sources[i - 1]["page"],
              "manual": sources[i - 1]["manual"]} for i in used]
    cited += [{"kind": "web", "url": u} for u in links]
    return out, cited, bad


def _usage_today(usage, day, uid=None):
    d = usage.get(day, {})
    if uid:
        return d.get(uid, {"questions": 0, "tokens": 0, "cost": 0.0})
    return {"questions": sum(v["questions"] for v in d.values()),
            "tokens": sum(v["tokens"] for v in d.values()),
            "cost": round(sum(v["cost"] for v in d.values()), 2)}


# ================================================================
#  THE ACTIONS
# ================================================================

def ai_route(cid, req, u):
    act = req.get("action")
    admin = u.get("role") == "admin"; librarian = allowed(cid, u, "manuals")
    s = ai_settings()

    # --- what a phone is allowed to know about the switch: never the key ---
    if act == "ai.status":
        p = AI_PROVIDERS.get(s["provider"]) or {}
        return {"ok": True, "enabled": bool(s["enabled"]),
                "internet": bool(s["enabled"] and s["internet"] and p.get("web")),
                "images": bool(s["enabled"] and p.get("images")),
                "reader": bool(s["reader_for_techs"]), "default_allowed": bool(s["default_allowed"])}

    # --- the library ---
    if act == "man.list":
        ms = sorted(_manuals().values(), key=lambda m: m.get("title", ""))
        if not librarian:
            if not s["reader_for_techs"]:
                return {"ok": False, "msg": "The manual reader is not switched on."}
            mine = _models_on_my_jobs(cid, u)
            ms = [m for m in ms if m.get("active", True) and set(m.get("models") or []) & mine]
        return {"ok": True, "manuals": ms, "types": MANUAL_TYPES}

    if act == "man.coverage":
        if not librarian:
            return {"ok": False, "msg": "For the admin."}
        cat = _catalog(cid)
        have = set(k for m in _manuals().values() if m.get("active", True) for k in (m.get("models") or []))
        return {"ok": True, "missing": sorted([{"id": k, "name": v.get("name")} for k, v in cat.items()
                                               if k not in have], key=lambda x: x["name"] or "")}

    if act in ("man.save", "man.remove"):
        if not librarian:
            return {"ok": False, "msg": "Manuals are managed by the admin."}
        with _ai_lock:
            ms = _manuals()
            m = ms.get(str(req.get("id") or ""))
            if not m:
                return {"ok": False, "msg": "No such manual."}
            if act == "man.remove":
                m["active"] = False; m["removedBy"] = u.get("name"); m["removedAt"] = pk_now().isoformat(timespec="seconds")
            else:
                if not str(req.get("title") or "").strip():
                    return {"ok": False, "msg": "A manual needs its title."}
                if req.get("type") not in MANUAL_TYPES:
                    return {"ok": False, "msg": "Choose the kind of manual."}
                models = [x for x in (req.get("models") or []) if x in _catalog(cid)]
                if not models:
                    return {"ok": False, "msg": "Choose at least one machine model from the catalogue."}
                m.update({"title": str(req["title"]).strip(), "type": req["type"], "models": models,
                          "language": str(req.get("language") or "English"),
                          "version": str(req.get("version") or ""), "notes": str(req.get("notes") or ""),
                          "active": True})
            _ai_store("manuals.json", ms)
        return {"ok": True}

    # --- the settings: the admin only, and the key never comes back ---
    if act == "ai.settings":
        if not admin:
            return {"ok": False, "msg": "AI settings are for the admin."}
        if req.get("set"):
            with _ai_lock:
                s = ai_settings()
                for k in ("provider", "model"):
                    if k in req:
                        v = str(req[k] or "").strip()
                        if k == "provider" and v and v not in AI_PROVIDERS:
                            return {"ok": False, "msg": "Unknown provider."}
                        if s.get(k) != v:
                            s[k] = v; s["tested_at"] = ""
                for k in ("internet", "default_allowed", "reader_for_techs"):
                    if k in req:
                        s[k] = bool(req[k])
                for k in ("daily_limit_rs", "per_tech_limit", "price_in_rs", "price_out_rs", "max_words"):
                    if k in req:
                        v = req[k]
                        s[k] = float(v) if v not in (None, "") and float(v) > 0 else None
                if req.get("key"):
                    k = str(req["key"]).strip()
                    s["key_enc"] = ai_encrypt(k); s["key_last4"] = k[-4:]; s["tested_at"] = ""
                if s["enabled"] and not (s["provider"] and s["key_enc"] and s["model"] and s["daily_limit_rs"]):
                    s["enabled"] = False                  # it cannot stay on without its ceiling
                _ai_save_settings(s)
        out = {k: v for k, v in s.items() if k != "key_enc"}
        out["has_key"] = bool(s["key_enc"])
        out["providers"] = {k: {"label": v["label"], "images": v["images"], "web": v["web"],
                                "models_hint": v["models_hint"]} for k, v in AI_PROVIDERS.items()}
        return {"ok": True, "settings": out}

    if act == "ai.test":
        if not admin:
            return {"ok": False, "msg": "For the admin."}
        p = AI_PROVIDERS.get(s["provider"])
        if not p or not s["key_enc"] or not s["model"]:
            return {"ok": False, "msg": "Choose the provider and the model, and enter the key, first."}
        try:
            r = p["ask"](ai_decrypt(s["key_enc"]), s["model"], "Reply with the single word OK.", "OK?",
                         max_tokens=5)
        except Exception as e:
            return {"ok": False, "msg": "The connection failed: %s" % e}
        with _ai_lock:
            s = ai_settings(); s["tested_at"] = pk_now().isoformat(timespec="seconds"); _ai_save_settings(s)
        return {"ok": True, "msg": "Connected. The provider answered: %s" % r["text"][:40]}

    if act == "ai.estimate":
        if not admin:
            return {"ok": False, "msg": "For the admin."}
        ms = [m for m in _manuals().values() if m.get("active", True)]
        return {"ok": True, "manuals": len(ms), "pages": sum(m.get("searchable", 0) for m in ms),
                "cost_rs": 0, "note": "The library is searched on your own server, not by the AI "
                "provider, so making it searchable costs nothing. Only questions cost money."}

    if act == "ai.switch":
        if not admin:
            return {"ok": False, "msg": "Only the admin turns the assistant on or off."}
        with _ai_lock:
            s = ai_settings()
            if req.get("on"):
                need = []
                if not s["provider"]: need.append("the provider")
                if not s["key_enc"]: need.append("the key")
                if not s["model"]: need.append("the model")
                if not s["daily_limit_rs"]: need.append("the daily spending limit")
                if not s["price_in_rs"] or not s["price_out_rs"]: need.append("the token prices, so spending can be counted")
                if not s["tested_at"]: need.append("a successful Test connection")
                if need:
                    return {"ok": False, "msg": "It cannot be switched on without " + ", ".join(need) + "."}
                s["enabled"] = True
                s["index_built_at"] = s["index_built_at"] or pk_now().isoformat(timespec="seconds")
            else:
                s["enabled"] = False
            _ai_save_settings(s)
        return {"ok": True, "enabled": s["enabled"]}

    if act == "ai.usage":
        if not admin:
            return {"ok": False, "msg": "For the admin."}
        usage = _ai_load("usage.json", {})
        users = store_load(cid).get("users", {})
        month = _today()[:7]
        per = {}
        for day, d in usage.items():
            if day.startswith(month):
                for uid, v in d.items():
                    x = per.setdefault(uid, {"name": (users.get(uid) or {}).get("name", uid),
                                             "questions": 0, "cost": 0.0})
                    x["questions"] += v["questions"]; x["cost"] = round(x["cost"] + v["cost"], 2)
        return {"ok": True, "today": _usage_today(usage, _today()), "month": {
                "questions": sum(v["questions"] for v in per.values()),
                "cost": round(sum(v["cost"] for v in per.values()), 2)}, "people": list(per.values())}

    # --- the conversation kept with the complaint ---
    if act == "ai.convo":
        c = store_load(cid).get("invoices", {}).get(str(req.get("complaint") or ""))
        if not c:
            return {"ok": False, "msg": "No such complaint."}
        if not admin and c.get("tech") != u["id"]:
            return {"ok": False, "msg": "Only your own complaints."}
        conv = _ai_load("convos.json", {}).get(c["id"])
        st = store_load(cid)
        machine = st.get("items", {}).get(c.get("machine")) or {}
        mid = _model_id_for_machine(cid, machine)
        mans = [{"title": m["title"], "type": m.get("type"), "scanned": m.get("scanned")}
                for m in (manuals_for_model(mid) if mid else [])]
        return {"ok": True, "convo": conv, "manuals": mans,
                "machine": " \u00b7 ".join(x for x in (machine.get("model"), machine.get("serial")) if x),
                "place": " \u00b7 ".join(x for x in ((st.get("clients", {}).get(c.get("client")) or {}).get("name"),
                                                      machine.get("dept"), machine.get("place")) if x)}

    # --- asking: every condition checked here, whatever the screen showed ---
    # --- the six draft agents: they write, a person sends ---
    if act == "ai.agents":
        # kaun se agents on hain (admin ke liye)
        return {"ok": True, "agents": AI_AGENTS,
                "enabled": bool(s["enabled"]),
                "on": s.get("agents_on") or {}}

    if act == "ai.agent":
        if u.get("role") not in ("admin", "supervisor"):
            return {"ok": False, "msg": "Drafts are for the office."}
        if not s["enabled"]:
            return {"ok": False, "off": True, "msg": "The AI assistant is switched off."}
        kind = str(req.get("agent") or "")
        spec = next((a for a in AI_AGENTS if a["id"] == kind), None)
        if not spec:
            return {"ok": False, "msg": "No such agent."}
        if (s.get("agents_on") or {}).get(kind) is False:
            return {"ok": False, "msg": "That agent is switched off."}
        brief = str(req.get("brief") or "").strip()
        if not brief:
            return {"ok": False, "msg": "Give the agent something to work from."}
        # daily limit
        usage = _ai_load("usage.json", {})
        day = _today()
        if s["daily_limit_rs"] and _usage_today(usage, day)["cost"] >= s["daily_limit_rs"]:
            _limit_note(cid, day)
            return {"ok": False, "limit": True, "msg": "The assistant has reached today's limit."}
        system = spec["system"]
        try:
            draft = ai_provider_ask(s, system, brief, web=False)
        except Exception as e:
            return {"ok": False, "msg": "The assistant could not answer: " + str(e)}
        # kharcha ginti (rough)
        text = draft.get("text") if isinstance(draft, dict) else str(draft)
        _agent_keep(cid, u, kind, brief, text, day)
        return {"ok": True, "agent": kind, "title": spec["label"], "draft": text}

    if act == "ai.ask":
        if not s["enabled"]:
            return {"ok": False, "off": True, "msg": "The AI assistant is switched off."}
        st = store_load(cid)
        c = st.get("invoices", {}).get(str(req.get("complaint") or ""))
        if not c:
            return {"ok": False, "msg": "No such complaint."}
        if not c.get("aiAllowed"):
            return {"ok": False, "msg": "The assistant is not allowed on this complaint."}
        if c.get("tech") != u["id"]:
            return {"ok": False, "msg": "Only the technician on this complaint can ask."}
        if c.get("status") not in ("started", "parts") or c.get("locked"):
            return {"ok": False, "msg": "Press Start the job first \u2014 the assistant is for use at the machine."}
        question = str(req.get("question") or "").strip()
        if not question:
            return {"ok": False, "msg": "Type the question."}
        usage = _ai_load("usage.json", {})
        day = _today()
        mine = _usage_today(usage, day, u["id"])
        if s["per_tech_limit"] and mine["questions"] >= s["per_tech_limit"]:
            return {"ok": False, "limit": True, "msg": "You have reached today's question limit. Call the office."}
        if s["daily_limit_rs"] and _usage_today(usage, day)["cost"] >= s["daily_limit_rs"]:
            _limit_note(cid, day)
            return {"ok": False, "limit": True, "msg": "The assistant has reached today's limit. Call the office."}

        p = AI_PROVIDERS.get(s["provider"]) or {}
        low = question.lower()
        mode = req.get("mode") if req.get("mode") in ("manual", "internet", "both") else "both"
        if "sirf manual" in low or "manual only" in low or "only manual" in low:
            mode = "manual"                       # said in words: obeyed
        elif "sirf internet" in low or "internet only" in low:
            mode = "internet"
        can_web = bool(s["internet"] and p.get("web"))
        if mode in ("internet", "both") and not can_web:
            mode = "manual" if mode == "both" else mode
            if mode == "internet":
                return {"ok": False, "msg": "Internet search is not switched on."}

        machine = st.get("items", {}).get(c.get("machine")) or {}
        model_id = _model_id_for_machine(cid, machine)
        has_manual = bool(model_id and manuals_for_model(model_id))
        sources = search_pages(model_id, question) if (model_id and mode != "internet") else []
        machine_line = " \u00b7 ".join(x for x in (machine.get("model"), machine.get("serial"),
                                     (st.get("clients", {}).get(c.get("client")) or {}).get("name")) if x)

        if mode == "manual" and not sources:
            # nothing to answer from: said plainly, and nothing spent
            answer = ("The manual does not cover this. I don't have a reliable answer. Call the office."
                      if has_manual else
                      "There is no manual in the library for this model, and you asked for the manual only. "
                      "I don't have a reliable answer. Call the office.")
            return _ai_keep(cid, c, u, mode, question, req.get("photo"), answer, [], [], 0, 0, 0.0, day)

        ctx = machine_context(cid, c)
        stock = stock_for(cid, " ".join(x["text"] for x in sources))
        system = _system_prompt(s, mode, machine_line, sources, ctx, stock, has_manual)
        photo = req.get("photo") if p.get("images") else None
        try:
            r = ai_provider_ask(s, system, question, image=photo, web=(mode in ("internet", "both") and can_web))
        except Exception as e:
            return {"ok": False, "msg": "The assistant could not answer: %s" % e}
        text, cited, bad = attribute(r["text"], sources, r["links"])
        cost = (r["tokens_in"] * (s["price_in_rs"] or 0) + r["tokens_out"] * (s["price_out_rs"] or 0)) / 1e6
        return _ai_keep(cid, c, u, mode, question, photo, text, cited, bad,
                        r["tokens_in"], r["tokens_out"], cost, day)

    return {"ok": False, "msg": "Unknown action"}


def _models_on_my_jobs(cid, u):
    st = store_load(cid)
    out = set()
    for c in st.get("invoices", {}).values():
        if isinstance(c, dict) and c.get("tech") == u["id"] and c.get("status") in ("pending", "started", "parts"):
            mid = _model_id_for_machine(cid, st.get("items", {}).get(c.get("machine")))
            if mid:
                out.add(mid)
    return out


def _limit_note(cid, day):
    with _ai_lock:
        seen = _ai_load("limit_told.json", {})
        if seen.get("day") == day:
            return
        _ai_store("limit_told.json", {"day": day})
    _tell_admins(cid, "AI assistant paused for today", "The daily spending limit was reached.")


def _ai_keep(cid, c, u, mode, question, photo, answer, cited, bad, tin, tout, cost, day):
    """Every message is kept: for review, for cost, and so that when someone
    says 'the AI told me to', the exact answer can be read back."""
    stamp = pk_now().isoformat(timespec="seconds")
    with _ai_lock:
        convos = _ai_load("convos.json", {})
        conv = convos.setdefault(c["id"], {"complaint": c["id"], "tech": u["id"], "started": stamp, "messages": []})
        conv["mode"] = mode
        conv["messages"].append({"role": "technician", "text": question, "photo": bool(photo), "at": stamp})
        conv["messages"].append({"role": "assistant", "text": answer, "sources": cited, "unverified": bad,
                                 "tokens": tin + tout, "cost": round(cost, 4), "at": stamp})
        _ai_store("convos.json", convos)
        if tin or tout:
            usage = _ai_load("usage.json", {})
            x = usage.setdefault(day, {}).setdefault(u["id"], {"questions": 0, "tokens": 0, "cost": 0.0})
            x["questions"] += 1; x["tokens"] += tin + tout; x["cost"] = round(x["cost"] + cost, 4)
            _ai_store("usage.json", usage)
    return {"ok": True, "answer": answer, "sources": cited, "unverified": bad, "mode": mode}


# ---------- uploading a manual, in pieces, so a large file can go on a phone connection ----------

_uploads = {}


def manual_upload(cid, req, u):
    if not allowed(cid, u, "manuals"):
        return {"ok": False, "msg": "Manuals are uploaded by the admin."}
    step = req.get("step")
    if step == "begin":
        if not str(req.get("title") or "").strip() or req.get("type") not in MANUAL_TYPES:
            return {"ok": False, "msg": "A manual needs its title and its kind."}
        models = [x for x in (req.get("models") or []) if x in _catalog(cid)]
        if not models:
            return {"ok": False, "msg": "Choose at least one machine model from the catalogue."}
        mid = secrets.token_hex(6)
        _uploads[mid] = {"meta": {"id": mid, "title": str(req["title"]).strip(), "type": req["type"],
                                  "models": models, "language": str(req.get("language") or "English"),
                                  "version": str(req.get("version") or ""), "notes": str(req.get("notes") or ""),
                                  "active": True, "by": u.get("name")},
                         "parts": {}, "at": time.time()}
        return {"ok": True, "id": mid}
    up = _uploads.get(str(req.get("id") or ""))
    if not up:
        return {"ok": False, "msg": "That upload has expired. Start again."}
    if step == "chunk":
        try:
            up["parts"][int(req["i"])] = _b64p.b64decode(str(req.get("data") or ""))
        except Exception:
            return {"ok": False, "msg": "A piece of the file could not be read."}
        return {"ok": True}
    if step == "finish":
        n = int(req.get("count") or 0)
        if n < 1 or any(i not in up["parts"] for i in range(n)):
            return {"ok": False, "msg": "Some of the file did not arrive. Try again."}
        mid = up["meta"]["id"]
        data = b"".join(up["parts"][i] for i in range(n))
        if not data.startswith(b"%PDF"):
            _uploads.pop(mid, None)
            return {"ok": False, "msg": "That is not a PDF file."}
        with open(_pdf_path(mid), "wb") as fh:
            fh.write(data)
        pages, err = extract_pages(_pdf_path(mid))
        if pages is None:
            os.remove(_pdf_path(mid)); _uploads.pop(mid, None)
            return {"ok": False, "msg": err}
        with open(_pages_path(mid), "w", encoding="utf-8") as fh:
            json.dump(pages, fh)
        readable = sum(1 for p in pages if p["has_text"])
        meta = up["meta"]
        meta.update({"pages": len(pages), "searchable": readable,
                     "scanned": readable < len(pages) * 0.5,
                     "blank_pages": [p["page"] for p in pages if not p["has_text"]][:400],
                     "size": len(data), "at": pk_now().isoformat(timespec="seconds")})
        with _ai_lock:
            ms = _manuals(); ms[mid] = meta; _ai_store("manuals.json", ms)
        _uploads.pop(mid, None)
        warn = ""
        if meta["scanned"]:
            warn = ("This manual appears to be scanned images \u2014 %d of %d pages have no readable text. "
                    "It will not be searchable until text recognition is applied." % (len(pages) - readable, len(pages)))
        return {"ok": True, "manual": meta, "warning": warn}
    return {"ok": False, "msg": "Unknown step"}


def manual_file_allowed(cid, mid, token):
    u = _session(cid, {"token": token})
    if not u:
        return False
    m = _manuals().get(mid)
    if not m or not m.get("active", True):
        return False
    if allowed(cid, u, "manuals"):
        return True
    return bool(ai_settings()["reader_for_techs"]) and bool(set(m.get("models") or []) & _models_on_my_jobs(cid, u))


# ================================================================
#  MY DAY \u2014 each person's own
#  ----------------------------------------------------------------
#  Goals with a target, daily habits, private tasks, weekly reviews
#  and progress photos. Kept here per person and handed to that
#  person alone. A goal marked "shared" is shown to the partners so
#  they can cheer it on; a photo is never shown to anyone else.
# ================================================================

_me_lock = threading.Lock()
ME_KINDS = ("goals", "checks", "habits", "hlog", "ptasks", "reviews")


def _me_dir():
    d = os.path.join(DATA_DIR, "me")
    os.makedirs(os.path.join(d, "photos"), exist_ok=True)
    return d


def _me_path(uid):
    return os.path.join(_me_dir(), _re.sub(r"[^A-Za-z0-9_-]", "", str(uid)) + ".json")


def me_load(uid):
    try:
        with open(_me_path(uid), encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        d = {}
    for k in ME_KINDS:
        d.setdefault(k, {})
    d.setdefault("cheers", [])
    return d


def me_save(uid, d):
    p = _me_path(uid)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(p + ".tmp", p)


def _partners(cid, u):
    return [x for x in store_load(cid).get("users", {}).values()
            if isinstance(x, dict) and x.get("active", True) and x.get("id") != u["id"]
            and x.get("role") in ("admin", "supervisor")]


def me_route(cid, req, u):
    act = req.get("action")
    uid = u["id"]

    if act == "me.get":
        d = me_load(uid)
        shared = []
        if u.get("role") in ("admin", "supervisor"):
            for p in _partners(cid, u):
                pd = me_load(p["id"])
                for g in pd["goals"].values():
                    if g.get("shared") and not g.get("gone"):
                        checks = [dict(c, photo="") for c in pd["checks"].values() if c.get("goal") == g["id"]]
                        shared.append({"owner": p["id"], "ownerName": p.get("name"), "goal": g, "checks": checks,
                                       "cheers": [c for c in pd["cheers"] if c.get("goal") == g["id"]][-20:]})
        return {"ok": True, "mine": {k: d[k] for k in ME_KINDS}, "cheers": d["cheers"][-50:], "shared": shared}

    if act == "me.put":
        # newest write wins, record by record, as the shared data does
        sent = req.get("records") or {}
        with _me_lock:
            d = me_load(uid)
            for k in ME_KINDS:
                for rid, rec in (sent.get(k) or {}).items():
                    if not isinstance(rec, dict):
                        continue
                    have = d[k].get(rid)
                    if have and str(have.get("_at") or "") >= str(rec.get("_at") or ""):
                        continue
                    d[k][rid] = rec
            me_save(uid, d)
        return {"ok": True}

    if act == "me.photo.put":
        data = str(req.get("data") or "")
        m = _re.match(r"data:image/(jpeg|png|webp);base64,(.+)$", data, _re.S)
        if not m:
            return {"ok": False, "msg": "That is not a photo."}
        raw = _b64p.b64decode(m.group(2))
        if len(raw) > 3 * 1024 * 1024:
            return {"ok": False, "msg": "The photo is too large."}
        pid = secrets.token_hex(8)
        folder = os.path.join(_me_dir(), "photos", _re.sub(r"[^A-Za-z0-9_-]", "", uid))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, pid + "." + m.group(1).replace("jpeg", "jpg")), "wb") as fh:
            fh.write(raw)
        return {"ok": True, "id": pid}

    if act == "me.photo.get":
        # a person's photo is theirs alone: looked up only in their own folder
        pid = _re.sub(r"[^a-f0-9]", "", str(req.get("id") or ""))
        folder = os.path.join(_me_dir(), "photos", _re.sub(r"[^A-Za-z0-9_-]", "", uid))
        for ext, mt in (("jpg", "jpeg"), ("png", "png"), ("webp", "webp")):
            f = os.path.join(folder, pid + "." + ext)
            if pid and os.path.exists(f):
                with open(f, "rb") as fh:
                    return {"ok": True, "data": "data:image/%s;base64,%s" % (mt, _b64p.b64encode(fh.read()).decode())}
        return {"ok": False, "msg": "Not found."}

    if act == "me.cheer":
        owner = str(req.get("owner") or "")
        text = str(req.get("text") or "\U0001F44F").strip()[:140]
        if (u.get("role") not in ("admin", "supervisor") or owner == uid
                or owner not in [p["id"] for p in _partners(cid, u)]):
            return {"ok": False, "msg": "Only a partner can cheer a shared goal."}
        with _me_lock:
            d = me_load(owner)
            g = d["goals"].get(str(req.get("goal") or ""))
            if not g or not g.get("shared"):
                return {"ok": False, "msg": "That goal is not shared."}
            d["cheers"].append({"goal": g["id"], "from": u.get("name"), "text": text,
                                "at": pk_now().isoformat(timespec="seconds")})
            d["cheers"] = d["cheers"][-200:]
            me_save(owner, d)
        _note_to(cid, owner, u.get("name") + " cheered your goal", g.get("title", "") + " \u2014 " + text)
        return {"ok": True}

    return {"ok": False, "msg": "Unknown action"}


def _note_to(cid, uid, what, detail):
    stamp = pk_now().isoformat(timespec="seconds")
    nid = "me" + secrets.token_hex(6)
    store_push(cid, {"records": {"notes": {nid: {"id": nid, "to": uid, "what": what, "detail": detail,
                                                  "link": "", "at": stamp, "read": False, "_at": stamp}}}})


def _habit_done(d, hid, day):
    return bool(d["hlog"].get(hid + "|" + day)) and not d["hlog"].get(hid + "|" + day, {}).get("gone")


def me_watch(cid="main"):
    """The three reminders: the morning list, the evening habits still to
    do, and the Sunday review. Each person who keeps a My Day gets them."""
    sent = {}
    while True:
        try:
            now = pk_now(); day = now.strftime("%Y-%m-%d"); hm = now.strftime("%H:%M")
            st = store_load(cid)
            for u in st.get("users", {}).values():
                if not isinstance(u, dict) or not u.get("active", True) or u.get("role") not in ("admin", "supervisor"):
                    continue
                d = me_load(u["id"])
                habits = [h for h in d["habits"].values() if not h.get("gone")]
                if not habits and not d["ptasks"] and not d["goals"]:
                    continue
                shared_tasks = [t for t in st.get("tasks", {}).values() if isinstance(t, dict)
                                and t.get("to") == u["id"] and t.get("state") in ("mine", "offered")
                                and str(t.get("when") or "")[:10] <= day and t.get("kind") != "meeting"]
                own = [t for t in d["ptasks"].values() if not t.get("done") and not t.get("gone")
                       and str(t.get("when") or "")[:10] <= day]
                key = u["id"] + day
                if hm >= "08:30" and sent.get(key + "am") is None:
                    sent[key + "am"] = 1
                    n = len(shared_tasks) + len(own)
                    if n or habits:
                        _note_to(cid, u["id"], "Your day", "%d task(s) and %d habit(s) today" % (n, len(habits)))
                if hm >= "19:30" and sent.get(key + "pm") is None:
                    sent[key + "pm"] = 1
                    left = [h["name"] for h in habits if not _habit_done(d, h["id"], day)]
                    if left:
                        _note_to(cid, u["id"], "Still to do today", ", ".join(left[:4]))
                if now.weekday() == 6 and hm >= "18:00" and sent.get(key + "wk") is None:
                    sent[key + "wk"] = 1
                    if d["goals"]:
                        _note_to(cid, u["id"], "Your weekly review", "How did the week go, and what is next?")
        except Exception as e:
            note("ME WATCH", repr(e))
        time.sleep(300)


# ================================================================
#  VOICE NOTES ON A COMPLAINT
#  ----------------------------------------------------------------
#  A client's voice note, or the office's own, kept here as a file.
#  The complaint carries only its id, so the shared data every phone
#  pulls stays small; the technician's phone fetches the sound when
#  he presses play, with his own session.
# ================================================================

VOICE_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
               "audio/aac": "aac", "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/amr": "amr",
               "audio/opus": "opus", "video/webm": "webm", "audio/3gpp": "3gp"}


def _voice_dir():
    d = os.path.join(DATA_DIR, "voice")
    os.makedirs(d, exist_ok=True)
    return d


def voice_put(cid, req, u):
    data = str(req.get("data") or "")
    m = _re.match(r"data:([a-z0-9/+.\-]+)(?:;[^,]*)?;base64,(.+)$", data, _re.S)
    if not m:
        return {"ok": False, "msg": "That is not a sound file."}
    mime = m.group(1).split(";")[0].lower()
    ext = VOICE_TYPES.get(mime)
    if not ext:
        return {"ok": False, "msg": "That kind of sound file is not taken (%s)." % mime}
    raw = _b64p.b64decode(m.group(2))
    if len(raw) > 6 * 1024 * 1024:
        return {"ok": False, "msg": "The voice note is too long."}
    vid = secrets.token_hex(8)
    with open(os.path.join(_voice_dir(), vid + "." + ext), "wb") as fh:
        fh.write(raw)
    return {"ok": True, "id": vid + "." + ext}


def voice_file(name):
    if not _re.fullmatch(r"[0-9a-f]{16}\.[a-z0-9]{2,4}", str(name or "")):
        return None
    f = os.path.join(_voice_dir(), name)
    return f if os.path.exists(f) else None


# ================================================================
#  TEAM CHAT
#  ----------------------------------------------------------------
#  One-to-one and group conversations between the staff: text,
#  emoji, reactions, replies, voice notes and photos, with sent and
#  read ticks. A message reaches only the members of its
#  conversation \u2014 checked here on every read and every write,
#  never left to a phone.
# ================================================================

_chat_lock = threading.Lock()
CHAT_TEAM = "team"
CHAT_REACTS = ("\U0001F44D", "\u2764\uFE0F", "\U0001F602", "\U0001F62E", "\U0001F622", "\U0001F64F")


# ---------- who is connected ----------
# Every request a signed-in phone makes marks its person as seen. An app in
# front of its user speaks every half minute, so "online" is "seen in the last
# ninety seconds"; putting the app away says so at once.
_presence = {}
_presence_saved = [0.0]


def presence_touch(uid, state="on"):
    _presence[uid] = {"at": pk_now().isoformat(timespec="seconds"), "state": state}
    if time.time() - _presence_saved[0] > 60 or state == "off":
        _presence_saved[0] = time.time()
        try:
            p = os.path.join(DATA_DIR, "presence.json")
            with open(p + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(_presence, fh)
            os.replace(p + ".tmp", p)
        except OSError:
            pass


def presence_of(uid):
    if not _presence:
        try:
            with open(os.path.join(DATA_DIR, "presence.json"), encoding="utf-8") as fh:
                _presence.update(json.load(fh))
        except (FileNotFoundError, ValueError, OSError):
            pass
    p = _presence.get(uid)
    if not p:
        return {"online": False, "at": ""}
    age = (pk_now() - _dt.datetime.fromisoformat(p["at"])).total_seconds()
    return {"online": p.get("state") == "on" and age < 90, "at": p["at"]}


def _chat_dir():
    d = os.path.join(DATA_DIR, "chat")
    os.makedirs(os.path.join(d, "photos"), exist_ok=True)
    return d


def _chat_convs():
    try:
        with open(os.path.join(_chat_dir(), "convs.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _chat_save_convs(c):
    p = os.path.join(_chat_dir(), "convs.json")
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(c, fh)
    os.replace(p + ".tmp", p)


def _chat_file(cid_):
    return os.path.join(_chat_dir(), _re.sub(r"[^A-Za-z0-9_-]", "", cid_) + ".json")


def _chat_load(cid_):
    try:
        with open(_chat_file(cid_), encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        d = {}
    d.setdefault("msgs", []); d.setdefault("read", {})
    return d


def _chat_store(cid_, d):
    p = _chat_file(cid_)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(p + ".tmp", p)


def _staff(cid):
    return {x["id"]: x for x in store_load(cid).get("users", {}).values()
            if isinstance(x, dict) and x.get("active", True) and x.get("role") != "operator"}


def _conv_members(cid, conv):
    """The team group is everyone on the staff, as it stands today."""
    if conv["id"] == CHAT_TEAM:
        return list(_staff(cid).keys())
    return list(conv.get("members") or [])


def _conv_for(cid, u, conv_id):
    convs = _chat_convs()
    if conv_id == CHAT_TEAM and CHAT_TEAM not in convs:
        convs[CHAT_TEAM] = {"id": CHAT_TEAM, "kind": "group", "name": "Paragon Team", "members": [], "by": "", "at": ""}
        _chat_save_convs(convs)
    conv = convs.get(conv_id)
    if not conv or u["id"] not in _conv_members(cid, conv):
        return None
    return conv


def _conv_title(cid, conv, uid):
    if conv["kind"] == "dm":
        staff = _staff(cid)
        other = [m for m in conv["members"] if m != uid]
        return (staff.get(other[0]) or {}).get("name", "Someone") if other else "Me"
    return conv.get("name") or "Group"


def _preview(m):
    if m.get("gone"):
        return "Message deleted"
    if m.get("voice"):
        return "\U0001F3A4 Voice note"
    if m.get("photo"):
        return "\U0001F4F7 Photo" + ((" \u2014 " + m["text"][:40]) if m.get("text") else "")
    if m.get("file"):
        return "\U0001F4C4 " + m["file"].get("name", "File")
    return (m.get("text") or "")[:80]


def chat_route(cid, req, u):
    act = req.get("action"); uid = u["id"]
    if u.get("role") == "operator":
        return {"ok": False, "msg": "Chat is for the staff."}
    aset = admin_settings()
    if not aset["chat_on"]:
        return {"ok": False, "off": True, "msg": "Chat is switched off by the admin."}
    stamp = pk_now().isoformat(timespec="milliseconds")

    # ---- in-app call signaling (WebRTC) ----
    if act == "call.signal":
        to = str(req.get("to") or "")
        if not to:
            return {"ok": False, "msg": "Whom to call?"}
        box = _chat_load("calls.json", {})
        inbox = box.setdefault(to, [])
        inbox.append({"from": uid, "fromName": u.get("name"),
                      "kind": str(req.get("kind") or ""),
                      "data": req.get("data"), "at": stamp})
        box[to] = inbox[-30:]
        _chat_store("calls.json", box)
        return {"ok": True}

    if act == "call.poll":
        box = _chat_load("calls.json", {})
        mine = box.get(uid, [])
        since = str(req.get("since") or "")
        out = [s for s in mine if str(s.get("at", "")) > since]
        return {"ok": True, "signals": out,
                "now": pk_now().isoformat(timespec="milliseconds")}

    if act == "call.clear":
        box = _chat_load("calls.json", {})
        box[uid] = []
        _chat_store("calls.json", box)
        return {"ok": True}

    if act == "chat.list":
        _conv_for(cid, u, CHAT_TEAM)                      # the team group always exists
        staff = _staff(cid)
        out = []
        for conv in _chat_convs().values():
            members = _conv_members(cid, conv)
            if uid not in members:
                continue
            d = _chat_load(conv["id"])
            last = d["msgs"][-1] if d["msgs"] else None
            # this phone has now received everything up to the last message: grey double tick
            if last and d.setdefault("deliv", {}).get(uid, "") < last["at"]:
                with _chat_lock:
                    d2 = _chat_load(conv["id"]); d2.setdefault("deliv", {})[uid] = last["at"]; _chat_store(conv["id"], d2)
                d = d2
            seen = d["read"].get(uid, "")
            unread = sum(1 for m in d["msgs"] if m["from"] != uid and m["at"] > seen and not m.get("gone"))
            out.append({"id": conv["id"], "kind": conv["kind"], "title": _conv_title(cid, conv, uid),
                        "members": [{"id": m, "name": (staff.get(m) or {}).get("name", "?")} for m in members],
                        "last": {"text": _preview(last), "at": last["at"],
                                 "from": (staff.get(last["from"]) or {}).get("name", "")} if last else None,
                        "unread": unread})
        out.sort(key=lambda c: (c["last"] or {}).get("at", ""), reverse=True)
        for c in out:
            if c["kind"] == "dm":
                other = [m["id"] for m in c["members"] if m["id"] != uid]
                c["presence"] = presence_of(other[0]) if other else None
        return {"ok": True, "convs": out, "reacts": list(CHAT_REACTS),
                "flags": _phone_flags(cid, uid, "on"),
                "people": [{"id": k, "name": v.get("name"), "role": v.get("role")} for k, v in staff.items() if k != uid]}

    if act == "presence.bye":
        presence_touch(uid, "off")
        return {"ok": True}

    if act == "presence.team":
        if u.get("role") not in ("admin", "supervisor"):
            return {"ok": False, "msg": "For the office."}
        staff = _staff(cid)
        return {"ok": True, "team": sorted([dict(presence_of(k), id=k, name=v.get("name"), role=v.get("role"))
                                            for k, v in staff.items()], key=lambda x: (not x["online"], x["name"] or ""))}

    if act == "chat.new":
        staff = _staff(cid)
        with _chat_lock:
            convs = _chat_convs()
            if req.get("kind") == "dm":
                other = str(req.get("with") or "")
                if other not in staff or other == uid:
                    return {"ok": False, "msg": "Choose someone on the staff."}
                pair = sorted([uid, other])
                for c in convs.values():
                    if c["kind"] == "dm" and sorted(c["members"]) == pair:
                        return {"ok": True, "id": c["id"]}
                nid = "dm" + secrets.token_hex(6)
                convs[nid] = {"id": nid, "kind": "dm", "members": pair, "by": uid, "at": stamp}
            else:
                if u.get("role") not in ("admin", "supervisor"):
                    return {"ok": False, "msg": "Groups are started by the office."}
                name = str(req.get("name") or "").strip()[:60]
                members = sorted(set([m for m in (req.get("members") or []) if m in staff] + [uid]))
                if not name or len(members) < 2:
                    return {"ok": False, "msg": "A group needs a name and at least one other person."}
                nid = "gr" + secrets.token_hex(6)
                convs[nid] = {"id": nid, "kind": "group", "name": name, "members": members, "by": uid, "at": stamp}
            _chat_save_convs(convs)
        return {"ok": True, "id": nid}

    reading = (u.get("role") == "admin" and aset["chat_read"] and act in ("chat.open", "chat.file", "chat.photo"))
    conv = _conv_for(cid, u, str(req.get("conv") or ""))
    if not conv and reading:
        conv = _chat_convs().get(str(req.get("conv") or ""))
    if not conv:
        return {"ok": False, "msg": "That conversation is not yours."}

    if act == "chat.open":
        d = _chat_load(conv["id"])
        after = str(req.get("after") or "")
        msgs = [m for m in d["msgs"] if not after or m["at"] > after or m.get("edited", "") > after][-300:]
        staff = _staff(cid)
        if d["msgs"] and d.setdefault("deliv", {}).get(uid, "") < d["msgs"][-1]["at"]:
            with _chat_lock:
                d2 = _chat_load(conv["id"]); d2.setdefault("deliv", {})[uid] = d["msgs"][-1]["at"]; _chat_store(conv["id"], d2)
            d = d2
        others = [m for m in _conv_members(cid, conv) if m != uid]
        return {"ok": True, "msgs": msgs, "read": d["read"], "deliv": d.get("deliv", {}),
                "presence": {m: presence_of(m) for m in others},
                "title": _conv_title(cid, conv, uid), "kind": conv["kind"],
                "members": [{"id": m, "name": (staff.get(m) or {}).get("name", "?")} for m in _conv_members(cid, conv)]}

    if act == "chat.send":
        text = str(req.get("text") or "").strip()[:4000]
        voice = str(req.get("voice") or "")
        photo = ""
        if req.get("photo"):
            mt = _re.match(r"data:image/(jpeg|png|webp);base64,(.+)$", str(req["photo"]), _re.S)
            if not mt:
                return {"ok": False, "msg": "That is not a photo."}
            photo = secrets.token_hex(8) + "." + mt.group(1).replace("jpeg", "jpg")
            with open(os.path.join(_chat_dir(), "photos", photo), "wb") as fh:
                fh.write(_b64p.b64decode(mt.group(2)))
        if voice and not voice_file(voice):
            return {"ok": False, "msg": "That voice note did not arrive."}
        # a document: PDF, Word, Excel, anything \u2014 kept as a file, sent to the members only
        fobj = None
        if req.get("file"):
            f = req["file"] if isinstance(req["file"], dict) else {}
            mf = _re.match(r"data:([^;,]*)(?:;[^,]*)?;base64,(.+)$", str(f.get("data") or ""), _re.S)
            name = _re.sub(r"[\\/:*?\"<>|\r\n]+", "_", str(f.get("name") or "file"))[:120] or "file"
            if not mf:
                return {"ok": False, "msg": "That file could not be read."}
            raw = _b64p.b64decode(mf.group(2))
            if len(raw) > 5 * 1024 * 1024:
                return {"ok": False, "msg": "Files up to 5 MB can be sent."}
            ext = (name.rsplit(".", 1)[1].lower() if "." in name else "bin")[:8]
            if not _re.fullmatch(r"[a-z0-9]{1,8}", ext) or ext in ("exe", "bat", "cmd", "com", "msi", "apk", "js", "vbs", "scr", "ps1", "sh"):
                return {"ok": False, "msg": "That kind of file is not sent (programs and scripts are not allowed)."}
            fid = secrets.token_hex(8) + "." + ext
            os.makedirs(os.path.join(_chat_dir(), "files"), exist_ok=True)
            with open(os.path.join(_chat_dir(), "files", fid), "wb") as fh:
                fh.write(raw)
            fobj = {"id": fid, "name": name, "size": len(raw), "mime": mf.group(1) or "application/octet-stream"}
        if not (text or voice or photo or fobj):
            return {"ok": False, "msg": "Nothing to send."}
        m = {"id": secrets.token_hex(6), "from": uid, "text": text, "voice": voice, "secs": req.get("secs"),
             "photo": photo, "file": fobj, "reply": str(req.get("reply") or ""), "at": stamp, "reacts": {}}
        with _chat_lock:
            d = _chat_load(conv["id"])
            # the clock must move forward, so "after" never misses a message sent in the same second
            if d["msgs"] and d["msgs"][-1]["at"] >= m["at"]:
                m["at"] = d["msgs"][-1]["at"] + "." + str(len(d["msgs"]))
            d["msgs"].append(m); d["read"][uid] = m["at"]; d.setdefault("deliv", {})[uid] = m["at"]
            _chat_store(conv["id"], d)
        title = _conv_title(cid, conv, uid) if conv["kind"] == "group" else ""
        for other in _conv_members(cid, conv):
            if other != uid:
                # the note travels with the shared data, so it says who, never what
                _note_to_chat(cid, other, (u.get("name") or "") + ((" \u00b7 " + title) if title else ""),
                              "sent a voice note" if m.get("voice") else "sent a photo" if m.get("photo") else "sent you a message",
                              conv["id"])
        return {"ok": True, "msg": m}

    if act == "chat.react":
        emo = str(req.get("emoji") or "")
        if emo not in CHAT_REACTS:
            return {"ok": False, "msg": "Not a reaction."}
        with _chat_lock:
            d = _chat_load(conv["id"])
            for m in d["msgs"]:
                if m["id"] == req.get("msg"):
                    r = m.setdefault("reacts", {})
                    for k in list(r.keys()):                       # one reaction per person
                        if uid in r[k] and k != emo:
                            r[k].remove(uid)
                    lst = r.setdefault(emo, [])
                    if uid in lst: lst.remove(uid)
                    else: lst.append(uid)
                    r = {k: v for k, v in r.items() if v}; m["reacts"] = r
                    m["edited"] = pk_now().isoformat(timespec="milliseconds")
                    _chat_store(conv["id"], d)
                    return {"ok": True, "msg": m}
        return {"ok": False, "msg": "No such message."}

    if act == "chat.read":
        with _chat_lock:
            d = _chat_load(conv["id"])
            upto = str(req.get("upto") or "")
            if upto > d["read"].get(uid, ""):
                d["read"][uid] = upto
                _chat_store(conv["id"], d)
        return {"ok": True}

    # Deleting and editing belong to the admin alone \u2014 any message, anyone's.
    # Nobody else can take back or change what was said, not even their own.
    if act == "chat.edit":
        if u.get("role") != "admin":
            return {"ok": False, "msg": "Only an admin can edit messages."}
        text = str(req.get("text") or "").strip()[:4000]
        if not text:
            return {"ok": False, "msg": "A message cannot be edited to nothing \u2014 delete it instead."}
        with _chat_lock:
            d = _chat_load(conv["id"])
            for m in d["msgs"]:
                if m["id"] == req.get("msg"):
                    if m.get("gone"):
                        return {"ok": False, "msg": "That message was deleted."}
                    if m.get("text") == text:
                        return {"ok": True, "msg": m}
                    m.setdefault("history", []).append({"text": m.get("text", ""), "at": m.get("editedAt") or m["at"]})
                    stamp2 = pk_now().isoformat(timespec="milliseconds")
                    m.update({"text": text, "editedAt": stamp2, "editedBy": u.get("name"), "edited": stamp2})
                    _chat_store(conv["id"], d)
                    return {"ok": True, "msg": m}
        return {"ok": False, "msg": "No such message."}

    if act == "chat.delete":
        if u.get("role") != "admin":
            return {"ok": False, "msg": "Only an admin can delete messages."}
        with _chat_lock:
            d = _chat_load(conv["id"])
            for m in d["msgs"]:
                if m["id"] == req.get("msg"):
                    m["deletedBy"] = u.get("name") if m["from"] != uid else ""
                    m.update({"gone": True, "text": "", "voice": "", "photo": "", "file": None, "reacts": {}, "history": [],
                              "edited": pk_now().isoformat(timespec="milliseconds")})
                    _chat_store(conv["id"], d)
                    return {"ok": True, "msg": m}
        return {"ok": False, "msg": "No such message."}

    if act == "chat.file":
        fid = str(req.get("id") or "")
        d = _chat_load(conv["id"])
        m = [x for x in d["msgs"] if (x.get("file") or {}).get("id") == fid]
        f = os.path.join(_chat_dir(), "files", fid)
        if not m or not _re.fullmatch(r"[0-9a-f]{16}\.[a-z0-9]{1,8}", fid) or not os.path.exists(f):
            return {"ok": False, "msg": "Not in this conversation."}
        with open(f, "rb") as fh:
            return {"ok": True, "name": m[0]["file"]["name"], "mime": m[0]["file"]["mime"],
                    "data": _b64p.b64encode(fh.read()).decode()}

    if act == "chat.photo":
        name = str(req.get("id") or "")
        d = _chat_load(conv["id"])
        if not any(m.get("photo") == name for m in d["msgs"]):
            return {"ok": False, "msg": "Not in this conversation."}
        f = os.path.join(_chat_dir(), "photos", name)
        if not _re.fullmatch(r"[0-9a-f]{16}\.(jpg|png|webp)", name) or not os.path.exists(f):
            return {"ok": False, "msg": "Not found."}
        with open(f, "rb") as fh:
            ext = name.rsplit(".", 1)[1].replace("jpg", "jpeg")
            return {"ok": True, "data": "data:image/%s;base64,%s" % (ext, _b64p.b64encode(fh.read()).decode())}

    return {"ok": False, "msg": "Unknown action"}


def _note_to_chat(cid, uid, what, detail, conv_id):
    stamp = pk_now().isoformat(timespec="seconds")
    nid = "ch" + secrets.token_hex(6)
    store_push(cid, {"records": {"notes": {nid: {"id": nid, "to": uid, "what": what, "detail": detail,
                                                  "link": "chat:" + conv_id, "kind": "chat",
                                                  "at": stamp, "read": False, "_at": stamp}}}})


# ================================================================
#  ADMIN SETTINGS AND LOCATION
#  ----------------------------------------------------------------
#  One place the admin turns things on and off. The server reads
#  these on every request, so a phone cannot go around them. The
#  defaults are the safe ones: chat on, admin cannot read, no
#  end-to-end, no location \u2014 nothing private happens until chosen.
# ================================================================

ADMIN_DEFAULTS = {"chat_on": True, "chat_read": False, "chat_e2e": False, "loc_on": False}
_loc_ask = {}
_loc_now = {}


def admin_settings():
    try:
        with open(os.path.join(DATA_DIR, "admin.json"), encoding="utf-8") as fh:
            s = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        s = {}
    out = dict(ADMIN_DEFAULTS); out.update({k: v for k, v in s.items() if k in ADMIN_DEFAULTS})
    if out["chat_e2e"]:
        out["chat_read"] = False
    return out


def _admin_save(s):
    q = os.path.join(DATA_DIR, "admin.json")
    with open(q + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(s, fh)
    os.replace(q + ".tmp", q)


def _phone_flags(cid, uid, state):
    s = admin_settings()
    want = _loc_ask.get(uid, 0)
    return {"chat_on": s["chat_on"], "loc_on": s["loc_on"],
            "loc_wanted": bool(state == "on" and want and time.time() - want < 120)}


def admin_route(cid, req, u):
    if u.get("role") != "admin":
        return {"ok": False, "msg": "Admin settings are for the admin."}
    if req.get("set") and isinstance(req.get("settings"), dict):
        s = admin_settings()
        for k in ADMIN_DEFAULTS:
            if k in req["settings"]:
                s[k] = bool(req["settings"][k])
        if s["chat_e2e"]:
            s["chat_read"] = False
        _admin_save(s)
    return {"ok": True, "settings": admin_settings()}


def loc_route(cid, req, u):
    act = req.get("action"); s = admin_settings()
    if act == "loc.mine":
        if not s["loc_on"]:
            return {"ok": True, "off": True}
        lat, lng = req.get("lat"), req.get("lng")
        if lat is not None and lng is not None:
            _loc_now[u["id"]] = {"lat": float(lat), "lng": float(lng), "acc": float(req.get("acc") or 0),
                                 "at": pk_now().isoformat(timespec="seconds")}
        want = _loc_ask.pop(u["id"], 0)
        return {"ok": True, "wanted": bool(want and time.time() - want < 120)}
    if u.get("role") != "admin":
        return {"ok": False, "msg": "For the admin."}
    if act == "loc.ask":
        if not s["loc_on"]:
            return {"ok": False, "off": True, "msg": "Turn location on in Admin Settings first."}
        who = str(req.get("who") or "")
        if who:
            _loc_ask[who] = time.time()
        return {"ok": True}
    if act == "loc.team":
        if not s["loc_on"]:
            return {"ok": False, "off": True, "msg": "Location is switched off."}
        staff = {x["id"]: x for x in store_load(cid).get("users", {}).values()
                 if isinstance(x, dict) and x.get("active", True) and x.get("role") != "operator"}
        out = []
        for uid, x in staff.items():
            if uid == u["id"]:
                continue
            loc = _loc_now.get(uid)
            fresh = loc and (pk_now() - _dt.datetime.fromisoformat(loc["at"])).total_seconds() < 300
            out.append({"id": uid, "name": x.get("name"), "role": x.get("role"),
                        "lat": loc["lat"] if loc else None, "lng": loc["lng"] if loc else None,
                        "acc": loc.get("acc") if loc else None, "at": loc["at"] if loc else "",
                        "fresh": bool(fresh)})
        return {"ok": True, "team": sorted(out, key=lambda z: (not z["fresh"], z["name"] or ""))}
    return {"ok": False, "msg": "Unknown action"}


# ================================================================
#  WEB PUSH
#  ----------------------------------------------------------------
#  For iPhones, and for any phone using the app in a browser rather
#  than the APK. Apple and Google each run a push service; this
#  server signs a short note saying who it is, sends an empty push
#  to the phone's address, and the phone asks for its own notes.
# ================================================================

import hashlib as _hl, hmac as _hm, base64 as _b64p, threading as _th
import urllib.parse as _up

# the P-256 curve, as the standard defines it
_P  = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_A  = _P - 3
_B  = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_N  = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
_GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5


def _inv(a, m):
    return pow(a, m - 2, m)


def _add(p1, p2):
    if p1 is None: return p2
    if p2 is None: return p1
    x1, y1 = p1; x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        l = (3 * x1 * x1 + _A) * _inv(2 * y1, _P) % _P
    else:
        l = (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    x3 = (l * l - x1 - x2) % _P
    return (x3, (l * (x1 - x3) - y1) % _P)


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def _i2b(n, size=32):
    return n.to_bytes(size, "big")


def _rfc6979_k(d, h):
    """The per-signature number, made from the key and the message rather
    than from randomness, so a weak random source can never leak the key."""
    x = _i2b(d); hb = _i2b(int.from_bytes(h, "big") % _N)
    V = b"\x01" * 32; K = b"\x00" * 32
    K = _hm.new(K, V + b"\x00" + x + hb, _hl.sha256).digest()
    V = _hm.new(K, V, _hl.sha256).digest()
    K = _hm.new(K, V + b"\x01" + x + hb, _hl.sha256).digest()
    V = _hm.new(K, V, _hl.sha256).digest()
    while True:
        V = _hm.new(K, V, _hl.sha256).digest()
        k = int.from_bytes(V, "big")
        if 1 <= k < _N:
            return k
        K = _hm.new(K, V + b"\x00", _hl.sha256).digest()
        V = _hm.new(K, V, _hl.sha256).digest()


def es256_sign(d, msg):
    """ECDSA over SHA-256, the signature the push services ask for.
    Returned as the 64 bytes r||s that a web token expects."""
    h = _hl.sha256(msg).digest()
    z = int.from_bytes(h, "big")
    while True:
        k = _rfc6979_k(d, h)
        r = _mul(k, (_GX, _GY))[0] % _N
        s = _inv(k, _N) * (z + r * d) % _N
        if r and s:
            if s > _N // 2:          # the low form, which every checker accepts
                s = _N - s
            return _i2b(r) + _i2b(s)
        h = _hl.sha256(h).digest()


def _b64u(b):
    return _b64p.urlsafe_b64encode(b).rstrip(b"=").decode()


def _push_path(cid):
    return os.path.join(DATA_DIR, str(cid or "main") + "-push.json")


_push_lock = threading.Lock()


def push_load(cid):
    try:
        with open(_push_path(cid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (FileNotFoundError, ValueError, OSError):
        d = {}
    d.setdefault("subs", {})
    if "vapid" not in d:
        # This server's own push identity, made once and kept. Phones
        # subscribe against its public half; losing it means every phone
        # has to switch notifications on again, so it lives beside the data.
        seed = secrets.token_bytes(32)
        dkey = int.from_bytes(seed, "big") % (_N - 1) + 1
        x, y = _mul(dkey, (_GX, _GY))
        d["vapid"] = {"d": str(dkey), "pub": _b64u(b"\x04" + _i2b(x) + _i2b(y))}
        push_save(cid, d)
    return d


def push_save(cid, d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _push_path(cid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, _push_path(cid))


def push_key(cid, req):
    return {"ok": True, "key": push_load(cid)["vapid"]["pub"],
            "now": datetime.datetime.now().isoformat(timespec="seconds")}


def push_subscribe(cid, req):
    sub = req.get("sub") or {}
    who = str(req.get("user") or "")
    ep = str(sub.get("endpoint") or "")
    if not who or not ep.startswith("https://"):
        return {"ok": False, "msg": "Needs a person and a push address"}
    with _push_lock:
        d = push_load(cid)
        d["subs"][ep] = {"user": who, "endpoint": ep,
                         "muted": str(req.get("mutedUntil") or ""),
                         "at": datetime.datetime.now().isoformat(timespec="seconds")}
        push_save(cid, d)
    note("%s push on for %s" % (cid, who))
    return {"ok": True}


def push_unsubscribe(cid, req):
    ep = str(req.get("endpoint") or "")
    with _push_lock:
        d = push_load(cid)
        gone = d["subs"].pop(ep, None) is not None
        push_save(cid, d)
    return {"ok": True, "removed": gone}


def push_send_one(cid, vapid, ep):
    """One empty push to one phone. Returns the status the service gave."""
    u = _up.urlparse(ep)
    aud = "%s://%s" % (u.scheme, u.netloc)
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"},
                            separators=(",", ":")).encode())
    body = _b64u(json.dumps({"aud": aud,
                             "exp": int(time.time()) + 12 * 3600,
                             "sub": PUSH_CONTACT}, separators=(",", ":")).encode())
    sig = _b64u(es256_sign(int(vapid["d"]), (head + "." + body).encode()))
    req = urllib.request.Request(ep, data=b"", method="POST", headers={
        "Authorization": "vapid t=%s.%s.%s, k=%s" % (head, body, sig, vapid["pub"]),
        "TTL": "86400",
        "Urgency": "high",
        "Content-Length": "0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return res.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def push_for(cid, user):
    """Wake every phone this person has switched notifications on for.
    Runs off to one side, so saving a note never waits on Apple."""
    def go():
        with _push_lock:
            d = push_load(cid)
            targets = [s for s in d["subs"].values() if s.get("user") == user]
            vapid = d["vapid"]
        now_s = datetime.datetime.now().isoformat(timespec="seconds")
        dead = []
        for s in targets:
            if s.get("muted") and s["muted"] > now_s:
                continue                      # asked for quiet
            st = push_send_one(cid, vapid, s["endpoint"])
            if st in (404, 410):
                dead.append(s["endpoint"])    # the phone switched it off
        if dead:
            with _push_lock:
                d = push_load(cid)
                for ep in dead:
                    d["subs"].pop(ep, None)
                push_save(cid, d)
    _th.Thread(target=go, daemon=True).start()


# ================================================================
#  THE APP, SERVED SO A PHONE CAN INSTALL IT
#  ----------------------------------------------------------------
#  Opened from a file, the app works but a phone will not offer to
#  install it. Served from here \u2014 with a manifest, icons and a
#  service worker \u2014 Android offers "Install app": its own icon in
#  the app drawer, full screen, no address bar, and it opens with no
#  signal at all.
#
#  One condition the browser sets, not us: it must come over HTTPS,
#  or from this computer itself. A phone on the office wifi reaching
#  http://192.168.1.5 will not be offered the install. See the guide.
# ================================================================

import base64 as _b64

ICONS = {'192': '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', '512': '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', '512m': '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', '180': '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'}

APP_NAME = "Paragon"


def _find_app():
    """The app file, wherever it was put. Beside this server first, then
    where the zip puts it, so either layout works without editing."""
    here = os.path.dirname(os.path.abspath(__file__)) or "."
    for p in (os.path.join(here, "Paragon Business App.html"),
              os.path.join(here, "app.html"),
              os.path.join(here, "..", "1 - App", "Paragon Business App.html")):
        if os.path.exists(p):
            return p
    return None


def manifest():
    return {
        "name": "Paragon Business App",
        "short_name": APP_NAME,
        "description": "Complaints, parts, quotations, contracts and tasks.",
        "start_url": "/app",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#1C2430",
        "theme_color": "#1C2430",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/icon-512m.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }


# The service worker keeps a copy of the app on the phone, so it opens with
# no signal. It never touches the data calls: those are POSTs, and a stale
# answer to "what jobs are there" would be worse than none.
SW_JS = r"""
var CACHE = 'paragon-app-v2';
var CFG = 'paragon-cfg';           /* who is signed in, kept apart from the app copy */
var SHELL = ['/app', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(SHELL); })
    .then(function(){ return self.skipWaiting(); }));
});

self.addEventListener('activate', function(e){
  e.waitUntil(caches.keys().then(function(keys){
    return Promise.all(keys.filter(function(k){ return k !== CACHE && k !== CFG; })
      .map(function(k){ return caches.delete(k); }));
  }).then(function(){ return self.clients.claim(); }));
});

self.addEventListener('fetch', function(e){
  if(e.request.method !== 'GET') return;          /* data is never cached */
  var url = new URL(e.request.url);
  if(url.origin !== location.origin) return;
  if(url.pathname === '/app' || url.pathname === '/'){
    e.respondWith(
      fetch(e.request).then(function(res){
        var copy = res.clone();
        caches.open(CACHE).then(function(c){ c.put('/app', copy); });
        return res;
      }).catch(function(){ return caches.match('/app'); })
    );
    return;
  }
  e.respondWith(caches.match(e.request).then(function(hit){
    return hit || fetch(e.request);
  }));
});

/* ---------- a push arrived ----------
   It carries nothing. It means "ask". The notes are fetched from the server
   and shown, the same notes the bell inside the app shows. */
function readJSON(path, fallback){
  return caches.open(CFG).then(function(c){ return c.match(path); })
    .then(function(r){ return r ? r.json() : fallback; })
    .catch(function(){ return fallback; });
}
function writeJSON(path, obj){
  return caches.open(CFG).then(function(c){
    return c.put(path, new Response(JSON.stringify(obj))); });
}

function showNew(){
  return Promise.all([readJSON('/cfg', null), readJSON('/state', { since:'', shown:[] })])
  .then(function(v){
    var cfg = v[0], st = v[1], shown = 0;
    var ask = (cfg && cfg.user)
      ? fetch('/', { method:'POST', headers:{'Content-Type':'text/plain'},
          body: JSON.stringify({ key:cfg.key, action:'notes', to:cfg.user, since:st.since }) })
          .then(function(r){ return r.json(); })
      : Promise.resolve({ ok:false });
    return ask.then(function(d){
      if(!d || !d.ok) return;
      var jobs = [];
      (d.notes || []).forEach(function(n){
        if(st.shown.indexOf(n.id) > -1) return;
        st.shown.push(n.id); shown++;
        jobs.push(self.registration.showNotification(n.what || 'Paragon', {
          body: n.detail || '', tag: n.id, icon:'/icon-192.png', badge:'/icon-192.png',
          data: { link: n.link || '' } }));
      });
      st.since = d.now || st.since;
      st.shown = st.shown.slice(-300);
      return Promise.all(jobs).then(function(){ return writeJSON('/state', st); });
    }).catch(function(){}).then(function(){
      /* An iPhone insists that every push shows something, and quietly
         stops delivering them to an app that does not. */
      if(!shown)
        return self.registration.showNotification('Paragon', {
          body:'Something new is waiting for you.', tag:'paragon-new',
          icon:'/icon-192.png', data:{ link:'' } });
    });
  });
}

self.addEventListener('push', function(e){ e.waitUntil(showNew()); });

/* tapping one opens the job it is about */
self.addEventListener('notificationclick', function(e){
  e.notification.close();
  var link = (e.notification.data && e.notification.data.link) || '';
  var url = '/app' + (link ? '?link=' + encodeURIComponent(link) : '');
  e.waitUntil(self.clients.matchAll({ type:'window', includeUncontrolled:true })
    .then(function(list){
      for(var i = 0; i < list.length; i++){
        if('focus' in list[i]){
          list[i].postMessage({ open: link });
          return list[i].focus();
        }
      }
      return self.clients.openWindow(url);
    }));
});
"""


# ================================================================
#  WHAT A REQUEST CAN ASK FOR
# ================================================================

BUILD = "2026-09-20 sharing + counters + console + QR complaints + installable app + every kind shared + phone notes + iPhone push + admin join + attendance + salary + receivables + manuals and assistant (off)"


def handle(req):
    if SHARED_SECRET and req.get("key") != SHARED_SECRET:
        return {"ok": False, "msg": "Shared secret does not match"}

    action = req.get("action", "")
    cid = "main"

    if action == "ping":
        return {"ok": True, "msg": "Paragon server is running", "build": BUILD}

    # one set of data for every phone
    if action == "number":    return next_number(cid, req)
    if action == "push":      return store_push(cid, req)
    if action == "pull":      return store_pull(cid, req)
    if action == "forget":    return store_forget(cid, req)
    if action == "stats":     return store_stats(cid)
    if action == "notes":     return notes_for(cid, req)
    if action == "machine.report":  return machine_report(cid, req)
    if action == "machine.beat":
        # agent zinda hai, counter/toner update (bina complaint ke)
        return machine_beat(cid, req)
    if action == "pushkey":   return push_key(cid, req)
    if action == "subscribe": return push_subscribe(cid, req)
    if action == "unsubscribe": return push_unsubscribe(cid, req)

    # counters
    if action == "meter":     return meter_keep(cid, req)
    if action == "meters":    return meter_list(cid, req)
    if action == "meterlog":  return meter_history(cid, req)
    if action == "readnow":   return meter_read_now(cid, req)

    # the desk
    if action == "overview":   return console_overview(cid)
    if action == "machines":   return console_machines(cid)
    if action == "readall":    return console_read_all(cid, req)
    if action == "assign":     return console_assign(cid, req)
    if action == "setmachine": return console_set_machine(cid, req)
    if action == "rental":     return console_rental_due(cid, req)

    return {"ok": False, "msg": "Unknown action: " + str(action)}


class Handler(BaseHTTPRequestHandler):
    server_version = "ParagonServer/1.0"

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
        if path in ("/app", "/"):
            f = _find_app()
            if not f:
                self._send({"ok": False, "msg": "The app file is not beside the "
                            "server. Put Paragon Business App.html in the same "
                            "folder as paragon_server.py."}, 404)
                return
            with open(f, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/voice/"):
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            if not _session("main", {"token": (qs.get("t") or [""])[0]}):
                return self._send({"ok": False, "msg": "Not allowed."}, 403)
            f = voice_file(path[len("/voice/"):])
            if not f:
                return self._send({"ok": False, "msg": "Not found."}, 404)
            ext = f.rsplit(".", 1)[1]
            mime = {v: k for k, v in VOICE_TYPES.items() if not k.startswith("video")}.get(ext, "application/octet-stream")
            with open(f, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "none")
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/manual/") and path.endswith(".pdf"):
            mid = path[len("/manual/"):-4]
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            tok = (qs.get("t") or [""])[0]
            if not _re.fullmatch(r"[0-9a-f]{12}", mid) or not manual_file_allowed("main", mid, tok):
                return self._send({"ok": False, "msg": "Not allowed."}, 403)
            try:
                with open(_pdf_path(mid), "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._send({"ok": False, "msg": "Not found."}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/manifest.json":
            body = json.dumps(manifest()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/sw.js":
            body = SW_JS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            # the worker may look after the whole site, not just its folder
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
            return
        icon_for = {"/icon-192.png": "192", "/icon-512.png": "512",
                    "/icon-512m.png": "512m", "/apple-touch-icon.png": "180",
                    "/favicon.ico": "192"}
        if path in icon_for:
            body = _b64.b64decode(ICONS[icon_for[path]])
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/c/"):
            body = SCAN_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/console", "/desk"):
            body = CONSOLE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send({"ok": True, "msg": "Paragon server is running.",
                    "app": "/app", "console": "/console"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"

        # The client's form is the one thing here with no shared secret,
        # because a client who has to be given one will phone instead. It
        # can only look up a machine and report a fault on it.
        if path == "/upload":
            # a manual, in pieces: the secret, a session, and the admin
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8 * 1024 * 1024:
                    return self._send({"ok": False, "msg": "That piece is too large"}, 413)
                req = json.loads(self.rfile.read(length).decode("utf-8", "replace") or "{}")
            except Exception:
                return self._send({"ok": False, "msg": "Could not read that."}, 400)
            if not hmac.compare_digest(str(req.get("key") or ""), SHARED_SECRET):
                return self._send({"ok": False, "msg": "Shared secret does not match"}, 403)
            u = _session("main", req)
            if not u:
                return self._send({"ok": False, "signin": True, "msg": "Sign in again."})
            try:
                return self._send(manual_upload("main", req, u))
            except Exception as e:
                note("UPLOAD ERROR", repr(e))
                return self._send({"ok": False, "msg": "Something went wrong here."}, 500)

        if path == "/hr":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                # room for a cheque or counter photo; nothing larger
                if length > 8 * 1024 * 1024:
                    return self._send({"ok": False, "msg": "Too large"}, 413)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                req = json.loads(raw) if raw else {}
            except Exception:
                return self._send({"ok": False, "msg": "Could not read that."}, 400)
            # a connected phone, and then a signed-in person on it
            if not hmac.compare_digest(str(req.get("key") or ""), SHARED_SECRET):
                return self._send({"ok": False, "msg": "Shared secret does not match"}, 403)
            try:
                return self._send(hr_route("main", req, self.client_address[0]))
            except Exception as e:
                note("HR ERROR", repr(e))
                return self._send({"ok": False, "msg": "Something went wrong here."}, 500)

        if path == "/join":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4096:
                    return self._send({"ok": False, "msg": "Too large"}, 413)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                req = json.loads(raw) if raw else {}
            except Exception:
                return self._send({"ok": False, "msg": "Could not read that."}, 400)
            return self._send(store_join("main", req, self.client_address[0]))

        if path == "/scan":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 8 * 1024 * 1024:
                    return self._send({"ok": False, "msg": "That photo is too "
                                       "large. Try again without it."}, 413)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                req = json.loads(raw) if raw else {}
            except Exception:
                return self._send({"ok": False, "msg": "Could not read that."}, 400)
            act = req.get("action")
            try:
                if act == "look":
                    return self._send(scan_look("main", req.get("serial")))
                if act == "report":
                    return self._send(scan_report("main", req))
            except Exception as e:
                note("SCAN ERROR", repr(e))
                return self._send({"ok": False, "msg": "Something went wrong "
                                   "here. Please call us."}, 500)
            return self._send({"ok": False, "msg": "Unknown action"}, 400)

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 16 * 1024 * 1024:
                return self._send({"ok": False, "msg": "Request too large"}, 413)
            raw = self.rfile.read(length).decode("utf-8", "replace")
            req = json.loads(raw) if raw else {}
        except Exception as e:
            return self._send({"ok": False, "msg": "Could not read that: %s" % e}, 400)
        try:
            self._send(handle(req))
        except Exception as e:
            note("ERROR", repr(e))
            self._send({"ok": False, "msg": "Server error: %s" % e}, 500)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if SHARED_SECRET == "change-this-please":
        note("WARNING: the shared secret is still the default. "
             "Change it in this file, and in the app.")
    note("Paragon server listening on port %d" % PORT)
    note("Build %s" % BUILD)
    note("App at      http://localhost:%d/app" % PORT)
    note("Console at  http://localhost:%d/console" % PORT)
    note("Client form at  /c/<serial>  \u2014 no login, by design")
    try:
        threading.Thread(target=hr_watch, daemon=True).start()
        threading.Thread(target=me_watch, daemon=True).start()
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        note("stopped")
        sys.exit(0)
