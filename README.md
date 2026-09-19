# bandwidtharr

> **Built with AI.** This entire project -- code, tests, CI, packaging -- was
> written by an LLM working with a human directing and reviewing it. It
> works for the author's own setup (binhex qBittorrent/SABnzbd on Unraid),
> but hasn't seen wide use. Read the code, and use it at your own risk -- do
> with it as you please.

bandwidtharr stops qBittorrent and SABnzbd from fighting each other for your
bandwidth. It watches both apps and gives each one the speed limit it
actually needs, live, instead of you having to guess at fixed caps for each.

- **Dynamic, demand-based sharing** -- a lone downloader gets the whole
  budget; once both are downloading, the budget splits by who can actually
  use more (not a flat 50/50), and an app pinned at its own cap is still
  treated as wanting more rather than assumed satisfied.
- **WAN failover detection** *(optional)* -- notices when your router fails
  over to a backup connection (Starlink, 5G, a hotspot...) and automatically
  swaps in a lower budget for it, no router integration or specific vendor
  required. Two detection modes (a private DNS-only check, or opt-in
  ISP-name matching for more precision), with debounce against false
  positives and state that survives restarts.
- **Optional qBittorrent upload cap** -- a separate static upload limit, with
  its own lower value to use while on the backup link.
- **Live dashboard** -- current speed/limit per app, a usage graph against
  the budget line, current link status, and a rolling log of past
  failover/recovery events.
- **Low-maintenance to run** -- one small Docker container, no root user
  inside it, a healthcheck that reflects real app connectivity (not just
  "the web server is up"), and automatic security-patch updates via CI.

## Requirements

- Docker + Docker Compose
- qBittorrent and SABnzbd already running as containers on a shared Docker
  network bandwidtharr can join (tested against the `binhex/arch-qbittorrentvpn`
  and `binhex/arch-sabnzbdvpn` images, but any qBittorrent/SABnzbd instance
  reachable by URL works)
- SABnzbd API key (Config -> General -> API Key)

## Quickstart

Pulls the prebuilt image from GHCR (`ghcr.io/distinctmotives/bandwidtharr`) by
default -- no local build needed.

```sh
git clone https://github.com/DistinctMotives/bandwidtharr.git
cd bandwidtharr
cp .env.example .env
# edit .env: DOCKER_NETWORK, QBIT_URL/SAB_URL, SAB_API_KEY, TOTAL_LIMIT_MBPS
docker compose up -d
```

bandwidtharr will join `DOCKER_NETWORK` and start managing both apps' speed
limits immediately, including correcting any stale/manual limit already set
on either one.

## Configuration

All configuration is via `.env` (see `.env.example`):

