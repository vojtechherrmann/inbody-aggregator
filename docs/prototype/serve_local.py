#!/usr/bin/env python3
"""Serve the UI prototype with the user's REAL measurements, on 127.0.0.1 only.

    python3 docs/prototype/serve_local.py [port]        # default port 8765

The data is built in memory on every request from the gitignored samples/ folder:
  - samples/sample_csv.csv            parsed with extraction/validate.py parse_csv
  - samples/expected/<sheet>.canonical.json         the normalized result sheets
and run through the product's day rule (docs/upload-pipeline.md section 3: one measurement per
user per day, first come wins, in upload order CSV -> Gym A -> Gym B -> Gym C screenshot).
Nothing is written to disk and nothing is sent anywhere. Assets are served as separate files under
the same strict CSP the real app will use, which also proves charts.js runs without inline code.
"""

from __future__ import annotations

import json
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SAMPLES = ROOT / "samples"
sys.path.insert(0, str(ROOT / "extraction"))
import validate  # noqa: E402  (stdlib only, no network)

HOST = "127.0.0.1"
ASSETS = {
    "/ui.css": "text/css",
    "/charts.css": "text/css",
    "/theme.js": "text/javascript",
    "/i18n.js": "text/javascript",
    "/charts.js": "text/javascript",
    "/app.js": "text/javascript",
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
DROP = {
    "converted_from",
    "warnings",
    "exercise_kcal_30min",
    "parser_version",
    "device_serial",
    "software_version",
    "sheet_template",
    "subject_age_years",
    "subject_height_cm",
    "subject_sex",
}

# Upload order and the gym as the user would type it into the upload form ("" = left empty).
# The CSV export has no gym; the Gym A sheet prints its gym; Gym B's sheet doesn't, so it
# comes from the upload form.
UPLOADS = [
    ("csv", "sample_csv.csv", None),
    ("sheet", "sample_sheet_a.canonical.json", None),
    ("sheet", "sample_sheet_b.canonical.json", "Gym B"),
    ("sheet", "sample_us_app.canonical.json", None),
]


def load_measurements() -> tuple[list[dict], list[str]]:
    accepted: dict[str, dict] = {}
    log: list[str] = []
    for kind, name, form_gym in UPLOADS:
        path = SAMPLES / name if kind == "csv" else SAMPLES / "expected" / name
        if not path.exists():
            log.append(f"missing {path.relative_to(ROOT)}")
            continue
        if kind == "csv":
            rows = validate.parse_csv(path.read_text(encoding="utf-8-sig"))
        else:
            rows = json.loads(path.read_text(encoding="utf-8"))
        # inside one upload: a full measurement beats a history point, then the earliest time
        by_day: dict[str, dict] = {}
        for r in sorted(
            rows, key=lambda r: (r.get("from_history", False), r.get("measured_time") or "")
        ):
            by_day.setdefault(r["measured_date"], r)
        for day, r in sorted(by_day.items()):
            if day in accepted:
                log.append(f"{name}: {day} rejected, day already has a measurement")
                continue
            gym = form_gym or (r.get("source_name") if r.get("origin") != "csv" else None)
            row = {
                k: v
                for k, v in r.items()
                if v is not None and k not in DROP and not k.startswith("z_")
            }
            row.pop("source_name", None)
            row.update({"id": day, "gym": gym})
            accepted[day] = row
            log.append(f"{name}: {day} accepted")
    return [accepted[d] for d in sorted(accepted)], log


def page() -> bytes:
    rows, _ = load_measurements()
    data = {
        "meta": {"mode": "local"},
        "viewer": "vojta",
        "viewerUsername": "vojta",
        "viewerIsAdmin": False,
        "sharing": {"grants": [], "directory": []},
        "people": [{"id": "vojta", "name": "vojta", "relation": "self", "measurements": rows}],
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return (
        (HERE / "index.html")
        .read_text(encoding="utf-8")
        .replace("__INBODY_DATA__", payload)
        .encode("utf-8")
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "inbody-proto"
    sys_version = ""

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        try:
            if path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, page(), "text/html")
            if re.fullmatch(
                r"/[\w.-]+\.woff2", path
            ):  # the self-hosted typefaces, src/web/static/fonts
                font = ROOT / "src/web/static/fonts" / path[1:]
                if font.is_file():
                    return self._send_bytes(font.read_bytes(), "font/woff2")
            if path in ASSETS:
                return self._send(HTTPStatus.OK, (HERE / path[1:]).read_bytes(), ASSETS[path])
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        except Exception as exc:  # show the parse error instead of a blank page
            return self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"error: {exc}".encode(), "text/plain"
            )

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    rows, log = load_measurements()
    print("\n".join(log))
    print(
        f"{len(rows)} measurements; serving http://{HOST}:{port}/ (localhost only, Ctrl+C stops)",
        flush=True,
    )
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
