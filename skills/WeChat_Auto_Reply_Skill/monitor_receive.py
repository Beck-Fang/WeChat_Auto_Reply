#!/usr/bin/env python3
"""
在指定工作目录下调用 mcporter 的 get_chat_history（暂不处理返回内容）。

用法:
  python monitor_receive.py "聊天名称或备注"
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import io
from datetime import date
import re
import os
import json
from openai import OpenAI


WORKSPACE = os.path.dirname(os.path.abspath(__file__))

current_dir = os.path.dirname(os.path.abspath(__file__))
script_dir = os.path.join(current_dir, "config")
config = {}
try:
    # 读取your_config.json文件
    with open(os.path.join(script_dir, "your_config.json"), "r", encoding="utf-8") as f:
        config = json.load(f)
    f.close()
except Exception as e:
    print(f"[ERROR] 读取your_config.json文件失败: {e}")
    sys.exit(1)


# 避免 Windows 控制台编码导致打印失败（UnicodeEncodeError）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 可选：OpenClaw 本地 API 回调地址（不设置则跳过调用）
# 例如（示例）：setx OPENCLAW_API_URL "http://127.0.0.1:12345/api/wechat"
OPENCLAW_API_URL = os.environ.get("OPENCLAW_API_URL", "").strip()


def first_day_of_previous_month(today: date) -> date:
    """当前日期的「上一个自然月」的 1 号；若当前为 1 月，则为上一年 12 月 1 日。"""
    if today.month == 1:
        return date(today.year - 1, 12, 1)
    return date(today.year, today.month - 1, 1)


def escape_double_quotes_for_cmd(s: str) -> str:
    """Windows cmd 下双引号内嵌双引号用 "" 转义。"""
    return s.replace('"', '""')


def decode_bytes(b: bytes) -> str:
    """尽量把 mcporter 输出 bytes 解码成可读文本（Windows 编码不稳定）。"""
    if not b:
        return ""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", errors="backslashreplace")


def parse_mcporter_history_output(text: str) -> tuple[str, str]:
    """按注释规则解析 mcporter 输出。

    - 第一行：取第一个空格之前的片段 => contect_name
    - 最后一行：若以 `"[... ] me: "` 开头，则跳过该最后一行
    - 否则/默认：content 从第4行开始（行号4 => 0-based index=3）
    """
    if not text:
        return "", ""

    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "", ""

    # 第一行可能为空，取第一个非空行更稳
    first_non_empty = next((ln.strip() for ln in lines if ln.strip()), "")
    contect_name = first_non_empty.split(" ", 1)[0] if first_non_empty else ""

    last_line = lines[-1].strip()
    last_is_me = bool(re.match(r"^\[.*\]\s*me:\s*", last_line))

    content_lines = lines[3:] if len(lines) > 3 else []
    content_lines = "\n".join(content_lines).strip()
    
    always_reply = config.get("always_reply", False)
 
    if last_is_me and not always_reply:
        content_lines=None

    return contect_name, content_lines

def call_openclaw_api(contect_name: str, content: str) -> None:
        # 读取配置文件

    try:
        api_key = config.get("api_key", "")
        base_url = config.get("base_url", "")
        model = config.get("model", "")
        if model == "" or api_key == "" or base_url == "":
            print("your_config.json文件配置错误")
            return
        # 读取prompt文件
        prompt=""

        try:
            with open(os.path.join(script_dir, f"{contect_name}.txt"), "r", encoding="utf-8") as f:
                prompt = f.read()
        except FileNotFoundError:
            pass
        if prompt.strip() == "":
            prompt = f'''你是一个微信自动回复助手，我会给你发送聊天记录，你根据这些记录（一个是对方，一个是我（me）），直接给我应该回复什么内容，不要回复废话'''
        # 开始调用API    
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )        
        completion = client.chat.completions.create(
        model=model, 
            messages=[
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': f'{content}'}
            ],
            stream=True,
            response_format={"type": "text"},
        )
        reply=""
        for chunk in completion:
            if chunk.choices[0].delta.content:
                # print(chunk.choices[0].delta.content, end="", flush=True)
                reply+=chunk.choices[0].delta.content
        print()
        GREEN = "\x1b[32m"
        RESET = "\x1b[0m"
        YELLOW = "\x1b[33m"
        print(f"{GREEN}[reply]{RESET} {YELLOW}[{contect_name}]{RESET} <-- {GREEN}{reply}{RESET}\n",flush=True)   
        if not reply == "[[[不回复]]]":
            subprocess.run(
                f"mcporter call WeChat-OnlySendMessage_Once.send_wechat_message contact_name=\"{contect_name}\" message=\"{reply}\"",
                cwd=WORKSPACE,
                shell=True,
                check=False,
                capture_output=True,
            )
    except Exception as e:
        print(f"错误信息：{e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 WeChat-Channel-MCP.get_chat_history")
    parser.add_argument(
        "chat_name",
        help="传给 get_chat_history 的 chat_name（可与备注/群名一致）",
    )
    args = parser.parse_args()

    today = date.today()
    start_time = first_day_of_previous_month(today).strftime("%Y-%m-%d")
    end_time = today.strftime("%Y-%m-%d")

    inner = escape_double_quotes_for_cmd(args.chat_name)

    limit = config.get("limit", 50)
    offset = config.get("offset", 0)
    if limit <= 0:
        limit = 50
    if offset < 0:
        offset = 0
    start_time = config.get("start_time", start_time)
    end_time = config.get("end_time", end_time)

    cmd = (
        f'mcporter call WeChat-OnlySearchData_Once.get_chat_history '
        f'chat_name="{inner}" limit={limit} offset={offset} '
        f"start_time={start_time} end_time={end_time}"
    )

    # 等价于先 cd 到工作目录再执行：
    # - 仍然“获取” res.stdout / res.stderr 供后续逻辑使用
    # - 默认“不自动打印”到终端（避免 poll/子进程刷屏）
    res = subprocess.run(
        cmd,
        cwd=WORKSPACE,
        shell=True,
        check=False,
        capture_output=True,
    )

    stdout_text = decode_bytes(res.stdout or b"")
    stderr_text = decode_bytes(res.stderr or b"")

    if res.returncode != 0:
        print(f"[WARN] mcporter 返回码: {res.returncode}", file=sys.stderr)
        if stderr_text.strip():
            print(stderr_text, file=sys.stderr, end="")
        return

    # 解析 result：提取 contect_name / content（不直接打印 mcporter 原始输出）
    contect_name, content = parse_mcporter_history_output(stdout_text)
    # print(f"***{contect_name}***\n",flush=True)
    flag=False
    for allow_name in config.get("allow_names_start_with", []):
        if contect_name.startswith(allow_name):
            flag=True
            break
    for allow_name in config.get("allow_names_end_with", []):
        if contect_name.endswith(allow_name):
            flag=True
            break
    if not flag:
        return
    if content:
        call_openclaw_api(contect_name, content)    

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
