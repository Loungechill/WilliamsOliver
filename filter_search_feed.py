#!/usr/bin/env python3
"""Поиск: тот же blacklist + исключение offer ID с более чем 1000 показов."""
from segment_feed import run

# Порог можно менять здесь или через --threshold / Run workflow.
MAX_IMPRESSIONS = 1000
SHEET_GID = "708485822"  # WO / Поиск

if __name__ == "__main__":
    raise SystemExit(run("search", SHEET_GID, MAX_IMPRESSIONS))
