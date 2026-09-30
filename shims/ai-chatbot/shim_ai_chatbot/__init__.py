"""ai-chatbot → harness-bench 协议转译 shim（接入侧组件，change-plan §2）。

分层：translator.py 是零依赖纯函数转译器（框架测试直接导入）；
server.py 是四端点 HTTP 服务（纯标准库实现，同 dev/mock_server 的先例）。
"""
