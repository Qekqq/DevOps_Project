import json
from pathlib import Path

from scripts import security_report as report


def scan_result(directory, *, state="complete"):
    directory.mkdir(parents=True, exist_ok=True)
    findings = [
        {
            "VulnerabilityID": "CVE-reviewed",
            "PkgName": "grpc",
            "InstalledVersion": "1",
            "FixedVersion": "2",
            "Severity": "HIGH",
            "Title": "Reviewed finding",
        },
        {
            "VulnerabilityID": "CVE-blocked",
            "PkgName": "pcre2",
            "InstalledVersion": "1",
            "FixedVersion": "2",
            "Severity": "CRITICAL",
            "Title": "Unsafe <b>markup</b> & text",
        },
        {
            "VulnerabilityID": "CVE-no-fix",
            "PkgName": "other",
            "InstalledVersion": "1",
            "Severity": "HIGH",
        },
        {
            "VulnerabilityID": "CVE-medium",
            "PkgName": "another",
            "InstalledVersion": "1",
            "FixedVersion": "2",
            "Severity": "MEDIUM",
        },
    ]
    review = {
        "target": "bin/tool",
        "type": "gobinary",
        "vulnerability": "CVE-reviewed",
        "package": "grpc",
        "version": "1",
        "status": "not_affected",
        "justification": "vulnerable_code_not_present",
        "reason": "Уязвимый код отсутствует.",
        "evidence": "review.md",
        "binary_sha256": "b" * 64,
        "reviewed": "2026-09-12",
        "expires": "2026-10-12",
    }
    data = {
        "ResolvedImageId": "sha256:" + "a" * 64,
        "RequestedImage": "sample/app:check",
        "Results": [
            {"Target": "bin/tool", "Type": "gobinary", "Vulnerabilities": findings}
        ],
        "ApplicabilityReviews": [review],
    }
    (directory / "report.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "scan-status.json").write_text(
        json.dumps(
            {
                "state": state,
                "image_id": data["ResolvedImageId"],
                "image": "sample/app:check",
            }
        ),
        encoding="utf-8",
    )
    return data


def test_pdf_decisions_retain_reviewed_and_unfixed_findings(tmp_path):
    scan_result(tmp_path / "api")
    images, findings = report.collect(tmp_path)
    by_id = {r["finding"]["VulnerabilityID"]: r for r in findings}
    assert len(findings) == 4
    assert by_id["CVE-reviewed"]["decision"] == "not_affected"
    assert by_id["CVE-reviewed"]["review"]["reason"] == "Уязвимый код отсутствует."
    assert by_id["CVE-blocked"]["decision"] == "blocked"
    assert by_id["CVE-no-fix"]["decision"] == "no_fix"
    assert by_id["CVE-medium"]["decision"] == "below_gate"
    assert "блокирующие" in report.verdict(images, findings, "failure")
    repeated = dict(
        by_id["CVE-blocked"],
        finding=dict(by_id["CVE-blocked"]["finding"], PkgName="pcre2-extra"),
    )
    groups = report.group_findings(findings + [repeated])
    assert len(groups) == 4
    assert {row["finding"]["PkgName"] for row in groups[0]} == {"pcre2", "pcre2-extra"}


def test_failed_scan_cannot_reuse_previous_reviews_or_claim_success(tmp_path):
    scan_result(tmp_path / "api", state="failed")
    images, findings = report.collect(tmp_path)
    assert all(r["decision"] == "incomplete" and r["review"] is None for r in findings)
    assert "не завершена" in report.verdict(images, findings, "failure")


def test_missing_malformed_and_mismatched_evidence_is_incomplete(tmp_path):
    assert "не завершена" in report.verdict(*report.collect(tmp_path), "success")
    scan_result(tmp_path / "api")
    (tmp_path / "api/scan-status.json").write_text("{broken", encoding="utf-8")
    assert not report.collect(tmp_path)[0][0]["complete"]
    (tmp_path / "api/scan-status.json").write_text(
        json.dumps({"state": "complete", "image_id": "other"}), encoding="utf-8"
    )
    assert not report.collect(tmp_path)[0][0]["complete"]


def test_skipped_or_failed_required_step_is_not_a_successful_scan(tmp_path):
    scan_result(tmp_path / "api")
    images, _ = report.collect(tmp_path)
    for outcome in ("failure", "skipped", "cancelled", "unknown"):
        assert "не подтверждено" in report.verdict(images, [], outcome)
    assert "Блокирующих находок нет" in report.verdict(images, [], "success")


def test_cyrillic_pdf_handles_advisory_markup_and_long_descriptions(tmp_path):
    data = scan_result(tmp_path / "api")
    data["Results"][0]["Vulnerabilities"][1]["Description"] = (
        "Описание уязвимости <img src='https://invalid/image'> & " * 100
    )
    (tmp_path / "api/report.json").write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "report.pdf"
    summary = report.write_pdf(tmp_path, output, scan_outcome="failure")
    assert summary["findings"] == 4 and summary["decisions"]["not_affected"] == 1
    assert output.read_bytes().startswith(b"%PDF-")
    assert output.stat().st_size > 10000
    assert "&lt;img" in report.text("<img src='remote'>")


def test_every_scanning_workflow_keeps_reports_after_failure():
    root = Path(__file__).resolve().parents[1]
    for name in ("ci.yml", "infrastructure.yml", "security.yml"):
        content = (root / ".github/workflows" / name).read_text(encoding="utf-8")
        assert "uses: ./.github/actions/security-report" in content
        assert "scan-outcome:" in content
    action = (root / ".github/actions/security-report/action.yml").read_text(
        encoding="utf-8"
    )
    assert "if: always()" in action
    assert "include-hidden-files: true" in action
    assert "**/report.json" in action and "security-report.pdf" in action
    assert "continue-on-error" not in action
