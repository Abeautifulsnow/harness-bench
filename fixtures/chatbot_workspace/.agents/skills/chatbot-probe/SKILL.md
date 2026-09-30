---
name: chatbot-probe
description: C 类评测用的探针技能：被加载后按固定格式回报一行确认串
whenToUse: 当用户要求加载 chatbot-probe 技能时
---

# chatbot-probe

这是一条**评测专用**技能，不承担任何实际业务功能。它的存在只有一个目的：
让 `harness.skill_load` 有确定的名字可断言。

被调用后，请在你的回答里原样包含下面这一行（不要改写、不要翻译）：

```
SKILL-PROBE-ACK: chatbot-probe
```
