from __future__ import annotations

import unittest

from microtask_swarm.dag import ScheduledTask, compile_layers

try:
    import swe.microtasks as _swe_microtasks  # noqa: F401
except Exception:  # pragma: no cover - depends on optional SWE package/deps
    _swe_microtasks = None


@unittest.skipIf(_swe_microtasks is None, "SWE Python package or jsonschema is absent")
class SweMicrotaskIntegrationTests(unittest.TestCase):
    def test_compiler_is_reused_when_importable(self) -> None:
        plan = compile_layers(
            [
                ScheduledTask("publish", depends_on=("lint", "test")),
                ScheduledTask("lint"),
                ScheduledTask("test", depends_on=("cache",)),
                ScheduledTask("cache"),
            ],
            concurrency=4,
        )
        self.assertEqual(plan.source, "swe.microtasks.compile_microtasks")
        self.assertEqual(plan.layers, (("lint", "cache"), ("test",), ("publish",)))
        self.assertIsNone(plan.fallback_reason)


if __name__ == "__main__":
    unittest.main()
