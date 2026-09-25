import unittest

from inspection_catalog_core.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        # 基础资料 + 工艺 + 风险标签
        self.assertEqual(3, result["records"])
        # 清单链路：当前清单含企业增项，任务冻结后撤销不改变历史
        self.assertIn("EX01", result["checklist_current_items"])
        self.assertEqual(result["checklist_current_items"], result["frozen_task_items"])
        self.assertEqual(1, len(result["frozen_task_version_ids"]))
        self.assertTrue(result["frozen_unchanged_after_revoke"])
        self.assertGreaterEqual(result["future_change_events"], 1)


if __name__ == "__main__":
    unittest.main()
