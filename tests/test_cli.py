import asyncio
import json

from agentbus import MessageBus, Node, Topic
from agentbus.cli import _format_json, _socket_request
from agentbus.schemas.common import InboundChat, OutboundChat


class CliNode(Node):
    name = "cli-node"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]


async def test_graph_json_output(short_tmp):
    socket_path = f"{short_tmp}/agentbus.sock"
    bus = MessageBus(socket_path=socket_path)
    bus.register_topic(Topic[InboundChat]("/inbound"))
    bus.register_topic(Topic[OutboundChat]("/outbound"))
    bus.register_node(CliNode())

    spin_task = asyncio.create_task(bus.spin(timeout=0.3))
    await asyncio.sleep(0.05)
    raw = await _socket_request({"cmd": "graph"}, socket_path=socket_path)
    output = _format_json(raw)
    data = json.loads(output)
    await spin_task

    assert any(node["name"] == "cli-node" for node in data["nodes"])
    assert any(topic["name"] == "/inbound" for topic in data["topics"])
