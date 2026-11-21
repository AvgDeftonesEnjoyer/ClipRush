# ClipRush

**ClipRush** — конвейер для подготовки и публикации клипов из **разрешённых** стримов через [OpenShorts](https://github.com/mutonby/openshorts) и [Upload-Post](https://docs.upload-post.com/api/upload-video/). Python/FastAPI, Celery/Redis, PostgreSQL, Twitch/Kick/YouTube Live + Whisper + LLM, Telegram-бот, учёт просмотров и выплат.

**Состояние:** программная реализация основных модулей находится в Draft PR. Деплой, реальные OAuth-подключения и интеграционные проверки на GPU, эфирах и платформах не выполнены. Нельзя считать это готовой автономной системой с доказанными KPI. По умолчанию **публикация отключена**.

## Состав

| Этап | Реализованная точка интеграции | Ограничение |
|---|---|---|
| 1 Live-Scout | Twitch Helix+IRC; Kick подписанные webhooks и Redis; YouTube liveChatMessages; всплески чата и кольцевой буфер сегментов | Для Kick нужен app webhook/subscription, для YouTube — API key и 11-символьный ID текущего эфира |
| 2 Detect | faster-whisper с таймкодами, LLM via OpenRouter, score 6+, окно 15–45 с | Требуются модель, ресурсы и ключи |
| 3 Edit | OpenShorts REST, 9:16, субтитры, хук и 6 реально применяемых пресетов OpenShorts | Нужно развернуть OpenShorts отдельно; качество требует ручной QA |
| 4 Package/Filter | анализ речи/кадров/OCR/AcoustID, отклонение подозрительных клипов | Не доказывает отсутствие лицензированной музыки; неподтверждённые сигналы только на ручную проверку |
| 5 Approve | Telegram owner-only webhook и кнопки, или защищённый FastAPI | Нужны токены и реальный просмотр владельцем |
| 6 Publish | Upload-Post REST, account quotas, idempotency, reconciliation | Не включается без ключей/профилей/настройки |
| 7 Track | Upload-Post per-post analytics, таблицы metrics/payouts, ввод подтверждённых выплат | Зависит от предоставленных платформой метрик и правил программы |
| 8 Learn | еженедельный отчёт, 72-часовые выборки, точки решения 10 января, минимум 100 зрелых постов для A/B-рекомендаций | Промпты/затраты не меняет без явного одобрения |

### Запуск инфраструктуры

```sh
cp .env.example .env
# Set strong ADMIN_TOKEN and POSTGRES_PASSWORD and matching DB credentials.
docker compose up --build -d
curl http://localhost:8080/health
```

API Docs: **http://localhost:8080/docs**. Админ-запросы: `Authorization: Bearer <ADMIN_TOKEN>`.

Запусти OpenShorts **отдельно** из `mutonby/openshorts`: `docker compose up --build -d`, настрой Gemini или LLM. `OPENSHORTS_API_URL` должен быть доступен контейнеру ClipRush; для Docker на Linux есть `host.docker.internal:host-gateway`.

Добавь в `.env` ключи: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_VISION_MODEL`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_BOT_NICK`, `TWITCH_BOT_OAUTH` (с префиксом `oauth:`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TELEGRAM_WEBHOOK_SECRET`, `UPLOAD_POST_API_KEY`, `ACOUSTID_API_KEY` (опционально). Конфигурация в `.env.example`.

### Kick и YouTube Live

**Kick:** создай Kick Developer App, включи публичный HTTPS webhook \`https://<domain>/hooks/kick\` в настройках приложения, настрой официальный app token и подпишись на \`chat.message.sent\` и \`livestream.status.updated\` для **разрешённых** \`broadcaster_user_id\`. После этого поставь \`KICK_WEBHOOKS_ENABLED=true\`, добавь стримера с \`platform=kick\`, \`channel=<kick-slug>\`. Сервер проверяет RSA-подпись, timestamp и ID сообщений и принимает сигналы только из зарегистрированных разрешённых программ. Если callback не приходит, запись не стартует.

**YouTube:** создай API key с доступом к YouTube Data API v3 и положи его в \`YOUTUBE_API_KEY\`. Зарегистрируй стрим с \`platform=youtube\`, \`channel=<11-char-video-id>\` (именно идентификатор текущего live-видео, не @handle). Поллинг использует \`videos.list\` и \`liveChatMessages.list\` с учётом \`pollingIntervalMillis\`; разрешённые записи читает Streamlink. При смене live video ID потребуется обновить запись стримера. API quota и права доступа зависят от Google-проекта.

Официальные источники: [Kick webhooks](https://github.com/KickEngineering/KickDevDocs/blob/main/events/webhook-security.md), [Kick event types](https://github.com/KickEngineering/KickDevDocs/blob/main/events/event-types.md), [YouTube Live chat](https://developers.google.com/youtube/data/live/streaming-live-chat), [Streamlink plugins](https://streamlink.github.io/plugins.html).

### Подключение Telegram

Публичный HTTPS reverse proxy должен безопасно проксировать `/hooks/telegram` и `/hooks/openshorts` на локальный `8080`. Установи `PUBLIC_BASE_URL=https://<your-domain>` и случайные секреты подписей.

```sh
curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://<your-domain>/hooks/telegram" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"
```

Без доступного webhook OpenShorts результаты синхронизируются Celery beat каждые 3 минуты. Для Telegram webhook альтернативы long-polling нет.

### Включение источника и аккаунтов

Все действия требуют ручного подтверждения прав и действующих правил конкурса. Ниже демонстрация с **выдуманным** тестовым правилом, не реальный конкурс:

```sh
export API=http://localhost:8080
export TOKEN='<ADMIN_TOKEN>'
curl -X POST "$API/programs" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
 -d '{"name":"licensed-example","rules_url":"https://example.com/authorized-rules","rights_confirmed":true,"enabled":true,"currency":"USD","min_views":100000,"reward_cents":2500}'
curl -X POST "$API/streamers" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
 -d '{"program_id":1,"platform":"twitch","channel":"authorizedchannel","enabled":true}'
curl -X POST "$API/recordings" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
 -d '{"program_id":1,"external_id":"test-001","source_url":"https://www.youtube.com/watch?v=REPLACE_WITH_AUTHORIZED_SOURCE"}'
```

Для публикации создай и подключи профиль **в Upload-Post**, затем зарегистрируй `POST /accounts` с `{"platform":"youtube","profile":"my-upload-post-profile","enabled":true}`. Проверяй `GET /clips` и видео, одобряй `POST /clips/{id}/review` с `{"decision":"approve","safety_checked":true}`. Лишь после интеграционных тестов установи `ENABLE_PUBLISH=true` и запроси `POST /clips/{id}/publish` с `{"account_ids":[1],"confirmation":"PUBLISH"}`.

При `AUTO_PUBLISH_ON_APPROVAL=true` одобрение в Telegram ставит ролик в очередь **только** после ручной проверки и при `ENABLE_PUBLISH=true`.

### Поиск конкурсов и правила

Чтобы получать кандидатов из конкретных Telegram-каналов, добавь бота администратором в разрешённые каналы, перечисли их числовые chat ID в `DISCOVERY_CHAT_IDS` через запятую и разреши `channel_post` при установке webhook. Новые сообщения с явными словами про конкурсы записываются как кандидаты и отправляются владельцу. Ставки и разрешения перепроверяются вручную; программа **не** включается автоматически. Whop требует отдельной авторизованной интеграции.

### Расходы и выплаты

Указывай фактические расходы вручную через `POST /expenses` (поля `vendor`, `amount_cents`, `note`), смотри `GET /budget`. После записанных расходов на сумму `BUDGET_LIMIT_USD` новые задачи обработки и отправки приостанавливаются. **Это не автосинхронизация биллинга:** неучтённые счета OpenRouter, GPU и Upload-Post могут превысить лимит.

Когда пост достигает заданного порога, система помечает выплату `eligible` и присылает Telegram-уведомление со ссылками. Подтвердить деньги можно через `POST /payouts/{id}/confirm` с суммой и доказательством перевода.

### Критические ограничения

- **Нужны разрешения на репост** каждого источника, отдельные от доступа к просмотру эфира.
- У проекта нет способа гарантировать 0 нарушений/страйков или соответствие правилам всех платформ. Vision/LLM/OCR/AcoustID не заменяют проверки прав на музыку.
- API не выдаёт внешний доступ по умолчанию; только `127.0.0.1:8080`. Для публичных webhook настрой TLS, прокси, правила доступа и мониторинг.
- Нельзя менять схему действующей БД только `create_all`: для production нужны миграции; сначала делай бэкап.
- Live-адаптеры **Twitch, Kick и YouTube** есть в коде, но на реальных авторизованных эфирах Kick/YouTube не тестировались; нужны подписки Kick или YouTube API key, а также разрешение на запись и репост.
- Прогноз выплат рассчитан по вручную введённым ставкам, **не** подтверждает реальное право на выплату. Выплату подтверждает только владелец с доказательством.
- При непредвиденной ошибке в публикации пометка `unknown` требует сверки с Upload-Post `request_id` — повторной отправки по таймауту нет.
- Результаты OpenShorts с посторонних доменов не скачиваются (SSRF-защита); облачная выдача подписанных S3 URL пока требует отдельной разрешённой интеграции.
- Нет подтверждённых испытаний 10–20 роликов/день, 30 минут SLA или реальных выплат. Нет фактического продакшен-деплоя.

Тесты: `python -m pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -q`.

Дорожная карта/критерии фактической приёмки: [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md).
