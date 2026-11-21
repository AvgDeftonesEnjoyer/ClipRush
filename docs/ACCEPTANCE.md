# ClipRush acceptance and owner gates

Specification dated 22 November 2026; end-of-week-7 decision: **10 January 2027**. These are targets, not statements of current performance.

| Phase | Implementation | Evidence still needed |
|---|---|---|
| Week 1 (23–29 Nov) recordings | OpenShorts REST adapter, Docker Compose, recording queue, review API | Run real source video and confirm 10+ safe output clips / day; submit YouTube API audit from owned Google account |
| Weeks 2–3 (30 Nov–13 Dec) live cycle | Twitch auth+IRC scout, rolling segment buffer, Whisper+LLM, moderation, owner Telegram | Live 2-hour test with end-to-end median source-to-Telegram <=20min and source-to-platform <=30min; 20 curated bad clips all blocked; verify streaming capture stability |
| Weeks 4–7 (14 Dec–10 Jan) production | Upload-Post handoff, quota guards, status reconciliation, per-post metrics, weekly reports | 3–5 verified programs, real posting accounts, 4-week stable run, payout evidence, zero strikes; then real decision on scale/switch/stop |

## External owner-only gates

1. Verify rights from the creator and clipping contest; verify the program's published rules, current payout methods and supported countries/currencies. Never activate unsupported/unlicensed streams.
2. Create and own YouTube/TikTok/Instagram and Upload-Post accounts; complete OAuth, developer compliance and platform audits where applicable. An unaudited private API upload is not public distribution.
3. Deploy secured GPU compute, PostgreSQL backups, TLS domain and key management. OpenShorts requires its own deployment and vendor keys.
4. Create Telegram bot, set owner ID and signed webhook, approve clips after watching. Connect each social account; set ENABLE_PUBLISH true **only after** dry-run.
5. Confirm payouts with evidence and choose at the checkpoint. No program payouts can be guaranteed by software.

## Test matrix (must be executed against real services)

- [ ] Docker Compose + OpenShorts healthy, no plaintext secrets in Git.
- [ ] 3 authorized archived broadcasts -> at least 10 usable candidates / day.
- [ ] A real Twitch stream -> ring buffer -> signal -> faster-whisper -> OpenShorts -> Telegram.
- [ ] 20 unsafe samples (casino promo, licensed songs, PII, hate, violent content, duplicates) -> **no false negatives** on the curated set. Do not claim full coverage from 20 samples.
- [ ] Speed median <=20min to Telegram and <=30min to published link.
- [ ] Multiple accounts obey onboarding caps, 2h gap and warning pause.
- [ ] Provider idempotency verified under network outage.
- [ ] 72h analytics for 100+ published clips; weekly tuning recommendation and A/B protocol.
- [ ] Actual program submission, one verified payout, cost under USD 80/month, zero strikes.
- [ ] Jan 10: choose scale/niche/stop from recorded evidence.

## Gaps / intentionally not represented as complete

- Kick and YouTube Live chat ingest/recording are not implemented.
- Authorized Telegram channel discovery is implemented for channels where the bot is an admin; Whop API discovery is not connected.
- No external guarantee of detecting licensed music. AcoustID is a heuristic.
- Caption presets are reapplied per clip via OpenShorts /api/subtitle; varied framing and title templates still require additional integration / QA.
- Weekly top/bottom 10% and suggestions run from 72h+ samples; actual A/B test allocation and prompt changes still require verified samples and owner approval.
- Fallback AI-laboratory video creation requires separate original content generation credentials and rights.
- No independent automatic enforcement of vendor dollar spend is possible without vendor billing records.
