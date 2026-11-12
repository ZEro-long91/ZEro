"""Offline, read-only aggregate evaluation of a route_eta_history.json record."""
import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


def evaluate(path, as_of=None, power_inputs=None):
    spec = importlib.util.spec_from_file_location("section11_eta_evaluation", Path(__file__).resolve().parents[1] / "examples/sync.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class OfflineNetworkBlocked(BaseException):
        pass
    def blocked(*args, **kwargs):
        raise OfflineNetworkBlocked("offline ETA evaluation forbids HTTP")
    # Guard the imported runtime as a backstop; this path calls no fetchers.
    saved = {verb: getattr(module.requests, verb) for verb in
             ("get", "post", "put", "delete", "patch", "head", "options", "request")}
    for verb in saved:
        setattr(module.requests, verb, blocked)
    try:
        return module.RouteWeather.evaluate_eta(json.loads(Path(path).read_text()), as_of, power_inputs)
    finally:
        for verb, original in saved.items():
            setattr(module.requests, verb, original)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", help="local route_eta_history.json; never modified")
    parser.add_argument("--as-of", help="YYYY-MM-DD, defaults to current UTC date; retain last 180 days")
    parser.add_argument("--outdoor-ftp-w", type=float, help="current thresholds.sports.cycling.ftp only; never eFTP or indoor FTP")
    parser.add_argument("--body-weight-kg", type=float, help="existing current wellness/profile body weight")
    parser.add_argument("--power-zone-upper-pct", type=float, nargs="+", help="optional outdoor zone upper boundaries in percent of main FTP")
    args = parser.parse_args()
    date = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.as_of else None
    inputs = {"ftp_w": args.outdoor_ftp_w, "body_weight_kg": args.body_weight_kg,
              "power_zone_upper_pct": args.power_zone_upper_pct, "weight_source": "explicit_existing_synced_value"}
    print(json.dumps(evaluate(args.record, date, inputs), indent=2, allow_nan=False))
