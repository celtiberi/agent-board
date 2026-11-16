"""Concurrency tests for gapless seq allocation under parallel writers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from agent_board.services import messages as messages_svc
from tests.conftest import BoardTestEnv


def test_50_parallel_writers_produce_gapless_seq(env: BoardTestEnv) -> None:
    def _post(idx: int) -> int:
        ctx = env.bob_ctx if idx % 2 == 0 else env.dev_ctx
        res = messages_svc.post_message(
            env.services,
            ctx,
            board_slug="alpha",
            msg_type="note",
            body_md=f"Parallel message {idx}",
            thread_title="concurrency",
        )
        return int(res["seq"])

    with ThreadPoolExecutor(max_workers=20) as pool:
        seqs = list(pool.map(_post, range(50)))

    assert sorted(seqs) == list(range(1, 51))
    assert len(set(seqs)) == 50

    read_back = messages_svc.read_messages(env.services, env.owner_ctx, board_slug="alpha", since_seq=0, limit=100)
    assert [m["seq"] for m in read_back["messages"]] == list(range(1, 51))
