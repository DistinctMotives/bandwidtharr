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
