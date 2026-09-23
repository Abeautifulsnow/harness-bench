# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | src/agent_eval 分层 + evals/ 定义树 + 运行产物布局 | Filled (P0) |
| [Error Handling](./error-handling.md) | 失败语义三分法 + exit code 0–3 映射 | Filled (P0) |
| [Quality Guidelines](./quality-guidelines.md) | 质量门槛、编码约束、本机构建怪癖 | Filled (P0) |

Database / Logging 指南待 P1 引入 DuckDB 与结构化日志后回填。

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
