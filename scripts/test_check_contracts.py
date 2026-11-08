#!/usr/bin/env python3
"""`check_contracts.py` 的反例测试——门禁最怕的事情是「什么都不查，然后打 OK」。

每个用例都从零搭一棵最小仓库树（manifests/ + contracts/），只保留触发器需要
的那一点结构，跑 `run()` 并断言它红在对的地方。绿的那些同样要断言，否则一个
恒返回 1 的脚本也能过全部红用例。
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_contracts as cc  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

GOOD_MANIFEST = """\
apiVersion: oriera.dev/v1
kind: LayerManifest
metadata:
  layer: L2
spec:
  provides:
    - id: oriera.core.scheduler.v1
      contract: proto/oriera/core/v1/scheduler.proto
      stability: stable
  requires: []
"""


class Tree:
    """一次性的最小仓库目录。"""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "manifests").mkdir()
        (self.root / "contracts").mkdir()

    def proto(self, rel: str, body: str) -> "Tree":
        p = self.root / "contracts" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return self

    def manifest(self, name: str, body: str) -> "Tree":
        (self.root / "manifests" / name).write_text(textwrap.dedent(body), encoding="utf-8")
        return self

    def cleanup(self) -> None:
        self._tmp.cleanup()


def run_gate(tree: Tree) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cc.run(tree.root, with_protoc=False, verbose=True)
    return code, buf.getvalue()


class ContractGateTest(unittest.TestCase):
    def test_repo_itself_is_green(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cc.run(REPO, with_protoc=False, verbose=True)
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("contract gate OK", buf.getvalue())

    def test_good_tree_is_green(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame {
              uint64 frame_index = 1;
              float  dt_seconds = 2;
            }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 0, out)
            self.assertIn("contract gate OK", out)
        finally:
            t.cleanup()

    def test_missing_proto_file_turns_red(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("不存在", out)
        finally:
            t.cleanup()

    def test_package_must_match_path(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v2;
            message BeginFrame { uint64 frame_index = 1; }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("与路径", out)
        finally:
            t.cleanup()

    def test_duplicate_field_number_turns_red(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame {
              uint64 frame_index = 1;
              float  dt_seconds = 1;
            }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("字段号 1 重复", out)
        finally:
            t.cleanup()

    def test_reserved_segment_and_zero_rejected(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame {
              uint64 a = 0;
              uint64 b = 19001;
            }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("字段号为 0", out)
            self.assertIn("保留段", out)
        finally:
            t.cleanup()

    def test_reusing_reserved_number_turns_red(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame {
              reserved 3 to 5;
              uint64 frame_index = 1;
              float  dt_seconds = 4;
            }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("已 reserved 的号 4", out)
        finally:
            t.cleanup()

    def test_enum_first_value_rules(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            enum Quality {
              LOW = 1;
              HIGH = 2;
            }
            message M { uint64 a = 1; }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("proto3 要求 0", out)
            self.assertIn("_UNSPECIFIED", out)
        finally:
            t.cleanup()

    def test_unbalanced_brace_turns_red(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame {
              uint64 frame_index = 1;
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("花括号", out)
        finally:
            t.cleanup()

    def test_import_must_resolve(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            import "oriera/common/v1/time.proto";
            message BeginFrame { uint64 frame_index = 1; }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("import", out)
        finally:
            t.cleanup()

    def test_orphan_proto_is_only_a_warning(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message BeginFrame { uint64 frame_index = 1; }
            """,
        )
        t.proto(
            "oriera/genai/v1/genai.proto",
            """\
            syntax = "proto3";
            package oriera.genai.v1;
            message Gen { uint64 a = 1; }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 0, out)
            self.assertIn("没有被任何 manifest 声明", out)
        finally:
            t.cleanup()

    def test_nested_type_names_are_checked(self):
        t = Tree()
        t.manifest("L2.yaml", GOOD_MANIFEST)
        t.proto(
            "oriera/core/v1/scheduler.proto",
            """\
            syntax = "proto3";
            package oriera.core.v1;
            message Outer {
              message Inner { uint64 a = 1; }
              uint64 b = 2;
            }
            message Inner { uint64 a = 1; }
            """,
        )
        try:
            code, out = run_gate(t)
            self.assertEqual(code, 1)
            self.assertIn("类型名 'Inner' 重复", out)
        finally:
            t.cleanup()


if __name__ == "__main__":
    unittest.main()
