"""策略引擎纯函数语义测试（python -m unittest discover -s tests）。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import engine  # noqa: E402
from app.normalize import (  # noqa: E402
    canonical_level,
    normalize_levels_csv,
    validate_time_window,
)


def rule(**kw):
    base = dict(id=1, name="r", enabled=1, priority=100, action="suppress",
                devices="", batches="", levels="", time_window="",
                cooldown_sec=0, cooldown_scope="device_batch")
    base.update(kw)
    return base


def alert(seq, device="D1", batch="B1", level="warning", t="2026-01-01T10:00:00+00:00"):
    return dict(id=seq, sequence=seq, device_id=device, batch_id=batch,
                level=level, alert_type="T", message="", alert_time=t)


class TestLevelNormalize(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(canonical_level("WARN"), "warning")
        self.assertEqual(canonical_level("严重"), "critical")
        self.assertEqual(canonical_level("notice"), "info")
        self.assertEqual(canonical_level(1), "critical")
        self.assertEqual(canonical_level(2), "warning")
        self.assertEqual(canonical_level("weird"), "weird")
        out = normalize_levels_csv("critical, WARN, info, critical")
        self.assertEqual(out, "critical,warning,info")

    def test_time_window(self):
        self.assertEqual(validate_time_window("8:5-6:00"), "08:05-06:00")
        self.assertEqual(validate_time_window(""), "")
        with self.assertRaises(ValueError):
            validate_time_window("25:00-06:00")
        with self.assertRaises(ValueError):
            validate_time_window("08:00-08:00")


class TestFieldMatch(unittest.TestCase):
    def test_empty_matches_anything(self):
        ok, _ = engine.match_field("", "anything")
        self.assertTrue(ok)

    def test_exact_and_prefix(self):
        ok, p = engine.match_field("REEFER-*", "REEFER-77")
        self.assertTrue(ok)
        self.assertEqual(p, "REEFER-*")
        ok, _ = engine.match_field("REEFER-*", "VAN-9")
        self.assertFalse(ok)
        ok, _ = engine.match_field("D1,D2", "D2")
        self.assertTrue(ok)
        ok, _ = engine.match_field("D1,D2", "D3")
        self.assertFalse(ok)


class TestDefaultAndPriority(unittest.TestCase):
    def test_no_rules_default_pass(self):
        res = engine.evaluate([], [alert(1)])
        self.assertEqual(res[0]["verdict"], "pass")
        self.assertTrue(res[0]["explanation"]["no_rule"])

    def test_priority_first_hit_wins(self):
        # priority 10 放行白名单优先于 20 抑制（抑制规则无冷却 → 命中即抑制）
        r1 = rule(id=1, name="whitelist", priority=10, action="pass")
        r2 = rule(id=2, name="silence", priority=20, action="suppress")
        res = engine.evaluate([r1, r2], [alert(1)])
        self.assertEqual(res[0]["verdict"], "pass")
        self.assertEqual(res[0]["rule_name"], "whitelist")
        # 交换优先级：抑制在前 → 直接抑制
        r1 = rule(id=1, name="whitelist", priority=30, action="pass")
        r2 = rule(id=2, name="silence", priority=20, action="suppress")
        res = engine.evaluate([r1, r2], [alert(1)])
        self.assertEqual(res[0]["verdict"], "suppress")
        self.assertEqual(res[0]["rule_name"], "silence")

    def test_disabled_rule_ignored(self):
        r = rule(id=1, enabled=0, action="suppress")
        res = engine.evaluate([r], [alert(1)])
        self.assertTrue(res[0]["explanation"]["no_rule"])

    def test_dimension_filtering(self):
        r = rule(id=1, devices="D1", batches="BX", levels="critical")
        res = engine.evaluate([r], [
            alert(1, device="D1", batch="BX", level="critical"),
            alert(2, device="D2", batch="BX", level="critical"),
            alert(3, device="D1", batch="B1", level="warning"),
        ])
        # 只有 #1 命中（无冷却纯抑制）；#2 #3 默认放行但无规则
        self.assertEqual(res[0]["rule_id"], 1)
        self.assertIsNone(res[1]["rule_id"])
        self.assertIsNone(res[2]["rule_id"])
        self.assertEqual([x["verdict"] for x in res], ["suppress", "pass", "pass"])


class TestCooldown(unittest.TestCase):
    def _rules(self, window=600, scope="device_batch", levels="warning"):
        return [rule(id=1, name="cd", priority=10, cooldown_sec=window,
                     cooldown_scope=scope, levels=levels)]

    def test_first_pass_then_repeat_suppressed_then_window_expires(self):
        alerts = [
            alert(1, t="2026-01-01T10:00:00+00:00"),
            alert(2, t="2026-01-01T10:05:00+00:00"),  # +300s 抑制
            alert(3, t="2026-01-01T10:11:00+00:00"),  # +660s 超窗，放行重置
        ]
        res = engine.evaluate(self._rules(600), alerts)
        self.assertEqual([x["verdict"] for x in res], ["pass", "suppress", "pass"])
        self.assertEqual(res[1]["cooldown_seconds"], 300)
        self.assertIsNone(res[2]["cooldown_seconds"])

    def test_cooldown_boundary(self):
        # 窗口是半开区间：间隔 < 窗口算抑制；等于窗口算窗口已过 → 放行重置
        alerts = [
            alert(1, t="2026-01-01T10:00:00+00:00"),
            alert(2, t="2026-01-01T10:09:59+00:00"),  # 599s：窗口内 → 抑制
            alert(3, t="2026-01-01T10:10:00+00:00"),  # 恰好 600s：窗口已过 → 放行
        ]
        res = engine.evaluate(self._rules(600), alerts)
        self.assertEqual(res[1]["verdict"], "suppress")
        self.assertEqual(res[1]["cooldown_seconds"], 1)
        self.assertEqual(res[2]["verdict"], "pass")

    def test_out_of_order_does_not_reset(self):
        # 基准已推进到 10:15，再来一条 10:05 的迟到告警不能重置冷却
        alerts = [
            alert(1, t="2026-01-01T10:00:00+00:00"),
            alert(2, t="2026-01-01T10:15:00+00:00"),  # 超窗，基准推进到 #2
            alert(3, t="2026-01-01T10:05:00+00:00"),  # 乱序，应抑制
        ]
        res = engine.evaluate(self._rules(600), alerts)
        self.assertEqual([x["verdict"] for x in res], ["pass", "pass", "suppress"])
        cd = res[2]["explanation"]["hit"]["cooldown"]
        self.assertEqual(cd["base_alert_sequence"], 2)

    def test_scope_device_ignores_batch(self):
        alerts = [
            alert(1, device="D1", batch="B1", t="2026-01-01T10:00:00+00:00"),
            alert(2, device="D1", batch="B2", t="2026-01-01T10:01:00+00:00"),
            alert(3, device="D2", batch="B2", t="2026-01-01T10:01:00+00:00"),
        ]
        res = engine.evaluate(self._rules(600, scope="device"), alerts)
        self.assertEqual([x["verdict"] for x in res], ["pass", "suppress", "pass"])

    def test_scope_global(self):
        alerts = [
            alert(1, device="D1", batch="B1", t="2026-01-01T10:00:00+00:00"),
            alert(2, device="D9", batch="B9", t="2026-01-01T10:02:00+00:00"),
        ]
        res = engine.evaluate(self._rules(600, scope="global"), alerts)
        self.assertEqual(res[1]["verdict"], "suppress")

    def test_no_cooldown_every_alert_suppressed(self):
        # 纯抑制（无冷却）：命中即抑制
        alerts = [alert(1, t="2026-01-01T10:00:00+00:00"),
                  alert(2, t="2026-01-01T10:01:00+00:00")]
        res = engine.evaluate(
            [rule(id=1, action="suppress", cooldown_sec=0)], alerts)
        self.assertEqual([x["verdict"] for x in res], ["suppress", "suppress"])

    def test_different_level_not_cooldown_target(self):
        alerts = [
            alert(1, level="warning", t="2026-01-01T10:00:00+00:00"),
            alert(2, level="critical", t="2026-01-01T10:01:00+00:00"),
        ]
        res = engine.evaluate(self._rules(600, levels="warning"), alerts)
        # critical 不命中 warning 规则 → 默认放行
        self.assertIsNone(res[1]["rule_id"])
        self.assertEqual(res[1]["verdict"], "pass")


class TestTimeWindow(unittest.TestCase):
    def test_daytime_window(self):
        r = rule(id=1, time_window="08:00-18:00")
        res = engine.evaluate([r], [
            alert(1, t="2026-01-01T09:00:00+00:00"),
            alert(2, t="2026-01-01T20:00:00+00:00"),
        ])
        self.assertEqual(res[0]["rule_id"], 1)
        self.assertIsNone(res[1]["rule_id"])

    def test_overnight_window(self):
        r = rule(id=1, time_window="22:00-06:00")
        res = engine.evaluate([r], [
            alert(1, t="2026-01-01T23:30:00+00:00"),
            alert(2, t="2026-01-02T05:59:00+00:00"),
            alert(3, t="2026-01-01T12:00:00+00:00"),
        ])
        self.assertEqual(res[0]["rule_id"], 1)
        self.assertEqual(res[1]["rule_id"], 1)
        self.assertIsNone(res[2]["rule_id"])

    def test_missing_alert_time_not_matched(self):
        r = rule(id=1, time_window="08:00-18:00")
        res = engine.evaluate([r], [alert(1, t=None)])
        self.assertIsNone(res[0]["rule_id"])

    def test_timezone_aware_comparison(self):
        # 时间窗按时间戳自身携带的当地钟点匹配，与部署机器时区无关
        r = rule(id=1, time_window="09:00-11:00")
        # +08:00 的 10:00 命中，即使它等于 UTC 的 02:00
        res = engine.evaluate([r], [
            alert(1, t="2026-01-01T10:00:00+08:00"),
        ])
        self.assertEqual(res[0]["rule_id"], 1)
        # Z（UTC）10:00 同样按 10:00 处理
        res2 = engine.evaluate([r], [
            alert(1, t="2026-01-01T10:00:00Z"),
        ])
        self.assertEqual(res2[0]["rule_id"], 1)
        # 同一绝对时刻写成 +00:00 的 02:00：按书写钟点 02:00，不命中 09:00-11:00
        res3 = engine.evaluate([r], [
            alert(1, t="2026-01-01T02:00:00+00:00"),
        ])
        self.assertIsNone(res3[0]["rule_id"])


class TestExplanation(unittest.TestCase):
    def test_checked_chain_explains_skips(self):
        r1 = rule(id=1, name="night", priority=10, action="pass",
                  devices="DOOR-*", time_window="22:00-06:00")
        r2 = rule(id=2, name="cd", priority=20, cooldown_sec=600)
        res = engine.evaluate([r1, r2], [
            alert(1, device="FREEZER-1", t="2026-01-01T12:00:00+00:00")])
        checked = res[0]["explanation"]["checked"]
        self.assertEqual(len(checked), 2)
        self.assertFalse(checked[0]["matched"])
        self.assertIn("devices", checked[0]["failures"])
        self.assertTrue(checked[1]["matched"])


if __name__ == "__main__":
    unittest.main()
