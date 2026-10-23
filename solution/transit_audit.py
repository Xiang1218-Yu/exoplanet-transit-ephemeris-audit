#!/usr/bin/env python3
"""Produce a deterministic audit of predicted exoplanet transits."""
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path

TOP = {"candidate", "observations", "policy"}
CANDIDATE = {"id", "reference_midpoint_bjd", "period_days", "duration_minutes", "reference_sigma_minutes", "drift_sigma_minutes_per_epoch"}
OBSERVATION = {"id", "start_bjd", "end_bjd", "quality", "airmass"}
POLICY = {"min_coverage_fraction", "max_airmass", "require_clear", "max_ephemeris_sigma_minutes"}

def fail(code):
    print("REJECT " + code, file=sys.stderr)
    raise ValueError(code)

def num(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        fail("RANGE")
    return float(value)

def exact(obj, keys):
    if not isinstance(obj, dict) or set(obj) != keys:
        fail("SCHEMA")

def validate(data):
    exact(data, TOP)
    c, observations, p = data["candidate"], data["observations"], data["policy"]
    exact(c, CANDIDATE); exact(p, POLICY)
    if not isinstance(c["id"], str) or not c["id"]: fail("SCHEMA")
    c["reference_midpoint_bjd"] = num(c["reference_midpoint_bjd"], 2400000, 2600000)
    c["period_days"] = num(c["period_days"], 0, 10000)
    if c["period_days"] == 0: fail("RANGE")
    c["duration_minutes"] = num(c["duration_minutes"], 0, 1440)
    if c["duration_minutes"] == 0: fail("RANGE")
    c["reference_sigma_minutes"] = num(c["reference_sigma_minutes"], 0, 1440)
    c["drift_sigma_minutes_per_epoch"] = num(c["drift_sigma_minutes_per_epoch"], 0, 1440)
    p["min_coverage_fraction"] = num(p["min_coverage_fraction"], 0, 1)
    if p["min_coverage_fraction"] == 0: fail("RANGE")
    p["max_airmass"] = num(p["max_airmass"], 1, 3)
    if not isinstance(p["require_clear"], bool): fail("SCHEMA")
    p["max_ephemeris_sigma_minutes"] = num(p["max_ephemeris_sigma_minutes"], 0, 1440)
    if not isinstance(observations, list) or not 1 <= len(observations) <= 200: fail("SCHEMA")
    ids = set()
    for visit in observations:
        exact(visit, OBSERVATION)
        if not isinstance(visit["id"], str) or not visit["id"] or visit["id"] in ids: fail("SCHEMA")
        ids.add(visit["id"])
        visit["start_bjd"] = num(visit["start_bjd"], 2400000, 2600000)
        visit["end_bjd"] = num(visit["end_bjd"], 2400000, 2600000)
        if visit["end_bjd"] <= visit["start_bjd"]: fail("RANGE")
        if visit["quality"] not in {"CLEAR", "THIN", "CLOUD"}: fail("SCHEMA")
        visit["airmass"] = num(visit["airmass"], 1, 3)
    return c, observations, p

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def merged_length(intervals):
    total = 0.0
    end = None
    for start, stop in sorted(intervals):
        if end is None:
            begin, end = start, stop
        elif start > end:
            total += end - begin; begin, end = start, stop
        else:
            end = max(end, stop)
    return total + (0.0 if end is None else end - begin)

def audit(c, observations, p):
    width = c["duration_minutes"] / 1440.0
    half = width / 2.0
    candidates = {}
    for visit in observations:
        first = math.ceil((visit["start_bjd"] - half - c["reference_midpoint_bjd"]) / c["period_days"])
        last = math.floor((visit["end_bjd"] + half - c["reference_midpoint_bjd"]) / c["period_days"])
        for epoch in range(first, last + 1):
            midpoint = c["reference_midpoint_bjd"] + epoch * c["period_days"]
            start, end = midpoint - half, midpoint + half
            left, right = max(start, visit["start_bjd"]), min(end, visit["end_bjd"])
            if right > left:
                candidates.setdefault(epoch, []).append((visit, left, right))
    result = []
    for epoch in sorted(candidates):
        qualifying = []
        intervals = []
        for visit, left, right in candidates[epoch]:
            permitted_quality = visit["quality"] == "CLEAR" or (not p["require_clear"] and visit["quality"] == "THIN")
            if permitted_quality and visit["airmass"] <= p["max_airmass"]:
                qualifying.append(visit["id"]); intervals.append((left, right))
        coverage = min(1.0, merged_length(intervals) / width)
        sigma = math.sqrt(c["reference_sigma_minutes"] ** 2 + (abs(epoch) * c["drift_sigma_minutes_per_epoch"]) ** 2)
        if coverage >= p["min_coverage_fraction"] and sigma <= p["max_ephemeris_sigma_minutes"]:
            state = "COVERED"
        elif coverage >= p["min_coverage_fraction"]:
            state = "TIMING_REVIEW"
        else:
            state = "LOW_COVERAGE"
        result.append({"coverage_fraction": round(coverage, 6), "epoch": epoch, "midpoint_bjd": round(c["reference_midpoint_bjd"] + epoch * c["period_days"], 8), "qualifying_visit_ids": sorted(qualifying), "sigma_minutes": round(sigma, 6), "status": state})
    if not result: top = "NO_TRANSIT"
    elif any(item["status"] == "TIMING_REVIEW" for item in result): top = "REVIEW"
    elif any(item["status"] == "COVERED" for item in result): top = "READY"
    else: top = "INSUFFICIENT"
    return {"candidate_id": c["id"], "epochs": result, "status": top, "summary": {"candidate_epochs": len(result), "covered_epochs": sum(item["status"] == "COVERED" for item in result), "timing_review_epochs": sum(item["status"] == "TIMING_REVIEW" for item in result)}}

def parse_ledger(path):
    if not path.exists(): return []
    try:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"candidate_id", "fingerprint", "qualified_epochs", "status"}: raise ValueError
            rows.append(row)
        return rows
    except Exception: fail("LEDGER_READ")

