#!/usr/bin/env bash
set -euo pipefail

# Installs the Linux x64 release for the GitHub Actions runner.
: "${RUNNER_TEMP:?RUNNER_TEMP must point to a temporary directory}"
: "${GITHUB_PATH:?GITHUB_PATH must point to the Actions path file}"
: "${GITLEAKS_VERSION:?GITLEAKS_VERSION must be set}"
: "${GITLEAKS_SHA256:?GITLEAKS_SHA256 must be set}"

archive="${RUNNER_TEMP}/gitleaks.tar.gz"
curl --fail --silent --show-error --location --retry 3 \
  "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" \
  --output "$archive"
printf '%s  %s\n' "$GITLEAKS_SHA256" "$archive" | sha256sum --check --strict
mkdir -p "${RUNNER_TEMP}/gitleaks-bin"
tar -xzf "$archive" -C "${RUNNER_TEMP}/gitleaks-bin" gitleaks
echo "${RUNNER_TEMP}/gitleaks-bin" >> "$GITHUB_PATH"
