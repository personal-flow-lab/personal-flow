#!/usr/bin/env python3
"""Personal Flow data connector for Codex (no AI API required)."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


DB_PATH = Path(__file__).resolve().parent / "personal-flow.db"


def rows(limit: int = 10) -> list[dict[str, object]]:
    if not DB_PATH.exists():
        return []
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        result = connection.execute(
            "SELECT id, url, title, summary, note, created_at FROM items ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 30),),
        ).fetchall()
        return [dict(row) for row in result]
    finally:
        connection.close()


def article(item_id: int) -> dict[str, object] | None:
    if not DB_PATH.exists():
        return None
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT id, url, title, article, summary, note, created_at FROM items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


TOOLS = [
    {
        "name": "personal_flow_recent",
        "description": "Personal Flowに最近保存した記事・動画を取得する。要約やnote案の材料を探すときに使う。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "取得件数（最大30）"}},
        },
    },
    {
        "name": "personal_flow_read",
        "description": "Personal Flowに保存した1件の本文とメモを読む。本文の要約や検討に使う。",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "保存情報の番号"}},
            "required": ["id"],
        },
    },
]


def result(data: object) -> dict[str, object]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


def handle(message: dict[str, object]) -> dict[str, object] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        payload = {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "personal-flow", "version": "1.0.0"},
        }
    elif method == "tools/list":
        payload = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "personal_flow_recent":
            payload = result(rows(int(arguments.get("limit", 10))))
        elif name == "personal_flow_read":
            item = article(int(arguments["id"]))
            payload = result(item) if item else {"content": [{"type": "text", "text": "指定された保存情報はありません。"}], "isError": True}
        else:
            payload = {"content": [{"type": "text", "text": "対応していない操作です。"}], "isError": True}
    else:
        payload = {"error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


for line in sys.stdin:
    try:
        request = json.loads(line)
        response = handle(request)
        if response:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": str(error)}}, ensure_ascii=False), flush=True)
