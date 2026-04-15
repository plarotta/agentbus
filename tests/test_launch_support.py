from agentbus.node import Node


class LaunchNode(Node):
    name = "launch-node"
    subscriptions = ["/inbound"]
    publications = ["/outbound"]
