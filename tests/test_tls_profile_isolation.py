from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TlsProfileIsolationTest(unittest.TestCase):
    def test_generator_uses_three_independent_certificate_authorities(self):
        """Фиксирует отдельную CA/leaf/key цепочку каждого worker-профиля."""
        source = (ROOT / "deployment/generate_tls.sh").read_text(encoding="utf-8")
        self.assertIn("generate_profile api worksheet-worker worker ca-api.crt", source)
        self.assertIn(
            "generate_profile local-user w495 local-user ca-local-user.crt", source,
        )
        self.assertIn(
            "generate_profile sandbox-admin worksheet-chat-admin "
            "sandbox-admin ca-sandbox-admin.crt",
            source,
        )
        installer = (ROOT / "deployment/install_admin_helper.sh").read_text(
            encoding="utf-8",
        )
        self.assertNotIn(
            "install -m 0640 -o root -g worksheet-chat-admin",
            installer,
        )

    def test_provisioning_pins_each_rpc_route_to_its_profile_ca(self):
        """Запрещает вернуть общий CA для API, local-user и sandbox routes."""
        source = (ROOT / "deployment/provision_instances.py").read_text(encoding="utf-8")
        for name in ("ca-api.crt", "ca-local-user.crt", "ca-sandbox-admin.crt"):
            self.assertIn(name, source)
        self.assertIn('"WORKSHEET_TASK_WORKER_CA"', source)


if __name__ == "__main__":
    unittest.main()
