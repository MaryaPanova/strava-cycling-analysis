"""Push a workout to Strava — no manual export needed.

Two modes:

  Manual activity (distance in km, time as seconds or H:MM:SS):
    python upload.py --type ride --distance 45.2 --time "1:30:00" --name "Evening loop"
    python upload.py --type virtualride --distance 30 --time 3600 --name "Zwift" --trainer

  File upload (GPX / TCX / FIT, optionally .gz):
    python upload.py --file morning_ride.gpx
    python upload.py --file ride.fit --name "Sunday long ride"
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from strava_client import StravaClient, StravaAuthError

# Friendly CLI type -> Strava sport_type.
SPORT_TYPES = {
    "ride": "Ride",
    "virtualride": "VirtualRide",
    "virtual": "VirtualRide",
    "zwift": "VirtualRide",
    "gravel": "GravelRide",
    "mtb": "MountainBikeRide",
    "ebike": "EBikeRide",
    "run": "Run",
    "walk": "Walk",
    "workout": "Workout",
    "weighttraining": "WeightTraining",
    "gym": "WeightTraining",
}

# data_type inferred from a file's extension.
DATA_TYPES = {
    ".gpx": "gpx",
    ".tcx": "tcx",
    ".fit": "fit",
}


def parse_time(value: str) -> int:
    """Accept seconds ('5400') or H:MM:SS / MM:SS and return seconds."""
    value = value.strip()
    if ":" in value:
        parts = [int(p) for p in value.split(":")]
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return int(float(value))


def infer_data_type(path: Path) -> str:
    suffixes = path.suffixes  # e.g. ['.fit', '.gz']
    gz = suffixes and suffixes[-1] == ".gz"
    base = suffixes[-2] if gz else (suffixes[-1] if suffixes else "")
    base = base.lower()
    if base not in DATA_TYPES:
        raise ValueError(
            f"Unsupported file type '{path.name}'. Use GPX, TCX, or FIT "
            "(optionally .gz)."
        )
    return DATA_TYPES[base] + (".gz" if gz else "")


def do_file_upload(client: StravaClient, args) -> None:
    path = Path(args.file)
    if not path.exists():
        sys.exit(f"✗ File not found: {path}")
    data_type = infer_data_type(path)
    print(f"Uploading {path.name} ({data_type})…")
    result = client.upload_file(
        path,
        data_type=data_type,
        name=args.name,
        description=args.description,
        activity_type=SPORT_TYPES.get((args.type or "").lower()),
        trainer=args.trainer,
    )
    upload_id = result["id"]
    print(f"  Upload queued (id {upload_id}); waiting for Strava to process…")
    status = client.wait_for_upload(upload_id)
    activity_id = status["activity_id"]
    print(f"✓ Activity created: https://www.strava.com/activities/{activity_id}")


def do_manual_activity(client: StravaClient, args) -> None:
    if args.type is None or args.time is None:
        sys.exit("✗ Manual activity needs at least --type and --time.")
    sport_type = SPORT_TYPES.get(args.type.lower())
    if sport_type is None:
        sys.exit(f"✗ Unknown --type '{args.type}'. "
                 f"Choices: {', '.join(sorted(set(SPORT_TYPES)))}.")

    elapsed = parse_time(args.time)
    distance_m = round(args.distance * 1000, 1) if args.distance is not None else None

    if args.date:
        start_local = args.date
    else:
        start_local = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    name = args.name or f"{sport_type} ({elapsed // 60} min)"
    print(f"Creating manual activity '{name}' ({sport_type})…")
    activity = client.create_activity(
        name=name,
        sport_type=sport_type,
        start_date_local=start_local,
        elapsed_time=elapsed,
        distance=distance_m,
        description=args.description,
        trainer=args.trainer,
    )
    print(f"✓ Activity created: "
          f"https://www.strava.com/activities/{activity['id']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Upload a workout to Strava (manual entry or a GPS file).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", help="Path to a GPX/TCX/FIT file to upload.")
    p.add_argument("--type", help="Activity type, e.g. ride, virtualride, run, gym.")
    p.add_argument("--distance", type=float, help="Distance in km (manual entry).")
    p.add_argument("--time", help="Duration: seconds (5400) or H:MM:SS.")
    p.add_argument("--name", help="Activity name.")
    p.add_argument("--description", help="Activity description / notes.")
    p.add_argument("--date", help="Start time, ISO 8601 (default: now, UTC).")
    p.add_argument("--trainer", action="store_true",
                   help="Mark as indoor / trainer activity.")
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        client = StravaClient()
    except StravaAuthError as exc:
        sys.exit(f"✗ {exc}")
    if not client.is_authenticated:
        sys.exit("✗ Not authenticated. Run `python auth.py` first.")

    if args.file:
        do_file_upload(client, args)
    else:
        do_manual_activity(client, args)


if __name__ == "__main__":
    main()
