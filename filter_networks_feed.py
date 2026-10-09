#!/usr/bin/env python3
"""Сети: тот же blacklist + исключение offer ID с более чем 1000 показов."""
from segment_feed import run

MAX_IMPRESSIONS = 1000
SHEET_GID = "0"  # WO / Сети

if __name__ == "__main__":
    raise SystemExit(run("networks", SHEET_GID, MAX_IMPRESSIONS))
