class AgentBusError(Exception):
    """Base class for all AgentBus exceptions."""


class TopicSchemaError(AgentBusError):
    """Payload type does not match the topic's declared schema."""


class UndeclaredPublicationError(AgentBusError):
    """Node attempted to publish to a topic not in its declared publications."""


class UndeclaredSubscriptionError(AgentBusError):
    """Node declared a subscription to a topic not registered on the bus."""


class DuplicateNodeError(AgentBusError):
    """A node with this name is already registered on the bus."""


class DuplicateTopicError(AgentBusError):
    """A topic with this name is already registered on the bus."""


class RequestTimeoutError(AgentBusError):
    """Request/reply timed out waiting for a matching correlation_id response."""


class NodeInitError(AgentBusError):
    """Node raised an exception during on_init()."""


class CircuitBreakerOpenError(AgentBusError):
    """Operation rejected because the circuit breaker is open."""
