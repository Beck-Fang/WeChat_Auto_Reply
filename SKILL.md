---
name: wechat-auto-reply
description: >-
  搭建、配置并运行微信自动回复系统。该系统持续监听微信新消息，调用大模型生成回复内容，并通过鼠标自动化发送到对方。
  当用户需要启动自动回复、调整大模型配置、设置联系人过滤规则、自定义 System Prompt，或排查自动回复异常时使用。
version: 1.0.0
tags: [wechat, auto-reply]
---

# 微信自动回复系统

## 工作原理

`monitor.py` 持续轮询微信数据库 WAL 文件，检测到新消息后：

1. 调用 **WeChat-Strong-MCP** 读取最近聊天记录（`get_chat_history`）
2. 将记录作为 User Prompt，调用大模型生成回复文本
3. 调用 **WeChat-MCP-Server** 的 `send_wechat_message` 通过鼠标自动化发送消息

## 快速启动

```bash
python skills/WeChat_Auto_Reply_Skill/monitor.py
```

将产生的config.json和all_keys.json两个文件，复制到mcp/WeChat-Strong-MCP/

运行前确保：微信客户端已登录，**且微信窗口可见**（不要最小化）。

## 配置文件(skills/WeChat_Auto_Reply_Skill)

### `config/your_config.json` — 核心配置

| 字段 | 必填 | 说明 |
|------|------|------|
| `api_key` | ✅ | 大模型 API Key（支持 OpenAI 格式） |
| `base_url` | ✅ | 大模型接口地址 |
| `model` | ✅ | 模型名称 |
| `limit` | 可选(默认50) | 查询最近聊天消息条数 |
| `offset` | 可选(默认0) | limit 的偏移量 |
| `start_time` | 可选(默认"1999-01-01") | 查询聊天记录开始时间 |
| `end_time` | 可选(默认"2099-12-31") | 查询聊天记录结束时间 |
| `poll_interval` | 可选(默认0.5s) | 消息轮询间隔，**不可热更新** |
| `monitor_receive_cooldown_sec` | 可选(默认20s) | 同一联系人两次回复的最短间隔，**不可热更新** |
| `always_reply` | 可选(默认false) | 最新消息是自己发的也继续回复 |
| `allow_names_start_with` | 可选(默认空) | 允许自动回复的联系人名字前缀白名单 |
| `allow_names_end_with` | 可选(默认空) | 允许自动回复的联系人名字后缀白名单 |

> 标记"可以实时配置"的字段在程序运行期间修改 JSON 文件后立即生效，无需重启。

### `config/{联系人名}.txt` — 每人专属 System Prompt

文件名即微信联系人昵称（如 `config/张三.txt`）。文件内容作为该联系人对话的 System Prompt。

若无对应文件，默认 System Prompt 为：
> 你是一个微信自动回复助手，我会给你发送聊天记录，你根据这些记录（一个是对方，一个是我（me）），直接给我应该回复什么内容，不要回复废话

参考 `config/example.txt` 编写自定义 Prompt。

### `config/mcporter.json` — MCP 服务配置

配置关联的 MCP 服务（WeChat-Strong-MCP 等），由用户手动维护。

## 可用 MCP 工具(在skills/WeChat_Auto_Reply_Skill使用mcporter命令)

### WeChat-Strong-MCP（读取消息）

| 工具 | 用途 |
|------|------|
| `get_recent_sessions(limit)` | 获取最近会话列表 |
| `get_chat_history(chat_name, limit, offset, start_time, end_time)` | 读取指定联系人聊天记录 |
| `search_messages(keyword, chat_name, ...)` | 跨聊天搜索消息 |
| `get_contacts(query, limit)` | 搜索联系人 |
| `get_new_messages()` | 获取上次调用后的新消息 |

### WeChat-MCP-Server（发送消息）

| 工具 | 用途 |
|------|------|
| `send_wechat_message(contact_name, message)` | 立即发送消息 |
| `schedule_wechat_message(contact_name, message, delay_seconds)` | 延时发送消息 |

## 注意事项

- **发送消息时程序会自动操控鼠标**（约 1~2 秒），此期间勿手动移动鼠标，否则发送失败
- 回复速度主要取决于大模型 API 响应速度，鼠标自动化本身约耗时 1~2 秒
- `poll_interval` 和 `monitor_receive_cooldown_sec` 修改后需重启生效

## 故障排查

| 现象 | 原因 | 解决方案 |
|------|------|----------|
| 收到消息但不回复 | 联系人不在白名单 | 检查 `allow_names_start_with` / `allow_names_end_with` 是否正确设置 |
| 大模型调用失败 | API 配置错误 | 检查 `api_key`、`base_url`、`model` 字段 |
| 消息发送失败 | 微信窗口不可见 | 确保微信窗口未最小化 |
| 数据库读取失败 | 密钥未提取 | 先运行 `WeChat-Strong-MCP/main.py` 完成密钥提取和数据库解密 |
| 回复频率过高 | cooldown 设置太小 | 调大 `monitor_receive_cooldown_sec` |

## 关闭程序

直接终止 `skills/WeChat_Auto_Reply_Skill/monitor.py` 进程即可。
