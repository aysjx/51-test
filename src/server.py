#!/usr/bin/env python3
"""招聘证据工作台本地演示服务器（仅标准库，无 API Key）。

提供完整 API：
- GET  /                返回 index.html
- GET  /standards       返回 S1-S5 岗位成功标准
- GET  /demo            返回 DEMO payload
- POST /analyze         分析候选人材料与面试记录
- POST /session/save    保存当前会话草稿
- GET  /history         列出已保存会话
- GET  /history/{id}    读取某候选人最新会话/审核结果
- POST /review/{id}     追加人工审核动作
"""
import argparse
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import analyze, DEMO, ROOT, STANDARDS

# 本地持久化目录
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    filename=ROOT / "logs" / "assistant.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(request_id)s] %(message)s",
)

HTML = (ROOT / "index.html").read_text(encoding="utf-8")

ALLOWED_ORIGINS = {"http://127.0.0.1", "http://localhost"}


def safe_id(cid: str) -> str:
    """把候选人编号转成合法文件名。"""
    return re.sub(r"[^\w\-\.]", "_", cid.strip())[:64] or "UNKNOWN"


def candidate_dir(cid: str) -> Path:
    d = DATA_DIR / safe_id(cid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 重定向到 logging；request_id 由具体 handler 写入
        pass

    def _request_id(self):
        return getattr(self, "_req_id", uuid.uuid4().hex[:12])

    def _set_cors(self):
        origin = self.headers.get("Origin", "")
        if any(origin.startswith(o) for o in ALLOWED_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._set_cors()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self._set_cors()
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self, max_size=300000):
        size = int(self.headers.get("Content-Length", "0"))
        if size > max_size:
            raise ValueError("输入过大（最大 300KB）")
        if size == 0:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        req_id = self._request_id()
        try:
            if self.path == "/":
                raw = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self._set_cors()
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/demo":
                logging.info("request_id=%s method=GET path=%s", req_id, self.path, extra={"request_id": req_id})
                self.send_json(200, DEMO)
            elif self.path == "/standards":
                logging.info("request_id=%s method=GET path=%s", req_id, self.path, extra={"request_id": req_id})
                self.send_json(200, {"standards": STANDARDS})
            elif self.path == "/history":
                logging.info("request_id=%s method=GET path=%s", req_id, self.path, extra={"request_id": req_id})
                items = []
                for d in sorted(DATA_DIR.iterdir()):
                    if d.is_dir():
                        session = read_json(d / "session.json")
                        review = read_json(d / "review.json")
                        if session:
                            items.append({
                                "candidate_id": session.get("candidate_id", d.name),
                                "updated_at": review.get("updated_at") if review else session.get("updated_at"),
                                "has_review": bool(review),
                            })
                self.send_json(200, {"history": items})
            elif self.path.startswith("/history/"):
                cid = self.path[len("/history/"):]
                logging.info("request_id=%s method=GET path=%s candidate=%s", req_id, self.path, cid, extra={"request_id": req_id})
                d = candidate_dir(cid)
                session = read_json(d / "session.json")
                review = read_json(d / "review.json")
                if not session:
                    return self.send_json(404, {"error": "未找到该候选人记录", "request_id": req_id})
                result = read_json(d / "analysis.json")
                self.send_json(200, {"candidate_id": cid, "session": session, "review": review, "analysis": result})
            else:
                self.send_json(404, {"error": "未找到页面", "request_id": req_id})
        except Exception:
            logging.exception("request_id=%s GET failure", req_id, extra={"request_id": req_id})
            self.send_json(500, {"error": "系统执行失败，请查看 logs/assistant.log 并使用人工模板兜底。", "request_id": req_id})

    def do_POST(self):
        req_id = self._request_id()
        start = datetime.now(timezone.utc)
        try:
            if self.path == "/analyze":
                payload = self._read_body()
                cid = str(payload.get("candidate_id") or "UNKNOWN").strip() or "UNKNOWN"
                logging.info(
                    "request_id=%s method=POST path=/analyze candidate=%s material_len=%d records=%d",
                    req_id, cid, len(str(payload.get("candidate_material", ""))),
                    len([r for r in payload.get("interview_records", []) if isinstance(r, dict)]),
                    extra={"request_id": req_id},
                )
                result = analyze(payload, request_id=req_id)
                # 保存分析结果与会话
                d = candidate_dir(cid)
                write_json(d / "analysis.json", result)
                session = {
                    "candidate_id": cid,
                    "candidate_material": payload.get("candidate_material", ""),
                    "interview_records": payload.get("interview_records", []),
                    "focus": payload.get("focus", ""),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                write_json(d / "session.json", session)
                elapsed_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
                logging.info("request_id=%s analyze_ok candidate=%s elapsed_ms=%d", req_id, cid, elapsed_ms, extra={"request_id": req_id})
                self.send_json(200, result)

            elif self.path == "/session/save":
                payload = self._read_body()
                cid = str(payload.get("candidate_id") or "UNKNOWN").strip() or "UNKNOWN"
                logging.info("request_id=%s method=POST path=/session/save candidate=%s", req_id, cid, extra={"request_id": req_id})
                d = candidate_dir(cid)
                session = {
                    "candidate_id": cid,
                    "candidate_material": payload.get("candidate_material", ""),
                    "interview_records": payload.get("interview_records", []),
                    "focus": payload.get("focus", ""),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                write_json(d / "session.json", session)
                self.send_json(200, {"saved": True, "candidate_id": cid, "request_id": req_id})

            elif self.path.startswith("/review/"):
                cid = self.path[len("/review/"):]
                payload = self._read_body()
                logging.info(
                    "request_id=%s method=POST path=/review/%s action=%s",
                    req_id, cid, payload.get("action", ""), extra={"request_id": req_id},
                )
                d = candidate_dir(cid)
                review_path = d / "review.json"
                review = read_json(review_path, {"actions": [], "candidate_id": cid})
                action = {
                    "action": payload.get("action", ""),
                    "item": payload.get("item", ""),
                    "note": payload.get("note", ""),
                    "actor": payload.get("actor", "面试官"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                review["actions"].append(action)
                review["updated_at"] = datetime.now(timezone.utc).isoformat()
                review["candidate_id"] = cid
                write_json(review_path, review)
                self.send_json(200, {"saved": True, "action": action, "request_id": req_id})

            else:
                self.send_json(404, {"error": "未找到接口", "request_id": req_id})

        except (ValueError, json.JSONDecodeError) as e:
            logging.warning("request_id=%s invalid input: %s", req_id, e, extra={"request_id": req_id})
            self.send_json(400, {"error": str(e), "request_id": req_id})
        except Exception:
            logging.exception("request_id=%s POST failure", req_id, extra={"request_id": req_id})
            self.send_json(500, {"error": "系统执行失败，请查看 logs/assistant.log 并使用人工模板兜底。", "request_id": req_id})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    print(f"招聘证据工作台已启动：http://127.0.0.1:{args.port}")
    print(f"数据持久化目录：{DATA_DIR}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
