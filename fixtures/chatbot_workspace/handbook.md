# 数据平台值班手册（fixture）

本文件是 C 类评测集（ai-chatbot，change-plan §3）的 fixture 文件之一，供被测
Agent 读取。它在用例里承担两个作用：给"读文件"类断言一个**确定的名字**，
以及给 `file_state` 提供一个"初态就存在"的对照。

## 关键约定

- 值班交接必须写明 HANDOFF-CODE，格式为 `HANDOFF-<4 位数字>`。
- 数据导出目录是 `data/`，归档目录是 `archive/`。
- 事故复盘写在 `archive/legacy_note.md` 里。
