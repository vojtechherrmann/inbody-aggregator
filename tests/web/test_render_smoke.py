import pipeline
from conftest import post
from helpers import FakeClient, csv_export, jpeg, wire


def test_render_everything(as_user):
    a = pipeline.admit("vojta", "x.csv", csv_export([("20260312061532", 190.0)]))
    pipeline.process_upload(a)
    b = pipeline.admit("vojta", "y.jpg", jpeg(tag=b"y"))
    pipeline.process_upload(b, FakeClient(wire(weight=80, bfm=16, pbf=30)))
    c = pipeline.admit("vojta", "z.jpg", jpeg(tag=b"z"))
    pipeline.process_upload(c, FakeClient(wire(when="2026-03-12T16:00")))
    admin = as_user("vojta-admin")
    vojta = as_user("vojta")
    post(vojta, "/sharing/grant", data={"username": "bara"})
    for client, paths in (
        (
            vojta,
            [
                "/history",
                "/sharing",
                f"/uploads/{a}",
                f"/uploads/{b}",
                f"/uploads/{c}",
                f"/uploads/{a}/live",
                "/history/uploads",
            ],
        ),
        (admin, ["/admin", "/admin/review", f"/admin/review/{b}", "/history?subject=vojta"]),
        (as_user("bara"), ["/history?subject=vojta", "/sharing"]),
    ):
        for p in paths:
            r = client.get(p)
            assert r.status_code == 200, (p, r.status_code, r.text[-300:])
    for lang in ("en", "cs"):
        assert vojta.get(f"/uploads/{c}", headers={"Accept-Language": lang}).status_code == 200
