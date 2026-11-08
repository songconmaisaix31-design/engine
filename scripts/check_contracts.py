#!/usr/bin/env python3
"""契约门禁：manifest 里的 proto 引用 + proto 自身的结构与语义粗检。

`check_manifests.py` 只回答「声明的 proto 文件在不在」。这个脚本回答下一个
问题：在不在是一回事，能不能用、和声明对不对得上是另一回事。

检查项：
  1. manifests 里每个 `proto/<path>` 引用都存在，且文件里的 package 与路径一致
     （`proto/oriera/core/v1/scheduler.proto` 必须声明 `oriera.core.v1`）
  2. 每个 .proto：`syntax = "proto3"`、package 与目录一致、import 可解析
  3. 字段号：同一容器内不重复、不为 0、不落 19000–19999 保留段、
     不使用 `reserved` 掉的号；字段名不重复
  4. enum：首值必须为 0 且以 `_UNSPECIFIED` 结尾（proto3 的零值语义）
  5. 同一 package 内类型名（含嵌套）不重复
  6. 机器上有 `protoc` 时再真编译一遍（descriptor 写到临时目录，不落仓库）

没被任何 manifest 声明的 proto 只报 warning（`contracts/oriera/genai/v1` 目前
就是这种：L8 的 manifest 还是 `rust:` 引用）。

退出码 1 表示有违规。仅依赖 stdlib + PyYAML，和仓库其它脚本保持一致。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FIELD_NUM_RESERVED_LO = 19000
FIELD_NUM_RESERVED_HI = 19999

TYPE_START = re.compile(r"\b(message|enum)\s+([A-Za-z_]\w*)\s*\{")
FIELD_RE = re.compile(
    r"^\s*(?:repeated\s+|optional\s+|required\s+)?"
    r"(?:map\s*<[^>]*>|[A-Za-z_][\w.]*)\s+"
    r"([A-Za-z_]\w*)\s*=\s*(\d+)\s*(?:\[[^\]]*\])?\s*;",
    re.M,
)
ENUM_VALUE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(-?\d+)\s*(?:\[[^\]]*\])?\s*;", re.M)
RESERVED_RE = re.compile(r"\breserved\b([^;]*);")
IMPORT_RE = re.compile(r'\bimport\s+(?:public\s+|weak\s+)?"([^"]+)"\s*;')
PACKAGE_RE = re.compile(r"\bpackage\s+([\w.]+)\s*;")
SYNTAX_RE = re.compile(r'\bsyntax\s*=\s*"([^"]+)"\s*;')


def strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释，但不动字符串字面量里的内容。"""
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def match_brace(text: str, open_idx: int) -> int:
    """返回与 text[open_idx] 处 '{' 配对的 '}' 下标；不配对返回 -1。"""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def strip_nested(text: str) -> str:
    """去掉嵌套的 message/enum 块，只留本层语句（字段、option、reserved）。"""
    out: list[str] = []
    pos = 0
    while True:
        m = TYPE_START.search(text, pos)
        if not m:
            out.append(text[pos:])
            break
        brace = text.find("{", m.end() - 1)
        close = match_brace(text, brace)
        if close < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:m.start()])
        pos = close + 1
    return "".join(out)


