"""Read retries and readiness (#11, interface: Reliability), over a fake clock and a fake sleep."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from forgectl import gateway as gateway_module
from forgectl import reads
from forgectl.backends import BACKEND_CLASSES, create_backend
from forgectl.config import LoadedConfig
from forgectl.errors import ForgectlError
from forgectl.gateway import Gateway
from forgectl.hosts import Hosts
from forgectl.reads import MERGE_READINESS, NO_READINESS, SHOW_READINESS, ReadEnv


class Clock:
    """A fake monotonic clock that only `sleep` advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def make_env(
    *, retries: int = 3, timeout: float = 60.0, jitter: float = 0.999, clock: Clock | None = None
) -> tuple[ReadEnv, Clock]:
    clock = clock or Clock()
    gateway = Gateway(LoadedConfig.defaults(), timeout=timeout)
    env = ReadEnv(
        gateway=gateway,
        hosts=Hosts(gateway),
        retries=retries,
        sleep=clock.sleep,
        clock=clock,
        jitter=lambda: jitter,
    )
    return env, clock


def failing(code: str, times: int, *, retry_after: float | None = None) -> Callable[[], str]:
    state = {"calls": 0}

    def fn() -> str:
        state["calls"] += 1
        if state["calls"] <= times:
            raise ForgectlError(code, f"{code} #{state['calls']}", retry_after=retry_after)
        return "ok"

    fn.state = state  # type: ignore[attr-defined]
    return fn


