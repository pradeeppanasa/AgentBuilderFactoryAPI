"""In-memory test doubles — no real AWS calls anywhere in this suite."""

from __future__ import annotations

from typing import Any


class ConditionalCheckFailedException(Exception):
    """Stand-in for botocore's real exception of the same name, raised by
    FakeTable.update_item() when its (narrow) condition check fails."""


class FakeTable:
    def __init__(
        self,
        items: dict[tuple[Any, ...], dict[str, Any]] | None = None,
        key_names: tuple[str, ...] = (),
    ) -> None:
        self.items = items or {}
        self.key_names = key_names
        self.put_calls: list[dict[str, Any]] = []
        self.query_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def _key_tuple(self, key: dict[str, Any]) -> tuple[Any, ...]:
        return (
            tuple(key[name] for name in self.key_names)
            if self.key_names
            else tuple(sorted(key.items()))
        )

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        self.get_calls.append(Key)
        item = self.items.get(self._key_tuple(Key))
        return {"Item": item} if item is not None else {}

    def put_item(self, Item: dict[str, Any]) -> dict[str, Any]:
        self.put_calls.append(Item)
        key = (
            self._key_tuple({name: Item[name] for name in self.key_names})
            if self.key_names
            else tuple(sorted(Item.items()))
        )
        self.items[key] = Item
        return {}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ConditionExpression: str | None = None,
    ) -> dict[str, Any]:
        """Narrow, purpose-built stand-in for exactly one real caller's
        shape today (ToolApprovalManager.try_claim_resume's atomic "claim
        once" guard) — not a general DynamoDB expression evaluator. Both
        "attribute missing" and "attribute present but None" (simulating
        a written DynamoDB NULL) count as claimable, matching the real
        expression's `attribute_not_exists(x) OR attribute_type(x, NULL)`.
        Extend deliberately, not by pattern-matching more expression
        strings, if a second caller ever needs real generality."""
        self.update_calls.append({"Key": Key, "ConditionExpression": ConditionExpression})
        key_tuple = self._key_tuple(Key)
        item = self.items.get(key_tuple, dict(Key))

        attr, placeholder = (
            part.strip() for part in UpdateExpression.removeprefix("SET").split("=")
        )
        if ConditionExpression is not None and item.get(attr) is not None:
            raise ConditionalCheckFailedException()

        item[attr] = ExpressionAttributeValues[placeholder]
        self.items[key_tuple] = item
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.query_calls.append(kwargs)
        return {"Items": list(self.items.values())}


class _FakeDynamoDBClientExceptions:
    ConditionalCheckFailedException = ConditionalCheckFailedException


class _FakeDynamoDBClient:
    exceptions = _FakeDynamoDBClientExceptions()


class _FakeDynamoDBResourceMeta:
    def __init__(self) -> None:
        self.client = _FakeDynamoDBClient()


class FakeDynamoDBResource:
    def __init__(self, tables: dict[str, FakeTable]) -> None:
        self._tables = tables
        self.meta = _FakeDynamoDBResourceMeta()

    def Table(self, name: str) -> FakeTable:
        return self._tables[name]


class FakeLambdaClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        function_name = kwargs["FunctionName"]

        class _Payload:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        import json

        result = self._responses.get(function_name, {"ok": True})
        return {"Payload": _Payload(json.dumps(result).encode("utf-8")), "StatusCode": 200}


class FakeBedrockRuntimeClient:
    def __init__(
        self,
        action: str = "NONE",
        outputs: list[dict[str, Any]] | None = None,
        raise_error: bool = False,
    ) -> None:
        self.action = action
        self.outputs = outputs or []
        self.raise_error = raise_error
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_error:
            raise RuntimeError("simulated Bedrock outage")
        return {"action": self.action, "outputs": self.outputs}


class FakeBedrockAgentRuntimeClient:
    def __init__(self, results: list[dict[str, Any]] | None = None) -> None:
        self.results = results or []
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


class FakeSNSClient:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **kwargs: Any) -> dict[str, Any]:
        self.published.append(kwargs)
        return {"MessageId": "fake-message-id"}
