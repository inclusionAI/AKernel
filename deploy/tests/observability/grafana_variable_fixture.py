"""Synthetic metric source and Tempo proxy for real Grafana interpolation checks."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/metrics":
            data = "".join(
                'adx_coordinator_node_capacity_cpu_millis{cluster="' + cluster
                + '",adx_env="shared"} 1000\n'
                for cluster in ("cluster.a", "clusterxa")
            ).encode()
            self.reply(200, data, "text/plain; version=0.0.4")
            return
        if self.path.startswith("/tempo/"):
            url = os.environ["TEMPO_URL"] + self.path.removeprefix("/tempo")
            try:
                with urlopen(Request(url), timeout=10) as reply:
                    data, status = reply.read(), reply.status
                    content_type = reply.headers.get("Content-Type", "application/json")
            except HTTPError as error:
                data, status = error.read(), error.code
                content_type = error.headers.get("Content-Type", "text/plain")
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            if query:
                try:
                    ids = [row["traceID"].zfill(32) for row in json.loads(data).get("traces", [])]
                except (ValueError, KeyError):
                    ids = []
                with Path("/state/requests.jsonl").open("a") as stream:
                    stream.write(json.dumps({"query": query, "status": status, "ids": ids}) + "\n")
            self.reply(status, data, content_type)
            return
        self.reply(404, b"{}", "application/json")

    def reply(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
