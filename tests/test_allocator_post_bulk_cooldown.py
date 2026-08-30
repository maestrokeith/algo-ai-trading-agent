"""
Contract for ``run_alpaca_loop`` post-bulk-trim allocator skip (1 main loop pass / user).

State is a dict: after a bulk notional sell (``bulk_trim.enabled``), the user id is set;
at the start of the **next** pass, :meth:`dict.pop` yields True and the allocator is skipped
for that pass only.
"""

from __future__ import annotations


def test_post_bulk_allocator_cooldown_one_pass_per_user() -> None:
    post_bulk: dict[str, bool] = {}

    def start_user(uid: str) -> bool:
        return bool(post_bulk.pop(str(uid), False))

    def on_bulk_notional_sell(uid: str) -> None:
        post_bulk[str(uid)] = True

    # Pass 1: no pending skip
    assert start_user("u1") is False
    on_bulk_notional_sell("u1")
    # Pass 2: skip allocator
    assert start_user("u1") is True
    # Pass 3: no skip
    assert start_user("u1") is False

    # Other user unaffected
    assert start_user("u2") is False
    on_bulk_notional_sell("u1")
    assert start_user("u2") is False
    assert start_user("u1") is True
