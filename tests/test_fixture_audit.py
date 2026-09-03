from __future__ import annotations

import json
from pathlib import Path

import scripts.audit_fixtures as fixture_audit
from scripts.derive_fixtures import derive_file


def test_committed_fixtures_truth_reports_and_goldens_pass_audit() -> None:
    assert fixture_audit.audit() == []


def test_derived_fixture_audit_is_effective_without_raw_data(tmp_path: Path, monkeypatch) -> None:
    derived = tmp_path / "data" / "fixtures" / "hints" / "derived"
    derived.mkdir(parents=True)
    fixture = derived / "source-set-001.jsonl"
    valid = {
        "author": "author_001",
        "category": "id_question",
        "position_ms": 12_000,
        "text": "anyone TitleLocal this track?",
    }
    fixture.write_text(json.dumps(valid) + "\n", encoding="utf-8")
    monkeypatch.setattr(fixture_audit, "ROOT", tmp_path)
    monkeypatch.setattr(fixture_audit, "RAW_ROOT", tmp_path / "missing-raw")
    monkeypatch.setattr(fixture_audit, "SCAN_ROOTS", (tmp_path / "data" / "fixtures",))
    assert fixture_audit.audit() == []

    invalid = {**valid, "text": "an opaque raw sentence survives unchanged"}
    fixture.write_text(json.dumps(invalid) + "\n", encoding="utf-8")
    failures = fixture_audit.audit()
    assert any("outside the safe vocabulary" in failure for failure in failures)


def test_fixture_audit_rejects_opaque_contextual_ids_without_raw_data(
    tmp_path: Path, monkeypatch
) -> None:
    golden = tmp_path / "tests" / "golden"
    golden.mkdir(parents=True)
    (golden / "source.json").write_text(
        json.dumps({"platform_id": "opaque-AZ9"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(fixture_audit, "ROOT", tmp_path)
    monkeypatch.setattr(fixture_audit, "RAW_ROOT", tmp_path / "missing-raw")
    monkeypatch.setattr(fixture_audit, "SCAN_ROOTS", (golden,))
    failures = fixture_audit.audit()
    assert any("non-null raw identifier field" in failure for failure in failures)


def test_fixture_derivation_is_byte_deterministic_with_sequential_authors(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.json"
    destination = tmp_path / "source-set-001.jsonl"
    raw.write_text(
        json.dumps(
            [
                {"body": "Anyone know this track?", "user_id": "opaque-a"},
                {"body": "track is Example Artist", "user_id": "opaque-b"},
                {"body": "ordinary background reaction", "user_id": "opaque-c"},
            ]
        ),
        encoding="utf-8",
    )
    derive_file(raw, destination)
    first = destination.read_bytes()
    derive_file(raw, destination)
    assert destination.read_bytes() == first
    records = [json.loads(line) for line in first.decode("utf-8").splitlines()]
    assert [record["author"] for record in records] == [
        "author_001",
        "author_002",
        "author_003",
    ]
