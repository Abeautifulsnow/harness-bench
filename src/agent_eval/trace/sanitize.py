"""生产 Trace 脱敏（P1-3 Trace Replay 的前置：真实问题变成可重复 Case）。

原则：**宁可漏脱敏给人工看，也不误脱敏破坏题面**——规则集保守，只替换高置信
模式；替换是类型化占位符（[EMAIL]/[PHONE]/[SECRET]/[TOKEN]/[ID]），保留语义
形状，judge 与 candidate agent 拿到的仍是"一个问题"，不是一堆乱码。
返回 (脱敏文本, 替换次数)；次数进 replay record，为 0 时如实记 0（None 与 0
在这里没有语义分差，但替换位置不可复原是既定事实）。
"""

from __future__ import annotations

import re

# 高置信模式，按"越具体越靠前"排序（先替换的赢）
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 密钥形态：openai sk- / github ghp_ / slack xox*-
    ("[SECRET]", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("[SECRET]", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("[SECRET]", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("[EMAIL]", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("[ID]", re.compile(r"\b\d{17}[\dXx]\b")),  # 18 位身份证
    ("[PHONE]", re.compile(r"\b1[3-9]\d{9}\b")),  # 大陆手机号
    # 通用长十六进制（32+）：trace id 之外的裸 token/会话密钥
    ("[TOKEN]", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
]


def sanitize_text(text: str) -> tuple[str, int]:
    """脱敏一段文本；返回 (结果, 替换次数)。"""
    if not text:
        return text, 0
    redactions = 0
    result = text
    for placeholder, pattern in _PATTERNS:
        result, n = pattern.subn(placeholder, result)
        redactions += n
    return result, redactions
