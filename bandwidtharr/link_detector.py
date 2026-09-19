"""Detect whether traffic is currently on a primary or backup WAN link (e.g.
failed over from a wired connection to Starlink or 5G), so main.py can swap
in a lower total budget while on backup.

Kept vendor-agnostic and pluggable: `LinkDetector` is the interface a future
vendor-specific detector (FortiGate SD-WAN, pfSense, UniFi, ...) would also
implement, without any changes to main.py's loop or allocator.py. v1 ships
one implementation, `DnsBaselineDetector`/`IspMatchDetector`, selected by
`build_link_detector()` based on config.

Same conventions as qbittorrent.py/sabnzbd.py: raise on any check failure,
never guess -- the caller decides what "unknown" means.
"""

import logging
import random
import socket
import struct

import requests

log = logging.getLogger("bandwidtharr.link_detector")

PRIMARY = "primary"
BACKUP = "backup"

_DNS_HEADER = struct.Struct("!HHHHHH")


class LinkDetector:
    def check(self) -> str:
        """Return PRIMARY or BACKUP. Raises on any failure to determine it."""
        raise NotImplementedError


class NullDetector(LinkDetector):
    """No detector configured -- always primary, so the feature is a no-op
    unless explicitly enabled via LINK_DETECTOR."""

    def check(self) -> str:
        return PRIMARY


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
    """

    def __init__(self, lookup_host: str, resolver: str, timeout: float = 5.0):
        self.lookup_host = lookup_host
        self.resolver = resolver
        self.timeout = timeout
        self._baseline_ip: str | None = None

    def check(self) -> str:
        ip = _query_a_record(self.lookup_host, self.resolver, self.timeout)
        if self._baseline_ip is None:
            self._baseline_ip = ip
            log.info("link_detector: baselined primary public IP as %s", ip)
            return PRIMARY
        return PRIMARY if ip == self._baseline_ip else BACKUP


def classify_isp(isp_string: str, primary_match: str, backup_match: str) -> str:
    """Pure classifier: decide PRIMARY vs BACKUP for an ISP/org/AS string
    given comma-separated, case-insensitive substrings to match against.

    - If `backup_match` is set and any term matches -> BACKUP.
    - Else if `primary_match` is set -> PRIMARY if a term matches, else BACKUP.
    - Else (only backup_match set, no match) -> PRIMARY.
    """
    haystack = (isp_string or "").lower()
    backup_terms = [t.strip().lower() for t in backup_match.split(",") if t.strip()]
    primary_terms = [t.strip().lower() for t in primary_match.split(",") if t.strip()]

    if backup_terms and any(term in haystack for term in backup_terms):
        return BACKUP
    if primary_terms:
        return PRIMARY if any(term in haystack for term in primary_terms) else BACKUP
    return PRIMARY


class IspMatchDetector(LinkDetector):
    """Opt-in: calls a configurable IP-info HTTP endpoint and classifies the
    returned ISP/org name against configured primary/backup substrings.
    Unlike DnsBaselineDetector, this sends the router's public IP to a
    third-party HTTP service on every check -- only used when the user
    explicitly sets PRIMARY_ISP_MATCH or BACKUP_ISP_MATCH."""

    def __init__(self, lookup_url: str, primary_match: str, backup_match: str, timeout: float = 5.0):
        self.lookup_url = lookup_url
        self.primary_match = primary_match
        self.backup_match = backup_match
        self.timeout = timeout
        self.session = requests.Session()

    def check(self) -> str:
        resp = self.session.get(self.lookup_url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        isp_string = " ".join(str(data.get(k, "")) for k in ("isp", "org", "as"))
        return classify_isp(isp_string, self.primary_match, self.backup_match)


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


def build_link_detector(env: dict) -> LinkDetector:
    kind = env.get("LINK_DETECTOR", "none").strip().lower()
    if kind in ("", "none"):
        return NullDetector()
    if kind != "public_ip":
        raise ValueError(f"unknown LINK_DETECTOR: {kind!r} (expected 'none' or 'public_ip')")

    primary_match = env.get("PRIMARY_ISP_MATCH", "")
    backup_match = env.get("BACKUP_ISP_MATCH", "")
    if primary_match or backup_match:
        lookup_url = env.get("IP_LOOKUP_URL", "http://ip-api.com/json/?fields=isp,org,as")
        return IspMatchDetector(lookup_url, primary_match, backup_match)

    lookup_host = env.get("DNS_LOOKUP_HOST", "myip.opendns.com")
    resolver = env.get("DNS_RESOLVER", "208.67.222.222")
    return DnsBaselineDetector(lookup_host, resolver)
