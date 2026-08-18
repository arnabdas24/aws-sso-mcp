"""Tests for aws_sso_mcp.engine.dag - declarative multi-step queries."""

from __future__ import annotations

from typing import Any

import pytest

from aws_sso_mcp.engine.dag import DagError, Step, run_dag, topological_order


def test_topological_order_simple_chain() -> None:
    steps = [
        Step(id="b", service="ec2", operation="DescribeInstances", depends_on=("a",)),
        Step(id="a", service="ec2", operation="DescribeVpcs"),
    ]
    ordered = topological_order(steps)
    assert [s.id for s in ordered] == ["a", "b"]


def test_topological_order_infers_deps_from_ref_in_params() -> None:
    steps = [
        Step(
            id="b",
            service="ec2",
            operation="DescribeInstances",
            params={"Filters": {"$ref": "a"}},
        ),
        Step(id="a", service="ec2", operation="DescribeVpcs"),
    ]
    ordered = topological_order(steps)
    assert [s.id for s in ordered] == ["a", "b"]


def test_unknown_ref_raises_dag_error() -> None:
    steps = [Step(id="a", service="ec2", operation="DescribeVpcs", depends_on=("missing",))]
    with pytest.raises(DagError, match="unknown step"):
        topological_order(steps)


def test_cycle_raises_dag_error() -> None:
    steps = [
        Step(id="a", service="ec2", operation="DescribeVpcs", depends_on=("b",)),
        Step(id="b", service="ec2", operation="DescribeVpcs", depends_on=("a",)),
    ]
    with pytest.raises(DagError, match="cycle"):
        topological_order(steps)


def test_run_dag_single_step_no_extract() -> None:
    steps = [Step(id="a", service="ec2", operation="DescribeVpcs")]

    def call(step: Step, params: dict[str, Any]) -> dict[str, Any]:
        return {"Vpcs": [{"VpcId": "vpc-1"}]}

    result = run_dag(steps, call=call)
    assert result["steps"]["a"]["extracted"] == {"Vpcs": [{"VpcId": "vpc-1"}]}


def test_run_dag_extracts_flattened_list() -> None:
    steps = [
        Step(
            id="a",
            service="ec2",
            operation="DescribeInstances",
            extract="Reservations[].Instances[].InstanceId",
        )
    ]

    def call(step: Step, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]},
                {"Instances": [{"InstanceId": "i-3"}]},
            ]
        }

    result = run_dag(steps, call=call)
    assert result["steps"]["a"]["extracted"] == ["i-1", "i-2", "i-3"]


def test_run_dag_substitutes_ref_between_steps() -> None:
    steps = [
        Step(
            id="ids",
            service="ec2",
            operation="DescribeInstances",
            extract="Reservations[].Instances[].InstanceId",
        ),
        Step(
            id="status",
            service="ec2",
            operation="DescribeInstanceStatus",
            params={"InstanceIds": {"$ref": "ids"}},
        ),
    ]
    seen_params: dict[str, Any] = {}

    def call(step: Step, params: dict[str, Any]) -> dict[str, Any]:
        if step.id == "ids":
            return {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}
        seen_params.update(params)
        return {"InstanceStatuses": []}

    run_dag(steps, call=call)
    assert seen_params == {"InstanceIds": ["i-1"]}


def test_run_dag_nested_ref_inside_dict() -> None:
    steps = [
        Step(id="a", service="s3", operation="ListBuckets"),
        Step(
            id="b",
            service="ec2",
            operation="DescribeInstances",
            params={"Filters": [{"Values": {"$ref": "a"}}]},
        ),
    ]
    seen: dict[str, Any] = {}

    def call(step: Step, params: dict[str, Any]) -> dict[str, Any]:
        if step.id == "a":
            return {"Buckets": ["b1"]}
        seen.update(params)
        return {}

    run_dag(steps, call=call)
    assert seen == {"Filters": [{"Values": {"Buckets": ["b1"]}}]}
