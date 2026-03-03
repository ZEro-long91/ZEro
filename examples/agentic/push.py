#!/usr/bin/env python3
"""
push.py — Write planned workouts to Intervals.icu calendar.

Part of Section 11 (https://github.com/CrankAddict/section-11).
Designed for agentic AI platforms (OpenClaw, Claude Code, Claude Cowork, etc.)
that can execute code. Chat-only users cannot use this.

Usage:
  # Single workout via CLI
  python push.py --name "Sweet Spot 3x15" --date 2026-03-05 --type Ride \
    --description "- 15m 55%\n\n3x\n- 15m 88-92%\n- 5m 55%\n\n- 10m 50%" \
    --duration 85 --tss 75

  # Agent imports directly
  from push import IntervalsPush
  pusher = IntervalsPush(athlete_id, api_key)
  result = pusher.push_workout({...})

  # Multiple workouts (training week)
  result = pusher.push_workouts([{...}, {...}, {...}])

Credentials (checked in order):
  1. CLI args: --athlete-id, --api-key
  2. .sync_config.json (same file sync.py uses)
  3. Environment: ATHLETE_ID, INTERVALS_KEY

Output: JSON to stdout for agent parsing.
  Success: {"success": true, "events": [{"id": ..., "name": ..., "date": ...}]}
  Failure: {"success": false, "error": "..."}
"""

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class IntervalsPush:
    """Push planned workouts to Intervals.icu calendar."""

    BASE_URL = "https://intervals.icu/api/v1"
    VERSION = "1.0.0"

    # Valid activity types (from Intervals.icu)
    VALID_TYPES = {
        "Ride", "VirtualRide", "MountainBikeRide", "GravelRide", "EBikeRide",
        "Run", "VirtualRun", "TrailRun",
        "Swim",
        "NordicSki", "VirtualSki",
        "Rowing",
        "WeightTraining",
        "Walk", "Hike",
        "Workout", "Other",
    }

    # Valid event categories
    VALID_CATEGORIES = {"WORKOUT", "RACE_A", "RACE_B", "RACE_C", "NOTE"}

    # Valid target modes
    VALID_TARGETS = {"POWER", "HR", "PACE", None}

    def __init__(self, athlete_id: str, api_key: str):
        if not athlete_id or not api_key:
            raise ValueError("athlete_id and api_key are required")
        self.athlete_id = athlete_id
        self.auth = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()

    def _post(self, endpoint: str, payload: list) -> dict:
        """POST to Intervals.icu API. Returns response JSON."""
        import requests

        url = f"{self.BASE_URL}/athlete/{self.athlete_id}/{endpoint}"
        headers = {
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()

    def validate_workout(self, workout: dict) -> Tuple[bool, Optional[str]]:
        """
        Validate a workout dict before pushing.

        Returns (True, None) if valid, (False, error_message) if not.
        """
        # Required fields
        name = workout.get("name")
        if not name or not name.strip():
            return False, "name is required"

        date = workout.get("date")
        if not date:
            return False, "date is required (YYYY-MM-DD)"

        # Date format
        try:
            workout_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return False, f"invalid date format: {date} (expected YYYY-MM-DD)"

        # No past dates for planned workouts
        today = datetime.now().date()
        if workout_date < today:
            return False, f"date {date} is in the past — planned workouts must be today or future"

        # Type validation
        wtype = workout.get("type", "Ride")
        if wtype not in self.VALID_TYPES:
            return False, f"invalid type: {wtype} — valid: {sorted(self.VALID_TYPES)}"

        # Category validation
        category = workout.get("category", "WORKOUT")
        if category not in self.VALID_CATEGORIES:
            return False, f"invalid category: {category} — valid: {sorted(self.VALID_CATEGORIES)}"

        # Target validation
        target = workout.get("target")
        if target is not None and target not in {"POWER", "HR", "PACE"}:
            return False, f"invalid target: {target} — valid: POWER, HR, PACE"

        # Duration sanity (if provided)
        duration = workout.get("duration_minutes")
        if duration is not None:
            if not isinstance(duration, (int, float)) or duration <= 0:
                return False, f"duration_minutes must be positive, got: {duration}"
            if duration > 720:
                return False, f"duration_minutes {duration} exceeds 12h — likely an error"

        # TSS sanity (if provided)
        tss = workout.get("tss")
        if tss is not None:
            if not isinstance(tss, (int, float)) or tss < 0:
                return False, f"tss must be non-negative, got: {tss}"
            if tss > 500:
                return False, f"tss {tss} exceeds 500 — likely an error"

        # Description validation (basic syntax check)
        desc = workout.get("description", "")
        if desc:
            valid, desc_error = self._validate_description(desc)
            if not valid:
                return False, f"description syntax: {desc_error}"

        return True, None

    def _validate_description(self, description: str) -> Tuple[bool, Optional[str]]:
        """
        Basic validation of Intervals.icu workout description syntax.

        Checks for obvious format errors. Does NOT fully parse — Intervals.icu
        handles that. We just catch the most common agent mistakes.
        """
        lines = description.strip().split("\n")

        has_step = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # Step lines start with -
            if stripped.startswith("-"):
                has_step = True
                step_text = stripped[1:].strip()
                if not step_text:
                    return False, "empty step line (dash with no content)"

            # Repeat lines: Nx or "Label Nx"
            elif re.match(r'^(\d+x|.+\s+\d+x)\s*$', stripped, re.IGNORECASE):
                continue

            # Section headers (text without dash, not a repeat)
            else:
                continue

        if not has_step:
            return False, "no step lines found (steps must start with -)"

        return True, None

    def _build_event(self, workout: dict) -> dict:
        """Convert a validated workout dict to an Intervals.icu event payload."""
        event = {
            "category": workout.get("category", "WORKOUT"),
            "start_date_local": f"{workout['date']}T00:00:00",
            "name": workout["name"],
            "type": workout.get("type", "Ride"),
        }

        description = workout.get("description", "")
        if description:
            event["description"] = description
            event["workout_doc"] = {}  # Triggers Intervals.icu to parse description

        target = workout.get("target")
        if target:
            event["target"] = target

        duration = workout.get("duration_minutes")
        if duration:
            event["moving_time"] = int(duration * 60)

        tss = workout.get("tss")
        if tss is not None:
            event["icu_training_load"] = tss

        color = workout.get("color")
        if color:
            event["color"] = color

        indoor = workout.get("indoor")
        if indoor is not None:
            event["indoor"] = indoor

        external_id = workout.get("external_id")
        if external_id:
            event["external_id"] = str(external_id)

        return event

    def push_workout(self, workout: dict) -> dict:
        """
        Validate and push a single workout. Returns result dict.

        Workout dict fields:
          Required: name, date (YYYY-MM-DD)
          Recommended: description (Intervals.icu syntax), type, duration_minutes, tss
          Optional: target (POWER/HR/PACE), category, color, indoor, external_id
        """
        return self.push_workouts([workout])

    def push_workouts(self, workouts: list) -> dict:
        """
        Validate and push multiple workouts. Returns result dict.

        Uses the bulk endpoint with upsert=true.
        """
        # Validate all before pushing any
        errors = []
        for i, w in enumerate(workouts):
            valid, error = self.validate_workout(w)
            if not valid:
                label = w.get("name", f"workout[{i}]")
                errors.append(f"{label}: {error}")

        if errors:
            return {"success": False, "error": "; ".join(errors)}

        # Build payloads
        events = [self._build_event(w) for w in workouts]

        # Push
        try:
            response = self._post("events/bulk?upsert=true", events)

            results = []
            if isinstance(response, list):
                for evt in response:
                    results.append({
                        "id": evt.get("id"),
                        "name": evt.get("name"),
                        "date": (evt.get("start_date_local") or "")[:10],
                        "type": evt.get("type"),
                        "category": evt.get("category"),
                    })

            return {
                "success": True,
                "count": len(results),
                "events": results,
            }

        except Exception as e:
            error_msg = str(e)
            # Extract HTTP error detail if available
            if hasattr(e, "response") and e.response is not None:
                try:
                    detail = e.response.json()
                    error_msg = f"{e.response.status_code}: {detail}"
                except Exception:
                    error_msg = f"{e.response.status_code}: {e.response.text[:200]}"
            return {"success": False, "error": error_msg}


def _load_credentials(args) -> Tuple[Optional[str], Optional[str]]:
    """Load credentials from CLI args, config file, or environment."""
    config = {}
    if os.path.exists(".sync_config.json"):
        with open(".sync_config.json") as f:
            config = json.load(f)

    athlete_id = (
        getattr(args, "athlete_id", None)
        or config.get("athlete_id")
        or os.getenv("ATHLETE_ID")
    )
    api_key = (
        getattr(args, "api_key", None)
        or config.get("intervals_key")
        or os.getenv("INTERVALS_KEY")
    )
    return athlete_id, api_key


def main():
    parser = argparse.ArgumentParser(
        description="Push planned workouts to Intervals.icu calendar"
    )
    parser.add_argument("--athlete-id", help="Intervals.icu athlete ID")
    parser.add_argument("--api-key", help="Intervals.icu API key")
    parser.add_argument("--name", help="Workout name (required unless --json)")
    parser.add_argument("--date", help="Date YYYY-MM-DD (required unless --json)")
    parser.add_argument("--type", default="Ride", help="Activity type (default: Ride)")
    parser.add_argument("--description", default="", help="Workout description (Intervals.icu syntax)")
    parser.add_argument("--duration", type=float, help="Planned duration in minutes")
    parser.add_argument("--tss", type=float, help="Planned TSS")
    parser.add_argument("--target", choices=["POWER", "HR", "PACE"], help="Target mode")
    parser.add_argument("--category", default="WORKOUT", help="Event category (default: WORKOUT)")
    parser.add_argument("--indoor", action="store_true", help="Mark as indoor")
    parser.add_argument("--json", type=str, help="Path to JSON file with workout(s) — overrides other fields")

    args = parser.parse_args()

    athlete_id, api_key = _load_credentials(args)
    if not athlete_id or not api_key:
        result = {
            "success": False,
            "error": "Missing credentials. Provide via --athlete-id/--api-key, .sync_config.json, or env vars ATHLETE_ID/INTERVALS_KEY",
        }
        print(json.dumps(result, indent=2))
        sys.exit(1)

    pusher = IntervalsPush(athlete_id, api_key)

    # JSON file mode: push one or more workouts from file
    if args.json:
        try:
            with open(args.json) as f:
                data = json.load(f)
            workouts = data if isinstance(data, list) else [data]
        except Exception as e:
            result = {"success": False, "error": f"Failed to read {args.json}: {e}"}
            print(json.dumps(result, indent=2))
            sys.exit(1)
    else:
        # CLI mode: name and date required
        if not args.name or not args.date:
            result = {
                "success": False,
                "error": "--name and --date are required (or use --json for file input)",
            }
            print(json.dumps(result, indent=2))
            sys.exit(1)

        # Handle escaped newlines from CLI (agent may pass "- 5m 55%\n\n3x\n- 15m 88%")
        description = args.description.replace("\\n", "\n") if args.description else ""

        workout = {
            "name": args.name,
            "date": args.date,
            "type": args.type,
            "description": description,
            "category": args.category,
        }
        if args.duration:
            workout["duration_minutes"] = args.duration
        if args.tss is not None:
            workout["tss"] = args.tss
        if args.target:
            workout["target"] = args.target
        if args.indoor:
            workout["indoor"] = True

        workouts = [workout]

    result = pusher.push_workouts(workouts)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
