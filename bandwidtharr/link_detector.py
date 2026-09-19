"""Detect whether traffic is currently on a primary or backup WAN link (e.g.
failed over from a wired connection to Starlink or 5G), so main.py can swap
in a lower total budget while on backup.

Kept vendor-agnostic and pluggable: `LinkDetector` is the interface a future
vendor-specific detector would also implement, without any changes to
main.py's loop or allocator.py. v1 ships one implementation,
`DnsBaselineDetector`/`IspMatchDetector`, selected by `build_link_detector()`
based on config.

Same conventions as qbittorrent.py/sabnzbd.py: raise on any check failure,
never guess -- the caller decides what "unknown" means.
"""

import json
import logging
import random
import socket
import struct
import time

import requests

log = logging.getLogger("bandwidtharr.link_detector")

PRIMARY = "primary"
BACKUP = "backup"

_DNS_HEADER = struct.Struct("!HHHHHH")


class LinkDetector:
    def check(self) -> tuple[str, str]:
        """Return (state, detail): state is PRIMARY or BACKUP, detail is a
        short human-readable string (e.g. the current public IP/ISP) for
        display, "" if there's nothing meaningful to show. Raises on any
        failure to determine the state."""
        raise NotImplementedError


class NullDetector(LinkDetector):
    """No detector configured -- always primary, so the feature is a no-op
    unless explicitly enabled via LINK_DETECTOR."""

    def check(self) -> tuple[str, str]:
        return PRIMARY, ""


def _encode_qname(hostname: str) -> bytes:
    out = bytearray()
    for part in hostname.strip(".").split("."):
        encoded = part.encode("ascii")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _skip_name(data: bytes, offset: int) -> int:
    """Return the offset just past a (possibly compressed) DNS name."""
    length = data[offset]
    if length & 0xC0 == 0xC0:  # compression pointer, always 2 bytes
        return offset + 2
    while length != 0:
        offset += 1 + length
        length = data[offset]
    return offset + 1


def _query_a_record(hostname: str, resolver: str, timeout: float) -> str:
    """Send a single A-record query straight to `resolver` over UDP and
    return the first IPv4 address in the response. No third-party HTTP
    service involved -- just an ordinary DNS query."""
    txid = random.randint(0, 0xFFFF)
    header = _DNS_HEADER.pack(txid, 0x0100, 1, 0, 0, 0)
    question = _encode_qname(hostname) + struct.pack("!HH", 1, 1)  # type=A, class=IN
    packet = header + question

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (resolver, 53))
        response, _ = sock.recvfrom(512)
    finally:
        sock.close()

    resp_id, _flags, qdcount, ancount, _nscount, _arcount = _DNS_HEADER.unpack_from(response, 0)
    if resp_id != txid:
        raise ValueError("DNS response transaction ID mismatch")
    if ancount < 1:
        raise ValueError(f"no A records returned for {hostname}")

    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(response, offset) + 4  # + QTYPE, QCLASS

    for _ in range(ancount):
        offset = _skip_name(response, offset)
        rtype, rclass, _ttl, rdlength = struct.unpack_from("!HHIH", response, offset)
        offset += 10
        if rtype == 1 and rclass == 1 and rdlength == 4:
            return socket.inet_ntoa(response[offset:offset + 4])
        offset += rdlength

    raise ValueError(f"no A record found in response for {hostname}")


class DnsBaselineDetector(LinkDetector):
    """Vendor-agnostic default: learns the public IP seen at startup (assumed
    to be on the primary link) via a single DNS A-record query -- no
    third-party HTTP call -- and reports BACKUP whenever a later check sees a
    different IP.

    Known limitation: if the primary ISP itself rotates the dynamic IP,
    that alone looks identical to a failover here. Mitigated by requiring
    several consecutive differing checks (see LinkStateTracker) before
    actually treating it as one; anyone who wants to avoid this entirely can
    opt into IspMatchDetector instead.

    If `state_file` is given, the learned baseline is persisted there (and
    loaded back on construction if present and not older than
    `max_persisted_age`), so a container restart while already on the
    backup link doesn't wrongly re-baseline backup-as-primary.
    """

    def __init__(
        self,
        lookup_host: str,
        resolver: str,
        timeout: float = 5.0,
        state_file: str | None = None,
        max_persisted_age: float = 86400,
    ):
        self.lookup_host = lookup_host
        self.resolver = resolver
        self.timeout = timeout
        self.state_file = state_file
        self.max_persisted_age = max_persisted_age
        self._baseline_ip: str | None = self._load_baseline() if state_file else None

    def _load_baseline(self) -> str | None:
        try:
            with open(self.state_file) as f:
                data = json.load(f)
            if time.time() - data["saved_at"] > self.max_persisted_age:
                return None
            log.info("link_detector: loaded persisted primary IP %s from %s", data["ip"], self.state_file)
            return data["ip"]
        except (FileNotFoundError, KeyError, ValueError, OSError, json.JSONDecodeError):
            return None

    def _save_baseline(self, ip: str) -> None:
        if not self.state_file:
            return
        try:
            with open(self.state_file, "w") as f:
                json.dump({"ip": ip, "saved_at": time.time()}, f)
        except OSError as e:
            log.warning("link_detector: failed to persist baseline IP to %s: %s", self.state_file, e)

    def check(self) -> tuple[str, str]:
        ip = _query_a_record(self.lookup_host, self.resolver, self.timeout)
        if self._baseline_ip is None:
            self._baseline_ip = ip
            log.info("link_detector: baselined primary public IP as %s", ip)
            self._save_baseline(ip)
            return PRIMARY, ip
        return (PRIMARY if ip == self._baseline_ip else BACKUP), ip


