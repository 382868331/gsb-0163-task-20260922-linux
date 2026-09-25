"""子进程助手：真实进程间传递 JSON 检查点。

本脚本被 tests/test_checkpoint.py 以子进程方式调用（不 import）：

    python tests/_checkpoint_worker.py export <checkpoint.json>
    python tests/_checkpoint_worker.py resume <checkpoint.json>
    python tests/_checkpoint_worker.py resume_mismatch <checkpoint.json>

- ``export``：用确定性字符表喂入前半 chunks，导出检查点写入 JSON 文件；
- ``resume``：从 JSON 文件恢复，喂入剩余 chunks，``finish`` 后把结果
  （码点 + 来源区间）以 JSON 打印到 stdout 最后一行；
- ``resume_mismatch``：用**内容不同**的表恢复同一检查点，期望被拒绝，
  把拒绝信息以 JSON 打印到 stdout 最后一行。

stdout 最后一行始终是单个 JSON 对象，便于父进程解析。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from canoncompose import (  # noqa: E402
    CheckpointError,
    StreamNormalizer,
)
from canoncompose.samples import (  # noqa: E402
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    P_A_GRAVE_RING,
    build_demo_table,
)
from canoncompose.table import DictCharacterTable  # noqa: E402

# 固定输入：两级递归预组合 + 跨块 ccc 重排 + 可组合段。
CHUNKS = [
    chr(P_A_GRAVE_RING) + chr(A),
    chr(M_GRAVE) + chr(M_DOTBELOW),
    chr(E) + chr(M_ACUTE),
]
SPLIT = 1  # export 喂 CHUNKS[:1]，resume 接着喂 CHUNKS[1:]


def _build_mismatch_table() -> DictCharacterTable:
    """与演示表**内容不同**的表（缺一条组合规则、ccc 不同）。"""
    return DictCharacterTable(
        decompositions={P_A_GRAVE_RING: (A,)},
        combining_classes={M_GRAVE: 231},
    )


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str]) -> int:
    mode, checkpoint_path = argv[1], argv[2]

    if mode == "export":
        sn = StreamNormalizer(build_demo_table())
        for chunk in CHUNKS[:SPLIT]:
            sn.feed(chunk)
        checkpoint = sn.export_checkpoint()
        with open(checkpoint_path, "w", encoding="utf-8") as fh:
            json.dump(checkpoint, fh, ensure_ascii=False, indent=2)
        _emit(
            {
                "mode": mode,
                "fed_chunks": SPLIT,
                "checkpoint_bytes": os.path.getsize(checkpoint_path),
            }
        )
        return 0

    with open(checkpoint_path, encoding="utf-8") as fh:
        checkpoint = json.load(fh)

    table = build_demo_table() if mode == "resume" else _build_mismatch_table()
    try:
        sn = StreamNormalizer.restore_checkpoint(checkpoint, table)
    except CheckpointError as exc:
        _emit({"mode": mode, "rejected": type(exc).__name__, "detail": str(exc)})
        # resume_mismatch 预期被拒绝；resume 被拒绝是失败。
        return 0 if mode == "resume_mismatch" else 1

    if mode == "resume_mismatch":
        # 不匹配却恢复成功：失败。
        _emit({"mode": mode, "rejected": None})
        return 1

    for chunk in CHUNKS[SPLIT:]:
        sn.feed(chunk)
    result = sn.finish()
    _emit(
        {
            "mode": mode,
            "codepoints": list(result.codepoints),
            "spans": [
                [s.chunk_index, s.start, s.end] for s in result.source_spans
            ],
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