| Variable                    | Meaning                                                             | Default |
|------------------------------|----------------------------------------------------------------------|---------|
| `DOCKER_NETWORK`             | External Docker network shared with qBittorrent/SABnzbd               | *(required, no default)* |
| `QBIT_URL`                   | qBittorrent WebUI base URL                                            | `http://binhex-qbittorrentvpn:8080` |
| `QBIT_USER` / `QBIT_PASS`    | qBittorrent WebUI credentials (leave blank if auth is bypassed for the docker subnet) | *(blank)* |
| `SAB_URL`                    | SABnzbd base URL                                                       | `http://binhex-sabnzbdvpn:8080` |
| `SAB_API_KEY`                | SABnzbd API key                                                        | *(required)* |
| `TOTAL_LIMIT_MBPS`           | Combined download budget to enforce                                    | `800` |
| `POLL_INTERVAL_SECONDS`      | How often to poll and re-evaluate                                      | `3` |
| `MIN_FLOOR_MBPS`             | Minimum share either app can be squeezed to once both are active       | `40` |
| `ACTIVE_THRESHOLD_MBPS`      | Speed above which an app counts as "active" rather than idle           | `2` |
| `PROBE_STEP_MBPS`            | How much extra demand to assume for an app saturating its own cap      | `40` |
| `CHANGE_THRESHOLD_FRACTION`  | Minimum relative change before a new limit is actually applied (hysteresis, avoids noisy API calls) | `0.05` |
| `WEB_PORT`                   | Port the dashboard listens on inside the container                     | `80` |
| `QBIT_UPLOAD_LIMIT_MBPS`      | Optional static cap on qBittorrent's upload speed. Untouched unless set; not shown in the dashboard | *(blank)* |
| `QBIT_UPLOAD_LIMIT_BACKUP_MBPS` | Optional different upload cap while on the backup link (requires `QBIT_UPLOAD_LIMIT_MBPS` to also be set) | *(blank)* |
| `LINK_DETECTOR`               | `none` or `public_ip` -- see [WAN failover detection](#wan-failover-detection)    | `none` |
| `BACKUP_TOTAL_LIMIT_MBPS`     | Budget to use while on the backup link (required if `LINK_DETECTOR` is set)  | *(none)* |
| `LINK_CHECK_INTERVAL_SECONDS` | How often to check which link is active while combined download speed is at/above `LINK_CHECK_MIN_SPEED_MBPS` | `30` |
| `LINK_CHECK_IDLE_INTERVAL_SECONDS` | Coarser cadence used instead, while combined download speed is below `LINK_CHECK_MIN_SPEED_MBPS` | `900` |
| `LINK_CHECK_MIN_SPEED_MBPS`   | Combined qbit+sab speed threshold that switches between the two cadences above (`0` = always use the active cadence) | `5` |
| `LINK_FAILOVER_CONFIRM_COUNT` | Consecutive matching checks required before actually switching budgets  | `2` |
| `BACKUP_ISP_MATCH`            | ISP/org/AS-name substrings (comma-separated) identifying the backup link (required if `LINK_DETECTOR` is set) | *(none)* |
| `DNS_LOOKUP_HOST` / `DNS_RESOLVER` | Hostname/resolver used for the "what's my public IP" and ISP/ASN lookups | `myip.opendns.com` / `208.67.222.222` |

## Web dashboard

Serves a live-updating page (1s refresh) showing each app's current
speed/limit and a stacked usage graph against the budget line. It listens on
`WEB_PORT` **inside the container only** -- not published to the host by
default, since it's meant to be reached from other containers on the same
Docker network (e.g. a reverse proxy) rather than exposed directly. To
publish it to the host instead, add to `docker-compose.yml`:

```yaml
services:
  bandwidtharr:
    ports:
      - "8880:80"
```

## WAN failover detection

If your router fails over to a backup link (Starlink, 5G, a cellular
hotspot...) the fixed `TOTAL_LIMIT_MBPS` budget is usually way too high for
that link, so bandwidtharr can optionally detect the failover and swap in a
separate `BACKUP_TOTAL_LIMIT_MBPS` while it's active. This works with any
router, regardless of vendor, since it detects the failover from the outside,
by noticing that your public egress path changed, not by talking to your
router.

Off by default. Set `LINK_DETECTOR=public_ip`, `BACKUP_TOTAL_LIMIT_MBPS`,
and `BACKUP_ISP_MATCH` (comma-separated, case-insensitive substrings
identifying your backup link's ISP/org/AS name, e.g.
`BACKUP_ISP_MATCH=Starlink,SpaceX`) to turn it on -- all three are required
together. `BACKUP_ISP_MATCH` classifies primary vs. backup directly by
who's actually serving your traffic, rather than inferring it from "did the
IP change": correct from the very first check regardless of which link is
active when bandwidtharr starts, and unaffected by your primary ISP simply
rotating its own dynamic IP. If the lookup ever returns something
unrecognized (a hiccup, an outage), it fails toward primary -- i.e. toward
*not* throttling -- rather than toward backup.

The lookup itself is three plain DNS queries against [Team Cymru's free
public IP-to-ASN service](https://www.team-cymru.com/ip-asn-mapping) (one
to learn your current public IP, two more for the ASN/org name behind it)
-- no third-party HTTP call at all.

Don't guess the match value -- query it directly for any IP (yours, or a
link you're not currently on) the same way bandwidtharr does -- reverse-IP
query for the origin ASN, then a query for that ASN's registered name:

```sh
ip=1.2.3.4; asn=$(dig +short TXT $(echo $ip | awk -F. '{print $4"."$3"."$2"."$1}').origin.asn.cymru.com | cut -d'|' -f1 | tr -d ' "'); dig +short TXT AS$asn.asn.cymru.com
```

The last `|`-separated field of the output is the org name -- pull your
match terms from there. For example, run against a real Starlink IP this
prints `"14593 | US | arin | 2018-09-05 | SPACEX-STARLINK - Space
Exploration Technologies Corporation, US"`, so the match terms would be
`BACKUP_ISP_MATCH=Starlink,SpaceX,Space Exploration` -- multiple
comma-separated words pulled from that field, not the whole string
verbatim, so a minor wording change (punctuation, a suffix like ", US")
doesn't silently break the match.

Once bandwidtharr is actually running, `docker logs` is the ground truth --
setting `LOG_LEVEL=DEBUG` shows the detected string on *every* check,
rather than only when a switch actually happens.

Every confirmed switch (in both directions) is logged at `INFO`
and shown live on the dashboard, which displays the current link state,
whether it's currently treating traffic as downloading or idle, when it was
last/next checked, and a rolling log (last 50 events) of recent
failover/failback events with timestamps -- persisted to the
`bandwidtharr_state` volume, so it survives restarts and updates. The
detected IP/ISP itself is never sent to the browser -- it's only ever
logged server-side (`docker logs`).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/
```

To run against your local changes instead of the published image:

```sh
docker compose up -d --build
```

Pushes to `main` run the test suite first, then rebuild and publish
`ghcr.io/distinctmotives/bandwidtharr:latest` via GitHub Actions
(`.github/workflows/docker-publish.yml`) -- the image is only built and
pushed if `pytest tests/` passes.

The allocation logic (`bandwidtharr/allocator.py`) is a pure function with no
I/O, so it's fully covered by fast unit tests independent of the qBittorrent
and SABnzbd API clients.

## License

MIT -- see [LICENSE](LICENSE).
