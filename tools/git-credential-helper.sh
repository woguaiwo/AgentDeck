#!/usr/bin/env bash
set -euo pipefail

host=""
protocol=""

while IFS= read -r line; do
  [[ -z "$line" ]] && break
  case "$line" in
    host=*) host="${line#host=}" ;;
    protocol=*) protocol="${line#protocol=}" ;;
  esac
done

if [[ "$protocol" != "https" || "$host" != "github.com" ]]; then
  exit 1
fi

token="${GITHUB_TOKEN:-${GH_TOKEN:-${AGENTDECK_GITHUB_TOKEN:-}}}"
if [[ -z "$token" ]]; then
  exit 1
fi

printf 'username=x-access-token\n'
printf 'password=%s\n' "$token"
