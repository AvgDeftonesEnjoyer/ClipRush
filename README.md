# ClipRush

Permission-first pipeline for turning **authorized recordings** into candidate vertical clips with [OpenShorts](https://github.com/mutonby/openshorts).

> **Status: week-1 foundation (MVP), not a production autopublisher.** The live chat scout, Whisper/LLM scoring, automated copyright/casino/PII moderation, payout tracking, analytics and reliable end-to-end latency guarantees are **not implemented**. Nothing is posted automatically.

## Included
- FastAPI endpoints to register approved streamer programs, ingest recordings, inspect jobs/clips, review clips and (optionally) manually trigger publishing.
- Celery + Redis background processing, PostgreSQL persistence, idempotency for recordings and signed OpenShorts completion webhooks.
- OpenShorts REST adapter; its upstream source remains unmodified.
- Manual *safe/unsafe* review and explicit approval required before a publish operation. Defaults block publishing.
- Docker Compose, environment template, unit tests and CI.

## Start

```sh
cp .env.example .env
# Set a strong ADMIN_TOKEN and POSTGRES_PASSWORD.
docker compose up --build -d
curl http://localhost:8080/health
```

API documentation: http://localhost:8080/docs. The OpenShorts backend must be launched **separately** per [its Docker guide](https://github.com/mutonby/openshorts#5-gpu-acceleration-optional-nvidia), reachable at `OPENSHORTS_API_URL` (on Linux use a reachable host address). OpenShorts needs its own AI configuration / keys. For hosted OpenShorts supply `OPENSHORTS_API_KEY`. Do not expose port 8080 publicly without TLS, gateway authentication and rate limiting.

### Authorized test flow

1. Register an authorized/verified clipping program using `POST /programs` with `rights_confirmed: true` and `enabled: true`. Record the actual rules and permissions; our checkbox is **not** proof of rights.
2. `POST /recordings` with `program_id`, a public authorized video `source_url` and a unique `external_id`. Processing runs in a worker.
3. OpenShorts calls `POST /hooks/openshorts` with HMAC signature when processing completes. The public `PUBLIC_BASE_URL` must be reachable from OpenShorts; alternatively call `POST /jobs/{id}/sync` to poll its status.
4. `GET /clips`; review each candidate using `POST /clips/{id}/review`, specifying `decision: approve` or `reject`, `safety_checked`, and a reason if rejecting. **Do not approve until gambling, licensed music, PII, violence and program rules have been checked manually.**
5. Publishing is disabled until `ENABLE_PUBLISH=true`. Even then it requires a separate `POST /clips/{id}/publish` (and an Upload-Post/social account configured in OpenShorts). No scheduled or automatic publishing is present.

For protected routes send `Authorization: Bearer YOUR_ADMIN_TOKEN`.

### API examples

```sh
curl -X POST localhost:8080/programs -H 'Authorization: Bearer YOUR_ADMIN_TOKEN' -H 'Content-Type: application/json' \
  -d '{"name":"authorized-demo","rights_confirmed":true,"enabled":true,"rules_url":"https://example.org/rules"}'
curl -X POST localhost:8080/recordings -H 'Authorization: Bearer YOUR_ADMIN_TOKEN' -H 'Content-Type: application/json' \
  -d '{"program_id":1,"external_id":"demo-001","source_url":"https://www.youtube.com/watch?v=VIDEO_ID"}'
```

## Limitations and safeguards

- The pipeline does **not** infer legal permission to repost. Verify program permissions and platform policies independently.
- Submitted URLs are restricted to HTTPS and allowlisted hosts; OpenShorts performs its own downstream validation. Only connect trusted OpenShorts services.
- Published status and platform post URLs are not reconciled yet: a successful publish request is recorded as `submitted`, not as confirmed publication.
- No real video, GPU, Docker or platform integration test has been performed by this PR; CI runs Python-level tests only.
- If OpenShorts completion webhook delivery fails, `/jobs/{id}/sync` is an authenticated recovery endpoint.
- Telegram approval bot, Live-Scout, Detect, advanced moderation and tracking are planned for later phases; **this scaffold does not meet the full 7-week specification**.

## Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```
