"""Detect whether traffic is currently on a primary or backup WAN link (e.g.
failed over from a wired connection to Starlink or 5G), so main.py can swap
in a lower total budget while on backup.

Kept vendor-agnostic and pluggable: `LinkDetector` is the interface a future
vendor-specific detector would also implement, without any changes to
main.py's loop or allocator.py. `build_link_detector()` selects
`AsnMatchDetector`, which classifies the ASN/org behind the current public
IP via plain DNS queries against Team Cymru's free public IP-to-ASN
service -- no third-party HTTP call. BACKUP_ISP_MATCH is required whenever
LINK_DETECTOR=public_ip is set.

Same conventions as qbittorrent.py/sabnzbd.py: raise on any check failure,
never guess -- the caller decides what "unknown" means.
"""

import logging
import random
import socket
import struct
import time

log = logging.getLogger("bandwidtharr.link_detector")

PRIMARY = "primary"
BACKUP = "backup"

_DNS_HEADER = struct.Struct("!HHHHHH")
_QTYPE_A = 1
_QTYPE_TXT = 16
_RCODE_NAMES = {1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}


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


def _dns_query(hostname: str, resolver: str, timeout: float, qtype: int) -> bytes:
    """Send a single DNS query of `qtype` straight to `resolver` over UDP
    and return the RDATA bytes of the first matching answer. No
    third-party HTTP service involved -- just an ordinary DNS query."""
    txid = random.randint(0, 0xFFFF)
    header = _DNS_HEADER.pack(txid, 0x0100, 1, 0, 0, 0)
    question = _encode_qname(hostname) + struct.pack("!HH", qtype, 1)  # class=IN
    packet = header + question

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(packet, (resolver, 53))
        response, _ = sock.recvfrom(512)
    finally:
        sock.close()

    resp_id, flags, qdcount, ancount, _nscount, _arcount = _DNS_HEADER.unpack_from(response, 0)
    if resp_id != txid:
        raise ValueError("DNS response transaction ID mismatch")
    rcode = flags & 0x000F
    if rcode != 0:
        # A server-side failure otherwise surfaces as a misleading "no
        # matching record" once the (empty) answer section is parsed.
        raise ValueError(f"DNS query for {hostname} failed: {_RCODE_NAMES.get(rcode, f'RCODE {rcode}')}")

    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(response, offset) + 4  # + QTYPE, QCLASS

    for _ in range(ancount):
        offset = _skip_name(response, offset)
        rtype, rclass, _ttl, rdlength = struct.unpack_from("!HHIH", response, offset)
        offset += 10
        if rtype == qtype and rclass == 1:
            return response[offset:offset + rdlength]
        offset += rdlength

    raise ValueError(f"no matching record found in response for {hostname}")


def _query_a_record(hostname: str, resolver: str, timeout: float) -> str:
    """Return the first IPv4 address for `hostname`'s A record."""
    rdata = _dns_query(hostname, resolver, timeout, _QTYPE_A)
    if len(rdata) != 4:
        raise ValueError(f"unexpected A record length for {hostname}")
    return socket.inet_ntoa(rdata)


def _query_txt_record(hostname: str, resolver: str, timeout: float) -> str:
    """Return `hostname`'s TXT record. TXT RDATA is a length-prefixed
    character-string; the services this is used against return exactly
    one."""
    rdata = _dns_query(hostname, resolver, timeout, _QTYPE_TXT)
    return rdata[1:1 + rdata[0]].decode("ascii", errors="replace")


def _remaining(deadline: float, floor: float = 0.05) -> float:
    """Seconds left until `deadline` (a `time.monotonic()` timestamp),
    never less than `floor` -- 0 would put a socket in non-blocking mode
    instead of giving it a short timeout."""
    return max(floor, deadline - time.monotonic())


def _query_asn_org_name(ip: str, resolver: str, deadline: float) -> str:
    """Look up the registered org name for `ip`'s origin AS via Team
    Cymru's free public IP-to-ASN DNS service: a reverse-IP query for the
    origin ASN, then a second query for that ASN's registered name. Both
    queries share the overall `deadline` rather than each getting a full
    timeout of their own."""
    reversed_octets = ".".join(reversed(ip.split(".")))
    origin = _query_txt_record(f"{reversed_octets}.origin.asn.cymru.com", resolver, _remaining(deadline))
    asn_parts = origin.split("|")[0].split()  # "AS1 AS2 ..." when multi-origin
    if not asn_parts or not asn_parts[0].isdigit():
        raise ValueError(f"unexpected origin ASN record for {ip}: {origin!r}")
    name_record = _query_txt_record(f"AS{asn_parts[0]}.asn.cymru.com", resolver, _remaining(deadline))
    return name_record.split("|")[-1].strip()


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


def _format_detail(ip: str, isp_string: str) -> str:
    """Human-readable "ip (isp)" string for dashboard display, gracefully
    handling either half being empty."""
    if ip and isp_string:
        return f"{ip} ({isp_string})"
    return ip or isp_string


class AsnMatchDetector(LinkDetector):
    """Default lookup mechanism: identifies the ASN/org behind your current
    public IP via up to three plain DNS queries -- no third-party HTTP call.
    A query for `lookup_host` sent straight to `resolver` (e.g. OpenDNS's
    myip.opendns.com special-cases that hostname to echo back the querying
    source IP) gets the current public IP; the ASN/org behind it (via Team
    Cymru's free public IP-to-ASN DNS service) is cached and only re-looked-up
    when that IP actually changes. All queries in a single check share one
    overall `timeout` budget rather than each getting a full timeout of
    their own.
    """

    def __init__(self, lookup_host: str, resolver: str, backup_match: str, timeout: float = 5.0):
        self.lookup_host = lookup_host
        self.resolver = resolver
        self.backup_match = backup_match
        self.timeout = timeout
        self._cached_ip: str | None = None
        self._cached_org_name = ""

    def check(self) -> tuple[str, str]:
        deadline = time.monotonic() + self.timeout
        ip = _query_a_record(self.lookup_host, self.resolver, _remaining(deadline))
        if ip != self._cached_ip:
            try:
                self._cached_org_name = _query_asn_org_name(ip, self.resolver, deadline)
            except Exception as e:
                # The ASN queries embed the public IP (plainly, and reversed
                # in the query hostname), and a check's error string ends up
                # on the unauthenticated dashboard -- which must never show
                # the detected IP. `from None` for the same reason as
                # sabnzbd.py's API-key redaction.
                reversed_ip = ".".join(reversed(ip.split(".")))
                raise RuntimeError(
                    f"ASN lookup failed: {str(e).replace(ip, '***').replace(reversed_ip, '***')}"
                ) from None
            self._cached_ip = ip
        return classify_isp(self._cached_org_name, self.backup_match), _format_detail(ip, self._cached_org_name)


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

    backup_match = env.get("BACKUP_ISP_MATCH", "").strip()
    if not backup_match:
        raise ValueError("BACKUP_ISP_MATCH must be set when LINK_DETECTOR=public_ip")

    lookup_host = env.get("DNS_LOOKUP_HOST", "myip.opendns.com")
    resolver = env.get("DNS_RESOLVER", "208.67.222.222")
    return AsnMatchDetector(lookup_host, resolver, backup_match)
