# Zoop Portal

A self-hosted media request portal that connects to Radarr and Sonarr. Users submit IMDB links to request movies and TV shows, admins approve them, and the media automatically downloads through your existing *arr stack.

![Zoop Portal](https://img.shields.io/badge/version-1.5-blue) ![Docker](https://img.shields.io/badge/docker-ready-green) ![Python](https://img.shields.io/badge/python-3.12-blue)

## Features

- **Media Requests** — users submit IMDB links, portal auto-detects movie vs TV show
- **Season Selection** — for TV shows, users choose which seasons to download or monitor new episodes only
- **Manage Seasons** — add/remove seasons from approved shows after the fact
- **Already Exists Check** — warns users if the title is already in the library
- **Admin Approval** — requests require admin approval before sending to Radarr/Sonarr
- **Batch Approve/Reject** — approve or reject multiple requests at once
- **Auto-Approve** — configurable per-user daily auto-approve limits
- **Live Downloads** — real-time download progress from qBittorrent
- **User Management** — approve, disable, promote to admin, reset passwords
- **High Contrast Mode** — accessibility toggle saved per account
- **Login Security** — accounts locked after 5 failed login attempts
- **What's New** — built-in changelog page

## Requirements

- Docker & Docker Compose
- Radarr instance with API key
- Sonarr instance with API key
- qBittorrent with Web UI enabled (optional, for downloads page)

## Quick Start

### 1. Pull and run with Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  zoop-portal:
    image: zoopzoop/zoop-portal:latest
    container_name: zoop-portal
    ports:
      - "8888:8888"
    environment:
      - RADARR_URL=http://radarr:7878
      - RADARR_API_KEY=your_radarr_api_key
      - RADARR_QUALITY_PROFILE_ID=1
      - RADARR_ROOT_FOLDER=/movies
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY=your_sonarr_api_key
      - SONARR_QUALITY_PROFILE_ID=1
      - SONARR_ROOT_FOLDER=/tv
      - QBIT_USERNAME=admin
      - QBIT_PASSWORD=your_qbit_password
      - SECRET_KEY=change-this-to-a-long-random-string
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=your_admin_password
    volumes:
      - ./zoop-portal/data:/config
      - ./zoop-portal/templates:/app/templates
      - ./zoop-portal/static:/app/static
    restart: unless-stopped
```

```bash
docker compose up -d
```

### 2. Access the portal

Open `http://your-server-ip:8888` in your browser and log in with the admin credentials you set.

### 3. Find your quality profile IDs

```bash
# Radarr
curl -H "X-Api-Key: YOUR_KEY" http://your-radarr:7878/api/v3/qualityprofile

# Sonarr
curl -H "X-Api-Key: YOUR_KEY" http://your-sonarr:8989/api/v3/qualityprofile
```

Use the `id` field from the profile you want.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RADARR_URL` | Yes | `http://radarr:7878` | Radarr base URL |
| `RADARR_API_KEY` | Yes | — | Radarr API key |
| `RADARR_QUALITY_PROFILE_ID` | Yes | `1` | Quality profile ID for movies |
| `RADARR_ROOT_FOLDER` | Yes | `/movies` | Root folder path in Radarr |
| `SONARR_URL` | Yes | `http://sonarr:8989` | Sonarr base URL |
| `SONARR_API_KEY` | Yes | — | Sonarr API key |
| `SONARR_QUALITY_PROFILE_ID` | Yes | `1` | Quality profile ID for shows |
| `SONARR_ROOT_FOLDER` | Yes | `/tv` | Root folder path in Sonarr |
| `QBIT_USERNAME` | No | `admin` | qBittorrent WebUI username |
| `QBIT_PASSWORD` | No | `adminadmin` | qBittorrent WebUI password |
| `SECRET_KEY` | Yes | `changeme` | Session signing key — **change this!** |
| `ADMIN_USERNAME` | No | `admin` | Initial admin username |
| `ADMIN_PASSWORD` | No | `admin` | Initial admin password — **change this!** |

## Exposing Publicly

I recommend using [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) to expose Zoop Portal without opening ports on your router.

## Building from Source

```bash
git clone https://github.com/yourusername/zoop-portal.git
cd zoop-portal
docker build -t zoop-portal .
```

## Upgrading

```bash
docker compose pull
docker compose up -d
```

The SQLite database is stored in `/config` and persists across updates.
