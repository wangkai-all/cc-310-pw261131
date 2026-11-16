"""HTTP 服务:分析页静态资源 + JSON API(仅标准库)。"""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import backtest
from .db import connect, init_db, is_seeded
from .rules import RULES

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(BASE_DIR, "web")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def _json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    conn = None  # 每进程一个连接(演示规模足够)

    def log_message(self, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _handle(self, fn):
        try:
            fn()
        except ValueError as e:
            _json(self, {"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001 - 演示服务,错误直接透出
            _json(self, {"error": f"{type(e).__name__}: {e}"}, 500)

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, q = parsed.path, parse_qs(parsed.query)
        if path in STATIC:
            return self._static(path)
        routes = {
            "/api/meta": lambda: _json(self, self._meta()),
            "/api/runs": lambda: _json(self, {"runs": backtest.list_runs(self.conn)}),
            "/api/run": lambda: self._get_run(q),
            "/api/reports": lambda: _json(self, {"reports": backtest.list_reports(self.conn)}),
            "/api/report": lambda: self._get_report(q),
            "/api/customer": lambda: self._customer(q),
        }
        if path in routes:
            return self._handle(routes[path])
        _json(self, {"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        routes = {
            "/api/backtest": self._run_backtest,
            "/api/recompute": self._recompute,
            "/api/reports": self._save_report,
        }
        if path in routes:
            return self._handle(routes[path])
        _json(self, {"error": "not found"}, 404)

    # -- endpoints --------------------------------------------------------
    def _static(self, path):
        fname, ctype = STATIC[path]
        full = os.path.join(WEB_DIR, fname)
        if not os.path.exists(full):
            return _json(self, {"error": "static missing"}, 404)
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _meta(self):
        return {
            "today": backtest.TODAY,
            "defaults": backtest.DEFAULTS,
            "method_labels": backtest.METHOD_LABELS,
            "rules": [
                {"version": v, "label": m["label"], "desc": m["desc"],
                 "effective_from": m["effective_from"]}
                for v, m in RULES.items()
            ],
        }

    def _run_backtest(self):
        config = self._body()
        results = backtest.run_backtest(self.conn, config)
        meta = backtest.save_run(self.conn, results["config"], results)
        _json(self, {"meta": meta, "results": results})

    def _get_run(self, q):
        run = backtest.get_run(self.conn, int(q["id"][0]))
        if not run:
            return _json(self, {"error": "run 不存在"}, 404)
        _json(self, run)

    def _recompute(self):
        run_id = int(self._body()["run_id"])
        _json(self, backtest.recompute(self.conn, run_id))

    def _save_report(self):
        body = self._body()
        meta = backtest.save_report(self.conn, body.get("name") or "未命名报告",
                                    int(body["run_id"]), body.get("notes", ""))
        _json(self, meta)

    def _get_report(self, q):
        rep = backtest.get_report(self.conn, int(q["id"][0]))
        if not rep:
            return _json(self, {"error": "报告不存在"}, 404)
        _json(self, rep)

    def _customer(self, q):
        data = backtest.customer_timeline(
            self.conn, q["id"][0], q["decision_date"][0], q.get("rule_version", ["v2"])[0])
        if not data:
            return _json(self, {"error": "客户不存在"}, 404)
        _json(self, data)


def make_server(host="127.0.0.1", port=8000):
    conn = connect()
    init_db(conn)
    if not is_seeded(conn):
        from .seed import seed
        print("首次运行:生成虚构样本数据 ...")
        seed(conn)
    Handler.conn = conn
    return HTTPServer((host, port), Handler)
