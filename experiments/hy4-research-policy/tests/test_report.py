import json
from pathlib import Path

from hy4_research_policy.report import build_summary

RUN = Path(__file__).resolve().parents[1] / "runs" / "20261123T213131Z"


def test_summary_rebuilds_the_saved_diagnosis_without_a_provider() -> None:
    summary = build_summary(RUN)
    saved = json.loads((RUN / "summary.json").read_text(encoding="utf-8"))
    assert summary == saved
    assert summary["paid_calls_for_this_report"] == 0
    assert summary["usd_cap"] == "2"
    assert summary["reserved_worst_usd"] == "0.068160806"
    assert summary["diagnosis"]["count"] == 9
    assert summary["diagnosis"]["output_usd"] == "N/A"
    assert summary["diagnosis"]["actual_usd"] == "N/A"
    assert summary["tasks"]["count"] == 40
    assert summary["tasks"]["schema_valid"] == 37
    assert summary["tasks"]["hidden_retry"] is False
    assert summary["tasks"]["output_usd"] == "0.006683743"
    assert summary["tasks"]["actual_usd"] == "N/A"
    assert summary["jev"]["count"] == 26
    assert summary["jev"]["choices"] == {"SUPPORTS": 11, "INSUFFICIENT": 15}
    assert summary["jev"]["input_tokens"] == 17309
    assert summary["jev"]["calculated_input_usd"] == "0.000726978"
    assert summary["jev"]["output_priced"] is False
    assert summary["jev"]["actual_usd"] == "N/A"
    assert summary["adapter"]["clef"] == "absent"
    assert summary["adapter"]["native_constructed"] is False
    assert all(row["exact_quote"] is None for row in summary["tasks"]["rows"] if row["structure_error"])
