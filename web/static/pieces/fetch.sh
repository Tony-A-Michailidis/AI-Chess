#!/bin/bash
# Fetches the Cburnett piece set from Wikimedia Commons (CC BY-SA 3.0).
# Only downloads files that are missing or obviously not SVG, and paces the
# requests so Commons doesn't rate-limit us.
cd "$(dirname "$0")" || exit 1
UA='chess-arena-local/1.0 (local dev; contact: none)'
for f in klt qlt rlt blt nlt plt kdt qdt rdt bdt ndt pdt; do
  if [ -s "${f}.svg" ] && head -c 6 "${f}.svg" | grep -q '<'  && ! grep -qi 'DOCTYPE html' "${f}.svg"; then
    printf 'ok   %s (%s bytes, cached)\n' "${f}.svg" "$(stat -c %s "${f}.svg")"
    continue
  fi
  for try in 1 2 3; do
    curl -sL --max-time 30 -A "$UA" -o "${f}.svg" \
      "https://commons.wikimedia.org/wiki/Special:FilePath/Chess_${f}45.svg"
    if ! grep -qi 'DOCTYPE html' "${f}.svg"; then break; fi
    sleep 3
  done
  if grep -qi 'DOCTYPE html' "${f}.svg"; then
    printf 'FAIL %s\n' "${f}.svg"
  else
    printf 'ok   %s (%s bytes)\n' "${f}.svg" "$(stat -c %s "${f}.svg")"
  fi
  sleep 1
done
