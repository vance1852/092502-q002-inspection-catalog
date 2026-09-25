"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .checklist_service import ChecklistService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    checklists = ChecklistService(service.database, service.clock)
    query = parse_qs(parsed.query)

    def param(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------------------
        # 专属巡查清单：模板与版本生命周期
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/checklist-templates":
            receipt = checklists.create_template(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions":
            receipt = checklists.create_template_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions/update":
            receipt = checklists.update_template_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions/submit":
            receipt = checklists.submit_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions/review":
            receipt = checklists.review_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions/publish":
            receipt = checklists.publish_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/checklist-versions/revoke":
            receipt = checklists.revoke_version(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/checklist-templates":
            return 200, {"items": checklists.list_templates()}
        if method == "GET" and parsed.path == "/checklist-versions":
            template_id = param("template_id", "")
            if not template_id:
                raise ValidationError("template_id 不能为空")
            return 200, {"items": [_version_dict(v) for v in checklists.list_versions(template_id)]}
        if method == "GET" and parsed.path == "/checklist-version":
            version_id = param("version_id", "")
            if not version_id:
                raise ValidationError("version_id 不能为空")
            return 200, _version_dict(checklists.get_version(version_id))

        # ------------------------------------------------------------
        # 企业覆盖层
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/overrides":
            receipt = checklists.add_override(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/overrides/revoke":
            receipt = checklists.revoke_override(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/overrides":
            site_id = param("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [_override_dict(o) for o in checklists.list_overrides(site_id)]}

        # ------------------------------------------------------------
        # 每日任务（生成即冻结）与状态流转
        # ------------------------------------------------------------
        if method == "POST" and parsed.path == "/daily-tasks":
            receipt = checklists.generate_daily_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/tasks/start":
            return 200, checklists.start_task(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/tasks/complete":
            return 200, checklists.complete_task(actor_id=actor_id, **body)
        if method == "POST" and parsed.path == "/tasks/cancel":
            receipt = checklists.cancel_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/tasks":
            site_id = param("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, {"items": [checklists.task_dict(t) for t in checklists.list_tasks(site_id)]}
        if method == "GET" and parsed.path == "/task":
            task_id = param("task_id", "")
            if not task_id:
                raise ValidationError("task_id 不能为空")
            return 200, checklists.task_dict(checklists.get_task(task_id))

        # ------------------------------------------------------------
        # 查询：当前清单、未来变更
        # ------------------------------------------------------------
        if method == "GET" and parsed.path == "/checklist/current":
            site_id = param("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, checklists.current_checklist(site_id, param("effective_at"))
        if method == "GET" and parsed.path == "/checklist/future":
            site_id = param("site_id", "")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, checklists.future_changes(site_id, param("within_to"))

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _version_dict(version) -> dict[str, Any]:
    """把模板版本序列化为接口响应。"""

    return {
        "version_id": version.version_id,
        "template_id": version.template_id,
        "version_no": version.version_no,
        "status": version.status,
        "applicability": {
            "process_profiles": sorted(version.applicability.process_profiles),
            "enterprise_tags": sorted(version.applicability.enterprise_tags),
        },
        "items": [item.__dict__ for item in version.items],
        "content_hash": version.content_hash,
        "drafted_by": version.drafted_by,
        "drafted_at": version.drafted_at,
        "submitted_by": version.submitted_by,
        "submitted_at": version.submitted_at,
        "reviewed_by": version.reviewed_by,
        "reviewed_at": version.reviewed_at,
        "review_result": version.review_result,
        "review_reason": version.review_reason,
        "published_by": version.published_by,
        "published_at": version.published_at,
        "effective_from": version.effective_from,
        "effective_to": version.effective_to,
        "revoked_by": version.revoked_by,
        "revoked_at": version.revoked_at,
        "revoke_reason": version.revoke_reason,
    }


def _override_dict(override) -> dict[str, Any]:
    """把企业覆盖序列化为接口响应。"""

    return {
        "override_id": override.override_id,
        "site_id": override.site_id,
        "template_id": override.template_id,
        "kind": override.kind,
        "item_code": override.item_code,
        "content": override.content.__dict__ if override.content else None,
        "reason": override.reason,
        "valid_from": override.valid_from,
        "valid_to": override.valid_to,
        "status": override.status,
        "created_by": override.created_by,
        "created_at": override.created_at,
        "revoked_by": override.revoked_by,
        "revoked_at": override.revoked_at,
        "revoke_reason": override.revoke_reason,
    }


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
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
