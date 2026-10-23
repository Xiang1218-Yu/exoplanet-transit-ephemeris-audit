"""Exoplanet transit ephemeris audit behavior tests.

Each test prepares its own scenario, executes the candidate program, and keeps
the contract assertions in the test function body.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROGRAM = Path(os.environ.get("PROGRAM_DIR", "/app")) / "transit_audit.py"
CANDIDATE_PYTHON = os.environ.get("CANDIDATE_PYTHON", sys.executable)
T0 = 2459000.0
AGENT_UID = 10001
AGENT_GID = 10001

SENTINEL_REPORT = b"prior-report\x00byte-stable\n"
SENTINEL_LEDGER = (
    b'{"candidate_id":"prior","fingerprint":"old",'
    b'"qualified_epochs":0,"status":"READY"}\n'
)
SENTINEL_FILE = b"keep byte-for-byte\n"


def scene(**changes):
    """Return a valid baseline scenario, applying explicit replacements."""
    payload = {
        "candidate": {
            "id": "EPX-17",
            "reference_midpoint_bjd": T0,
            "period_days": 2.0,
            "duration_minutes": 120.0,
            "reference_sigma_minutes": 2.0,
            "drift_sigma_minutes_per_epoch": 1.5,
        },
        "observations": [
            {
                "id": "v1",
                "start_bjd": T0 - 1 / 48,
                "end_bjd": T0 + 1 / 48,
                "quality": "CLEAR",
                "airmass": 1.2,
            }
        ],
        "policy": {
            "min_coverage_fraction": 0.5,
            "max_airmass": 2.0,
            "require_clear": False,
            "max_ephemeris_sigma_minutes": 10.0,
        },
    }
    payload.update(changes)
    return payload


def _drop_privileges():
    """Run candidate code as the unprivileged container user when possible."""
    os.setgroups([])
    os.setgid(AGENT_GID)
    os.setuid(AGENT_UID)
    os.umask(0o077)


def _candidate_env(work_dir):
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(work_dir),
        "TMPDIR": str(work_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }


def _own_tree(root):
    """Give the unprivileged candidate access to its isolated work directory."""
    if os.geteuid() != 0:
        return
    for path in sorted(root.rglob("*"), reverse=True):
        os.chown(path, AGENT_UID, AGENT_GID)
    os.chown(root, AGENT_UID, AGENT_GID)


def execute(input_path, output_path, ledger_path, work_dir):
    """Execute the candidate once; this helper never decides correctness."""
    command = [
        CANDIDATE_PYTHON,
        "-I",
        str(PROGRAM),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
        "--ledger",
        str(ledger_path),
    ]
    return subprocess.run(
        command,
        cwd=str(work_dir),
        env=_candidate_env(work_dir),
        capture_output=True,
        text=True,
        preexec_fn=_drop_privileges if os.geteuid() == 0 else None,
    )


def run_audit(
    payload,
    report=SENTINEL_REPORT,
    ledger=SENTINEL_LEDGER,
    extra_files=None,
    output_directory=False,
    ledger_directory=False,
    raw_input=None,
):
    """Run one scenario in an isolated directory and return a result snapshot."""
    work_dir = Path(tempfile.mkdtemp(prefix="exo-audit-"))
    input_path = work_dir / "in.json"
    output_path = work_dir / "report.json"
    ledger_path = work_dir / "ledger.jsonl"
    try:
        if raw_input is None:
            input_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            raw = raw_input if isinstance(raw_input, bytes) else raw_input.encode("utf-8")
            input_path.write_bytes(raw)
        if output_directory:
            output_path.mkdir()
            (output_path / "keep.txt").write_bytes(SENTINEL_FILE)
        else:
            output_path.write_bytes(report)
        if ledger_directory:
            ledger_path.mkdir()
            (ledger_path / "keep.txt").write_bytes(SENTINEL_FILE)
        else:
            ledger_path.write_bytes(ledger)
        for name, content in (extra_files or {}).items():
            (work_dir / name).write_bytes(content)
        _own_tree(work_dir)

        process = execute(input_path, output_path, ledger_path, work_dir)

        output_entries = None
        if output_path.is_dir():
            output_entries = sorted(
                (entry.name, entry.read_bytes())
                for entry in output_path.iterdir()
                if entry.is_file()
            )
        ledger_entries = None
        if ledger_path.is_dir():
            ledger_entries = sorted(
                (entry.name, entry.read_bytes())
                for entry in ledger_path.iterdir()
                if entry.is_file()
            )
        return {
            "process": process,
            "report": output_path.read_bytes() if output_path.is_file() else None,
            "output_is_directory": output_path.is_dir(),
            "output_entries": output_entries,
            "ledger": ledger_path.read_bytes() if ledger_path.is_file() else None,
            "ledger_is_directory": ledger_path.is_dir(),
            "ledger_entries": ledger_entries,
            "root_entries": sorted(entry.name for entry in work_dir.iterdir()),
            "temporary_entries": sorted(
                entry.name
                for entry in work_dir.iterdir()
                if entry.name.startswith(".transit-audit-")
            ),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def report_json(result):
    """Decode a successful report without making any correctness claim."""
    return json.loads(result["report"])


def test_epoch_enumeration_and_partial_coverage():
    """R2/R3：每个严格相交历元都必须列出，并按凌星区间计算部分覆盖。"""
    visits = [
        {
            "id": "late",
            "start_bjd": T0 + 2 - 1 / 24,
            "end_bjd": T0 + 2,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
        {
            "id": "core",
            "start_bjd": T0 - 1 / 48,
            "end_bjd": T0 + 1 / 48,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
    ]
    result = run_audit(scene(observations=visits), report=b"", ledger=b"")

    assert result["process"].returncode == 0
    report = report_json(result)
    assert [epoch["epoch"] for epoch in report["epochs"]] == [0, 1]
    assert report["epochs"][0]["coverage_fraction"] == 0.5
    assert report["epochs"][1]["coverage_fraction"] == 0.5
    assert [epoch["status"] for epoch in report["epochs"]] == [
        "COVERED",
        "COVERED",
    ]
    assert report["summary"] == {
        "candidate_epochs": 2,
        "covered_epochs": 2,
        "timing_review_epochs": 0,
    }


def test_negative_epoch_and_touching_boundaries_are_not_candidates():
    """R2：负历元可出现；仅端点相接的观测不构成候选。"""
    visits = [
        {
            "id": "neg",
            "start_bjd": T0 - 2 - 1 / 96,
            "end_bjd": T0 - 2 + 1 / 96,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
        {
            "id": "touch",
            "start_bjd": T0 + 1 / 24,
            "end_bjd": T0 + 1 / 12,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
    ]
    result = run_audit(scene(observations=visits), report=b"", ledger=b"")

    assert result["process"].returncode == 0
    report = report_json(result)
    assert [epoch["epoch"] for epoch in report["epochs"]] == [-1]
    assert report["epochs"][0]["sigma_minutes"] == 2.5
    assert report["epochs"][0]["status"] == "LOW_COVERAGE"
    assert report["status"] == "INSUFFICIENT"


def test_overlapping_visits_use_union_not_sum():
    """R3：同一凌星的重叠曝光按并集而非重复时长计入。"""
    visits = [
        {
            "id": "b",
            "start_bjd": T0 - 1 / 96,
            "end_bjd": T0 + 1 / 48,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
        {
            "id": "a",
            "start_bjd": T0 - 1 / 24,
            "end_bjd": T0,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
    ]
    result = run_audit(scene(observations=visits), report=b"", ledger=b"")

    assert result["process"].returncode == 0
    report = report_json(result)
    assert report["epochs"][0]["coverage_fraction"] == 0.75
    assert report["epochs"][0]["qualifying_visit_ids"] == ["a", "b"]
    assert report["epochs"][0]["status"] == "COVERED"


def test_quality_airmass_and_clear_policy_control_coverage():
    """R3：质量政策和气团上限共同决定哪些观测可计入。"""
    visits = [
        {
            "id": "thin",
            "start_bjd": T0 - 1 / 24,
            "end_bjd": T0,
            "quality": "THIN",
            "airmass": 1.1,
        },
        {
            "id": "high",
            "start_bjd": T0,
            "end_bjd": T0 + 1 / 24,
            "quality": "CLEAR",
            "airmass": 2.5,
        },
    ]

    relaxed = run_audit(scene(observations=visits), report=b"", ledger=b"")
    assert relaxed["process"].returncode == 0
    relaxed_report = report_json(relaxed)
    assert len(relaxed_report["epochs"]) == 1
    assert relaxed_report["epochs"][0]["coverage_fraction"] == 0.5
    assert relaxed_report["epochs"][0]["qualifying_visit_ids"] == ["thin"]
    assert relaxed_report["epochs"][0]["status"] == "COVERED"

    strict_policy = {
        "min_coverage_fraction": 0.5,
        "max_airmass": 2.0,
        "require_clear": True,
        "max_ephemeris_sigma_minutes": 10.0,
    }
    strict = run_audit(
        scene(observations=visits, policy=strict_policy),
        report=b"",
        ledger=b"",
    )
    assert strict["process"].returncode == 0
    strict_report = report_json(strict)
    assert len(strict_report["epochs"]) == 1
    assert strict_report["epochs"][0]["coverage_fraction"] == 0.0
    assert strict_report["epochs"][0]["qualifying_visit_ids"] == []
    assert strict_report["epochs"][0]["status"] == "LOW_COVERAGE"
    assert strict_report["status"] == "INSUFFICIENT"


def test_coverage_threshold_uses_unrounded_value():
    """R3：阈值比较必须使用原始覆盖率，不能被报告舍入值抬高。"""
    width = 1 / 12
    visit = {
        "id": "edge",
        "start_bjd": T0 - width / 2,
        "end_bjd": T0 - width / 2 + width * 0.4999996,
        "quality": "CLEAR",
        "airmass": 1.1,
    }
    result = run_audit(
        scene(observations=[visit]),
        report=b"",
        ledger=b"",
    )

    assert result["process"].returncode == 0
    report = report_json(result)
    assert report["epochs"][0]["coverage_fraction"] == 0.5
    assert report["epochs"][0]["status"] == "LOW_COVERAGE"
    assert report["status"] == "INSUFFICIENT"
    assert report["summary"]["covered_epochs"] == 0


def test_sigma_is_quadrature_and_threshold_is_inclusive():
    """R4：历表不确定度按平方和开方，并在相等上限时接受。"""
    visit = {
        "id": "e1",
        "start_bjd": T0 + 2 - 1 / 24,
        "end_bjd": T0 + 2 + 1 / 24,
        "quality": "CLEAR",
        "airmass": 1.1,
    }
    policy = {
        "min_coverage_fraction": 0.999,
        "max_airmass": 2.0,
        "require_clear": False,
        "max_ephemeris_sigma_minutes": 2.5,
    }
    result = run_audit(
        scene(observations=[visit], policy=policy),
        report=b"",
        ledger=b"",
    )

    assert result["process"].returncode == 0
    report = report_json(result)
    assert report["epochs"][0]["epoch"] == 1
    assert report["epochs"][0]["sigma_minutes"] == 2.5
    assert report["epochs"][0]["status"] == "COVERED"
    assert report["status"] == "READY"


def test_review_has_priority_over_ready():
    """R4：只要有覆盖达标但历表超限的历元，顶层状态必须复核。"""
    visits = [
        {
            "id": "zero",
            "start_bjd": T0 - 1 / 24,
            "end_bjd": T0 + 1 / 24,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
        {
            "id": "one",
            "start_bjd": T0 + 2 - 1 / 24,
            "end_bjd": T0 + 2 + 1 / 24,
            "quality": "CLEAR",
            "airmass": 1.1,
        },
    ]
    policy = {
        "min_coverage_fraction": 0.999,
        "max_airmass": 2.0,
        "require_clear": False,
        "max_ephemeris_sigma_minutes": 2.1,
    }
    result = run_audit(
        scene(observations=visits, policy=policy),
        report=b"",
        ledger=b"",
    )

    assert result["process"].returncode == 0
    report = report_json(result)
    assert [epoch["status"] for epoch in report["epochs"]] == [
        "COVERED",
        "TIMING_REVIEW",
    ]
    assert report["status"] == "REVIEW"
    assert report["summary"] == {
        "candidate_epochs": 2,
        "covered_epochs": 1,
        "timing_review_epochs": 1,
    }


def test_no_transit_and_insufficient_are_distinct():
    """R4：无相交历元和有候选但覆盖不足必须给出不同结论。"""
    far = run_audit(
        scene(
            observations=[
                {
                    "id": "far",
                    "start_bjd": T0 + 0.2,
                    "end_bjd": T0 + 0.3,
                    "quality": "CLEAR",
                    "airmass": 1.1,
                }
            ]
        ),
        report=b"",
        ledger=b"",
    )
    assert far["process"].returncode == 0
    far_report = report_json(far)
    assert far_report["status"] == "NO_TRANSIT"
    assert far_report["epochs"] == []

    tiny = run_audit(
        scene(
            observations=[
                {
                    "id": "tiny",
                    "start_bjd": T0 - 1 / 480,
                    "end_bjd": T0 + 1 / 480,
                    "quality": "CLEAR",
                    "airmass": 1.1,
                }
            ]
        ),
        report=b"",
        ledger=b"",
    )
    assert tiny["process"].returncode == 0
    tiny_report = report_json(tiny)
    assert tiny_report["status"] == "INSUFFICIENT"
    assert len(tiny_report["epochs"]) == 1
    assert tiny_report["epochs"][0]["status"] == "LOW_COVERAGE"


def test_report_contract_is_compact_sorted_rounded_and_stable():
    """R5：成功报告必须满足精确字段、紧凑排序、LF 与稳定数值契约。"""
    first = run_audit(scene(), report=b"", ledger=b"")
    second = run_audit(scene(), report=b"", ledger=b"")

    assert first["process"].returncode == 0
    assert second["process"].returncode == 0
    parsed = report_json(first)
    assert set(parsed) == {"candidate_id", "epochs", "status", "summary"}
    assert set(parsed["epochs"][0]) == {
        "coverage_fraction",
        "epoch",
        "midpoint_bjd",
        "qualifying_visit_ids",
        "sigma_minutes",
        "status",
    }
    assert set(parsed["summary"]) == {
        "candidate_epochs",
        "covered_epochs",
        "timing_review_epochs",
    }
    expected = (
        json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    assert first["report"] == expected
    assert b", " not in first["report"]
    assert first["report"].endswith(b"\n")
    assert first["report"] == second["report"]
    assert first["ledger"] == second["ledger"]


def test_canonical_ledger_is_idempotent_across_key_order():
    """R6：相同规范输入即使源键顺序不同，账本也只能保留一条事件。"""
    payload = scene()
    with tempfile.TemporaryDirectory(prefix="exo-ledger-") as raw:
        work_dir = Path(raw)
        input_path = work_dir / "in.json"
        output_path = work_dir / "report.json"
        ledger_path = work_dir / "ledger.jsonl"

        input_path.write_text(json.dumps(payload), encoding="utf-8")
        _own_tree(work_dir)
        first = execute(input_path, output_path, ledger_path, work_dir)
        assert first.returncode == 0
        first_report = output_path.read_bytes()
        first_ledger = ledger_path.read_bytes()

        input_path.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
        second = execute(input_path, output_path, ledger_path, work_dir)
        assert second.returncode == 0
        assert output_path.read_bytes() == first_report
        assert ledger_path.read_bytes() == first_ledger

        integer_payload = json.loads(json.dumps(payload))
        integer_payload["candidate"]["period_days"] = 2
        input_path.write_text(
            json.dumps(integer_payload, sort_keys=True),
            encoding="utf-8",
        )
        third = execute(input_path, output_path, ledger_path, work_dir)
        assert third.returncode == 0
        assert output_path.read_bytes() == first_report

        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 2
        assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
        event = rows[0]
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert event["fingerprint"] == hashlib.sha256(canonical).hexdigest()
        assert set(event) == {
            "candidate_id",
            "fingerprint",
            "qualified_epochs",
            "status",
        }


def test_distinct_valid_input_appends_one_ledger_event():
    """R6：不同规范输入成功后账本应只新增一个确定事件。"""
    with tempfile.TemporaryDirectory(prefix="exo-ledger-") as raw:
        work_dir = Path(raw)
        input_path = work_dir / "in.json"
        output_path = work_dir / "report.json"
        ledger_path = work_dir / "ledger.jsonl"

        second_candidate = {
            "id": "EPX-18",
            "reference_midpoint_bjd": T0,
            "period_days": 2.0,
            "duration_minutes": 120.0,
            "reference_sigma_minutes": 2.0,
            "drift_sigma_minutes_per_epoch": 1.5,
        }
        for payload in (scene(), scene(candidate=second_candidate)):
            input_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            _own_tree(work_dir)
            process = execute(input_path, output_path, ledger_path, work_dir)
            assert process.returncode == 0

        rows = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        assert len(rows) == 2
        assert {row["candidate_id"] for row in rows} == {"EPX-17", "EPX-18"}


def test_malformed_ledger_rejection_preserves_outputs_and_directory():
    """R1/R6：无法读取既有账本时，两个目标和目录都保持不变。"""
    result = run_audit(
        scene(),
        report=SENTINEL_REPORT,
        ledger=b"not-json\n",
        extra_files={"sentinel.bin": SENTINEL_FILE},
    )

    assert result["process"].returncode != 0
    assert result["process"].stderr.splitlines()
    assert result["process"].stderr.splitlines()[0].startswith("REJECT ")
    assert result["process"].stdout == ""
    assert result["report"] == SENTINEL_REPORT
    assert result["ledger"] == b"not-json\n"
    assert result["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert result["temporary_entries"] == []


def test_duplicate_observation_id_rejection_preserves_outputs():
    """R1：同一输入中的重复观测 ID 必须被拒收且不改写已有对象。"""
    payload = scene()
    payload["observations"].append(dict(payload["observations"][0]))
    result = run_audit(
        payload,
        extra_files={"sentinel.bin": SENTINEL_FILE},
    )

    assert result["process"].returncode != 0
    assert result["process"].stderr.splitlines()
    assert result["process"].stderr.splitlines()[0].startswith("REJECT ")
    assert result["process"].stdout == ""
    assert result["report"] == SENTINEL_REPORT
    assert result["ledger"] == SENTINEL_LEDGER
    assert result["output_is_directory"] is False
    assert result["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert result["temporary_entries"] == []


def test_schema_type_and_range_rejection_preserves_outputs():
    """R1：布尔伪数值和越界值必须被拒收且不改写已有对象。"""
    boolean_period = scene()
    boolean_period["candidate"]["period_days"] = True
    boolean_result = run_audit(
        boolean_period,
        extra_files={"sentinel.bin": SENTINEL_FILE},
    )
    assert boolean_result["process"].returncode != 0
    assert boolean_result["process"].stderr.splitlines()
    assert boolean_result["process"].stderr.splitlines()[0].startswith(
        "REJECT "
    )
    assert boolean_result["process"].stdout == ""
    assert boolean_result["report"] == SENTINEL_REPORT
    assert boolean_result["ledger"] == SENTINEL_LEDGER
    assert boolean_result["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert boolean_result["temporary_entries"] == []

    bad_airmass = scene()
    bad_airmass["observations"][0]["airmass"] = 3.1
    airmass_result = run_audit(
        bad_airmass,
        extra_files={"sentinel.bin": SENTINEL_FILE},
    )
    assert airmass_result["process"].returncode != 0
    assert airmass_result["process"].stderr.splitlines()
    assert airmass_result["process"].stderr.splitlines()[0].startswith(
        "REJECT "
    )
    assert airmass_result["process"].stdout == ""
    assert airmass_result["report"] == SENTINEL_REPORT
    assert airmass_result["ledger"] == SENTINEL_LEDGER
    assert airmass_result["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert airmass_result["temporary_entries"] == []


def test_success_has_no_temp_artifacts_and_output_target_error_is_rejected():
    """R5/R6：成功不残留临时文件；无效输出目标必须不污染账本。"""
    success = run_audit(scene(), report=b"", ledger=b"")
    assert success["process"].returncode == 0
    assert success["report"] is not None
    assert success["report"].endswith(b"\n")
    assert success["root_entries"] == ["in.json", "ledger.jsonl", "report.json"]
    assert success["temporary_entries"] == []

    rejected = run_audit(
        scene(),
        ledger=SENTINEL_LEDGER,
        extra_files={"sentinel.bin": SENTINEL_FILE},
        output_directory=True,
    )
    assert rejected["process"].returncode != 0
    assert rejected["process"].stderr.splitlines()
    assert rejected["process"].stderr.splitlines()[0].startswith("REJECT ")
    assert rejected["process"].stdout == ""
    assert rejected["output_is_directory"] is True
    assert rejected["output_entries"] == [("keep.txt", SENTINEL_FILE)]
    assert rejected["ledger"] == SENTINEL_LEDGER
    assert rejected["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert rejected["temporary_entries"] == []


def test_invalid_json_rejection_preserves_outputs():
    """R1：无法解析的原始 JSON 必须拒收且保留全部既有产物。"""
    result = run_audit(
        None,
        raw_input=b'{"candidate": ',
        extra_files={"sentinel.bin": SENTINEL_FILE},
    )

    assert result["process"].returncode != 0
    assert result["process"].stderr.splitlines()
    assert result["process"].stderr.splitlines()[0].startswith("REJECT ")
    assert result["process"].stdout == ""
    assert result["report"] == SENTINEL_REPORT
    assert result["ledger"] == SENTINEL_LEDGER
    assert result["output_is_directory"] is False
    assert result["root_entries"] == [
        "in.json",
        "ledger.jsonl",
        "report.json",
        "sentinel.bin",
    ]
    assert result["temporary_entries"] == []
