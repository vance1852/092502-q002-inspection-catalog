"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .checklist_service import ChecklistService
from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database


def _receipt_status(receipt) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.replayed else 201, receipt.__dict__


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          checklist: ChecklistService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = dict(body or {})
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    segments = [s for s in parsed.path.split("/") if s]
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            return _receipt_status(service.register_organization(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/actors":
            return _receipt_status(service.register_actor(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/sites":
            return _receipt_status(service.register_site(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/domain-records":
            return _receipt_status(service.record_domain_data(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        if checklist is None:
            return 404, {"error": "route_not_found", "message": "接口不存在"}

        # ------------------------------------------------------------ 清单模板
        if method == "POST" and parsed.path == "/checklist-templates":
            return _receipt_status(checklist.create_template(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/checklist-templates":
            organization_id = query.get("organization_id", [""])[0]
            if not organization_id:
                raise ValidationError("organization_id 不能为空")
            return 200, checklist.get_template(organization_id)

        # ------------------------------------------------------------ 清单版本
        if method == "POST" and parsed.path == "/checklist-versions":
            return _receipt_status(checklist.draft_version(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/checklist-versions":
            template_id = query.get("template_id", [""])[0]
            if not template_id:
                raise ValidationError("template_id 不能为空")
            return 200, {"items": checklist.list_versions(template_id)}
        if len(segments) == 3 and segments[0] == "checklist-versions":
            version_id = segments[1]
            action = segments[2]
            if method == "GET" and action == "detail":
                return 200, checklist.get_version(version_id)
            if method == "POST" and action == "update":
                body["version_id"] = version_id
                return _receipt_status(checklist.update_draft(actor_id=actor_id, **body))
            if method == "POST" and action == "submit":
                body["version_id"] = version_id
                return _receipt_status(checklist.submit_version(actor_id=actor_id, **body))
            if method == "POST" and action == "review":
                body["version_id"] = version_id
                return _receipt_status(checklist.review_version(actor_id=actor_id, **body))
            if method == "POST" and action == "publish":
                body["version_id"] = version_id
                return _receipt_status(checklist.publish_version(actor_id=actor_id, **body))
            if method == "POST" and action == "rollback":
                body["version_id"] = version_id
                return _receipt_status(checklist.rollback_version(actor_id=actor_id, **body))

        # ------------------------------------------------------------ 企业覆盖层
        if method == "POST" and parsed.path == "/checklist-overlays":
            return _receipt_status(checklist.create_overlay(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/checklist-overlays":
            template_id = query.get("template_id", [""])[0]
            site_id = query.get("site_id", [""])[0]
            if not template_id or not site_id:
                raise ValidationError("template_id 与 site_id 不能为空")
            return 200, {"items": checklist.list_overlays(template_id, site_id)}
        if (method == "POST" and len(segments) == 3
                and segments[0] == "checklist-overlays" and segments[2] == "revoke"):
            body["overlay_id"] = segments[1]
            return _receipt_status(checklist.revoke_overlay(actor_id=actor_id, **body))

        # ------------------------------------------------------------ 清单查询
        if method == "GET" and parsed.path == "/checklist/current":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            on_date = query.get("on_date", [None])[0]
            return 200, checklist.get_current_checklist(site_id, on_date)
        if method == "GET" and parsed.path == "/checklist/future-changes":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            from_date = query.get("from_date", [None])[0]
            return 200, checklist.get_future_changes(site_id, from_date)

        # ------------------------------------------------------------ 巡查任务
        if method == "POST" and parsed.path == "/inspection-tasks":
            return _receipt_status(checklist.create_daily_task(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/inspection-tasks":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            task_date = query.get("task_date", [None])[0]
            return 200, {"items": checklist.list_tasks(site_id, task_date)}
        if len(segments) >= 2 and segments[0] == "inspection-tasks":
            task_id = segments[1]
            if len(segments) == 2 and method == "GET":
                return 200, checklist.get_task(task_id)
            if len(segments) == 3 and method == "POST" and segments[2] == "start":
                return 200, checklist.start_task(actor_id=actor_id, task_id=task_id)
            if len(segments) == 3 and method == "POST" and segments[2] == "complete":
                return 200, checklist.complete_task(actor_id=actor_id, task_id=task_id)
            if len(segments) == 3 and method == "GET" and segments[2] == "rule-sources":
                return 200, checklist.get_task_rule_sources(task_id)

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    checklist: ChecklistService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                checklist=self.checklist)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动环保业务基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.checklist = ChecklistService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
