# Required channel membership

Protected features require membership in `@gamas_AI` by default. This gate is
separate from credit, bans and admin authorization.

## Setup on either platform

1. Make the bot an **administrator** of the configured Telegram channel.
2. Edit existing `.env` keys; do not append duplicates:

```dotenv
SUBSCRIPTION_CHANNEL=@gamas_AI
SUBSCRIPTION_REQUIRED=true
SUBSCRIPTION_CACHE_SECONDS=0
SUBSCRIPTION_TIMEOUT_SECONDS=20
TELEGRAM_API_BASE_URL=https://api.telegram.org
```

3. Run the [check](CONFIGURATION.md#validate), then restart using the
   [Windows](DEPLOY_WINDOWS.md) or [cPanel](DEPLOY_CPANEL.md) guide.
4. Inspect subscription self-check logs. With a **non-admin** test account: leave
   channel → join prompt; join → **🔍 بررسی عضویت** → allowed; leave again → blocked
   on the next protected interaction.

Use a resolvable public username or numeric channel ID. For a private channel,
verify the user join-link workflow manually. `SUBSCRIPTION_REQUIRED=false` disables
the gate for testing/administration. Migrations run at normal startup; no separate
migration command is needed.

## Behavior and diagnosis

- `creator`, `administrator`, `member`, `restricted` count as members. Confirmed
  `left`/`kicked` or not-participant responses block access.
- `ADMIN_IDS` are exempt; do not use admin accounts to test enforcement.
- Cache `0` checks every interaction. Positive values cache confirmed members;
  concurrent checks for one user share a request.
- **Infrastructure errors fail open** and alert admins with throttling. Missing
  bot admin rights or network failures can therefore allow access; fix warnings.
- For rate limits, reduce requests or set a short positive cache. Cached members
  may retain access briefly after leaving.

Bot API `getChatMember` checks membership; the bot cannot join users to a channel.
Never share token-bearing Bot API URLs in diagnostics.
