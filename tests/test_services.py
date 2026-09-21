"""服务层集成测试：临时 SQLite + 真实服务调用（注入/撤销/对比/收件箱）。"""
from __future__ import annotations

import os
import tempfile
import unittest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["SANDBOX_DB"] = _tmp.name

from app import services  # noqa: E402


def a(device="D1", batch="B1", level="warning", t="2026-01-01T10:00:00+00:00", **kw):
    base = dict(device_id=device, batch_id=batch, level=level,
                alert_type="TEMP", message="m", alert_time=t)
    base.update(kw)
    return base


class TestServiceFlow(unittest.TestCase):
    def setUp(self):
        # 每个用例干净开始
        with services.transaction() as conn:
            for table in ("receiver_events", "decisions", "alerts", "rules",
                          "policy_versions"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                         "('alerts','rules','receiver_events','policy_versions')")
        services.set_setting("auto_forward", "1")

    def test_inject_evaluates_and_forwards(self):
        services.create_rule(dict(
            name="冷却", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="warning", time_window="",
            cooldown_sec=600, cooldown_scope="device_batch", note=""))
        r1 = services.inject_alert(a(t="2026-01-01T10:00:00+00:00"))
        self.assertEqual(r1["decision"]["verdict"], "pass")
        self.assertTrue(r1["forwarded"])
        r2 = services.inject_alert(a(t="2026-01-01T10:05:00+00:00"))
        self.assertEqual(r2["decision"]["verdict"], "suppress")
        self.assertFalse(r2["forwarded"])
        inbox = services.list_inbox()
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["source"], "forward")
        self.assertEqual(inbox[0]["payload"]["sequence"], 1)

    def test_pure_suppression_never_forwards(self):
        services.create_rule(dict(
            name="静默", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="", time_window="",
            cooldown_sec=0, cooldown_scope="device_batch", note=""))
        r = services.inject_alert(a())
        self.assertEqual(r["decision"]["verdict"], "suppress")
        self.assertFalse(r["forwarded"])
        self.assertEqual(len(services.list_inbox()), 0)

    def test_undo_removes_alert_and_inbox_event_and_recomputes(self):
        services.create_rule(dict(
            name="冷却", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="warning", time_window="",
            cooldown_sec=600, cooldown_scope="device_batch", note=""))
        services.inject_alert(a(t="2026-01-01T10:00:00+00:00"))
        services.inject_alert(a(t="2026-01-01T10:05:00+00:00"))  # 被抑制
        self.assertEqual(len(services.list_alerts()), 2)
        undone = services.undo_last_injection()
        self.assertEqual(undone["removed_sequence"], 2)
        # 再撤销第一条（它转发过）：收件箱应一并清空
        services.undo_last_injection()
        self.assertEqual(len(services.list_alerts()), 0)
        self.assertEqual(len(services.list_inbox()), 0)
        with self.assertRaises(LookupError):
            services.undo_last_injection()

    def test_import_collects_bad_rows(self):
        r = services.import_alerts([
            {"level": "warning"},  # 缺设备
            a(t="2026-01-01T10:00:00+00:00"),
        ])
        self.assertEqual(r["imported"], 1)
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(r["errors"][0]["index"], 0)

    def test_version_compare_detects_change(self):
        services.create_rule(dict(
            name="全部抑制", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="", time_window="",
            cooldown_sec=0, cooldown_scope="device_batch", note=""))
        services.import_alerts([a(t="2026-01-01T10:00:00+00:00"),
                                a(level="critical", t="2026-01-01T10:01:00+00:00")])
        v = services.create_version("v1")
        # 改成只抑制 warning：critical 由抑制变默认放行
        services.update_rule(1, dict(
            name="全部抑制", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="warning", time_window="",
            cooldown_sec=0, cooldown_scope="device_batch", note=""))
        cmp = services.compare_with_version(v["id"])
        self.assertEqual(cmp["summary"]["before_suppress"], 2)
        self.assertEqual(cmp["summary"]["after_suppress"], 1)
        self.assertEqual(cmp["summary"]["changed"], 1)
        changed_rows = [r for r in cmp["rows"] if r["changed"]]
        self.assertEqual(len(changed_rows), 1)
        self.assertEqual(changed_rows[0]["level"], "critical")
        self.assertEqual(changed_rows[0]["before"]["verdict"], "suppress")
        self.assertEqual(changed_rows[0]["after"]["verdict"], "pass")

    def test_rule_crud_toggle_and_recompute(self):
        rid = services.create_rule(dict(
            name="r", enabled=True, priority=10, action="suppress",
            devices="", batches="", levels="", time_window="",
            cooldown_sec=0, cooldown_scope="device_batch", note=""))["id"]
        services.inject_alert(a())
        self.assertEqual(services.list_decisions()[0]["verdict"], "suppress")
        services.update_rule(rid, {"enabled": False})
        self.assertEqual(int(services.list_rules()[0]["enabled"]), 0)
        self.assertEqual(services.list_decisions()[0]["verdict"], "pass")
        services.delete_rule(rid)
        self.assertEqual(services.list_rules(), [])

    def test_webhook_endpoint_service(self):
        ev = services.receive_webhook({"hello": "world"})
        self.assertEqual(ev["source"], "webhook")
        self.assertEqual(ev["echo"], {"hello": "world"})
        self.assertEqual(services.overview()["inbox_total"], 1)

    def test_forward_pending_dedup(self):
        services.import_alerts([a(t="2026-01-01T10:00:00+00:00")], auto_forward=True)
        n1 = services.forward_all_pending()
        self.assertEqual(n1, [])  # 已投递不重复
        self.assertEqual(len(services.list_inbox()), 1)


if __name__ == "__main__":
    unittest.main()
