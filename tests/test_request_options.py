from __future__ import annotations

import unittest

from messenger_worksheets import request_options


class RequestOptionsTest(unittest.TestCase):
    def test_model_and_effort_are_bounded_data_without_global_mutation(self):
        """Опции независимы от исходного dict и не допускают shell/config syntax."""
        original = {"model": "test-model.1", "reasoning_effort": "high"}
        checked = request_options.validate_options(original)
        checked["model"] = "other-model"
        self.assertEqual(original["model"], "test-model.1")
        self.assertEqual(request_options.effective_settings(original, model="default", reasoning_effort="low"), {
            "model": "test-model.1", "reasoning_effort": "high", "mode": "normal",
        })
        for bad in ({"model": "model;echo secret"}, {"model": "x\n-c"}, {"model": "x" * 101},
                    {"model": "модель"}, {"model": "--dangerously-bypass-approvals-and-sandbox"},
                    {"model": ""}, {"model": None}, {"reasoning_effort": "infinite"},
                    {"mode": "admin"}, {"unknown": "setting"}, [], "model"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                request_options.validate_options(bad)

    def test_normal_mode_keeps_permissions_and_replaces_only_requested_values(self):
        """Обычный запрос сохраняет установленную политику и заменяет model/effort один раз."""
        argv = ["/usr/bin/codex", "exec", "--model", "default-model", "-c", 'model_reasoning_effort="low"',
                "--sandbox", "danger-full-access", "--ephemeral", "-"]
        self.assertEqual(request_options.command_options(argv), argv)
        updated = request_options.command_options(argv, {"model": "other", "reasoning_effort": "high"})
        self.assertEqual(updated.count("--model"), 1)
        self.assertIn("other", updated)
        self.assertNotIn("default-model", updated)
        self.assertNotIn('model_reasoning_effort="low"', updated)
        self.assertIn('model_reasoning_effort="high"', updated)
        self.assertIn("danger-full-access", updated)
        self.assertEqual(argv[3], "default-model")

    def test_plan_and_review_force_readonly_ephemeral_without_resume(self):
        """План/ревью снимают admin/workspace permissions и не продолжают прежнюю сессию."""
        argv = ["/usr/bin/codex", "exec", "-c", 'approval_policy="never"',
                "--sandbox", "danger-full-access", "--ephemeral", "-c", 'default_permissions="task"',
                "-c", 'permissions.task.filesystem={":minimal"="read","/workspace"="write"}',
                "-c", "permissions.task.network.enabled=true", "--enable", "image_generation",
                "resume", "synthetic-session", "--image=/input/image.png", "-"]
        for mode in ("plan", "review"):
            with self.subTest(mode=mode):
                result = request_options.command_options(argv, {"mode": mode})
                self.assertEqual(result.count("--sandbox"), 1)
                self.assertEqual(result[result.index("--sandbox") + 1], "read-only")
                self.assertEqual(result.count("--ephemeral"), 1)
                self.assertNotIn("danger-full-access", result)
                self.assertNotIn("resume", result)
                self.assertNotIn("synthetic-session", result)
                self.assertFalse(any(item.startswith(("permissions.", "default_permissions=")) for item in result))
                self.assertIn("--image=/input/image.png", result)
                self.assertIn('approval_policy="never"', result)
                self.assertEqual(result[result.index("--disable") + 1], "image_generation")
                disabled = {result[index + 1] for index, item in enumerate(result[:-1]) if item == "--disable"}
                self.assertTrue({"plugins", "hooks", "apps", "multi_agent", "remote_plugin"} <= disabled)
                self.assertIn("Не", request_options.option_instructions({"mode": mode}))
        self.assertEqual(request_options.option_instructions(), "")

    def test_incomplete_internal_arguments_fail_closed(self):
        """Повреждённый внутренний argv не превращается в другую исполняемую команду."""
        for argv in (["codex", "review", "-"], ["codex", "exec"], ["codex", "exec", "--sandbox", "-"],
                     ["codex", "exec", "-c", "-"], ["codex", "exec", "resume", "-"]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                request_options.command_options(argv, {"mode": "plan"})


if __name__ == "__main__":
    unittest.main()
