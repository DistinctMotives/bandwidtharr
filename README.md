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
  treated as wanting more rather than assumed satisfied. Also compensates
  automatically if qBittorrent's real throughput keeps exceeding its
  assigned limit (common with UDP-heavy torrent traffic that's hard to
  rate-limit precisely), squeezing it further until combined usage comes
  back within budget.
- **WAN failover detection** *(optional)* -- notices when your router fails
  over to a backup connection (Starlink, 5G, a hotspot...) and automatically
  swaps in a lower budget for it, no router integration or vendor-specific
  setup required. DNS-based ISP matching, with debounce against false
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
| `ACTIVE_THRESHOLD_MBPS`      | Speed above which an app counts as "active" rather than idle           | `2` |
| `REALLOCATION_SETTLE_SECONDS` | Minimum time between fairness-driven reallocation adjustments, letting qBittorrent/SABnzbd settle into a newly-assigned share before being judged again. Doesn't affect how quickly actual usage is brought back under budget if it overshoots -- only how quickly unused headroom gets reclaimed from one app and handed to the other | `30` |
| `WEB_PORT`                   | Port the dashboard listens on inside the container                     | `80` |
| `QBIT_UPLOAD_LIMIT_MBPS`      | Optional static cap on qBittorrent's upload speed. Untouched unless set; not shown in the dashboard | *(blank)* |
| `QBIT_UPLOAD_LIMIT_BACKUP_MBPS` | Optional different upload cap while on the backup link (requires `QBIT_UPLOAD_LIMIT_MBPS` to also be set) | *(blank)* |
| `LINK_DETECTOR`               | `none` or `public_ip` -- see [WAN failover detection](#wan-failover-detection)    | `none` |

The rest of the WAN-failover variables (`BACKUP_TOTAL_LIMIT_MBPS`,
`BACKUP_ISP_MATCH`, and tuning knobs) are covered in that section.

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
separate, lower `BACKUP_TOTAL_LIMIT_MBPS` while it's active. It works with
any router regardless of vendor, since it detects the failover from the
outside -- noticing that your public egress path changed -- rather than
talking to your router.

Off by default. Three variables turn it on, all required together:

```sh
LINK_DETECTOR=public_ip
BACKUP_TOTAL_LIMIT_MBPS=50
BACKUP_ISP_MATCH=Starlink,SpaceX,Space Exploration
```

### Example: finding your `BACKUP_ISP_MATCH` value

Don't guess it -- query it directly for your backup link's IP the same way
bandwidtharr does, before you even turn the feature on:

```sh
$ ip=188.92.250.182; asn=$(dig +short TXT $(echo $ip | awk -F. '{print $4"."$3"."$2"."$1}').origin.asn.cymru.com | cut -d'|' -f1 | tr -d ' "'); dig +short TXT AS$asn.asn.cymru.com
"14593 | US | arin | 2018-09-05 | SPACEX-STARLINK - Space Exploration Technologies Corporation, US"
```

The last `|`-separated field is the org name -- pull a few distinctive
words from it, not the whole string verbatim (so a minor wording change
later, like a dropped ", US" suffix, doesn't silently break the match):

```sh
BACKUP_ISP_MATCH=Starlink,SpaceX,Space Exploration
```

### How it works

- **Lookup:** three plain DNS queries against [Team Cymru's free public
  IP-to-ASN service](https://www.team-cymru.com/ip-asn-mapping) -- one to
  learn your current public IP, two more for the ASN/org name behind it.
  No third-party HTTP call.
- **Classification:** `BACKUP_ISP_MATCH` matches directly against who's
  actually serving your traffic, so it's correct from the very first check
  regardless of which link is active when bandwidtharr starts, and
  unaffected by your primary ISP rotating its own dynamic IP.
- **Fail-safe:** an unrecognized result (a hiccup, an outage) is treated as
  primary -- i.e. it fails toward *not* throttling, never toward backup.
- **Debounce:** `LINK_FAILOVER_CONFIRM_COUNT` consecutive matching checks
  are required before a switch actually happens, so a single transient
  blip can't flap the budget.
- **Visibility:** every confirmed switch is logged at `INFO`; set
  `LOG_LEVEL=DEBUG` to see the detected string on *every* check instead,
  useful for finding your match value without waiting for a real failover.
  The dashboard shows live link state and a rolling log (last 50 events)
  of failover/recovery events, persisted to the `bandwidtharr_state`
  volume so it survives restarts. The detected IP/ISP itself is never sent
  to the browser -- only ever logged server-side (`docker logs`).

### Tuning (optional, defaults shown)

| Variable                      | Meaning | Default |
|--------------------------------|---------|---------|
| `LINK_CHECK_INTERVAL_SECONDS` | How often to check which link is active while combined download speed is at/above `LINK_CHECK_MIN_SPEED_MBPS` | `30` |
| `LINK_CHECK_IDLE_INTERVAL_SECONDS` | Coarser cadence used instead, while combined download speed is below `LINK_CHECK_MIN_SPEED_MBPS` | `900` |
| `LINK_CHECK_MIN_SPEED_MBPS`   | Speed threshold that switches between the two cadences above (`0` = always use the active cadence) | `5` |
| `LINK_FAILOVER_CONFIRM_COUNT` | Consecutive matching checks required before a switch actually happens | `2` |

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
