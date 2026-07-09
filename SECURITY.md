# Security — AX Relay

- **Never commit `.env`** or any file containing API keys, bot tokens, or chat IDs. Use `.env.example` for variable names only.
- If a secret is exposed (committed, pasted in a issue, or shared by mistake), **rotate it immediately** at the provider (OpenRouter, Groq, Telegram BotFather, etc.) and update your local `.env`.
- The Telegram bridge only accepts commands from `TELEGRAM_ALLOWED_CHAT_ID`; keep that value private.
