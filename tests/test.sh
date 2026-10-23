#!/bin/sh
set -eu
umask 077

TESTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$TESTS_DIR" = "/tests" ]; then
    BASE_DIR=/logs/verifier
    REWARD_FILE=${REWARD_FILE:-$BASE_DIR/reward.txt}
    PROGRAM_DIR=${PROGRAM_DIR:-/app}
else
    BASE_DIR=$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)
    REWARD_FILE=${REWARD_FILE:-$BASE_DIR/reward.txt}
    PROGRAM_DIR=${PROGRAM_DIR:-$BASE_DIR/environment}
fi
PYTHON_BIN=${PYTHON_BIN:-$(command -v python3)}
CANDIDATE_PYTHON=${CANDIDATE_PYTHON:-$PYTHON_BIN}
export PROGRAM_DIR CANDIDATE_PYTHON PYTHON_BIN

mkdir -p "$BASE_DIR" "$(dirname -- "$REWARD_FILE")"

write_zero_reward() {
    "$PYTHON_BIN" -I - "$REWARD_FILE" <<'PY'
import os
import sys
import tempfile

path = sys.argv[1]
directory = os.path.dirname(os.path.abspath(path)) or "."
fd, temporary = tempfile.mkstemp(prefix=".reward-", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write("0.000000\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise
PY
}

write_zero_reward

TMP=$(mktemp -d "${TMPDIR:-/tmp}/exo-verifier.XXXXXX")
FINALIZED=0
cleanup() {
    rm -rf "$TMP"
}
on_exit() {
    if [ "$FINALIZED" -ne 1 ]; then
        write_zero_reward || true
    fi
    cleanup
}
trap 'on_exit' EXIT
trap 'FINALIZED=0; exit 130' HUP INT TERM

# The candidate owns /app and must not be able to inject interpreter startup
# hooks or replace the verifier's initialized score. This probe deliberately
# places a sitecustomize.py in the working directory and supplies a hostile
# PYTHONPATH, then starts an isolated interpreter with an allowlisted
# environment. Any marker or score mutation means isolation is broken.
PROBE_DIR="$TMP/startup-probe"
PROBE_MARKER="$PROBE_DIR/imported"
mkdir -p "$PROBE_DIR"
cat > "$PROBE_DIR/sitecustomize.py" <<'PY'
import atexit
import os
from pathlib import Path

Path(os.environ["PROBE_MARKER"]).write_text("imported", encoding="utf-8")
atexit.register(
    lambda: Path(os.environ["REWARD_FILE"]).write_text(
        "1.000000\n", encoding="utf-8"
    )
)
PY
if ! (
    cd "$PROBE_DIR" &&
    env -i \
        PATH=/usr/local/bin:/usr/bin:/bin \
        HOME="$TMP" \
        TMPDIR=/tmp \
        LANG=C.UTF-8 \
        LC_ALL=C.UTF-8 \
        PYTHONPATH="$PROBE_DIR" \
        PROBE_MARKER="$PROBE_MARKER" \
        REWARD_FILE="$REWARD_FILE" \
        "$PYTHON_BIN" -I -c 'import sys; assert sys.flags.isolated == 1'
)
then
    FINALIZED=1
    write_zero_reward
    exit 0
fi
if [ -e "$PROBE_MARKER" ] || [ "$(cat "$REWARD_FILE" 2>/dev/null || true)" != '0.000000' ]; then
    FINALIZED=1
    write_zero_reward
    exit 0
fi

export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_INDEX=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

"$PYTHON_BIN" -I -m venv "$TMP/venv"

if ! env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME="$TMP" \
    TMPDIR="$TMP" \
    LANG=C.UTF-8 \
    "$TMP/venv/bin/python" -I -m pip install \
        --no-index \
        --find-links "$TESTS_DIR/wheels" \
        --require-hashes \
        -r "$TESTS_DIR/requirements-test.txt" \
        >"$TMP/pip.log" 2>&1
then
    cat "$TMP/pip.log"
    FINALIZED=1
    write_zero_reward
    exit 0
fi

cat > "$TMP/run_pytest.py" <<'PY'
import os
import signal
import subprocess
import sys

mode, timeout_text, test_file, output, status_path, tmp, program_dir, candidate_python = sys.argv[1:9]
command = [
    os.path.join(tmp, "venv", "bin", "python"),
    "-I",
    "-m",
    "pytest",
    "-p",
    "no:cacheprovider",
    "-q",
]
if mode == "collect":
    command.extend(["--collect-only", test_file])
else:
    command.extend([test_file, "--junitxml=" + os.path.join(tmp, "results.xml")])
environment = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": tmp,
    # The candidate runs as an unprivileged user and creates its own
    # transient work directory below a traversable root. Keep the verifier's
    # private venv/results outside that agent-writable path.
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    "PROGRAM_DIR": program_dir,
    "CANDIDATE_PYTHON": candidate_python,
}
with open(output, "wb") as log:
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=tmp,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return_code = process.wait(timeout=float(timeout_text))
    except subprocess.TimeoutExpired:
        if process is not None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        return_code = 124
with open(status_path, "w", encoding="ascii") as stream:
    stream.write(f"{return_code}\n")
PY

run_pytest() {
    mode=$1
    timeout_seconds=$2
    output=$3
    status_path=$4
    "$PYTHON_BIN" -I "$TMP/run_pytest.py" \
        "$mode" "$timeout_seconds" "$TESTS_DIR/test_transit_audit.py" \
        "$output" "$status_path" "$TMP" "$PROGRAM_DIR" "$CANDIDATE_PYTHON"
}

set +e
run_pytest collect 600 "$TMP/collect.txt" "$TMP/collect-status"
COLLECT_STATUS=$?
set -e
if [ "$COLLECT_STATUS" -ne 0 ] || [ ! -f "$TMP/collect-status" ]; then
    cat "$TMP/collect.txt" 2>/dev/null || true
    FINALIZED=1
    write_zero_reward
    exit 0
fi
COLLECT_RC=$(cat "$TMP/collect-status")

if [ "$COLLECT_RC" -ne 0 ]; then
    cat "$TMP/collect.txt" 2>/dev/null || true
    FINALIZED=1
    write_zero_reward
    exit 0
fi

set +e
run_pytest run 600 "$TMP/pytest.log" "$TMP/pytest-status"
PYTEST_WRAPPER_STATUS=$?
set -e
if [ "$PYTEST_WRAPPER_STATUS" -ne 0 ] || [ ! -f "$TMP/pytest-status" ]; then
    cat "$TMP/pytest.log" 2>/dev/null || true
    FINALIZED=1
    write_zero_reward
    exit 0
fi
PYTEST_STATUS=$(cat "$TMP/pytest-status")

case "$PYTEST_STATUS" in
    0|1)
        ;;
    *)
        cat "$TMP/pytest.log" 2>/dev/null || true
        FINALIZED=1
        write_zero_reward
        exit 0
        ;;