def staged_replace(output, ledger, report, event):
    rows = parse_ledger(ledger)
    if not any(row["fingerprint"] == event["fingerprint"] for row in rows): rows.append(event)
    report_raw = canonical(report) + b"\n"
    ledger_raw = b"".join(canonical(row) + b"\n" for row in rows)
    out_tmp = ledger_tmp = None
    try:
        for target in (output, ledger):
            if target.exists() and target.is_dir(): fail("IO")
            target.parent.mkdir(parents=True, exist_ok=True)
        f, out_tmp = tempfile.mkstemp(prefix=".transit-audit-", dir=output.parent); os.close(f)
        f, ledger_tmp = tempfile.mkstemp(prefix=".transit-audit-", dir=ledger.parent); os.close(f)
        Path(out_tmp).write_bytes(report_raw); Path(ledger_tmp).write_bytes(ledger_raw)
        os.replace(ledger_tmp, ledger); ledger_tmp = None
        os.replace(out_tmp, output); out_tmp = None
    except ValueError: raise
    except Exception: fail("IO")
    finally:
        for temporary in (out_tmp, ledger_tmp):
            if temporary:
                try: os.unlink(temporary)
                except FileNotFoundError: pass

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--input", required=True); parser.add_argument("--output", required=True); parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    try:
        raw = Path(args.input).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        print("REJECT INPUT", file=sys.stderr); return 2
    try:
        c, observations, p = validate(data)
        report = audit(c, observations, p)
        event = {"candidate_id": c["id"], "fingerprint": hashlib.sha256(canonical(data)).hexdigest(), "qualified_epochs": report["summary"]["covered_epochs"], "status": report["status"]}
        staged_replace(Path(args.output), Path(args.ledger), report, event)
    except ValueError:
        return 2
    return 0
if __name__ == "__main__": raise SystemExit(main())
