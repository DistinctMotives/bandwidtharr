# bandwidtharr

> **Built with AI.** This entire project -- code, tests, CI, packaging -- was
> written by Claude (Anthropic) working with a human directing and reviewing
> it. It works for the author's own setup (binhex qBittorrent/SABnzbd on
> Unraid), but hasn't seen wide use. Read the code, and use it at your own
> risk -- do with it as you please.

Dynamic bandwidth arbitration between qBittorrent and SABnzbd, so they share a
fixed total budget instead of fighting each other for your connection.

If you run both a torrent client and a Usenet client, setting a static speed
limit on each wastes bandwidth (only one running at a time still gets capped)
and no limit at all means they contend for your whole pipe when both run
together. bandwidtharr polls both apps every few seconds and adjusts their
speed limits live:

- If only one is downloading, it gets the **entire** budget.
- If both are downloading at once, the budget is split **proportional to
  demand** (not a flat 50/50) -- whichever app can actually use more
  bandwidth gets more of it, and an app pinned at its own limit is treated as
  still wanting more rather than assumed satisfied.

It also ships a small live web dashboard (speed/limit per app, a usage graph
against the budget line) served from inside the container.

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
| `PRIMARY_ISP_MATCH` / `BACKUP_ISP_MATCH` | Optional ISP-name substrings (comma-separated) -- switches to the ISP-lookup detector mode | *(blank)* |
| `IP_LOOKUP_URL`               | IP-info endpoint used by ISP-name matching                              | `http://ip-api.com/json/?fields=isp,org,as,query` |
| `DNS_LOOKUP_HOST` / `DNS_RESOLVER` | Hostname/resolver used by the default DNS-only IP-baseline check    | `myip.opendns.com` / `208.67.222.222` |

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

Off by default. Set `LINK_DETECTOR=public_ip` and `BACKUP_TOTAL_LIMIT_MBPS`
to turn it on; set `LINK_DETECTOR` back to `none` (or remove it) to turn it
off again -- bandwidtharr then makes no DNS/HTTP calls for link detection and
the dashboard's link badge disappears. Two detection modes, chosen
automatically by whether you've set an ISP match:

- **Default (no config beyond the two above):** a DNS-only check -- no
  third-party HTTP call -- that remembers the public IP seen when
  bandwidtharr started (assumed to be the primary link) and treats any later,
  persistent change as a failover. Simple and private, but a primary ISP that
  itself rotates your dynamic IP can look like a failover; `LINK_FAILOVER_CONFIRM_COUNT`
  (default 2 consecutive checks) guards against a single blip, but a longer-lived
  IP rotation could still misfire.
- **Opt-in ISP matching:** set `PRIMARY_ISP_MATCH` and/or `BACKUP_ISP_MATCH`
  (comma-separated, case-insensitive substrings, e.g.
  `BACKUP_ISP_MATCH=Starlink,T-Mobile`) and bandwidtharr instead calls
  `IP_LOOKUP_URL` (an IP-info API, default `ip-api.com`) each check to read
  the actual ISP/org name behind your current public IP. More robust against
  ordinary IP rotation on the primary link, at the cost of sending your
  public IP to that third-party API on every check.

Either way, every confirmed switch (in both directions) is logged at `INFO`
and shown live on the dashboard, which displays the current link state,
whether it's currently treating traffic as downloading or idle, when it was
last/next checked, and a rolling log of recent failover/failback events with
timestamps. The detected IP/ISP itself is never sent to the browser -- it's
only ever logged server-side (`docker logs`).

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

Pushes to `main` automatically rebuild and publish
`ghcr.io/distinctmotives/bandwidtharr:latest` via GitHub Actions
(`.github/workflows/docker-publish.yml`).

The allocation logic (`bandwidtharr/allocator.py`) is a pure function with no
I/O, so it's fully covered by fast unit tests independent of the qBittorrent
and SABnzbd API clients.

## License

MIT -- see [LICENSE](LICENSE).
