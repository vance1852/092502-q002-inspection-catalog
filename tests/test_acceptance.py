import unittest

from inspection_catalog_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(3, result["records"])
        # 当前清单含基础条目与覆盖层新增条目
        self.assertEqual(["GAS-01", "GEN-01", "SP-01"], result["current_codes"])
        # 已开始任务在新版本发布后条目与版本均不变
        self.assertEqual(["GAS-01", "GEN-01", "SP-01"], result["frozen_codes"])
        self.assertEqual(result["frozen_codes"], result["frozen_codes_after_new_publish"])
        self.assertEqual(64, len(result["frozen_manifest_hash"]))
        # 未来变更同时覆盖版本与覆盖层
        self.assertIn("version_effective", result["future_change_types"])
        self.assertIn("overlay_expiry", result["future_change_types"])
        # 标签快照保存了三类领域资料
        self.assertEqual(["enterprise_tag", "pollution_factor", "process_profile"],
                         result["tag_snapshot_keys"])


if __name__ == "__main__":
    unittest.main()
