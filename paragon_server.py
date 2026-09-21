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
         "notes", "goals", "habits")


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

ICONS = {'192': 'iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAAC5UlEQVR42u3cD03DQBjG4d1lBlCweWAJJvAwEjSgAg0kzAMmSIaHTQEShgagf+7ufR4FXfv9+rXJ0s0GAAAAAAAAAGAYpaeD3R3PN5esD9fToQjAwNN4EMXQkxxDMfgkh1AMPskhFINPcgjF4JMcQjX8tGbJOamGn+QIisEn+ZGoGn6St0E1/CRHUA0/yRFUw09yBNUpJVltuU6Ye86q4Sc5gmr4SY7AOwDeAdz9Sd0CNgA2gLs/qVvABsAGcPcndQvYANgA7v6kbgEbABsABODxh8DHIBsAGwAEAALw/E/We4ANQLTtqD/s8n4fcQH3T1+m2DtA5vCn/VYBGAi/WQAGwW8XAAgABAACAAGAAEAAIAAQAAgABAACAAGAAEAACMApQAAgABBA15K/kODrEAKIHQTDL4DYgTD8/7M1GNgAIAAQAAgABAACAAGAAEAAIAAQAAgABAAdG/bfoK/Pe1d3Yi9vFxvA8Oca8bxWF4nk81tdHJLPs5dgogkAAYAAQAAgABAACAAEAAIAAYAAQAAgABAACAAEAAIAAaxlxK8WOM8CEIHzKwAROK9z2LpYeAcAAYAAQAAgABAACAAEAAIAAYAAQAAgAOjYsP8GfXy4a+6YPj6/TZwNkDn8LR+XAAy/4xOA4XecAgABgABAACAAEAAIAAQAAgABgABAACAAEAAIAAQAAgABgAAW0MtXF3wdQgCxw2X4BRA7ZIa/PcN+GMuw4SUYpgzgejoUp4zW/WZObQBsABAACMB7ADnP/zYANoBTgAA8BhH4+GMDYAMsWRu0dPe3AbAB1qgOWrj72wDYAGvWB2vPnw2ADWALkHj3n3QDiIDehn/yRyAR0NPwewfAO0DLdcLc81V7OEiYa65qTweL4e8mABHQwxwtNqC74/nmUtLaDbSO9GMw/M0GIAJanJPVBtIjES3cIFe/IwuBNZ8MmnkkEYLBjw5ACAZfAGIw9AIQhIEHAAAAAAAAgL/6Ae13+f9Yg1NUAAAAAElFTkSuQmCC', '512': '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', '512m': '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', '180': 'iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAACuElEQVR42u3cAU3DUBSG0fZlBlAwPLAEE3gYCRpQgQYS5gETJMPDpgAJQwILWdvb/52joOv7uLkl64YBAAAAAAAA6M+4hovc7o8XR1XD+bAbBS1ekfcUtIjFHRG0kIUdEbSQhR0RtJCFPbUmZpLOv4mZpKhHIZO0gjQxkzStm5hJirqJmaSom5hJirqJmaSom1tJklbprwtTevGgxUylqJuYSYraDo0d2nSm6pQ2oTGhTWeqTmkTmr4ntOlM5SltQmOHhoigrRtUXztMaKwcIGgQNEwUtAdC1vBguEn60KePh8jDvH/+VnRvK0dqzOmfTdCdHrioOwm6p4MWdUcrBwgaQYOgQdAgaAQNggZBg6BB0AgaBA2CBkGDoBE0CHpBPb1A6mXZTiZ0Dwct5s5WjuQDF/P1Ng4eExoEDYIGQSNoEDQIGgQNgkbQIGgQNEwh6stJby/3TvSfXt9PJrSYDQNBOwz3UdBidj89FNIhQSNoEDQIGgSNoEHQIGgQNAgaQYOgQdAgaBA0gl6TlHfh3E9Bi9p9zF05RO3+DUPYzxiIGg+FCBoEDYIGQSNoEDQIGgQNgkbQIGioI+rLSU+PdyWu4/PrR1kmdEbM1a5F0GIWtaCFI2pBg6BB0AgaBA2CBkGDoBE0CBoEDYIGQSNoEDQIGgQNKUFXfiHVy7KCjglHzIKOCUjMy4n6XQ4hcfWEPh92o9vFUq7tz385sEODoEHQMGHQHgyp/EBoQmPlgKigrR1UXTdMaExoU5qq09mExoQ2pak6nU1oTGhTmqrT+SYTWtRUiflmK4eoqdKRHRo7tClNxel88wktapbuplW+OMRcYocWNUt10tZ0sYj5L7NEt90fL46QOQZdS/gQiHnWoEXNXOe/SGRWECFHBS1sIUcGLWwhRwYtbhHHBi1y8QIAAAAAADCDX5xn8e1QoUjCAAAAAElFTkSuQmCC'}

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

BUILD = "2026-09-20 sharing + counters + console + QR complaints + installable app + every kind shared + phone notes + iPhone push"


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
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        note("stopped")
        sys.exit(0)
