#!/usr/bin/env bash
# Saves site/status.json to main. Called by both monitor jobs in .github/workflows/monitor.yml
# (the normal check and the second opinion), so the rule below lives in one place.
#
#   bash save-status.sh <state>      state = operational | partial | outage (goes into the commit message)
#
# GitHub sometimes refuses the push for a short while ("remote rejected ... Internal Server Error",
# 2026-11-25): three tries in 15 seconds all failed and the owner got a "Run failed" email. Keep
# trying for about three minutes, waiting longer each time, before giving up.
set -e

git config user.name "klc-status-monitor"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add site/status.json
git commit -m "status: ${1:-unknown}"
for wait in 10 20 40 60 60 0; do
  git pull --rebase origin main && git push origin HEAD:main && exit 0
  git rebase --abort || true
  sleep "$wait"
done
exit 1
