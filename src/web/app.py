"""FastAPI 应用程序 - 提供面板 REST API、SSE 实时日志流与静态页面分发。"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.ai.models import EndpointConfig
from src.ai.service import AIService
from src.config import Config, DEFAULTS
from src.web.manager import ProcessManager, sanitize_text


logger = logging.getLogger(__name__)

# 获取数据目录
DATA_DIR = os.getenv("DATA_DIR", "data")
manager = ProcessManager(DATA_DIR)

app = FastAPI(
    title="Rainclass Assistant Web Panel",
    description="雨课堂自动化助手管理面板",
    version="1.0.0",
    docs_url=None,  # 生产环境隐藏文档以防探测
    redoc_url=None,
)

# 敏感字段列表
SENSITIVE_CONFIG_KEYS = {
    "doubao_api_key",
    "gemini_api_key",
    "custom_ai_api_key",
    "xxtui_api_key",
}


def mask_secret(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    if len(value) <= 6:
        return "******"
    return f"{value[:3]}******{value[-3:]}"


# ==================== 请求/响应模型 ====================

class ControlRequest(BaseModel):
    action: str  # start, stop, restart
    mode: Optional[str] = "observe"  # observe, auto


class ConfigUpdateRequest(BaseModel):
    settings: dict[str, Any]
    clear_keys: Optional[list[str]] = None


# ==================== API 路由 ====================

@app.get("/api/health")
async def health_check():
    """容器内部与外部健康检查端点。"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "worker_running": manager.is_running(),
    }


@app.get("/api/status")
async def get_system_status():
    """获取整体运行状态机视图。"""
    return manager.get_status()


@app.post("/api/control")
async def control_worker(payload: ControlRequest):
    """控制 Worker 启停与模式切换。"""
    action = payload.action.lower()
    mode = (payload.mode or "observe").lower()

    if action == "start":
        success, msg = manager.start(mode)
    elif action == "stop":
        success, msg = manager.stop()
    elif action == "restart":
        success, msg = manager.restart()
    else:
        raise HTTPException(status_code=400, detail=f"未知操作: {action}")

    return {
        "success": success,
        "message": msg,
        "status": manager.get_status(),
    }


@app.get("/api/config")
async def get_configuration():
    """获取当前配置（敏感密钥已脱敏掩码）。"""
    cfg = Config(manager.config_path).to_dict()
    sanitized = {}
    key_configured_flags = {}

    for k, v in cfg.items():
        if k in SENSITIVE_CONFIG_KEYS:
            has_val = bool(v and str(v).strip())
            key_configured_flags[f"{k}_configured"] = has_val
            sanitized[k] = mask_secret(v) if has_val else ""
        else:
            sanitized[k] = v

    return {
        "config": sanitized,
        "flags": key_configured_flags,
        "defaults": {k: v for k, v in DEFAULTS.items() if k not in SENSITIVE_CONFIG_KEYS},
    }


@app.post("/api/config")
async def update_configuration(payload: ConfigUpdateRequest):
    """更新配置；保留未修改的敏感密钥，支持显式清空。"""
    cfg = Config(manager.config_path)
    existing = cfg.to_dict()
    new_settings = dict(payload.settings)
    clear_keys = set(payload.clear_keys or [])

    # 处理敏感字段逻辑
    for k in SENSITIVE_CONFIG_KEYS:
        if k in clear_keys:
            new_settings[k] = ""
        elif k in new_settings:
            val = str(new_settings[k]).strip()
            # 若传入空串、掩码串或未提供，保留磁盘现有值
            if not val or "******" in val:
                new_settings[k] = existing.get(k, "")

    errors = cfg.save(new_settings)
    if errors:
        return JSONResponse(
            status_code=400,
            content={"success": False, "errors": errors, "message": "配置校验未通过"},
        )

    manager.log_buffer.append("Web 面板更新了业务配置", level="INFO")
    return {
        "success": True,
        "message": "配置已保存至磁盘（若 Worker 正在运行，请重启 Worker 以生效）",
        "status": manager.get_status(),
    }


@app.post("/api/session")
async def upload_session(request: Request, file: Optional[UploadFile] = File(None)):
    """上传并校验登录会话文件 browser_state.json。"""
    session_data = None
    if file:
        content = await file.read()
        try:
            session_data = json.loads(content.decode("utf-8"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"上传的文件不是合法的 JSON: {e}")
    else:
        try:
            body = await request.json()
            session_data = body.get("session") or body
        except Exception:
            raise HTTPException(status_code=400, detail="请上传文件或提供 JSON 会话数据")

    if not isinstance(session_data, dict):
        raise HTTPException(status_code=400, detail="会话数据格式错误，顶层必须是 JSON 对象")

    success, msg = manager.import_session(session_data)
    if not success:
        return JSONResponse(status_code=400, content={"success": False, "message": msg})

    return {
        "success": True,
        "message": msg,
        "session": manager.get_session_info(),
    }


@app.get("/api/records")
async def get_quiz_records(limit: int = 50, offset: int = 0):
    """获取历史答题记录列表与聚合耗时统计。"""
    return manager.get_records(limit=min(limit, 200), offset=max(0, offset))


@app.get("/api/logs")
async def get_logs(limit: int = 200, level: Optional[str] = None, keyword: Optional[str] = None):
    """获取环形队列历史日志行。"""
    lines = manager.log_buffer.get_lines(limit=min(limit, 1000), level=level, keyword=keyword)
    return {"lines": lines}


@app.get("/api/logs/stream")
async def stream_logs():
    """SSE (Server-Sent Events) 实时脱敏日志推送流。"""
    async def event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)

        def sync_listener(entry: dict[str, Any]):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, entry)
            except Exception:
                pass  # 满载丢弃，防止慢客户端阻塞

        unsubscribe = manager.log_buffer.subscribe(sync_listener)
        try:
            # 建立连接先送出首包确认
            yield f"event: connected\ndata: {json.dumps({'time': datetime.now().isoformat()})}\n\n"
            while True:
                entry = await queue.get()
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ai/test")
async def test_ai_connectivity(request: Request):
    """单次测试当前配置的模型联通性与返回格式。"""
    cfg = Config(manager.config_path)
    service = AIService(cfg)

    # 构造一次快速单选测试提问（耗费极低 token 预算）
    start_time = time.monotonic()
    try:
        ans = service.answer_text_question(
            "测试题目：下列协议中运行在传输层的是？ A. TCP B. IP C. HTTP D. ARP",
            test_mode=True,
        )
        duration_ms = round((time.monotonic() - start_time) * 1000, 1)

        # 校验返回格式是否合法
        is_valid_format = False
        parsed_type = ""
        parsed_answer = ""
        try:
            data = json.loads(ans)
            if isinstance(data, dict) and "type" in data:
                is_valid_format = True
                parsed_type = data.get("type", "")
                parsed_answer = str(data.get("answers", ""))
        except Exception:
            pass

        return {
            "success": True,
            "duration_ms": duration_ms,
            "raw_response": sanitize_text(ans),
            "is_valid_json": is_valid_format,
            "parsed": {
                "type": parsed_type,
                "answers": parsed_answer,
            } if is_valid_format else None,
        }
    except Exception as e:
        duration_ms = round((time.monotonic() - start_time) * 1000, 1)
        return {
            "success": False,
            "duration_ms": duration_ms,
            "error": sanitize_text(str(e)),
        }
    finally:
        service.shutdown()


# ==================== 静态文件分发 ====================

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
