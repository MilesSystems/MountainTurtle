import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


turtle = load("handoff_turtle", ROOT / "service/turtle_service.py")
handoff = load("handoff", ROOT / "scripts/restart-service-safely.py")


class HandoffTests(unittest.TestCase):
    def test_restore_only_changes_maintenance_intent_and_keeps_new_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = turtle.Paths(home=temporary)
            paths.prepare()
            store = turtle.Store(paths)
            original = dict(id="original", name="User renamed while upgrading", host="new-host",
                            autoConnect=False, desiredConnected=False, revision=7)
            added = dict(id="added-later", name="New drive", desiredConnected=True, revision=3)
            with store.update() as state:
                state.update(shutdown=True, launchAtLogin=True, connections=[original, added])
            handoff.restore_intent(turtle, paths, {"original": True, "removed-drive": True})
            after = store.read()
            self.assertFalse(after["shutdown"])
            self.assertTrue(after["launchAtLogin"])
            self.assertEqual(after["connections"][0], dict(original, desiredConnected=True,
                                                         reconnectRequested=False, revision=8))
            self.assertEqual(after["connections"][1], added)
            self.assertEqual(len(after["connections"]), 2)


if __name__ == "__main__":
    unittest.main()
