from agentbus.utils import CircuitBreaker


def test_initially_closed():
    breaker = CircuitBreaker(name="test", max_failures=3)
    assert not breaker.is_open
    assert breaker.consecutive_failures == 0


def test_opens_at_max_failures():
    breaker = CircuitBreaker(name="test", max_failures=3)
    assert not breaker.record_failure()  # 1st failure — still closed
    assert not breaker.record_failure()  # 2nd failure — still closed
    opened = breaker.record_failure()    # 3rd failure — now open
    assert opened
    assert breaker.is_open


def test_record_failure_returns_false_while_closed():
    breaker = CircuitBreaker(name="test", max_failures=5)
    for _ in range(4):
        result = breaker.record_failure()
        assert not result
    assert not breaker.is_open


def test_record_failure_returns_true_when_open():
    breaker = CircuitBreaker(name="test", max_failures=2)
    breaker.record_failure()
    opened = breaker.record_failure()
    assert opened is True


def test_success_resets_counter():
    breaker = CircuitBreaker(name="test", max_failures=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    assert breaker.consecutive_failures == 0
    assert not breaker.is_open


def test_success_allows_reopening():
    breaker = CircuitBreaker(name="test", max_failures=2)
    breaker.record_failure()
    breaker.record_success()
    assert not breaker.record_failure()  # back to 1
    assert breaker.record_failure()      # hits max again


def test_stays_open_after_more_failures():
    breaker = CircuitBreaker(name="test", max_failures=2)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open
    breaker.record_failure()  # already open
    assert breaker.is_open


def test_max_failures_one():
    breaker = CircuitBreaker(name="hair-trigger", max_failures=1)
    assert breaker.record_failure()
    assert breaker.is_open
