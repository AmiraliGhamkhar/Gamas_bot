# Required channel membership

Value delivery requires membership in `@gamas_AI` by default. This gate is
separate from credit, bans and admin authorization.

The gate is **deferred**, not global: `/start`, help, wallet, purchases, support,
library browsing and receipt upload stay available, so a non-member can pay and
ask for help. File submissions, booklet generation, quizzes and flashcards are
gated at the moment work would begin; flashcard DOCX export is also gated.
Answering an already-paid quiz session remains ungated. A blocked file is remembered for up to
seven days and resumes automatically after **✅ بررسی عضویت** succeeds; the user
does not send the file again.

## Setup on either platform

1. Make the bot an **administrator** of the configured Telegram channel.
2. Edit existing `.env` keys; do not append duplicates:

```dotenv
SUBSCRIPTION_CHANNEL=@gamas_AI
SUBSCRIPTION_REQUIRED=true
SUBSCRIPTION_CACHE_SECONDS=300
SUBSCRIPTION_TIMEOUT_SECONDS=20
TELEGRAM_API_BASE_URL=https://api.telegram.org
```

3. Run the [check](CONFIGURATION.md#validate), then restart using the
   [Windows](DEPLOY_WINDOWS.md) or [cPanel](DEPLOY_CPANEL.md) guide.
4. Inspect subscription self-check logs. With a **non-admin** test account:
   leave the channel → sending a file shows the join prompt; join → **✅ بررسی
   عضویت** → the original file resumes; leave again → the next submission is
   blocked within five minutes (or immediately with cache 0). Help, wallet and support must still open while not a member.

Use a resolvable public username or numeric channel ID. For a private channel,
verify the user join-link workflow manually. `SUBSCRIPTION_REQUIRED=false` disables
the gate for testing/administration. Migrations run at normal startup; no separate
migration command is needed.

## Behavior and diagnosis

- `creator`, `administrator`, `member`, `restricted` count as members. Confirmed
  `left`/`kicked` or not-participant responses block access.
- `ADMIN_IDS` are exempt; do not use admin accounts to test enforcement.
- Cache `0` checks every gated action. The default 300 seconds caches confirmed members;
  concurrent checks for one user share a request.
- A bounded sweep re-checks about 20 recently active users every six hours,
  one request at a time, oldest check first. It resumes after restarts through
  `users.subscription_checked_at`, never cancels running jobs, and stops the
  batch on an inconclusive check instead of mass-blocking users.
- **Infrastructure errors fail open** and alert admins with throttling. Missing
  bot admin rights or network failures can therefore allow access; fix warnings.
- For rate limits, reduce requests or set a short positive cache. Cached members
  may retain access briefly after leaving.

Bot API `getChatMember` checks membership; the bot cannot join users to a channel.
Never share token-bearing Bot API URLs in diagnostics.
