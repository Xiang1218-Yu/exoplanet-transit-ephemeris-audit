#!/bin/sh
set -u
TESTS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$TESTS_DIR" = "/tests" ]; then
  REWARD_FILE=${REWARD_FILE:-/logs/verifier/reward.txt}
  export PROGRAM_DIR=${PROGRAM_DIR:-/app}
else
  ROOT=$(CDPATH= cd -- "$TESTS_DIR/.." && pwd)
  REWARD_FILE=${REWARD_FILE:-"$ROOT/reward.txt"}
  export PROGRAM_DIR=${PROGRAM_DIR:-"$ROOT/environment"}
fi
mkdir -p "$(dirname -- "$REWARD_FILE")" || exit 1
printf '0.000000\n' > "$REWARD_FILE"
TMP=$(mktemp -d) || exit 1
trap 'rm -rf "$TMP"' EXIT
export PYTHONDONTWRITEBYTECODE=1
python3 -m venv "$TMP/venv" || exit 1
"$TMP/venv/bin/python" -m pip install --no-index --find-links "$TESTS_DIR/wheels" --require-hashes -r "$TESTS_DIR/requirements-test.txt" || exit 1
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$TMP/venv/bin/python" -m pytest --collect-only -q "$TESTS_DIR/test_transit_audit.py" >"$TMP/collect.txt" 2>&1 || exit 1
set +e
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$TMP/venv/bin/python" -m pytest -p no:cacheprovider -q "$TESTS_DIR/test_transit_audit.py" --junitxml="$TMP/results.xml"
status=$?
set -e
python3 - "$TMP/results.xml" "$REWARD_FILE" <<'PY2'
import sys,xml.etree.ElementTree as ET
groups={
 'ephemeris':(.25,{'test_epoch_enumeration_and_partial_coverage','test_negative_epoch_and_touching_boundaries_are_not_candidates','test_sigma_is_quadrature_and_threshold_is_inclusive','test_review_has_priority_over_ready','test_no_transit_and_insufficient_are_distinct'}),
 'coverage':(.25,{'test_overlapping_visits_use_union_not_sum','test_quality_airmass_and_clear_policy_control_coverage'}),
 'rejection_atomicity':(.22,{'test_malformed_ledger_rejection_preserves_outputs_and_directory','test_duplicate_observation_id_rejection_preserves_outputs','test_schema_type_and_range_rejection_preserves_outputs','test_success_has_no_temp_artifacts_and_output_target_error_is_rejected'}),
 'ledger':(.16,{'test_canonical_ledger_is_idempotent_across_key_order','test_distinct_valid_input_appends_one_ledger_event'}),
 'serialization':(.12,{'test_report_contract_is_compact_sorted_rounded_and_stable'}),
}
score=0.0
try:
 root=ET.parse(sys.argv[1]).getroot(); outcomes={}
 for case in root.findall('.//testcase'):
  name=case.attrib.get('name','')
  outcomes[name]=case.find('failure') is None and case.find('error') is None and case.find('skipped') is None
 expected=set().union(*(tests for _,tests in groups.values()))
 if set(outcomes)!=expected or sum(weight for weight,_ in groups.values()) != 1.0:
  score=0.0
 else:
  score=sum(weight*sum(outcomes[name] for name in tests)/len(tests) for weight,tests in groups.values())
except Exception: score=0.0
with open(sys.argv[2],'w',encoding='utf-8') as stream: stream.write(f'{score:.6f}\n')
PY2
exit "$status"
