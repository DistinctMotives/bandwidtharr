# bandwidtharr

> **Built with AI.** This entire project -- code, tests, CI, packaging -- was
> written by an LLM working with a human directing and reviewing it. It
> works for the author's own setup (binhex qBittorrent/SABnzbd on Unraid),
> but hasn't seen wide use. Read the code, and use it at your own risk -- do
> with it as you please.

bandwidtharr stops qBittorrent and SABnzbd from fighting each other for your
bandwidth. It watches both apps and gives each one the speed limit it
actually needs, live, instead of you having to guess at fixed caps for each.

- **Fair, demand-aware sharing** -- a lone downloader gets the whole
  budget; once both are active, they split evenly, and share only shifts
  from one to the other once it's demonstrably not using what it has --
  no fixed ratio, no penalizing a past lull. Also compensates if
  qBittorrent's real throughput keeps exceeding its limit (common with
  UDP-heavy torrent traffic that's hard to rate-limit precisely),
  squeezing it further until combined usage is back within budget.
- **Tolerant of one app being down** -- if one app's API stays unreachable
  for over a minute (e.g. a VPN container reconnecting), the other is
  handed the full active budget instead of sitting frozen at the last
  two-way split. Brief blips don't trigger it; normal fairness resumes
  automatically once both are reachable again.
- **WAN failover detection** *(optional)* -- notices when your router fails
  over to a backup connection (Starlink, 5G, a hotspot...) and swaps in a
  lower budget automatically, no router integration or vendor-specific
  setup required. DNS-based ISP matching, debounced against false
  positives, with state that survives restarts.
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
| `WEB_PORT`                   | Port the dashboard listens on inside the container                     | `80` |
| `QBIT_UPLOAD_LIMIT_MBPS`      | Optional static cap on qBittorrent's upload speed. Untouched unless set; not shown in the dashboard | *(blank)* |
| `QBIT_UPLOAD_LIMIT_BACKUP_MBPS` | Optional different upload cap while on the backup link (requires `QBIT_UPLOAD_LIMIT_MBPS` to also be set) | *(blank)* |
| `LINK_DETECTOR`               | `none` or `public_ip` -- see [WAN failover detection](#wan-failover-detection)    | `none` |

The rest of the WAN-failover variables (`BACKUP_TOTAL_LIMIT_MBPS`,
`BACKUP_ISP_MATCH`, `SLACK_WEBHOOK_URL`) are covered in that section.

The internal timing knobs (poll interval, settle timers, link-check
cadence, confirmation count) are deliberately left out of the docs: the
defaults are meant to just work. They're all still overridable via `.env`
-- see the `os.environ` reads at the top of `bandwidtharr/main.py` if you
ever need one.

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
hotspot...), the fixed `TOTAL_LIMIT_MBPS` budget is usually too high for it.
bandwidtharr can detect that and swap in a lower `BACKUP_TOTAL_LIMIT_MBPS`
while it's active -- works with any router, since it detects the change
from the outside (your public egress path changing) rather than talking
to the router itself.

Off by default. Three variables turn it on, all required together:

```sh
LINK_DETECTOR=public_ip
BACKUP_TOTAL_LIMIT_MBPS=50
BACKUP_ISP_MATCH=Starlink,SpaceX,Space Exploration
```

### Example: finding your `BACKUP_ISP_MATCH` value

Don't guess it -- query it the same way bandwidtharr does, before turning
the feature on:

```sh
$ ip=188.92.250.182; asn=$(dig +short TXT $(echo $ip | awk -F. '{print $4"."$3"."$2"."$1}').origin.asn.cymru.com | cut -d'|' -f1 | tr -d ' "'); dig +short TXT AS$asn.asn.cymru.com
"14593 | US | arin | 2018-09-05 | SPACEX-STARLINK - Space Exploration Technologies Corporation, US"
```

The last `|`-separated field is the org name -- use a few distinctive
words from it, not the whole string (so a later wording tweak, like a
dropped ", US" suffix, doesn't break the match):

```sh
BACKUP_ISP_MATCH=Starlink,SpaceX,Space Exploration
```

### How it works

- **Lookup:** three plain DNS queries against [Team Cymru's free public
  IP-to-ASN service](https://www.team-cymru.com/ip-asn-mapping) -- one to
  learn your current public IP, two more for the ASN/org name behind it.
  No third-party HTTP call.
- **Classification:** `BACKUP_ISP_MATCH` matches against who's actually
  serving your traffic, so it's correct from the first check regardless
  of which link is active at startup, and unaffected by your primary
  ISP's dynamic IP rotating.
- **Fail-safe:** an unrecognized result (a hiccup, an outage) is treated as
  primary -- i.e. it fails toward *not* throttling, never toward backup.
- **Debounce:** two consecutive matching checks are required before a
  switch actually happens, so a single transient blip can't flap the
  budget.
- **Visibility:** every confirmed switch is logged at `INFO`; set
  `LOG_LEVEL=DEBUG` to see the detected string on *every* check instead,
  useful for finding your match value without waiting for a real failover.
  The dashboard shows live link state and a rolling log (last 50 events)
  of failover/recovery events, persisted to the `bandwidtharr_state`
  volume so it survives restarts. The detected IP/ISP itself is never sent
  to the browser -- only ever logged server-side (`docker logs`).

### Slack notifications (optional)

Set `SLACK_WEBHOOK_URL` to an [Incoming
Webhook](https://api.slack.com/messaging/webhooks) URL (Slack app
settings -> Incoming Webhooks) to post a message on every confirmed
failover/recovery -- off by default. The message has the state
transition, budget Mbps values, and a UTC timestamp -- never the
detected IP/ISP. Sent in the background with a few retries on failure;
if it still can't get through, that's only logged as a warning and never
affects the bandwidth arbitration loop.

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

The allocation logic (`bandwidtharr/allocator.py`) has no I/O, so it's fully
covered by fast unit tests independent of the qBittorrent and SABnzbd API
clients -- including `tests/test_simulation.py`, which drives it through
realistic, fluctuating multi-cycle scenarios (ramp-up lag, imprecise rate
limiting, random jitter, WAN failover) rather than idealized instant
convergence.

## License

MIT -- see [LICENSE](LICENSE).
