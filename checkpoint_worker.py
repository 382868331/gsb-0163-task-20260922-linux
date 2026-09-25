#!/usr/bin/env python3
"""检查点跨进程 worker：纯 JSON 进、JSON 出，不用 pickle。

供 ``demo.py`` 与 ``tests/test_checkpoint.py`` 启动真实子进程使用，
验证检查点可以在另一个进程里仅凭 JSON 数据和同内容字符表恢复。

用法（在仓库根目录）::

    python checkpoint_worker.py save    < spec.json
    python checkpoint_worker.py resume  < spec.json

save 输入::

    {"table": "demo", "chunks": ["...", "..."]}

``table`` 也可以是显式 JSON 规则（推荐的跨进程形态，演示不依赖对象
地址、仅凭规则内容重建同内容表）::

    {"table": {"rules": {  # DictCharacterTable.canonical_rules() 的输出
        "decompositions": [...], "combining_classes": [...],
        "compositions": [...]}}, "chunks": [...]}

save 输出::

    {"checkpoint": {...}, "state": {"chunk_count": N, "stream_pos": N,
     "finished": false, "pending_segment": [码点, ...]}}

resume 输入::

    {"table": "demo", "tamper": false, "checkpoint": {...}, "chunks": [...]}

``tamper: true`` 时改用内容不同的表（多一条组合规则），用于演示表
不匹配。resume 正常输出 ``{"result": {"text": ..., "codepoints": [...],
"spans": [[chunk, start, end], ...]}}``；检查点错误（如表指纹不匹配）
输出 ``{"error": "..."}`` 并以退出码 1 结束。
"""

from __future__ import annotations

import json
import sys

from canoncompose import (
    CheckpointError,
    DictCharacterTable,
    StreamNormalizer,
)
from canoncompose.samples import build_demo_table, build_demo_table_plus


def _build_table(spec: dict):
    """按 spec 构造表：内置 demo 表，或仅凭 JSON 规则重建同内容表。"""
    table_spec = spec.get("table", "demo")
    if isinstance(table_spec, dict):
        rules = table_spec.get("rules")
        if not isinstance(rules, dict):
            raise SystemExit("table.rules must be an object")
        return DictCharacterTable(
            decompositions={
                cp: tuple(children)
                for cp, children in rules.get("decompositions", [])
            },
            combining_classes={
                cp: ccc for cp, ccc in rules.get("combining_classes", [])
            },
            compositions={
                (left, right): result
                for left, right, result in rules.get("compositions", [])
            },
        )
    if table_spec != "demo":
        raise SystemExit(f"unknown table {table_spec!r}")
    if spec.get("tamper", False):
        return build_demo_table_plus()
    return build_demo_table()


def _cmd_save(spec: dict) -> dict:
    table = _build_table(spec)
    stream = StreamNormalizer(table)
    for chunk in spec["chunks"]:
        stream.feed(chunk)
    checkpoint = stream.export_checkpoint()
    return {
        "checkpoint": checkpoint,
        "state": {
            "chunk_count": checkpoint["chunk_count"],
            "stream_pos": checkpoint["stream_pos"],
            "finished": checkpoint["finished"],
            "pending_segment": [
                entry[0] for entry in checkpoint["pending_segment"]
            ],
        },
    }


def _cmd_resume(spec: dict) -> dict:
    table = _build_table(spec)
    stream = StreamNormalizer.restore_checkpoint(spec["checkpoint"], table)
    for chunk in spec["chunks"]:
        stream.feed(chunk)
    result = stream.finish()
    return {
        "result": {
            "text": result.text,
            "codepoints": list(result.codepoints),
            "spans": [
                [s.chunk_index, s.start, s.end] for s in result.source_spans
            ],
        }
    }


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    spec = json.load(sys.stdin)
    if mode == "save":
        payload = _cmd_save(spec)
    elif mode == "resume":
        payload = _cmd_resume(spec)
    else:
        sys.stderr.write(__doc__)
        return 2
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckpointError as exc:
        json.dump(
            {"error": f"{type(exc).__name__}: {exc}"},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        sys.exit(1)
