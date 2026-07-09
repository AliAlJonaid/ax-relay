# Security — AX Relay

- **Never commit `.env`** or any file containing API keys, bot tokens, or chat IDs. Use `.env.example` for variable names only.
- If a secret is exposed (committed, pasted in a issue, or shared by mistake), **rotate it immediately** at the provider (OpenRouter, Groq, Telegram BotFather, etc.) and update your local `.env`.
- The Telegram bridge only accepts control commands from `TELEGRAM_ALLOWED_CHAT_ID`; keep that value private. If it is unset, control commands fail closed and only `/whoami` is available for setup.
- Destructive-action detection is a confirmation heuristic, not a hardened authorization boundary. It evaluates both model text and selected Accessibility-element metadata; use least-privilege accounts and keep sensitive applications out of scope.
