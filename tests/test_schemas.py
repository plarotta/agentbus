"""Round-trip serialization tests for all Phase 1 schemas."""
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agentbus.schemas.common import InboundChat, OutboundChat, ToolRequest, ToolResult
from agentbus.schemas.harness import (
    ContentBlock,
    ConversationTurn,
    PlannerStatus,
    ToolCall,
    ToolResult as HarnessToolResult,
)
from agentbus.schemas.system import (
    BackpressureEvent,
    Heartbeat,
    LifecycleEvent,
    TelemetryEvent,
)


def roundtrip(model):
    """Serialize to JSON and back, assert equality."""
    data = model.model_dump_json()
    return type(model).model_validate_json(data)


# ── common schemas ──────────────────────────────────────────────────────────

class TestInboundChat:
    def test_roundtrip(self):
        m = InboundChat(channel="slack", sender="alice", text="hello")
        assert roundtrip(m) == m

    def test_metadata_defaults_empty(self):
        m = InboundChat(channel="cli", sender="user", text="hi")
        assert m.metadata == {}

    def test_metadata_custom(self):
        m = InboundChat(channel="slack", sender="bob", text="yo", metadata={"thread": "T123"})
        assert roundtrip(m).metadata == {"thread": "T123"}


class TestOutboundChat:
    def test_roundtrip(self):
        m = OutboundChat(text="response", reply_to="user-1")
        assert roundtrip(m) == m

    def test_reply_to_optional(self):
        m = OutboundChat(text="hello")
        assert m.reply_to is None


class TestToolRequest:
    def test_roundtrip(self):
        m = ToolRequest(tool="browser", action="navigate", params={"url": "https://example.com"})
        assert roundtrip(m) == m

    def test_action_optional(self):
        m = ToolRequest(tool="memory")
        assert m.action is None

    def test_params_default_empty(self):
        m = ToolRequest(tool="code")
        assert m.params == {}


class TestToolResult:
    def test_roundtrip(self):
        m = ToolResult(tool_call_id="call-1", output="hello world")
        assert roundtrip(m) == m

    def test_error_case(self):
        m = ToolResult(tool_call_id="call-2", error="connection refused")
        assert m.output is None
        assert roundtrip(m).error == "connection refused"


# ── system schemas ───────────────────────────────────────────────────────────

class TestLifecycleEvent:
    def test_roundtrip_started(self):
        m = LifecycleEvent(node="planner", event="started")
        assert roundtrip(m).event == "started"

    def test_roundtrip_error(self):
        m = LifecycleEvent(node="browser", event="error", error="timeout")
        rt = roundtrip(m)
        assert rt.error == "timeout"
        assert rt.event == "error"

    def test_invalid_event(self):
        with pytest.raises(ValidationError):
            LifecycleEvent(node="x", event="unknown_event")  # type: ignore[arg-type]

    def test_timestamp_auto(self):
        m = LifecycleEvent(node="x", event="stopped")
        assert m.timestamp is not None


class TestHeartbeat:
    def test_roundtrip(self):
        m = Heartbeat(
            uptime_s=3600.0,
            node_count=3,
            topic_count=5,
            total_messages=1000,
            messages_per_second=2.5,
            node_states={"planner": "RUNNING"},
            queue_depths={"planner": 0},
        )
        rt = roundtrip(m)
        assert rt.uptime_s == 3600.0
        assert rt.node_states == {"planner": "RUNNING"}


class TestBackpressureEvent:
    def test_roundtrip(self):
        m = BackpressureEvent(
            topic="/tools/request",
            subscriber_node="browser",
            queue_size=100,
            dropped_message_id="abc-123",
            policy="drop-oldest",
        )
        assert roundtrip(m) == m

    def test_invalid_policy(self):
        with pytest.raises(ValidationError):
            BackpressureEvent(
                topic="/t",
                subscriber_node="n",
                queue_size=1,
                dropped_message_id="x",
                policy="drop-middle",  # type: ignore[arg-type]
            )


class TestTelemetryEvent:
    def test_roundtrip(self):
        m = TelemetryEvent(event="context_pressure", detail="85% used", session_id="sess-1")
        rt = roundtrip(m)
        assert rt.event == "context_pressure"
        assert rt.session_id == "sess-1"

    def test_all_event_types_valid(self):
        events = [
            "stall_detected", "context_pressure", "tool_timeout",
            "model_demotion", "compact_triggered", "breaker_tripped",
        ]
        for event in events:
            m = TelemetryEvent(event=event, detail="test", session_id="s")  # type: ignore[arg-type]
            assert m.event == event


# ── harness schemas ──────────────────────────────────────────────────────────

class TestToolCall:
    def test_roundtrip(self):
        m = ToolCall(id="call-1", name="browser", arguments={"url": "https://example.com"})
        assert roundtrip(m) == m

    def test_arguments_default_empty(self):
        m = ToolCall(id="c", name="memory")
        assert m.arguments == {}


class TestHarnessToolResult:
    def test_roundtrip(self):
        m = HarnessToolResult(tool_call_id="call-1", output="page loaded")
        assert roundtrip(m) == m

    def test_error_and_output_both_optional(self):
        m = HarnessToolResult(tool_call_id="x")
        assert m.output is None
        assert m.error is None


class TestPlannerStatus:
    def test_roundtrip(self):
        m = PlannerStatus(
            event="thinking",
            iteration=1,
            context_tokens=1000,
            context_capacity=0.25,
        )
        assert roundtrip(m).event == "thinking"

    def test_all_events_valid(self):
        events = ["thinking", "tool_dispatched", "tool_received", "compacting", "responding", "error"]
        for event in events:
            m = PlannerStatus(event=event, iteration=1, context_tokens=0, context_capacity=0.0)  # type: ignore[arg-type]
            assert m.event == event

    def test_optional_fields(self):
        m = PlannerStatus(event="thinking", iteration=1, context_tokens=500, context_capacity=0.1)
        assert m.tool_name is None
        assert m.detail is None


class TestConversationTurn:
    def test_roundtrip_user(self):
        m = ConversationTurn(role="user", content="hello", token_count=5)
        assert roundtrip(m).content == "hello"

    def test_roundtrip_with_tool_calls(self):
        tc = ToolCall(id="c1", name="browser", arguments={})
        m = ConversationTurn(role="assistant", content="", tool_calls=[tc], token_count=10)
        rt = roundtrip(m)
        assert rt.tool_calls[0].name == "browser"

    def test_content_as_blocks(self):
        block = ContentBlock(type="text", text="hello")
        m = ConversationTurn(role="assistant", content=[block], token_count=3)
        rt = roundtrip(m)
        assert isinstance(rt.content, list)
        assert rt.content[0].text == "hello"

    def test_timestamp_auto(self):
        m = ConversationTurn(role="user", content="hi", token_count=1)
        assert m.timestamp is not None

    def test_token_count_defaults_zero(self):
        m = ConversationTurn(role="user", content="hi")
        assert m.token_count == 0