def calls(fn: Callable[[], str]) -> int:
    return fn.state["calls"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("code", ["rate_limited", "upstream_error", "network_error"])
def test_a_retryable_failure_is_retried_until_it_succeeds(code: str) -> None:
    env, clock = make_env()
    fn = failing(code, 2)
    assert env.call(fn) == "ok"
    assert calls(fn) == 3
    assert len(clock.slept) == 2


@pytest.mark.parametrize(
    "code",
    ["auth_required", "permission_denied", "not_found", "host_rejected", "wrong_host_response"],
)
def test_other_codes_are_never_retried(code: str) -> None:
    env, clock = make_env()
    details = {"status": 400} if code == "host_rejected" else None
    if code == "wrong_host_response":
        details = {"observed": "x"}
    calls_made: list[int] = []

    def fn() -> str:
        calls_made.append(1)
        raise ForgectlError(code, "no", details=details)

    with pytest.raises(ForgectlError) as caught:
        env.call(fn)
    assert caught.value.code == code
    assert calls_made == [1]
    assert clock.slept == []


def test_retries_are_bounded_and_the_last_failure_is_raised() -> None:
    env, clock = make_env(retries=3)
    fn = failing("upstream_error", 99)
    with pytest.raises(ForgectlError) as caught:
        env.call(fn)
    assert caught.value.code == "upstream_error"
    assert calls(fn) == 4  # the first try and 3 retries
    assert len(clock.slept) == 3


def test_retries_zero_turns_retrying_off() -> None:
    env, clock = make_env(retries=0)
    fn = failing("network_error", 1)
    with pytest.raises(ForgectlError):
        env.call(fn)
    assert calls(fn) == 1
    assert clock.slept == []


def test_the_backoff_doubles_from_one_second_to_eight_and_stays_there() -> None:
    env, clock = make_env(retries=6, jitter=0.999999, timeout=1000)
    fn = failing("upstream_error", 6)
    assert env.call(fn) == "ok"
    expected = [1, 2, 4, 8, 8, 8]
    assert len(clock.slept) == 6
    for waited, base in zip(clock.slept, expected, strict=True):
        assert base <= waited <= base * 1.25  # jitter adds up to a quarter, never less


def test_the_jitter_changes_the_wait_and_the_lowest_is_the_table_value() -> None:
    low, _ = make_env(jitter=0.0)
    high, _ = make_env(jitter=0.999999)
    assert [low.backoff(n) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]
    assert 4.99 < high.backoff(2) <= 5.0


def test_retry_after_longer_than_the_backoff_is_honored() -> None:
    env, clock = make_env(timeout=100)
    fn = failing("rate_limited", 1, retry_after=7.0)
    assert env.call(fn) == "ok"
    assert clock.slept == [7.0]


def test_retry_after_shorter_than_the_backoff_keeps_the_backoff() -> None:
    env, clock = make_env(jitter=0.0)
    fn = failing("rate_limited", 1, retry_after=0.1)
    assert env.call(fn) == "ok"
    assert clock.slept == [1.0]


def test_retry_after_past_the_budget_exits_at_once_with_retry_after() -> None:
    env, clock = make_env(timeout=10)
    fn = failing("rate_limited", 5, retry_after=30.0)
    with pytest.raises(ForgectlError) as caught:
        env.call(fn)
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 30.0
    assert calls(fn) == 1
    assert clock.slept == []


def test_a_backoff_that_does_not_fit_the_budget_raises_instead_of_waiting() -> None:
    env, clock = make_env(timeout=0.4, jitter=0.999)
    fn = failing("upstream_error", 5)
    with pytest.raises(ForgectlError):
        env.call(fn)
    assert clock.slept == []


def test_a_wait_counts_against_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    env, _ = make_env(timeout=100, jitter=0.0)
    real = gateway_module.time.monotonic
    offset = {"s": 0.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:  # only the clock moves: the budget must see it itself
        waits.append(seconds)
        offset["s"] += seconds

    monkeypatch.setattr(gateway_module.time, "monotonic", lambda: real() + offset["s"])
    env.sleep = sleep
    env.call(failing("upstream_error", 2))
    assert waits == [1.0, 2.0]
    assert env.gateway.budget.spent >= sum(waits)  # the waits are inside the budget


def test_a_retry_after_that_leaves_under_a_second_exits_at_once_with_retry_after() -> None:
    env, clock = make_env(timeout=5.0)
    fn = failing("rate_limited", 5, retry_after=4.5)
    with pytest.raises(ForgectlError) as caught:
        env.call(fn)
    assert caught.value.code == "rate_limited"
    assert caught.value.retry_after == 4.5
    assert calls(fn) == 1
    assert clock.slept == []


def test_a_backoff_that_leaves_under_a_second_raises_the_previous_error() -> None:
    env, clock = make_env(timeout=1.5, jitter=0.0)
    fn = failing("network_error", 5)
    with pytest.raises(ForgectlError) as caught:
        env.call(fn)
    assert caught.value.code == "network_error"
    assert calls(fn) == 1
    assert clock.slept == []


# readiness -------------------------------------------------------------------------------------


def readiness_reads(values: list[str]) -> Callable[[], str]:
    it = iter(values)
    seen: list[str] = []

    def fetch() -> str:
        value = next(it)
        seen.append(value)
        return value

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def test_a_known_answer_is_read_once() -> None:
    env, clock = make_env()
    fetch = readiness_reads(["CLEAN"])
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("CLEAN", False)
    assert clock.slept == []


def test_an_unknown_answer_is_read_again_until_known() -> None:
    env, clock = make_env()
    fetch = readiness_reads(["UNKNOWN", "UNKNOWN", "CLEAN"])
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("CLEAN", False)
    assert len(clock.slept) == 2


def test_show_gives_up_after_three_tries_and_reports_unknown() -> None:
    env, _ = make_env()
    fetch = readiness_reads(["UNKNOWN"] * 5)
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("UNKNOWN", True)
    assert len(fetch.seen) == 3  # type: ignore[attr-defined]


def test_merge_gives_up_after_five_tries_within_ten_seconds() -> None:
    env, clock = make_env()
    fetch = readiness_reads(["UNKNOWN"] * 9)
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", MERGE_READINESS) == ("UNKNOWN", True)
    assert len(fetch.seen) == 5  # type: ignore[attr-defined]
    assert sum(clock.slept) <= 10.0


def test_the_window_ends_the_tries_early_when_reads_are_slow() -> None:
    clock = Clock()
    env, _ = make_env(clock=clock)

    def slow() -> str:
        clock.now += 3.0  # each read takes 3 s
        return "UNKNOWN"

    assert env.until_known(slow, lambda v: True, SHOW_READINESS) == ("UNKNOWN", True)
    assert clock.now <= 4.0 + 3.0  # a second read at most


def test_a_readiness_pause_that_leaves_under_a_second_is_not_taken() -> None:
    env, clock = make_env(timeout=2.0)  # pause 4/3 s + 1 s > 2 s
    fetch = readiness_reads(["UNKNOWN", "CLEAN"])
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("UNKNOWN", True)
    assert clock.slept == []
    assert len(fetch.seen) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("code", ["rate_limited", "upstream_error", "network_error"])
def test_a_readiness_re_read_that_fails_retryably_leaves_the_value_unknown(code: str) -> None:
    env, _ = make_env()
    state = {"n": 0}

    def fetch() -> str:
        state["n"] += 1
        if state["n"] > 1:
            raise ForgectlError(code, "the budget ran out")
        return "UNKNOWN"

    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("UNKNOWN", True)
    assert state["n"] == 2


def test_a_readiness_re_read_that_fails_otherwise_still_raises() -> None:
    env, _ = make_env()
    state = {"n": 0}

    def fetch() -> str:
        state["n"] += 1
        if state["n"] > 1:
            raise ForgectlError("not_found", "gone")
        return "UNKNOWN"

    with pytest.raises(ForgectlError) as caught:
        env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS)
    assert caught.value.code == "not_found"


def test_prs_has_no_readiness_retry() -> None:
    env, clock = make_env()
    fetch = readiness_reads(["UNKNOWN", "CLEAN"])
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", NO_READINESS) == ("UNKNOWN", True)
    assert clock.slept == []


def test_retries_zero_turns_the_readiness_retry_off_too() -> None:
    env, clock = make_env(retries=0)
    fetch = readiness_reads(["UNKNOWN", "CLEAN"])
    assert env.until_known(fetch, lambda v: v == "UNKNOWN", SHOW_READINESS) == ("UNKNOWN", True)
    assert clock.slept == []


def test_a_readiness_read_retries_through_call_and_is_not_retried_twice() -> None:
    env, _ = make_env()
    state = {"n": 0}

    def fetch() -> str:
        state["n"] += 1
        if state["n"] == 2:
            raise ForgectlError("upstream_error", "blip")
        return "UNKNOWN" if state["n"] < 4 else "CLEAN"

    assert env.until_known(lambda: env.call(fetch), lambda v: v == "UNKNOWN", SHOW_READINESS) == (
        "CLEAN",
        False,
    )
    assert state["n"] == 4  # one failure retried once, three readiness reads
    with pytest.raises(ForgectlError):  # until_known itself never retries a failure
        env.until_known(failing("upstream_error", 1), lambda v: False, SHOW_READINESS)


# the backend table -----------------------------------------------------------------------------


def test_the_backend_table_has_github_and_names_a_module_and_class() -> None:
    assert BACKEND_CLASSES["github"] == ("forgectl.backends.github", "GitHubBackend")


def test_an_unknown_backend_is_an_internal_error_not_a_traceback() -> None:
    env, _ = make_env()
    with pytest.raises(ForgectlError) as caught:
        create_backend("nope", env)
    assert caught.value.code == "internal"


def test_create_backend_builds_the_class_of_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    env, _ = make_env()
    built: list[Any] = []

    class Fake:
        name = "github"

        @classmethod
        def from_env(cls, given: ReadEnv) -> Any:
            built.append(given)
            return cls()

    import forgectl.backends.github as module

    monkeypatch.setattr(module, "GitHubBackend", Fake)
    assert isinstance(create_backend("github", env), Fake)
    assert built == [env]


def test_show_is_registered_by_reads() -> None:
    from forgectl import registry

    assert "forgectl.reads" in registry.HANDLER_MODULES
    assert callable(reads.show)


def test_a_cancel_during_a_backoff_ends_the_wait_early() -> None:
    from forgectl.errors import Cancelled
    from forgectl.gateway import CALL_SCOPE, CallScope

    stop = {"now": False}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        stop["now"] = True  # the cancel arrives during the first slice

    token = CALL_SCOPE.set(CallScope(cancelled=lambda: stop["now"]))
    try:
        env, _clock = make_env()
        env.sleep = sleep
        with pytest.raises(Cancelled):
            env.call(failing("network_error", 1))
    finally:
        CALL_SCOPE.reset(token)
    assert len(slept) == 1 and slept[0] < 1.0  # a slice, not the whole backoff
