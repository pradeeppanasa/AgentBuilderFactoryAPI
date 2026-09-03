from __future__ import annotations

from fakes import FakeDynamoDBResource, FakeSNSClient, FakeTable

from hitl import HITLManager


def _manager(**overrides) -> tuple[HITLManager, FakeTable, FakeSNSClient]:
    table = FakeTable(key_names=("tenant_id", "review_id"))
    dynamodb = FakeDynamoDBResource({"panasa-hitl-reviews": table})
    sns = FakeSNSClient()
    defaults: dict = dict(
        agent_id="agent-1",
        tenant_id="tenant-a",
        enabled=True,
        trigger_conditions=["high_risk_decision"],
        dynamodb=dynamodb,
        sns_client=sns,
    )
    defaults.update(overrides)
    return HITLManager(**defaults), table, sns


async def test_disabled_never_pauses() -> None:
    manager, _table, _sns = _manager(enabled=False)
    result = await manager.pre_check("this is a high risk decision", context="")
    assert result == {"pause": False}


async def test_no_trigger_conditions_never_pauses() -> None:
    manager, _table, _sns = _manager(trigger_conditions=[])
    result = await manager.pre_check("this is a high risk decision", context="")
    assert result == {"pause": False}


async def test_matching_trigger_condition_pauses() -> None:
    manager, _table, _sns = _manager(trigger_conditions=["high_risk_decision"])
    result = await manager.pre_check("Please approve this high risk decision for the account.", context="")
    assert result["pause"] is True
    assert result["trigger_condition"] == "high_risk_decision"


async def test_non_matching_message_does_not_pause() -> None:
    manager, _table, _sns = _manager(trigger_conditions=["high_risk_decision"])
    result = await manager.pre_check("What's the weather today?", context="")
    assert result == {"pause": False}


async def test_create_review_writes_item_and_publishes_sns() -> None:
    manager, table, sns = _manager(notification_sns_arn="arn:aws:sns:eu-west-2:111122223333:hitl-topic")

    review_id = await manager.create_review(
        run_id="run-1", agent_id="agent-1", message="approve this", session_id="s1",
        trigger_condition="high_risk_decision",
    )

    assert review_id.startswith("HITL-")
    assert len(table.put_calls) == 1
    saved = table.put_calls[0]
    assert saved["tenant_id"] == "tenant-a"
    assert saved["agent_id"] == "agent-1"
    assert saved["status"] == "pending"
    assert saved["trigger_condition"] == "high_risk_decision"
    assert saved["requested_by"] == "agent-runtime:agent-1"

    assert len(sns.published) == 1
    assert sns.published[0]["TopicArn"] == "arn:aws:sns:eu-west-2:111122223333:hitl-topic"


async def test_create_review_without_sns_arn_skips_publish() -> None:
    manager, table, sns = _manager(notification_sns_arn=None)

    await manager.create_review(run_id="run-1", agent_id="agent-1", message="x", session_id="s1")

    assert len(table.put_calls) == 1
    assert sns.published == []
