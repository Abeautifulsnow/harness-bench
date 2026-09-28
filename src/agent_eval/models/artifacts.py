"""Case 级产物（PRD §90，Spec §21）。

产物是**一次执行的现场**：工作区变成了什么样、库快照是什么、原始 trace 在哪。
它与 Raw Trace 是同一类东西（PRD §110-2「Raw Trace 必须永久可追溯」），
只是观测面不同——trace 记 agent **说了什么**，artifact 记环境**变成了什么**。

两条硬约束：

1. **必须与 case_run_id 关联**（PRD §90 原文）。索引存在
   ``CaseRunResult.artifacts`` 里，而 case_run_id 就是 ``CaseRunResult.id``：
   反查天然成立，不需要第二份"case_run_id → 产物"的映射表。多一份映射就多一个
   漂移点，而漂移的表现是"产物在磁盘上、索引里查不到"——比没有索引更难查。
2. **采不到的不要造空文件占位**。一个 0 字节的 ``screenshot.png`` 会让"已采集"
   的统计说谎。当前采不到的类型在 Spec §21.1 的能力表里如实标注，报告与 Web
   据此说明缺口，而不是留下占位文件。

落盘位置是 ``<run_dir>/artifacts/<case_id>/iter<N>/``，与 fixture 的
``workspace/`` 同级——**不是** inside workspace：provider 的 cleanup 会删掉
workspace，而产物必须在 cleanup 之后仍然可读（Spec §21.2 的采集时机）。

与 ``fixtures/snapshot.py`` 的 ``EnvironmentSnapshot`` 是两个东西，别混：
后者是**判定用的读句柄**（``database_state`` / ``file_state`` 断言去查它），
不落盘、不索引；本模块的 ``SnapshotArtifact`` 是**给人看的现场副本**，会写进
run 目录并被索引。两者共用"cleanup 前采集"这一条时机约束（Spec §19.1/§21.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ArtifactKind(StrEnum):
    """可采集的产物类型（PRD §90 的清单里当前真能落地的那几项）。"""

    files = "files"
    database = "database"
    trace = "trace"


# PRD §90 列了但当前**采不到**的类型。写在这里而不是只写在 Spec 里，
# 是因为测试与报告都要引用它：一份会漂移的口头说明等于没有说明。
# 每项必须给出"为什么采不到"，不允许笼统的"暂不支持"。
UNAVAILABLE_KINDS: dict[str, str] = {
    "screenshots": ("平台无浏览器/桌面 fixture，没有截图观测面（PRD §88 的 remote 环境也未落地）"),
    "logs": "当前没有 run 级日志文件——进程日志走 stdout，未落盘，故无文件可采集",
    "git_diff": "裁决不实现：fixture workdir 会继承外层平台仓库（Spec §20.4），改用 files 快照",
    "command_output": (
        "观测面已由 trace 事件流覆盖（command.started/finished、tool.result），不重复落盘"
    ),
    "reports": "run 级五件套已存在（Spec §6.2），不属于 case 级产物",
}


class SnapshotUnavailable(Exception):
    """provider 明确知道"这次该采的采不到"时的信号（Spec §21.2）。

    它**不是错误**：runner 兜底把它记进 ``CaseRunResult.artifact_notes``，
    执行结论不变。它与返回 ``[]`` 的区别在语义：``[]`` 是"本次没有可采集的
    东西"，本异常是"该采的这次拿不到，原因如下"——把后者静默表现成前者，
    正是"少了一件"看起来像"本来就没有"的那种漂移。
    """


@dataclass
class SnapshotArtifact:
    """provider 快照产出的待落盘内容（fixtures → runner 的**唯一**交接形状）。

    ``content`` 是 bytes 而不是 str：变更文件里可能有二进制（脚本、图片、sqlite
    文件），按文本读会抛异常或静默替换字符，两者都会让"现场"失真。
    """

    name: str
    kind: ArtifactKind
    content: bytes
    truncated: bool = False
    note: str | None = None


class ArtifactRecord(BaseModel):
    """一条已落盘产物的索引项（PRD §90：文件名 → 类型 → 大小 → 时间）。

    ``case_id`` / ``iteration`` 不在字段里：索引就挂在 ``CaseRunResult`` 上，
    它们由宿主对象给出。冗余一份会让"索引说 iter1、文件在 iter2"这种漂移没有
    单一事实源。

    ``path`` 是 **run 目录相对路径**，且是下载端点解析文件的唯一入口：
    端点拿 ``name`` 去索引里查 ``path``，绝不用 ``name`` 拼路径——拼路径就是
    路径穿越的来源（Spec §21.3）。
    """

    name: str
    kind: ArtifactKind
    path: str
    bytes: int
    collected_at: datetime
    truncated: bool = False
    note: str | None = None
