#!/usr/bin/env bash
set -euo pipefail

# Checks only files that Git would include in a public commit. It reports file
# paths—not matched content—so a failed check does not echo a secret into logs.
ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"

failed=0

check_content() {
  local label="$1"
  local pattern="$2"
  local matches=""
  local file

  while IFS= read -r -d '' file; do
    # This script contains the detection patterns themselves.
    [[ "$file" == "scripts/check-public-safety.sh" ]] && continue

    if grep -I -E -q "$pattern" -- "$file"; then
      matches+="$file"$'\n'
    fi
  done < <(git ls-files -z --cached --others --exclude-standard)

  if [[ -n "$matches" ]]; then
    printf 'Public-safety check failed: %s found in:\n%s' "$label" "$matches" >&2
    failed=1
  fi
}

check_path() {
  local file

  while IFS= read -r -d '' file; do
    case "$file" in
      .env|.env.local|.env.development|.env.production|.npmrc|settings.local.json|*.pem|*.key|*.p12|*.pfx|*.mobileprovision|credentials*.json|service-account*.json)
        printf 'Public-safety check failed: sensitive file staged for publication: %s\n' "$file" >&2
        failed=1
        ;;
    esac
  done < <(git ls-files -z --cached --others --exclude-standard)
}

# Provider and credential formats with enough structure to avoid flagging the
# variable names documented in .env.example.
check_content "credential-like value" '(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9_-]{20,}|csk_[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|[0-9]{7,12}:[A-Za-z0-9_-]{25,}|-----BEGIN( [A-Z]+)? PRIVATE KEY-----)'
check_content "machine-specific home path" '(^|[^[:alnum:]_])(/Users/|/home/)'
check_content "email address" '[[:alnum:]._%+-]+@[[:alnum:].-]+\.[[:alpha:]]{2,}'
check_content "retired project name" '(MacBridge|Monster|OpenClaw-G)'
check_path

if (( failed )); then
  exit 1
fi

printf 'Public-safety check passed.\n'
