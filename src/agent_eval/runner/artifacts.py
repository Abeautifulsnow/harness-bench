"""Case 级产物的落盘与索引（PRD §90，Spec §21）。

职责边界：**provider 产出内容，collector 决定落哪、怎么记**。provider 不知道
run 目录布局，也不该知道——那是编排层的知识（与 fixtures 的"只负责制造环境"
一致）。

落盘位置 ``<run_dir>/artifacts/<case_id>/iter<N>/artifacts/``：与 fixture 的
``workspace/`` 同级而不是 inside 它（workspace 会被 cleanup 删掉，产物必须活得
比它久），再加一层 ``artifacts/`` 子目录把"给 agent 用的"与"留给人看的"分开。
文件名由 provider 给（``database.sql`` / ``files.changes.txt``），collector
只做**越界校验**——name 会直接变成磁盘路径，边界检查放在唯一的落盘点最省事。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from agent_eval.models.artifacts import ArtifactKind, ArtifactRecord, SnapshotArtifact

# artifact name 只允许"相对路径"形态：不含盘符、不以 / 开头、无 .. 分量。
# name 会参与拼接磁盘路径，所以这条校验是落盘的前置条件，不是提示。
_UNSAFE_NAME = re.compile(r"^[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*$")
MAX_NAME_LENGTH = 200
CONTENT_DIR = "artifacts"


def safe_artifact_name(name: str) -> str | None:
    """合法则返回 name，否则 None（调用方跳过并在 reason 里记账）。

    两层判断缺一不可：字符集白名单挡掉盘符 / 反斜杠 / 绝对路径，逐段 ``..``
    检查挡掉向上跳（``.`` 在字符集里，所以 ``../x`` 能通过第一层——这正是
    单靠正则容易漏的地方）。

    用白名单而不是"拦掉 ../"：后者要想到所有逃逸写法（``..``、``%2e%2e``、
    盘符、UNC、symlink），白名单只需要想清楚哪些字符是合法的。产物名由平台
    自己生成，收紧的代价为零。
    """
    if not name or len(name) > MAX_NAME_LENGTH:
        return None
    if _UNSAFE_NAME.match(name) is None:
        return None
    if ".." in name.split("/"):
        return None
    return name


def collect_artifacts(
    snapshot: list[SnapshotArtifact],
    *,
    workdir: Path,
    run_dir: Path,
    now: datetime,
) -> tuple[list[ArtifactRecord], list[str]]:
    """把 provider 的快照落盘，返回 (索引, 跳过记账)。

    ``workdir`` 是 ``<run_dir>/artifacts/<case_id>/iter<N>``（runner 已经建好，
    因为 fixture 就住在那里）；``run_dir`` 用来算出索引里的**run 目录相对路径**——
    下载端点只认这个字段，绝不用 name 拼路径（Spec §21.3）。

    第二个返回值是跳过原因：名字非法 / 写盘失败都要出现在结果里，静默跳过会让
    "少了一件产物"看起来像"这次本来就没有"。
    """
    records: list[ArtifactRecord] = []
    skipped: list[str] = []
    target_dir = workdir / CONTENT_DIR
    for item in snapshot:
        # provider 的 snapshot 是对外扩展点，可能是第三方代码：畸形元素记账后
        # 跳过。让它抛出去会演变成"一次跑完的 run 因为一件附属品变 ERROR"。
        if not isinstance(item, SnapshotArtifact):
            skipped.append(f"snapshot element is not a SnapshotArtifact: {type(item).__name__}")
            continue
        if not isinstance(item.content, bytes):
            skipped.append(
                f"artifact content must be bytes: {item.name!r} got {type(item.content).__name__}"
            )
            continue
        name = safe_artifact_name(item.name)
        if name is None:
            skipped.append(f"artifact name rejected (unsafe path): {item.name!r}")
            continue
        path = target_dir / name
        if target_dir.resolve() not in path.resolve().parents:
            # 白名单已排除 ..，这里是第二道闸：symlink 之类的解析期逃逸
            skipped.append(f"artifact path escapes the artifacts dir: {name!r}")
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(item.content)
        except OSError as exc:
            skipped.append(f"artifact write failed: {name!r} ({exc})")
            continue
        records.append(
            ArtifactRecord(
                name=name,
                kind=item.kind,
                path=path.relative_to(run_dir).as_posix(),
                bytes=len(item.content),
                collected_at=now,
                truncated=item.truncated,
                note=item.note,
            )
        )
    return records, skipped


def trace_artifact_record(
    trace_path: Path | None, run_dir: Path, now: datetime
) -> list[ArtifactRecord]:
    """把已落盘的 Raw Trace 也登记进 case 级索引（PRD §90 的现场一类）。

    Trace 由 trace 子系统写（本任务不碰它的格式），这里只登记路径——这样
    "这个 case_run 有哪些现场可看"能从一个地方回答，而不是让人记两套路径。
    """
    if trace_path is None:
        return []
    path = Path(trace_path)
    if not path.is_file():
        return []
    try:
        relative = path.relative_to(run_dir)
    except ValueError:  # trace 不在本 run 目录下：不登记（相对路径无法安全回读）
        return []
    return [
        ArtifactRecord(
            name="raw.trace.jsonl",
            kind=ArtifactKind.trace,
            path=relative.as_posix(),
            bytes=path.stat().st_size,
            collected_at=now,
            note="Raw Trace（事件流，Span Tree 可由它重建）",
        )
    ]