def classify_isp(isp_string: str, backup_match: str) -> str:
    """Pure classifier: BACKUP if any comma-separated, case-insensitive
    substring in `backup_match` is found in the ISP/org/AS string, else
    PRIMARY -- including when `backup_match` is unset or matches nothing,
    so an unrecognized ISP always fails safe toward PRIMARY (no wrongful
    throttling) rather than toward BACKUP.
    """
    haystack = (isp_string or "").lower()
    backup_terms = [t.strip().lower() for t in backup_match.split(",") if t.strip()]
    return BACKUP if backup_terms and any(term in haystack for term in backup_terms) else PRIMARY


class IspMatchDetector(LinkDetector):
    """Opt-in: calls a configurable IP-info HTTP endpoint and classifies the
    returned ISP/org name against a configured backup substring. Unlike
    DnsBaselineDetector, this sends the router's public IP to a third-party
    HTTP service on every check -- only used when the user explicitly sets
    BACKUP_ISP_MATCH."""

    def __init__(self, lookup_url: str, backup_match: str, timeout: float = 5.0):
        self.lookup_url = lookup_url
        self.backup_match = backup_match
        self.timeout = timeout
        self.session = requests.Session()

    def check(self) -> tuple[str, str]:
        resp = self.session.get(self.lookup_url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        isp_string = " ".join(v for k in ("isp", "org", "as") if (v := str(data.get(k, "")).strip()))
        ip = str(data.get("query", "")).strip()
        detail = f"{ip} ({isp_string})" if ip and isp_string else (ip or isp_string)
        return classify_isp(isp_string, self.backup_match), detail


class LinkStateTracker:
    """Turns raw per-check readings into a confirmed state, requiring
    `confirm_count` consecutive matching readings before actually flipping --
    protects against a single transient blip (a dropped DNS query, a one-off
    misclassification) flapping the budget."""

    def __init__(self, confirm_count: int, initial: str = PRIMARY):
        self.confirmed = initial
        self._pending: str | None = None
        self._pending_count = 0
        self._confirm_count = max(1, confirm_count)

    def observe(self, reading: str) -> str:
        if reading == self.confirmed:
            self._pending = None
            self._pending_count = 0
            return self.confirmed

        if reading == self._pending:
            self._pending_count += 1
        else:
            self._pending = reading
            self._pending_count = 1

        if self._pending_count >= self._confirm_count:
            self.confirmed = reading
            self._pending = None
            self._pending_count = 0

        return self.confirmed


def next_link_check_decision(
    now: float,
    next_link_check: float,
    is_active: bool,
    active_interval: float,
    idle_interval: float,
) -> tuple[bool, float]:
    """Return (check_due, interval_if_checked): whether a link check should
    run this cycle, and the cadence to schedule the next one on if it does
    (the caller only uses the interval when check_due is True).

    A check made while idle schedules the next one on the coarser idle
    cadence, which can be minutes out. If downloads resume partway through
    that wait, don't sit on the stale schedule -- due immediately whenever
    active and the existing schedule is further out than the active cadence
    would ever wait, not just once the schedule time is actually reached.
    """
    check_due = now >= next_link_check or (is_active and next_link_check - now > active_interval)
    interval = active_interval if is_active else idle_interval
    return check_due, interval


def build_link_detector(env: dict) -> LinkDetector:
    kind = env.get("LINK_DETECTOR", "none").strip().lower()
    if kind in ("", "none"):
        return NullDetector()
    if kind != "public_ip":
        raise ValueError(f"unknown LINK_DETECTOR: {kind!r} (expected 'none' or 'public_ip')")

    backup_match = env.get("BACKUP_ISP_MATCH", "")
    if backup_match:
        lookup_url = env.get("IP_LOOKUP_URL", "http://ip-api.com/json/?fields=isp,org,as,query")
        return IspMatchDetector(lookup_url, backup_match)

    lookup_host = env.get("DNS_LOOKUP_HOST", "myip.opendns.com")
    resolver = env.get("DNS_RESOLVER", "208.67.222.222")
    return DnsBaselineDetector(lookup_host, resolver, state_file="/app/state/baseline_ip.json")
