"""FilesystemFixture：把 fixtures/<name>/ 拷贝进 iteration 工作目录（PRD §89）。

快照口径是**前后比对**（Spec §20.4 的裁决：不引 git）：``prepare`` 时记下
workspace 的清单（路径 → 大小 + 摘要），``snapshot`` 时重扫一遍，产出
"变更了什么 + 变更后的内容"。这样得到的事实是"agent 把工作区改成了什么样"，
而不是"相对某个初态的补丁"——断言与失败复盘要的正是前者。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from agent_eval.fixtures.base import FixtureHandle, FixtureProvider, resolve_fixture_dir
from agent_eval.models.artifacts import ArtifactKind, SnapshotArtifact
from agent_eval.models.case import EnvironmentSpec

# 单次快照最多留几个变更文件的内容。上限的意义是防病态情况（agent 写了几千个
# 文件）把报告与磁盘撑爆；超出时**如实标注**被省略的个数，不静默截断。
MAX_FILE_ARTIFACTS = 20
# 单个变更文件的内容上限。超限仍留文件（记 truncated），因为"它有多大"本身
# 是现场的一部分——直接跳过会让一次 100MB 的写入看起来像没发生过。
MAX_FILE_CONTENT_BYTES = 256 * 1024
# 变更清单的行数上限：清单给人看，不必求全，但省略了多少要写出来。
MAX_CHANGE_LINES = 500

_MANIFEST_KEY = "manifest"
_CHANGES_NAME = "files.changes.txt"
_CONTENT_DIR = "files"

# 初态清单的签名：字节数 + **全文件**内容摘要。用摘要而不是 mtime——mtime 在
# copytree / checkout / 容器挂载下都会抖，把"没改过"判成"改过"会制造假差异；
# 摘要也不能只取头部省时间，那会把头之后的就地改写漏判成"没改过"（见 _digest）。
Signature = tuple[int, str]


def _digest(path: Path) -> str:
    """全文件 sha256[:16]，流式读取、**不设上限**。

    这里不能"只摘前 N 字节"省时间：截断的摘要会把 N 之后的就地改写漏判成
    "没改过"——一份 2MiB、大小不变的日志在中段被改 100 字节，changes 清单就
    会说"无变更"，而那是**静默的假结论**。漏判比误报危险（与语义 diff 约束
    同源），所以宁可多读。file_digest 分块读取，内存不随文件大小涨。
    """
    try:
        with path.open("rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()[:16]
    except OSError:
        return "unreadable"


def _manifest(root: Path) -> dict[str, Signature]:
    manifest: dict[str, Signature] = {}
    if not root.is_dir():
        return manifest
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:  # 并发删除：跳过而不是让整次快照失败
            continue
        manifest[path.relative_to(root).as_posix()] = (size, _digest(path))
    return manifest


def _diff_manifest(
    before: dict[str, Signature], after: dict[str, Signature]
) -> dict[str, str]:
    """返回 {相对路径: added|modified|deleted}（按路径排序，结论可复现）。"""
    changes: dict[str, str] = {}
    for rel, signature in after.items():
        if rel not in before:
            changes[rel] = "added"
        elif before[rel] != signature:
            changes[rel] = "modified"
    for rel in before:
        if rel not in after:
            changes[rel] = "deleted"
    return dict(sorted(changes.items()))


class FilesystemFixture(FixtureProvider):
    name = "filesystem"

    def __init__(self, fixtures_root: Path) -> None:
        self._root = fixtures_root

    async def prepare(self, spec: EnvironmentSpec, workdir: Path) -> FixtureHandle:
        target = workdir / "workspace"
        target.mkdir(parents=True, exist_ok=True)
        if spec.fixture:
            source = resolve_fixture_dir(self._root, spec.fixture)
            shutil.copytree(source, target, dirs_exist_ok=True)
        handle = FixtureHandle(name=spec.fixture or "none", workdir=target)
        # 清单必须在 copytree **之后**取：那才是这次执行的初态。
        handle.info[_MANIFEST_KEY] = _manifest(target)
        return handle

    async def snapshot(self, handle: FixtureHandle) -> list[SnapshotArtifact]:
        before = handle.info.get(_MANIFEST_KEY)
        if not isinstance(before, dict):
            # 没有初态清单就没有"变更"可言；如实返回空而不是把所有文件当新增
            # （那会把 fixture 自带的全部文件冒充成 agent 的产出）。
            return []
        after = _manifest(handle.workdir)
        changes = _diff_manifest(before, after)
        changes_artifact = _changes_artifact(len(changes), changes, after, before)
        contents, omitted = _content_artifacts(handle.workdir, changes, after)
        if omitted > 0:
            # 省略记账挂在 changes 产物上（它必然存在），而不是最后一条内容产物：
            # 内容产物可能一件都没留存（全部读失败），那时的省略最需要被看见。
            note = f"另有 {omitted} 个变更文件未留存内容（上限 {MAX_FILE_ARTIFACTS} 个）"
            changes_artifact.note = (
                f"{changes_artifact.note}；{note}" if changes_artifact.note else note
            )
        return [changes_artifact, *contents]

    async def cleanup(self, handle: FixtureHandle) -> None:
        shutil.rmtree(handle.workdir, ignore_errors=True)


def _changes_artifact(
    total: int,
    changes: dict[str, str],
    after: dict[str, Signature],
    before: dict[str, Signature],
) -> SnapshotArtifact:
    """变更清单：即使零变更也要产出——"agent 没改工作区"是一个真结论。

    这与 Spec §21.4"不造空文件占位"不冲突：占位指的是**采不到的观测面**被一个
    空文件冒充（0 字节的 screenshot.png）。这里采集到了，而且"本次零变更"
    本身就是要记录的事实。
    """
    lines = ["# workspace 变更（相对 prepare 时的清单，Spec §21.2）"]
    if not changes:
        lines.append("（无变更：agent 未新增 / 修改 / 删除任何文件）")
    else:
        for rel, kind in list(changes.items())[:MAX_CHANGE_LINES]:
            lines.append(f"{kind}\t{_size_for(rel, after, before)}\t{rel}")
        omitted = total - MAX_CHANGE_LINES
        if omitted > 0:
            lines.append(f"... 另有 {omitted} 个变更未列出（上限 {MAX_CHANGE_LINES} 行）")
    text = "\n".join(lines) + "\n"
    return SnapshotArtifact(
        name=_CHANGES_NAME,
        kind=ArtifactKind.files,
        content=text.encode("utf-8"),
        note=None if changes else "本次执行未改动工作区",
        truncated=total > MAX_CHANGE_LINES,
    )


def _size_for(rel: str, after: dict[str, Signature], before: dict[str, Signature]) -> int:
    """变更后的大小；已删除的文件报初态大小（0 会读成"空文件"）。"""
    signature = after.get(rel) or before.get(rel)
    return signature[0] if signature else 0


def _content_artifacts(
    workdir: Path, changes: dict[str, str], after: dict[str, Signature]
) -> tuple[list[SnapshotArtifact], int]:
    """为新增 / 修改的文件留下内容（失败现场的可复原部分）。

    只留 added / modified：deleted 的文件已不在磁盘上，没有内容可采。
    返回 (留存的产物, 未留存的个数)：省略个数交由调用方记账——它可能发生在
    "一件都没留存"的读失败上，不能在这里悄悄吞掉。
    """
    kept: list[SnapshotArtifact] = []
    candidates = [rel for rel, kind in changes.items() if kind in {"added", "modified"}]
    for rel in candidates[:MAX_FILE_ARTIFACTS]:
        path = workdir / rel
        try:
            # 只读上限 +1 字节：大文件不整个进内存，同时能判出"被截断"。
            with path.open("rb") as fh:
                raw = fh.read(MAX_FILE_CONTENT_BYTES + 1)
        except OSError:
            continue
        truncated = len(raw) > MAX_FILE_CONTENT_BYTES
        total_size = (after.get(rel) or (len(raw), ""))[0]
        kept.append(
            SnapshotArtifact(
                name=f"{_CONTENT_DIR}/{rel}",
                kind=ArtifactKind.files,
                content=raw[:MAX_FILE_CONTENT_BYTES],
                truncated=truncated,
                note=(
                    f"内容已截断（原始 {total_size} 字节，上限 {MAX_FILE_CONTENT_BYTES}）"
                    if truncated
                    else None
                ),
            )
        )
    return kept, len(candidates) - MAX_FILE_ARTIFACTS