def reserved_numbers(stmt_body: str) -> set[int]:
    """解析 `reserved 2, 4 to 6;` 里的号段。"""
    nums: set[int] = set()
    for part in stmt_body.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)\s+to\s+(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo <= hi and hi - lo <= 4096:
                nums.update(range(lo, hi + 1))
            continue
        m = re.fullmatch(r"(\d+)", part)
        if m:
            nums.add(int(m.group(1)))
    return nums


def lint_message(body: str, where: str, errs: list[str], rel: str) -> None:
    flat = strip_nested(body)
    reserved: set[int] = set()
    for m in RESERVED_RE.finditer(flat):
        reserved |= reserved_numbers(m.group(1))

    numbers: dict[int, str] = {}
    names: dict[str, int] = {}
    for m in FIELD_RE.finditer(flat):
        fname, num = m.group(1), int(m.group(2))
        if num == 0:
            errs.append(f"{rel}: {where}.{fname} 字段号为 0（proto3 不允许）")
        if FIELD_NUM_RESERVED_LO <= num <= FIELD_NUM_RESERVED_HI:
            errs.append(f"{rel}: {where}.{fname} 用了保留段号 {num}（19000–19999）")
        if num in reserved:
            errs.append(f"{rel}: {where}.{fname} 用了已 reserved 的号 {num}")
        if num in numbers:
            errs.append(f"{rel}: {where} 字段号 {num} 重复（{numbers[num]} 与 {fname}）")
        else:
            numbers[num] = fname
        if fname in names:
            errs.append(f"{rel}: {where} 字段名 {fname!r} 重复")
        else:
            names[fname] = num


def lint_enum(body: str, where: str, errs: list[str], rel: str) -> None:
    flat = strip_nested(body)
    values = ENUM_VALUE_RE.findall(flat)
    if not values:
        errs.append(f"{rel}: {where} 是空 enum")
        return
    first_name, first_val = values[0]
    if int(first_val) != 0:
        errs.append(f"{rel}: {where} 首值 {first_name}={first_val}，proto3 要求 0")
    if not first_name.endswith("_UNSPECIFIED"):
        errs.append(f"{rel}: {where} 首值应叫 *_UNSPECIFIED，当前是 {first_name!r}")
    allow_alias = re.search(r"allow_alias\s*=\s*true", body) is not None
    seen: dict[int, str] = {}
    for name, val in values:
        n = int(val)
        if n in seen and not allow_alias:
            errs.append(f"{rel}: {where} 枚举值 {n} 重复（{seen[n]} 与 {name}）")
        elif n not in seen:
            seen[n] = name


def scan_scope(
    code: str,
    lo: int,
    hi: int,
    where: str,
    errs: list[str],
    rel: str,
    types: list[tuple[str, str]],
) -> None:
    """递归走完 [lo, hi) 里的 message/enum，任意嵌套深度。"""
    pos = lo
    while True:
        m = TYPE_START.search(code, pos, hi)
        if not m:
            return
        brace = code.find("{", m.end() - 1, hi)
        if brace < 0:
            errs.append(f"{rel}: {where}: `{m.group(2)}` 缺左花括号")
            return
        close = match_brace(code, brace)
        if close < 0 or close > hi:
            errs.append(f"{rel}: {where}: `{m.group(2)}` 花括号未闭合")
            return
        kind, name = m.group(1), m.group(2)
        child_where = f"{where}.{name}" if where else name
        types.append((kind, child_where))
        body = code[m.end():close]
        if kind == "message":
            lint_message(body, child_where, errs, rel)
        else:
            lint_enum(body, child_where, errs, rel)
        scan_scope(code, m.end(), close, child_where, errs, rel, types)
        pos = close + 1


def expected_package(rel: str) -> str:
    """oriera/core/v1/scheduler.proto -> oriera.core.v1"""
    return rel.rsplit("/", 1)[0].replace("/", ".")


def lint_proto(path: Path, contracts_root: Path, rel: str) -> list[str]:
    """检查单个 .proto，返回问题列表（空 = 通过）。"""
    errs: list[str] = []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [f"{rel}: 空文件"]

    code = strip_comments(text)

    m = SYNTAX_RE.search(code)
    if not m:
        errs.append(f'{rel}: 缺 `syntax = "proto3";`')
    elif m.group(1) != "proto3":
        errs.append(f"{rel}: syntax 必须是 proto3，当前是 {m.group(1)!r}")

    m = PACKAGE_RE.search(code)
    want = expected_package(rel)
    if not m:
        errs.append(f"{rel}: 缺 package 声明（按路径应为 {want!r}）")
    elif m.group(1) != want:
        errs.append(f"{rel}: package 是 {m.group(1)!r}，按路径应为 {want!r}")

    for imp in IMPORT_RE.findall(code):
        if not (contracts_root / imp).is_file():
            errs.append(f"{rel}: import {imp!r} 在 contracts/ 下找不到")

    if code.count("{") != code.count("}"):
        errs.append(f"{rel}: 花括号不配平（{code.count('{')} 开 / {code.count('}')} 闭）")

    types: list[tuple[str, str]] = []
    scan_scope(code, 0, len(code), "", errs, rel, types)

    seen: dict[str, str] = {}
    for kind, name in types:
        short = name.rsplit(".", 1)[-1]
        if short in seen and seen[short] != name:
            errs.append(f"{rel}: 类型名 {short!r} 重复（{seen[short]} 与 {name}）")
        seen[short] = name

    return errs


def collect_manifest_refs(root: Path) -> tuple[list[tuple[str, str, str]], list[str]]:
    """返回 [(manifest 名, provides id, proto 相对路径)] 与解析错误。"""
    refs: list[tuple[str, str, str]] = []
    errs: list[str] = []
    for path in sorted((root / "manifests").glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            errs.append(f"{path.name}: YAML 解析失败：{exc}")
            continue
        for prov in (doc.get("spec") or {}).get("provides") or []:
            contract = prov.get("contract", "")
            if isinstance(contract, str) and contract.startswith("proto/"):
                refs.append((path.name, prov.get("id", "?"), contract[len("proto/"):]))
    return refs, errs


def protoc_compile(protos: list[Path], contracts_root: Path) -> tuple[bool, str]:
    """有 protoc 时把全部 proto 编译一遍，返回 (是否真的跑过, 输出)。"""
    protoc = shutil.which("protoc")
    if not protoc:
        return False, "protoc 未安装，跳过编译检查"
    rels = [p.relative_to(contracts_root).as_posix() for p in protos]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "contracts.pb"
        proc = subprocess.run(
            [protoc, f"--proto_path={contracts_root}", f"--descriptor_set_out={out}", *rels],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return True, proc.stderr.strip() or f"protoc 退出码 {proc.returncode}"
        return True, f"protoc OK（{len(rels)} 个文件，descriptor {out.stat().st_size} B）"


def run(root: Path, with_protoc: bool = True, verbose: bool = False) -> int:
    contracts_root = root / "contracts"
    errs: list[str] = []
    warns: list[str] = []

    refs, ref_errs = collect_manifest_refs(root)
    errs.extend(ref_errs)

    declared: dict[str, set[str]] = {}
    for manifest, prov_id, rel in refs:
        declared.setdefault(rel, set()).add(prov_id)
        target = contracts_root / rel
        if not target.is_file():
            errs.append(f"{manifest}: provides {prov_id} 引用的 {rel} 不存在")
            continue
        m = PACKAGE_RE.search(strip_comments(target.read_text(encoding="utf-8")))
        want = expected_package(rel)
        if m and m.group(1) != want:
            errs.append(
                f"{manifest}: {rel} 声明 package {m.group(1)!r}，与路径 {want!r} 不符"
            )

    protos = sorted(contracts_root.rglob("*.proto"))
    if not protos:
        errs.append("contracts/ 下没有任何 .proto")

    for path in protos:
        rel = path.relative_to(contracts_root).as_posix()
        errs.extend(lint_proto(path, contracts_root, rel))
        if rel not in declared:
            warns.append(f"{rel}: 没有被任何 manifest 声明（`rust:` 引用或尚未接入）")

    if with_protoc and protos:
        compiled, output = protoc_compile(protos, contracts_root)
        if compiled and not output.startswith("protoc OK"):
            errs.append(f"protoc 编译失败：\n{output}")
        elif verbose:
            print(f"  · {output}")

    if errs:
        print("contract gate FAILED:")
        for e in errs:
            print(f"  - {e}")
        for w in warns:
            print(f"  ! {w}")
        return 1

    if verbose:
        for w in warns:
            print(f"  ! {w}")
    print(
        f"contract gate OK: {len(protos)} protos, {len(refs)} manifest refs, "
        f"packages match paths, field numbers clean"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="contracts/ 契约门禁")
    ap.add_argument("--root", default=str(ROOT), help="仓库根目录（默认脚本所在仓库）")
    ap.add_argument("--no-protoc", action="store_true", help="不调用 protoc，只做结构检查")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    return run(Path(args.root).resolve(), with_protoc=not args.no_protoc, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
