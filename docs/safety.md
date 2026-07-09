# Safety and privacy

## Configuration

Credentials, bot tokens, authorized chat IDs, and provider settings belong only in a local `.env` file. The repository includes `.env.example` with variable names only.

```bash
cp .env.example .env
# Add values locally. Never commit this file.
bash scripts/check-public-safety.sh
```

The project ignores common secret and machine-local files, and its CI runs the same public-safety check before portable tests.

## Operating boundaries

- The remote bridge accepts commands only from the configured authorized chat ID.
- The executor has a gate for destructive-looking actions that requires confirmation through its configured callback.
- The agent treats a visual or textual claim of completion as insufficient without a verification step.
- A watchdog can restart the bridge process after a failure; it cannot prove that a task was correct.

## Data boundary

Selected providers may receive task text and Accessibility-tree observations. Optional screenshots and remote notifications can expose more context. Use least-privilege accounts, avoid sensitive applications, and review the data policies of each configured provider and channel.

## Reporting a vulnerability

Do not open a public issue containing a credential, token, user identifier, screenshot, or reproduction trace with private data. Remove/rotate the sensitive value first, then report the issue with a sanitized description.

