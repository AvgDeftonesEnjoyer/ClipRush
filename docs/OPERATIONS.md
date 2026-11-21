# ClipRush operations runbook

## 1. Install or update

1. Clone the repository **on a private server**, never put real credentials in Git.
2. Copy `cp .env.example .env`; choose a random ADMIN_TOKEN and strong unique POSTGRES_PASSWORD; update the DATABASE_URL password as well.
3. Configure providers and authorization rules, then `docker compose up --build -d`.
4. Migration service runs **before** API, worker, scheduler and scouts. Check `docker compose logs migrate` and `docker compose ps`.
5. If you already have a database created by older `create_all()` code: **stop**, take a backup, assess schema differences, and migrate it deliberately. Do not blindly stamp its Alembic revision.

```sh
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/health/ready
docker compose logs --tail 80 api worker beat scout
```

The simple `/health` is a liveness probe, `/health/ready` checks PostgreSQL and Redis. `/readiness`, `/ops/stats`, `/ops/stuck` and `/ops/audit` require the Bearer admin token. When behind a proxy, allow unauthenticated external access only to explicitly required provider webhooks, not administrative routes or docs.

## 1a. HTTPS ingress for external webhooks

The base Compose file exposes only `127.0.0.1:8080`. For signed webhooks, configure DNS for your own domain and open ports 80/443, set `CLIPRUSH_DOMAIN` and `PUBLIC_BASE_URL=https://your-domain`, then deploy:

```sh
docker compose -f docker-compose.yml -f docker-compose.edge.yml up -d --build
```

The Caddy reverse proxy obtains and renews HTTPS certificates and **only** forwards `/hooks/openshorts`, `/hooks/kick` and `/hooks/telegram`. All other public paths return 404. Do not publish `:8080` directly or expose the admin API, `/docs` and diagnostics on the internet. For administration, use an SSH tunnel:

```sh
ssh -L 8080:127.0.0.1:8080 your-server
```

The webhook handlers still require valid signatures; TLS is not a replacement for them.

## 2. Emergency stop

```sh
curl -X POST http://127.0.0.1:8080/controls/publishing \
 -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
 -d '{"paused":true,"reason":"Owner emergency stop"}'
```

This blocks new outbound sends and is checked again just before a media upload. It cannot reverse videos already accepted by a platform. Additionally set `ENABLE_PUBLISH=false` and redeploy for a configuration-level off switch. Review existing `sending/submitted/unknown` rows via `/posts` and the provider dashboard.

## 3. Failed/stalled jobs

Celery beat sweeps stale tasks every 5 minutes. It requeues work that has no external side effects, and marks uncertain external jobs as `unknown`. Never blindly retry a submitted OpenShorts job or social post.

- Recording `unknown`: inspect upstream, then attach the verified OpenShorts job ID using `POST /jobs/{id}/attach-upstream` with `{"job_id":"...","confirmation":"VERIFIED_UPSTREAM_JOB"}`.
- Clip `style_failed`: manually inspect whether re-render happened, then `POST /clips/{id}/retry-style`.
- Clip `scan_failed`: use `POST /clips/{id}/retry-safety`; never approve a failed scan directly.
- Post `unknown`: inspect Upload-Post by `request_id`, and let automatic reconciliation fetch status; no automatic resubmission.
- Emergency `paused`: disable only after checking the incident and current platform warnings.

## 4. Backups

For an **empty/new database** the Compose migration service creates all tables. For production, take routine PostgreSQL backups and store encrypted copies offsite. Run a manual, checksum-verified archive:

```sh
docker compose --profile ops run --rm backup
```

This writes `/backups/cliprush-TIMESTAMP.dump` and `sha256` into named Docker volume `backups`. The backup is local only; **the operator must schedule the command and copy backups off-host**. Validate restore on a disposable server first. `pg_restore` can destroy data; do not use it automatically on the live database.

## 5. Logging and analytics

Audit trail: `GET /ops/audit` (owner edits and publishing-control actions). Status histograms: `GET /ops/stats`. Provider analytics use the per-upload request ID when available; metrics are not substituted with aggregate profile views. The manual USD spend ledger does not read cloud invoices.

## 6. Deployment limitations

No guarantee of licensed-music clearance, zero strikes, delivery under 30 minutes, or revenue without actual field tests. Set TLS, reverse proxy allowlists, rate limiting, Postgres backups, process monitoring and vendor billing alerts before enabling publishing.

## 7. Media retention and owner alerts

The weekly `storage_retention` job deletes old temporary clips only after their review is rejected/blocked or all corresponding posting jobs have reached a terminal state. It also cleans abandoned frame folders and finalized source windows. Default `MEDIA_RETENTION_DAYS=14` (minimum seven days). **Database backups do not contain the media volume.** To preserve raw streams and published clip files longer, back up the `clips` volume independently or increase the retention setting.

Every 30 minutes `ops_alerts` checks unknown recordings/uploads, failed safety scans and paused social accounts; it notifies the configured Telegram owner with a one-hour Redis deduplication window. Alerts only notify; they never retry uploads.
