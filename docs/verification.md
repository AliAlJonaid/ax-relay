# Verification

AX Relay separates portable unit tests from live macOS checks.

## Portable automated checks

Run from the repository root:

```bash
bash scripts/check-public-safety.sh
cd agents
../.venv/bin/python test_rate_limit_verifier.py
../.venv/bin/python test_lessons.py
../.venv/bin/python test_appname_verify.py
../.venv/bin/python test_executor_gate.py
```

| Suite | What it exercises |
|---|---|
| `test_rate_limit_verifier.py` | Provider failover, retry limits, timeout handling, and verifier guards. |
| `test_lessons.py` | Persistence, deduplication, and corrupt-state resilience for lessons. |
| `test_appname_verify.py` | App-name resolution, task-start baselines, and send-versus-type safeguards. |
| `test_executor_gate.py` | Destructive-action confirmation behavior without moving the real mouse or using the network. |
| `check-public-safety.sh` | Detects common credential formats, personal email/home paths, sensitive files, and retired project names in Git-includable files. |

These four Python suites and the public-safety scan passed locally on 2026-07-09 with Python 3.11. The GitHub Actions workflow runs the portable suites on Linux.

## macOS-only checks

Additional tests cover Accessibility-tree, perception, interrupt, Reflexion, and Telegram-control behavior. They require PyObjC or a live macOS environment, so the Linux workflow intentionally does not represent them as portable CI coverage.

## Manual acceptance scenarios

Before a release, run the following on a non-sensitive test account and a disposable task:

1. Confirm that the app exposes expected Accessibility elements.
2. Verify that a model-selected element ID maps to the intended interface element.
3. Trigger a destructive-looking action and confirm the gate asks for confirmation.
4. Test an intentionally stale state and verify it is not accepted as a new result.
5. Force a provider failure and confirm the configured failure behavior is visible and bounded.

No personal chat logs, screenshots, task history, or provider credentials are used as public evidence.

