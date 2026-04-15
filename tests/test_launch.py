import json

from agentbus.launch import build_bus_from_config


def test_build_bus_from_config_registers_topics_and_nodes(tmp_path):
    config = {
        "bus": {"heartbeat_interval": 1.0, "introspection_socket": None},
        "topics": [
            {
                "name": "/inbound",
                "schema": "agentbus.schemas.common:InboundChat",
                "retention": 5,
            },
            {
                "name": "/outbound",
                "schema": "agentbus.schemas.common:OutboundChat",
            },
        ],
        "nodes": [
            {
                "class": "tests.test_launch_support:LaunchNode",
                "concurrency": 2,
            }
        ],
    }

    config_path = tmp_path / "agentbus.yaml"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    bus = build_bus_from_config(config)

    assert "/inbound" in bus._topics
    assert "/outbound" in bus._topics
    assert "launch-node" in bus._nodes
    assert bus._nodes["launch-node"].node.concurrency == 2
