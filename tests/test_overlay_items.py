from contextlib import ExitStack
import itertools
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from core import config, render
from core import __main__ as app


class OverlayItemsTests(unittest.TestCase):
    def test_all_item_combinations(self):
        labels = {"claude-code": "Claude", "fable": "Fable", "codex": "Codex"}
        for count in range(1, 5):
            for selected in itertools.combinations(config.DEFAULTS["overlay_items"], count):
                with self.subTest(selected=selected):
                    html = render.render_overlay({}, {"overlay_items": list(selected)})
                    for key, label in labels.items():
                        self.assertEqual(">" + label + "</span>" in html, key in selected)
                    self.assertEqual("id='clk'" in html, "clock" in selected)
                    self.assertEqual("aria-label='ChatGPT'" in html, "codex" in selected)
                    self.assertEqual(html.count("class='div'"), int(
                        "codex" in selected and bool(set(selected) & {"claude-code", "fable"})))
                    self.assertIn("q_console:drag-start", html)
        only_codex = render.render_overlay({}, {"overlay_items": ["codex"]})
        width = lambda html: int(re.search("data-w='(\\d+)'", html)[1])
        self.assertLess(width(only_codex), width(render.render_overlay({})))

    def test_old_or_invalid_config_keeps_default_display(self):
        for value in (None, "codex", [], ["invalid"]):
            self.assertEqual(config.overlay_items({"overlay_items": value}),
                             config.DEFAULTS["overlay_items"])

    def test_worker_persists_and_renders_selection_without_network_refresh(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(config, "APP_HOME", directory))
            for name, filename in (("CONFIG_PATH", "config.json"),
                                   ("OVERLAY_PATH", "overlay.html")):
                stack.enter_context(patch.object(config, name, str(Path(directory) / filename)))
            stack.enter_context(patch.object(app, "load_cached_snapshot", return_value={"providers": []}))
            refresh = stack.enter_context(patch.object(app, "refresh"))
            self.assertEqual(app.main(["--set-overlay-items", "codex"]), 0)
            self.assertEqual(config.load()["overlay_items"], ["codex"])
            html = Path(config.OVERLAY_PATH).read_text(encoding="utf-8")
            self.assertIn(">Codex</span>", html)
            self.assertNotIn(">Claude</span>", html)
            self.assertNotIn("id='clk'", html)
            refresh.assert_not_called()
            with patch.object(app, "emit"):
                self.assertEqual(app.main(["--set-overlay-items", ""]), 2)
            self.assertEqual(config.load()["overlay_items"], ["codex"])

    def test_inflight_refresh_uses_latest_display_selection(self):
        previous = dict(config.DEFAULTS)
        current = {**previous, "overlay_items": ["codex"]}
        with patch.object(config, "load", return_value=current), \
             patch.object(app.os.path, "isfile", return_value=True), \
             patch.object(app, "load_cached_snapshot", return_value=None), \
             patch.object(app.snapshot, "build", return_value={"providers": []}), \
             patch.object(app, "write_outputs") as write:
            app.refresh(previous)
        self.assertEqual(write.call_args.args[1]["overlay_items"], ["codex"])
