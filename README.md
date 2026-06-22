# Jumbo VPS Backup

Complete backup of the Jumbo Homes DigitalOcean VPS (jumbo-vm).
All configs, scripts, and system definitions needed to rebuild from scratch.

**⚠️ SECRETS: This repo does NOT contain actual secrets.**
All `.env.example` files show the required keys with `YOUR_*_HERE` placeholders.
Real secrets are stored only on the VPS and in Tarun's password manager.

## Repo Structure

```
docker/              - Docker Compose files, .env templates, Dockerfiles, patches
  twenty-*           - Twenty CRM configs
  webhook-proxy-*    - Webhook proxy (Node.js) source + config
  wordpress-*        - WordPress blog configs
scripts/
  jops/              - All operational scripts (zone allocation, sync, etc.)
  root-scripts/      - Cron scripts (WhatsApp notifications, etc.)
config/              - Caddy, crontab, docker mounts manifest
systemd/             - Systemd service files
hermes/              - Hermes agent configs + profile SOULs
docs/                - Documentation
```

## What's Backed Up

### Docker Services
| Service | Image | URL |
|---------|-------|-----|
| Twenty CRM | twentycrm/twenty:latest | admin.jumbohomes.in |
| Twenty DB | postgres:16 | internal |
| Twenty Redis | redis | internal |
| Webhook Proxy | custom (webhook-proxy) | webhook.jumbohomes.in |
| WordPress | wordpress:6.8-php8.3-fpm-alpine | blog.jumbohomes.in |
| WP DB | mariadb:11 | internal |
| Caddy (host) | - | reverse proxy for all |

### Scripts
| Script | Location | Schedule | Purpose |
|--------|----------|----------|---------|
| buyer-stage-calculator.py | /opt/jops/ | Every 4h | Auto buyer stage |
| property-status-calculator.py | /opt/jops/ | on-demand | Property status |
| jum621_push_listings.py | /opt/jops/ | Manual | Housing.com push |
| zone-allocate-buildings.sh | /opt/jops/ | Cron | Zone Step 1 |
| zone-assign-properties.sh | /opt/jops/ | Cron | Zone Step 2 |
| zone-assign-buyers.sh | /opt/jops/ | Cron | Zone Step 3 |
| zone-assign-visits.sh | /opt/jops/ | Cron | Zone Step 4 |
| zone-assign-enquiries.sh | /opt/jops/ | Cron | Zone Step 5 |
| whatsapp-notifications.py | /root/scripts/ | Every 5min | WA notifications |
| sync-buildings/ | /opt/jops/ | on-demand | Supabase sync |

### NOT Backed Up (Stored Elsewhere)
- **Database data** (Postgres) — separate dump backup to encrypted storage
- **Secrets** (.env files) — only on VPS + Tarun's password manager
- **Docker volumes** — data lives in Docker volumes, not in this repo
- **Node_modules** — rebuilt from package.json on restore

## Restore

See `docs/restore-procedure.md` for full rebuild instructions.

## Backup Schedule

Daily auto-push at 2 AM IST.
