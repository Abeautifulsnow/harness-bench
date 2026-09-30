#!/usr/bin/env python3
"""第 0 步冒烟脚本：直连被测平台 POST /api/chat，钉死外部评测接入依赖的运行时行为。

需求源：`docs/external-agent-integration-change-plan.md` §7 第 0 步（首个接入对象：
ai-chatbot/Sime，服务默认 http://localhost:3000，`pnpm start:agent` + license 激活）。
静态事实（端点、载荷字段、常量）已源码级核实（同文档附录 §8），本脚本只负责
**运行时行为**——五条验收：

  1. chunk 的实际顺序（start → 工具 → finish → [DONE]？data-* part 夹在何处）
  2. 402（license 无效）发生在流前（HTTP 状态码）还是流内 error chunk
  3. 审批触发后流是否干净以 finish+[DONE] 收尾，tool part 的 state 值实测
  4. data-sub-open/done 的 id 是否等于父流 tool part 的 toolCallId
  5. data-context-usage 出现的时机与频率（每轮一次还是每 delta 一次）

用法：

    python scripts/smoke-agent-protocol.py --base-url http://localhost:3000
    python scripts/smoke-agent-protocol.py --scenario approval   # 单场景
    python scripts/smoke-agent-protocol.py --dump-dir tmp/smoke  # 原始 chunk 落盘

仅用标准库；只读探测（conversationId 用 smoke- 前缀，事后可按前缀清理）。
安全边界：本工具的请求目标是**操作者本机的被测服务**，因此默认只允许
http(s) + 回环/私网地址，且请求钉定已校验的 IP（Host 头保留原主机名），
不跟随重定向——消除 SSRF/DNS-rebinding 暴露面；探测公网需显式 `--allow-public`。
场景提示词只负责"诱发目标行为"，模型是否配合属运行时事实——换接入对象时
按其工具面改写 SCENARIOS 即可，判定逻辑与平台无关。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_BASE_URL = "http://localhost:3000"
TIMEOUT_S = 300.0  # 子 agent 场景实测可达数分钟（被测平台 /api/chat maxDuration=300s）

# 场景 → 提示词。提示词只负责"诱发目标行为"，模型是否配合属运行时事实——
# 脚本如实报告观测到了什么，不因没诱发出而报错。
SCENARIOS: dict[str, dict] = {
    "basic": {
        "title": "基础流序 + 用量频率（验收 1/2/5）",
        "prompt": "用一句话回答：1+1 等于几？",
    },
    "approval": {
        "title": "审批挂起的流收尾（验收 3）",
        "prompt": "请调用 ask_user_question 工具向我提问，澄清我想要的输出格式。",
    },
    "subagent": {
        "title": "子代理事件关联（验收 4）",
        "prompt": "请用 agent 工具委托一个子任务：让子代理报告当前目录的文件数量。",
    },
}


def validate_target(base_url: str, allow_public: bool) -> str | None:
    """请求目标边界：仅 http(s)；默认仅回环/私网（本工具探测的是本机被测服务）。

    返回钉定的 IP（默认模式）：请求直接连该 IP、Host 头保留原主机名，消除
    "校验后二次解析"的 DNS rebinding 窗口；--allow-public 时返回 None（操作者
    显式承担公网探测风险）。
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise SystemExit(f"拒绝：协议必须是 http/https，得到 {parsed.scheme!r}")
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SystemExit(f"拒绝：无法解析主机 {host!r}: {exc}") from exc
    pinned: str | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            pinned = pinned or str(ip)
            continue
        if not allow_public:
            raise SystemExit(
                f"拒绝：{host} 解析到公网地址 {ip}。本脚本只应探测本机/内网被测服务；"
                "确需探测公网请显式加 --allow-public。"
            )
    if pinned is None and not allow_public:
        raise SystemExit(f"拒绝：{host} 未解析到任何回环/私网地址（默认只允许本机/内网目标）。")
    return pinned


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """冒烟对象是固定端点：不跟随重定向（重定向到别处本身就是要暴露的异常）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit(f"拒绝：服务返回重定向 {code} → {newurl}（冒烟目标不应重定向）")


_OPENER = urllib.request.build_opener(_NoRedirect)


def post_chat(base_url: str, body: dict, timeout: float, pinned_ip: str | None):
    """POST /api/chat；返回 (status, headers, 响应对象)。非 2xx 时响应对象仍可读 body。"""
    split = urlsplit(base_url.rstrip("/") + "/api/chat")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    url = base_url.rstrip("/") + "/api/chat"
    if pinned_ip is not None:
        # 钉 IP：连接走已校验地址，Host 头保留原主机名（路由/虚拟主机语义不变）
        netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
        if split.port:
            netloc += f":{split.port}"
        url = urlunsplit((split.scheme, netloc, split.path, "", ""))
        headers["Host"] = split.netloc
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = _OPENER.open(request, timeout=timeout)
        return resp.status, dict(resp.headers), resp
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"!! 服务不可达：{base_url}（{reason}）。请先启动被测服务并确认端口。")
        raise SystemExit(2) from exc


def iter_sse_data(resp):
    """逐行读 SSE，yield 每个 data: 载荷（原始字符串，含 [DONE]）。"""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            yield line[len("data:") :].strip()


def dump_path_for(dump_dir: str, name: str) -> Path | None:
    """dump 目录边界：拒绝 `..`，规范化为绝对路径；文件名由固定场景名拼出。"""
    if not dump_dir:
        return None
    if ".." in Path(dump_dir).parts:
        raise SystemExit(f"拒绝：dump 目录含 '..': {dump_dir}")
    directory = Path(dump_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"smoke-{name}.json"


def run_scenario(
    base_url: str,
    name: str,
    spec: dict,
    timeout: float,
    pinned_ip: str | None,
    dump_path: Path | None,
) -> dict:
    conversation_id = f"smoke-{uuid.uuid4().hex[:12]}"
    body = {"conversationId": conversation_id, "message": spec["prompt"]}
    print(f"\n=== 场景 [{name}] {spec['title']} ===")
    print(f"conversationId: {conversation_id}")

    started = time.time()
    status, headers, resp = post_chat(base_url, body, timeout, pinned_ip)
    print(
        f"HTTP {status}  Content-Type: {headers.get('Content-Type', '-')}  "
        f"X-Conversation-Id: {headers.get('X-Conversation-Id', '-')}"
    )

    report: dict = {
        "scenario": name,
        "status": status,
        "conversation_id": conversation_id,
        "chunks": [],
        "raw": [],
    }
    if status != 200:
        # 验收 2：非 200 = 违约发生在流前（HTTP 层）。license 无效时这里应为
        # 402 + body 含 error: LICENSE_INVALID（被测平台对 /api/* 的门禁）。
        error_body = resp.read().decode("utf-8", errors="replace") if resp is not None else ""
        print(f"[验收2] 非 200 → 违约发生在**流前**（HTTP 层），body: {error_body[:300]}")
        report["stream_before_error"] = {"body": error_body[:1000]}
        return report

    tool_call_ids: list[str] = []  # tool part 的 toolCallId（tool-input-available）
    sub_event_ids: list[tuple[str, str]] = []  # data-sub-open/done 的 (类型, id)
    usage_positions: list[int] = []
    types: list[str] = []

    for index, payload in enumerate(iter_sse_data(resp)):
        if payload == "[DONE]":
            types.append("[DONE]")
            report["chunks"].append({"index": index, "type": "[DONE]"})
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            types.append("<non-json>")
            report["chunks"].append({"index": index, "type": "<non-json>", "raw": payload[:200]})
            continue
        ctype = str(chunk.get("type", "<no-type>"))
        types.append(ctype)
        report["raw"].append(chunk)

        if ctype == "tool-input-available":
            tool_call_ids.append(str(chunk.get("toolCallId", "")))
        if ctype.startswith("data-sub-"):
            sub_event_ids.append((ctype, str(chunk.get("id", ""))))
        if ctype == "data-context-usage":
            usage_positions.append(index)
            if len(usage_positions) == 1:
                payload = chunk.get("data", chunk)
                keys = ", ".join(sorted(payload.keys()))
                print(f"[验收5] 首个 data-context-usage @chunk#{index} 载荷键: {keys}")
        if ctype == "error":
            print(
                f"[观察] 流内 error chunk @#{index}: {json.dumps(chunk, ensure_ascii=False)[:200]}"
            )

    elapsed = time.time() - started
    report["types"] = types
    report["tool_call_ids"] = tool_call_ids
    report["sub_event_ids"] = sub_event_ids
    report["usage_positions"] = usage_positions
    report["elapsed_s"] = round(elapsed, 1)

    # ---- 验收 1：chunk 顺序（折叠连续重复，给全局视野）----
    folded: list[str] = []
    for item in types:
        if not folded or folded[-1] != item:
            folded.append(item)
    print(f"[验收1] chunk 类型序列（折叠重复，共 {len(types)} 块）: {' → '.join(folded)}")

    # ---- 验收 2（200 路径补充）：error 是否出现在流内 ----
    in_stream_errors = [item for item in types if item == "error"]
    print(
        f"[验收2] HTTP 200 路径：流内 error chunk 数 = {len(in_stream_errors)}"
        f"（违约若在流前，本条不会执行到——上面已按非 200 报告）"
    )

    # ---- 验收 3：收尾形态 + tool part state（审批场景重点）----
    ended_done = types[-1] == "[DONE]" if types else False
    finish_idx = types.index("finish") if "finish" in types else -1
    done_idx = types.index("[DONE]") if ended_done else -1
    clean_tail = finish_idx != -1 and ended_done and done_idx == len(types) - 1
    print(
        f"[验收3] 收尾：finish @#{finish_idx}，[DONE] {'是' if ended_done else '否'}为最后一块，"
        f"干净收尾（finish 后紧跟 [DONE]）= {clean_tail}"
    )
    states = [
        c.get("state")
        for c in report["raw"]
        if isinstance(c, dict) and "state" in c and str(c.get("type", "")).startswith(("tool",))
    ]
    if states:
        print(f"[验收3] tool part 的 state 实测值: {states}")

    # ---- 验收 4：data-sub-* 的 id 与父流 toolCallId 关联 ----
    if sub_event_ids:
        print(f"[验收4] tool-input-available 的 toolCallId: {tool_call_ids}")
        for subtype, sub_id in sub_event_ids:
            hit = sub_id in tool_call_ids
            print(f"[验收4] {subtype} id={sub_id!r} ∈ toolCallId 集合 = {hit}")
        report["sub_id_matches_tool_call"] = all(
            sub_id in tool_call_ids for _, sub_id in sub_event_ids
        )
    else:
        print("[验收4] 本场景未观测到 data-sub-* 事件（提示词未诱发子代理时属预期）")

    # ---- 验收 5：usage 频率 ----
    if usage_positions:
        cadence = "每轮一次" if len(usage_positions) <= 1 else "多次/每步"
        print(
            f"[验收5] data-context-usage 出现 {len(usage_positions)} 次，"
            f"chunk 序号: {usage_positions}（{cadence}）"
        )
    else:
        print("[验收5] 未观测到 data-context-usage（被测平台源码默认每步注入，缺失即异常）")

    print(f"[耗时] {elapsed:.1f}s")
    if dump_path is not None:
        dump_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[dump] 原始 chunk 已写入 {dump_path}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    parser.add_argument("--dump-dir", default=None, help="逐场景原始 chunk JSON 的输出目录")
    parser.add_argument(
        "--allow-public",
        action="store_true",
        help="允许探测公网地址（默认仅回环/私网——本工具的探测对象是本机被测服务）",
    )
    args = parser.parse_args()

    pinned_ip = validate_target(args.base_url, args.allow_public)

    # 验收 2 的 400 分支：缺 conversationId 必须被 400 拦下（调用方自选 id 的前提）
    print("=== 场景 [no-conversation-id] 缺 conversationId 应 400 ===")
    status, _, resp = post_chat(args.base_url, {"message": "hi"}, args.timeout, pinned_ip)
    body_text = resp.read().decode("utf-8", errors="replace") if resp is not None else ""
    print(
        f"HTTP {status} body: {body_text[:200]}  → "
        f"{'符合（400 Missing conversationId）' if status == 400 else '不符合预期'}"
    )

    exit_code = 0
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        report = run_scenario(
            args.base_url,
            name,
            SCENARIOS[name],
            args.timeout,
            pinned_ip,
            dump_path_for(args.dump_dir, name),
        )
        if report["status"] != 200:
            exit_code = 2  # 服务不可用 / license 无效：冒烟未完成，不是通过
    print("\n=== 冒烟结束 ===")
    print("五条验收的结论以本输出的 [验收N] 行为准；请把输出与 dump JSON 一起回填到")
    print("`docs/external-agent-integration-change-plan.md` 的 B2 映射表校准记录。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
