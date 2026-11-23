# Alert channels

Every channel you list receives every alert. List them in `.env` (or as Render variables / GitHub secrets):

```
NOTIFY_PROVIDERS=pushover,email,whatsapp
```

A channel that fails is retried on its own; the others are not sent twice. The dashboard's Notifications tab shows each channel's result. After setup, press **Test notification** with your phone locked.

## Which ones

| Channel | Cost | Notes |
|---|---|---|
| Pushover | One-time app purchase after a trial | Best for not missing it: repeats every 30 s until acknowledged, can break through silent mode |
| ntfy | Free | Good. Use a long random topic |
| Email | Free | Reliable but slow to be noticed. Good as a record |
| WhatsApp (CallMeBot) | Free, personal use only | Easiest WhatsApp route. Third-party service, no delivery guarantee |
| WhatsApp (Cloud API) | Meta's official API | Needs a Meta business setup and an approved template. More work, more dependable |
| Telegram | Free | Unmute the bot |

Suggested: `pushover,email,whatsapp`.

## Email (Gmail)

1. Turn on 2-Step Verification for the Google account.
2. Create an app password: Google Account → Security → App passwords. Copy the 16 characters.
3. Set:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=<app password>
EMAIL_TO=you@gmail.com
```

`EMAIL_TO` can hold several addresses separated by commas. Other providers: use their SMTP host; port 587 goes with `SMTP_SECURITY=starttls`. Mark the first alert "not spam" if it lands there.

## WhatsApp with CallMeBot

1. Follow the activation steps at https://www.callmebot.com (WhatsApp section): add the bot's number to your contacts and send it the activation message it gives.
2. It replies with your API key.
3. Set `CALLMEBOT_PHONE` (with country code, e.g. `+919812345678`) and `CALLMEBOT_APIKEY`.

The API key is personal; keep it secret.

## WhatsApp with Meta's Cloud API

Only if you want the official route.

1. In Meta for Developers create an app with WhatsApp, and note the **phone number ID** and a **permanent access token**.
2. Create a message template, category Utility, language English, body:
   `{{1}} Book here: {{2}}`
   Wait for approval.
3. Set `WA_CLOUD_TOKEN`, `WA_CLOUD_PHONE_NUMBER_ID`, `WA_CLOUD_TO` (your number with country code), `WA_CLOUD_TEMPLATE` (the template name), and `WA_CLOUD_LANGUAGE` if not `en`. Add `whatsapp_cloud` to `NOTIFY_PROVIDERS`.

Meta charges per business-initiated message in some cases; check its current pricing. `WA_CLOUD_API_VERSION` defaults to v23.0; change it if Meta retires that version.

## Pushover

1. Install Pushover on the iPhone and sign in. Copy your **User Key**.
2. On pushover.net create an application. Copy its **API Token**.
3. Set `PUSHOVER_USER_KEY` and `PUSHOVER_APP_TOKEN`.
4. Allow critical alerts in the app's settings.

## ntfy

1. Make a topic: `python -c "import secrets; print('dw-' + secrets.token_urlsafe(18))"`
2. Subscribe to it in the ntfy iPhone app.
3. Set `NTFY_TOPIC`.

## Telegram

1. Message `@BotFather`, send `/newbot`, copy the token.
2. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## On the phone

Allow notifications for each app and add them to any Focus mode you use at school.

## What you will receive

- Show available, available again, sold out, or a change in how full it is (at most one of the last per 30 minutes).
- WATCHER HEALTH messages, at most one per kind per 6 hours (`notifications.system_alerts: false` to turn off).
- BookMyShow reminders, only if you set times.
- If you run both Render and GitHub, the same alert can arrive twice; the GitHub copy ends "Sent by: github watcher".

## Dead-man's switch

Create a free check at healthchecks.io (period 5 minutes, grace 5 minutes), connect it to your phone or email, and put its ping URL in `HEARTBEAT_URL` on Render. For the GitHub runner use a second check (period 15 minutes) as the `GITHUB_HEARTBEAT_URL` secret.