esac

"$PYTHON_BIN" -I - "$TMP/results.xml" "$TMP/collect.txt" "$REWARD_FILE" <<'PY'
import os
import sys
import tempfile
import xml.etree.ElementTree as ET

results_path, collect_path, reward_path = sys.argv[1:4]

GROUPS = {
    "ephemeris": (
        0.25,
        (
            "test_epoch_enumeration_and_partial_coverage",
            "test_negative_epoch_and_touching_boundaries_are_not_candidates",
            "test_sigma_is_quadrature_and_threshold_is_inclusive",
            "test_review_has_priority_over_ready",
            "test_no_transit_and_insufficient_are_distinct",
        ),
    ),
    "coverage": (
        0.25,
        (
            "test_overlapping_visits_use_union_not_sum",
            "test_quality_airmass_and_clear_policy_control_coverage",
            "test_coverage_threshold_uses_unrounded_value",
        ),
    ),
    "rejection_atomicity": (
        0.22,
        (
            "test_malformed_ledger_rejection_preserves_outputs_and_directory",
            "test_duplicate_observation_id_rejection_preserves_outputs",
            "test_schema_type_and_range_rejection_preserves_outputs",
            "test_success_has_no_temp_artifacts_and_output_target_error_is_rejected",
            "test_invalid_json_rejection_preserves_outputs",
        ),
    ),
    "ledger": (
        0.16,
        (
            "test_canonical_ledger_is_idempotent_across_key_order",
            "test_distinct_valid_input_appends_one_ledger_event",
        ),
    ),
    "serialization": (
        0.12,
        ("test_report_contract_is_compact_sorted_rounded_and_stable",),
    ),
}
HARD_GATES = frozenset({"rejection_atomicity", "ledger"})
EXPECTED = frozenset(
    name for _, tests in GROUPS.values() for name in tests
)


def atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(prefix=".reward-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def score():
    if abs(sum(weight for weight, _ in GROUPS.values()) - 1.0) > 1e-12:
        return 0.0
    try:
        root = ET.parse(results_path).getroot()
        cases = root.findall(".//testcase")
        names = [case.attrib.get("name", "") for case in cases]
        if len(names) != len(set(names)):
            return 0.0
        if set(names) != EXPECTED:
            return 0.0
        if root.find(".//collection") is not None:
            return 0.0
        for suite in root.findall(".//testsuite"):
            # Ordinary assertion failures are scored by group below. Errors and
            # skips mean the test did not produce a trustworthy outcome.
            for attribute in ("errors", "skipped"):
                if int(suite.attrib.get(attribute, "0") or "0") != 0:
                    return 0.0
        outcomes = {}
        for case in cases:
            name = case.attrib["name"]
            if case.find("error") is not None or case.find("skipped") is not None:
                return 0.0
            outcomes[name] = case.find("failure") is None
        with open(collect_path, "r", encoding="utf-8") as stream:
            collected_text = stream.read()
        collected_tests = [
            line.strip()
            for line in collected_text.splitlines()
            if "::" in line
        ]
        if len(collected_tests) != len(EXPECTED):
            return 0.0
        if len(collected_tests) != len(set(collected_tests)):
            return 0.0
        for name in EXPECTED:
            if not any(
                line.rsplit("::", 1)[-1] == name for line in collected_tests
            ):
                return 0.0
        for group, (_, tests) in GROUPS.items():
            if group in HARD_GATES and not all(outcomes[name] for name in tests):
                return 0.0
        total = 0.0
        for weight, tests in GROUPS.values():
            passed = sum(1 for name in tests if outcomes[name])
            total += weight * passed / len(tests)
        return max(0.0, min(1.0, total))
    except Exception:
        return 0.0


atomic_write(reward_path, f"{score():.6f}\n")
PY

cat "$TMP/pytest.log"
FINALIZED=1
exit 0
