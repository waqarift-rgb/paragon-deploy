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
    admin = u.get("role") == "admin"
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
             if isinstance(x, dict) and x.get("active", True)
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
    if u.get("role") != "admin":
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
    if u.get("role") != "admin":
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
    if act == "in":      return att_in(cid, req, u)
    if act == "out":     return att_out(cid, req, u)
    if act == "month":   return att_month(cid, req, u)
    if act == "fix":     return att_fix(cid, req, u)
    if act == "holiday": return hol_set(cid, req, u)
    if act == "whoami":  return {"ok": True, "user": u["id"], "role": u.get("role")}
    if act in ("slips.mine", "overview", "person", "rate", "advance", "bonus",
               "approve", "slips.make", "slips.month", "slips.final"):
        return pay_route(cid, req, u)
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
    n = max(1, int(adv.get("instalments") or 1))
    base = int(adv["amount"] // n)
    done = sum(1 for r in d.get("recoveries", {}).values() if r.get("advance") == adv["id"])
    this = adv["amount"] - base * (n - 1) if done + 1 >= n else base
    return min(this, bal)


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
        if a.get("user") != uid:
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

    net = (basic_due + ot_amount + bonus
           - leave_ded - late_ded - absent_ded - half_ded - early_ded - adv_total)
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
        "concessions": concessions, "no_out": no_out, "table": table,
        "rate_from": rate.get("from"),
    }}


ROLE_NAMES = {"admin": "Admin", "supervisor": "Supervisor", "store": "Store Manager",
              "tech": "Technician", "helper": "Helper"}


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

    if not _need_admin(u):
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
                            "advances": advs,
                            "bonuses": [b for b in d["bonuses"].values() if b.get("user") == x["id"]]})
            return {"ok": True, "people": out}

        if act == "person":            # joining date, leaving date, designation
            e = need("user")
            if e: return e
            p = d["people"].setdefault(req["user"], {})
            for k in ("joined", "left", "designation"):
                if k in req:
                    p[k] = str(req.get(k) or "").strip()
            hr_save(cid, d)
            return {"ok": True}

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

BUILD = "2026-09-20 sharing + counters + console + QR complaints + installable app + every kind shared + phone notes + iPhone push + admin join + attendance + salary"


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
        if path == "/hr":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 65536:
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
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        note("stopped")
        sys.exit(0)
