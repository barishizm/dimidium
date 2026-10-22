from app.rate_limit import SlidingWindowLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_limit_per_key_within_window() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 10, clock=clock)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")  # keys are independent


def test_window_slides_and_rejected_hits_do_not_count() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 10, clock=clock)
    assert limiter.allow("a")
    clock.now = 9
    assert not limiter.allow("a")
    clock.now = 10.5  # the first hit left the window; the rejected one was never recorded
    assert limiter.allow("a")


def test_idle_keys_are_forgotten() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(5, 10, clock=clock)
    limiter.allow("a")
    limiter.allow("b")
    clock.now = 20
    limiter.allow("c")
    assert len(limiter) == 1
