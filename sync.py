"""Pull recent activities from Strava and merge them into activities.csv.

    python sync.py                # everything new since the last sync
    python sync.py --days 30      # everything in the last 30 days
    python sync.py --all          # full history (ignores the watermark)

New activities are normalized into the same column names the analysis
notebook expects and appended to `activities.csv`, deduped by Activity ID.
A small `.strava_sync_state.json` tracks the last-synced timestamp so
repeat runs only fetch what changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from strava_client import StravaClient, StravaAuthError

PROJECT_ROOT = Path(__file__).resolve().parent
CSV_PATH = PROJECT_ROOT / "activities.csv"
STATE_PATH = PROJECT_ROOT / ".strava_sync_state.json"

# CSV uses '%b %d, %Y, %I:%M:%S %p' (e.g. "May 30, 2026, 2:00:00 PM").
CSV_DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"

# Strava sport_type -> the display names used in the bulk-export CSV, so the
# notebook's `isin(['Virtual Ride', 'Ride'])` filters keep working.
SPORT_TYPE_TO_CSV = {
    "Ride": "Ride",
    "VirtualRide": "Virtual Ride",
    "GravelRide": "Ride",
    "MountainBikeRide": "Ride",
    "EBikeRide": "Ride",
    "Run": "Run",
    "VirtualRun": "Virtual Run",
    "Walk": "Walk",
    "WeightTraining": "Weight Training",
    "Workout": "Workout",
}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(last_epoch: int) -> None:
    STATE_PATH.write_text(json.dumps(
        {"last_start_epoch": last_epoch,
         "last_sync": datetime.now(timezone.utc).isoformat()},
        indent=2,
    ))


def determine_after(args, existing: pd.DataFrame) -> int | None:
    """Epoch seconds to fetch activities after, based on flags/state/CSV."""
    if args.all:
        return None
    if args.days is not None:
        return int(datetime.now(timezone.utc).timestamp()) - args.days * 86400
    state = load_state()
    if state.get("last_start_epoch"):
        return int(state["last_start_epoch"])
    # First sync with no state: pick up where the CSV leaves off.
    if not existing.empty and "Activity Date" in existing:
        parsed = pd.to_datetime(existing["Activity Date"],
                                format=CSV_DATE_FORMAT, errors="coerce").dropna()
        if not parsed.empty:
            return int(parsed.max().replace(tzinfo=timezone.utc).timestamp())
    return None


def normalize(activity: dict) -> dict:
    """Map a Strava API activity onto the analysis CSV's columns."""
    start_local = activity.get("start_date_local") or activity.get("start_date")
    dt = datetime.fromisoformat(start_local.replace("Z", "+00:00"))
    sport = activity.get("sport_type") or activity.get("type") or ""
    return {
        "Activity ID": activity["id"],
        "Activity Date": dt.strftime(CSV_DATE_FORMAT),
        "Activity Name": activity.get("name"),
        "Activity Type": SPORT_TYPE_TO_CSV.get(sport, sport),
        "Elapsed Time": activity.get("elapsed_time"),
        "Moving Time": activity.get("moving_time"),
        "Distance": round((activity.get("distance") or 0) / 1000, 2),  # m -> km
        "Max Speed": activity.get("max_speed"),                        # m/s
        "Average Speed": activity.get("average_speed"),               # m/s
        "Elevation Gain": activity.get("total_elevation_gain"),
        "Max Heart Rate": activity.get("max_heartrate"),
        "Average Heart Rate": activity.get("average_heartrate"),
        "Max Cadence": activity.get("max_cadence"),
        "Average Cadence": activity.get("average_cadence"),
        "Max Watts": activity.get("max_watts"),
        "Average Watts": activity.get("average_watts"),
        "Weighted Average Power": activity.get("weighted_average_watts"),
        "Calories": activity.get("calories"),
        "Relative Effort": activity.get("suffer_score"),
        "Commute": activity.get("commute"),
        "From Upload": not activity.get("manual", False),
    }


def merge(existing: pd.DataFrame, new_rows: list[dict]) -> tuple[pd.DataFrame, int]:
    new_df = pd.DataFrame(new_rows)
    if existing.empty:
        return new_df, len(new_df)

    existing_ids = set(pd.to_numeric(existing.get("Activity ID"),
                                     errors="coerce").dropna().astype("int64"))
    new_df = new_df[~new_df["Activity ID"].isin(existing_ids)]
    if new_df.empty:
        return existing, 0

    # Union of columns, preserving the existing file's order first.
    cols = list(existing.columns) + [c for c in new_df.columns
                                     if c not in existing.columns]
    combined = pd.concat([existing, new_df.reindex(columns=new_df.columns)],
                         ignore_index=True)
    return combined.reindex(columns=cols), len(new_df)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Strava activities into activities.csv.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, help="Fetch the last N days.")
    group.add_argument("--all", action="store_true", help="Fetch full history.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be synced without writing.")
    args = parser.parse_args()

    try:
        client = StravaClient()
    except StravaAuthError as exc:
        sys.exit(f"✗ {exc}")
    if not client.is_authenticated:
        sys.exit("✗ Not authenticated. Run `python auth.py` first.")

    existing = pd.read_csv(CSV_PATH) if CSV_PATH.exists() else pd.DataFrame()
    after = determine_after(args, existing)

    when = ("full history" if after is None
            else f"since {datetime.fromtimestamp(after, timezone.utc):%b %d, %Y %H:%M UTC}")
    print(f"Fetching activities ({when})…")
    activities = client.list_activities(after=after)
    print(f"  Strava returned {len(activities)} activities.")
    if not activities:
        print("✓ Nothing new to sync.")
        return

    new_rows = [normalize(a) for a in activities]
    latest_epoch = max(
        int(datetime.fromisoformat(
            (a.get("start_date") or a["start_date_local"]).replace("Z", "+00:00")
        ).timestamp())
        for a in activities
    )

    combined, added = merge(existing, new_rows)
    if added == 0:
        print("✓ Already up to date (all fetched activities were known).")
        if not args.dry_run:
            save_state(latest_epoch)
        return

    print(f"  {added} new activit{'y' if added == 1 else 'ies'} to add:")
    preview = pd.DataFrame(new_rows)[["Activity Date", "Activity Type",
                                      "Distance", "Activity Name"]].tail(added)
    for _, row in preview.iterrows():
        print(f"    {row['Activity Date']:<26} {str(row['Activity Type']):<14} "
              f"{row['Distance']:>6} km  {row['Activity Name']}")

    if args.dry_run:
        print("\n(dry run — nothing written)")
        return

    combined.to_csv(CSV_PATH, index=False)
    save_state(latest_epoch)
    print(f"✓ Appended {added} activities to {CSV_PATH.name} "
          f"({len(combined)} total). Re-run the notebook to refresh the analysis.")


if __name__ == "__main__":
    main()
