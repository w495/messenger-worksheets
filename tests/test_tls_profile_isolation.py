from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TlsProfileIsolationTest(unittest.TestCase):
    def test_provisioning_pins_each_rpc_route_to_its_profile_ca(self):
        """Запрещает вернуть общий CA для API, local-user и sandbox routes."""
        source = (ROOT / "deployment/provision_instances.py").read_text(encoding="utf-8")
        for name in ("ca-api.crt", "ca-local-user.crt", "ca-sandbox-admin.crt"):
            self.assertIn(name, source)
        self.assertIn('"WORKSHEET_TASK_WORKER_CA"', source)


if __name__ == "__main__":
    unittest.main()
