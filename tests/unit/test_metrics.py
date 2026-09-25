"""EMF records must be shaped so CloudWatch actually extracts a metric."""

from __future__ import annotations

import json

from common import metrics


def emitted(capsys) -> dict:
    metrics.emit(metrics.REFUNDS_DENIED, tool="process_refund", reason="exceeds_refund_limit")
    return json.loads(capsys.readouterr().out.strip())


def test_emf_envelope(capsys):
    record = emitted(capsys)
    definition = record["_aws"]["CloudWatchMetrics"][0]
    assert definition["Namespace"] == "csagent"
    assert definition["Metrics"] == [{"Name": "RefundsDenied", "Unit": "Count"}]
    assert record["RefundsDenied"] == 1.0
    assert isinstance(record["_aws"]["Timestamp"], int)


def test_every_dimension_is_present_as_a_field(capsys):
    """CloudWatch silently drops the metric if a named dimension has no value."""
    record = emitted(capsys)
    for dimension in record["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]:
        assert dimension in record, f"{dimension} is declared but never set"


def test_dimensions_stay_low_cardinality(capsys):
    """order_id must be a searchable field, never a dimension: one metric per
    order would be both useless and expensive."""
    record = emitted(capsys)
    dimensions = set(record["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0])
    assert dimensions == {"Stage", "Tool"}


def test_extra_fields_are_searchable_but_not_dimensions(capsys):
    metrics.emit(metrics.REFUNDS_PROCESSED, tool="process_refund", order_id="ORD-1001")
    record = json.loads(capsys.readouterr().out.strip())
    assert record["order_id"] == "ORD-1001"
    assert "order_id" not in record["_aws"]["CloudWatchMetrics"][0]["Dimensions"][0]


def test_none_fields_are_dropped(capsys):
    metrics.emit(metrics.TOOL_ERRORS, tool="get_order", error_code=None, refund_id=None)
    record = json.loads(capsys.readouterr().out.strip())
    assert "error_code" not in record
    assert "refund_id" not in record


def test_emit_never_raises(capsys):
    class Unserialisable:
        def __repr__(self) -> str:
            raise RuntimeError("boom")

    # Telemetry failing must never fail the tool that emitted it.
    metrics.emit(metrics.TOOL_ERRORS, tool="get_order", weird=Unserialisable())
