"""Drive the real page in a real browser against the real server (with a fake model behind it).

Skipped automatically when Playwright or its Chromium is not installed:
    pip install playwright && playwright install chromium
Set WONTDO_SHOTS=/some/folder to also save screenshots.
"""

import datetime as dt
import glob
import json
import os
import shutil
import tempfile
import threading
import unittest

from tests.mock_llm import MockLLM
from wontdo.server import make_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

SHOTS = os.environ.get("WONTDO_SHOTS", "")

SAMPLE_TASKS = 13


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._pw = sync_playwright().start()
        try:
            cls.browser = cls._pw.chromium.launch()
        except Exception as err:
            cls._pw.stop()
            raise unittest.SkipTest("Chromium is not available: %s" % str(err)[:80]) from err
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wontdo-ui-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mock = MockLLM().start()
        self.addCleanup(self.mock.stop)
        self.server = make_server("127.0.0.1", 0, os.path.join(self.tmp, "data"))
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self._stop_server)
        self.history_dir = os.path.join(self.tmp, "data", "history")
        self.problems = []
        self.contexts = []
        self.allow_http_errors = False  # tests that provoke a 4xx/5xx on purpose set this

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def tearDown(self):
        for context in self.contexts:
            context.close()
        self.assertEqual(self.problems, [], "the page logged errors")

    def open(self, path="/", width=1280, height=900, dialogs=True):
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.contexts.append(context)
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.on("requestfailed", lambda r: self.problems.append("request failed: %s" % r.url))
        if dialogs:
            page.on("dialog", lambda d: d.accept())
        page.goto(self.base + path)
        return page

    def _on_console(self, message):
        if message.type not in ("error", "warning"):
            return
        if self.allow_http_errors and message.text.startswith("Failed to load resource: the server responded with a status of"):
            return
        self.problems.append("console %s: %s" % (message.type, message.text))

    def use_model(self, page=None):
        import urllib.request

        request = urllib.request.Request(
            self.base + "/api/settings", data=json.dumps({"base_url": self.mock.url}).encode(), method="PUT",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request).read()

    def settings(self, **patch):
        import urllib.request

        request = urllib.request.Request(
            self.base + "/api/settings", data=json.dumps(patch).encode(), method="PUT", headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(request).read()

    def shot(self, page, name, full=True):
        if SHOTS:
            if full:  # a sticky header would otherwise be drawn part-way down a full-page capture
                page.evaluate("() => window.scrollTo({ top: 0, behavior: 'instant' })")
                page.wait_for_timeout(120)
            page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=full)

    def sort_sample(self, page):
        page.click("#sample-btn")
        page.click("#sort-btn")
        page.wait_for_selector("#list-root .bucket")

    def count(self, page, bucket):
        return int(page.locator(".bucket-%s .count" % bucket).inner_text())

    def test_home_page(self):
        self.use_model()
        page = self.open()
        page.wait_for_selector("#model-chip[data-state='model']")
        self.assertEqual(page.title(), "The Won’t-Do List")
        self.assertEqual(page.locator("h1").first.inner_text().replace("\n", " "), "Do less, on purpose.")
        self.assertIn("gemma3:4b", page.locator("#model-chip-text").inner_text())
        self.assertTrue(page.locator("#model-banner").is_hidden())
        self.assertEqual(page.locator("details").count(), 6)
        page.click("details:has-text('Does my list leave my computer?') summary")
        self.assertTrue(page.locator("details[open] p").first.is_visible())
        self.shot(page, "home")

    def test_sample_sort_and_the_result_page(self):
        self.use_model()
        page = self.open()
        page.click("#sample-btn")
        page.wait_for_function("() => document.getElementById('parse-note').textContent.includes('13 tasks found')")
        self.shot(page, "home-filled", full=False)
        page.click("#sort-btn")
        page.wait_for_selector("#list-root .bucket")
        self.assertRegex(page.url, r"#/list/\d{8}-\d{6}-[0-9a-f]{4}$")
        self.assertEqual(sum(self.count(page, b) for b in ("now", "later", "expire", "notmine")), SAMPLE_TASKS)
        self.assertIn("13 tasks in.", page.locator("#list-root h1").inner_text())
        self.assertTrue(page.locator(".bucket-expire .task", has_text="Learn Rust someday").is_visible())
        self.assertTrue(page.locator(".bucket-notmine .task", has_text="landlord").is_visible())
        self.assertTrue(page.locator(".bucket-now .task", has_text="Pay electricity bill").is_visible())
        self.assertNotIn("null", page.locator("#list-root").inner_text())
        self.assertNotIn("[object", page.locator("#list-root").inner_text())
        self.shot(page, "result")
        # the list was saved: it survives a reload and shows up in history
        page.reload()
        page.wait_for_selector("#list-root .bucket")
        self.assertEqual(self.count(page, "expire"), 1)

    def test_ctrl_enter_sorts(self):
        self.use_model()
        page = self.open()
        page.fill("#tasks", "Pay rent by tomorrow\nLearn Rust someday")
        page.press("#tasks", "Control+Enter")
        page.wait_for_selector("#list-root .bucket")
        self.assertIn("2 tasks in.", page.locator("#list-root h1").inner_text())

    def test_model_text_is_never_treated_as_html(self):
        self.use_model()
        page = self.open()
        evil = '<img src=x onerror="window.__pwned=1"> & <script>window.__pwned=2</script>'
        page.fill("#tasks", evil + "\nLearn Rust someday")
        page.click("#sort-btn")
        page.wait_for_selector("#list-root .bucket")
        self.assertIsNone(page.evaluate("() => window.__pwned"))
        self.assertEqual(page.locator("#list-root img[src='x']").count(), 0)
        self.assertIn("<img src=x", page.locator("#list-root").inner_text())

    def test_moving_a_task_and_undoing_it(self):
        self.use_model()
        page = self.open()
        self.sort_sample(page)
        before = self.count(page, "expire")
        row = page.locator(".bucket-expire .task", has_text="Learn Rust someday")
        row.locator("select.move-select").select_option("later")
        page.wait_for_selector(".bucket-later .task:has-text('Learn Rust someday')")
        self.assertEqual(self.count(page, "expire"), before - 1)
        self.assertIn("Moved to Do later.", page.locator("#toast").inner_text())
        self.assertIn("You moved this from Let it expire", page.locator(".bucket-later .task", has_text="Learn Rust someday").inner_text())
        page.click("#toast button")
        page.wait_for_selector(".bucket-expire .task:has-text('Learn Rust someday')")
        self.assertEqual(self.count(page, "expire"), before)
        # and the put-it-back link on a moved task
        page.locator(".bucket-expire .task", has_text="Learn Rust someday").locator("select.move-select").select_option("now")
        page.wait_for_selector(".bucket-now .task:has-text('Learn Rust someday')")
        page.click(".bucket-now .task:has-text('Learn Rust someday') .link-btn")
        page.wait_for_selector(".bucket-expire .task:has-text('Learn Rust someday')")

    def test_energy_slider_reflows_without_asking_the_model_again(self):
        self.use_model()
        page = self.open()
        page.fill("#tasks", "\n".join("Reply to Anita about item %d" % n for n in range(1, 7)))
        page.evaluate("() => { const b = document.getElementById('budget'); b.value = 0; b.dispatchEvent(new Event('input')); }")
        self.assertEqual(page.locator("#budget-out").inner_text(), "No limit")
        page.click("#sort-btn")
        page.wait_for_selector("#list-root .bucket")
        calls = len(self.mock.calls)
        self.assertEqual(self.count(page, "now") + self.count(page, "later"), 6)
        page.focus("#list-budget")
        page.evaluate("() => { const s = document.getElementById('list-budget'); s.value = 15; s.dispatchEvent(new Event('input')); s.dispatchEvent(new Event('change')); }")
        page.wait_for_function("() => document.querySelectorAll('.bucket-now .task').length < 6")
        self.assertEqual(len(self.mock.calls), calls)

    def test_export_menu(self):
        self.use_model()
        page = self.open()
        self.sort_sample(page)
        page.click("details.menu summary")
        links = page.locator(".menu-list a")
        self.assertEqual(links.count(), 4)
        hrefs = [links.nth(i).get_attribute("href") for i in range(4)]
        self.assertTrue(all("/api/export/" in h and "download=1" in h for h in hrefs))
        self.assertEqual([h.split("fmt=")[1].split("&")[0] for h in hrefs], ["md", "csv", "ics", "json"])
        with page.expect_download() as info:
            links.nth(1).click()
        self.assertTrue(info.value.suggested_filename.endswith(".csv"))
        self.shot(page, "export-menu", full=False)

    def test_check_ins(self):
        self.use_model()
        page = self.open()
        self.sort_sample(page)
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        path = glob.glob(os.path.join(self.history_dir, "*.json"))[0]
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        for task in data["tasks"]:
            if task["bucket"] in ("expire", "notmine"):
                task["check_on"] = yesterday
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        page = self.open("/#/checkins")
        page.wait_for_selector("#checkins-root .stat")
        self.assertEqual(page.locator("#checkin-badge").inner_text(), "2")
        self.assertIn("Due now (2)", page.locator("#checkins-root").inner_text())
        self.shot(page, "checkins")
        page.click(".row:has-text('Learn Rust someday') button:has-text('It bit me')")
        page.wait_for_selector("h2:has-text('Due now (1)')")
        page.click(".row:has-text('landlord') button:has-text('Nothing happened')")
        page.wait_for_selector("h2:has-text('Due now (0)')")
        text = page.locator("#checkins-root").inner_text()
        self.assertIn("Checked (2)", text)
        self.assertIn("(50%)", text)
        page.click(".row:has-text('landlord') button:has-text('Undo')")
        page.wait_for_selector("h2:has-text('Due now (1)')")
        self.assertEqual(page.locator("#checkin-badge").inner_text(), "1")

    def test_history_and_delete(self):
        self.use_model()
        page = self.open()
        self.sort_sample(page)
        page.click("a[data-nav='history']")
        page.wait_for_selector("#history-root .row")
        self.assertIn("13 tasks", page.locator("#history-root .row").first.inner_text())
        self.shot(page, "history")
        page.click("#history-root .row button:has-text('Delete')")
        page.wait_for_selector("#history-root .empty-card")
        self.assertEqual(glob.glob(os.path.join(self.history_dir, "*.json")), [])
        page.click("a[data-nav='checkins']")
        page.wait_for_selector("#checkins-root .empty-card")

    def test_settings_page_and_test_connection(self):
        self.use_model()
        page = self.open("/#/settings")
        page.wait_for_selector("#s-budget")
        self.assertEqual(page.input_value("#s-budget"), "90")
        page.click("#test-btn")
        page.wait_for_selector(".doctor .notice-ok")
        self.assertEqual(page.locator(".doctor-steps li").count(), 5)
        self.shot(page, "settings")
        page.fill("#s-budget", "45")
        page.fill("#s-never", "mum\nlandlord")
        page.click("button:has-text('Save settings')")
        page.wait_for_selector("#toast:has-text('Settings saved.')")
        page.reload()
        page.wait_for_selector("#s-budget")
        self.assertEqual(page.input_value("#s-budget"), "45")
        self.assertEqual(page.input_value("#s-never"), "mum\nlandlord")
        page.goto(self.base + "/")
        self.assertEqual(page.input_value("#budget"), "45")

    def test_invalid_settings_are_explained(self):
        self.allow_http_errors = True
        self.use_model()
        page = self.open("/#/settings")
        page.wait_for_selector("#s-budget")
        page.fill("#s-budget", "5000")
        page.click("button:has-text('Save settings')")
        page.wait_for_selector(".settings-form .notice-error")
        self.assertIn("Budget must be between", page.locator(".settings-form .notice-error").inner_text())

    def test_without_a_model_the_demo_is_offered_and_labelled(self):
        self.allow_http_errors = True
        self.settings(base_url="http://127.0.0.1:9")  # nothing listens here
        page = self.open()
        page.wait_for_selector("#model-chip[data-state='demo']")
        self.assertIn("No local model found", page.locator("#model-banner").inner_text())
        self.shot(page, "home-no-model", full=False)
        page.click("#sample-btn")
        page.click("#sort-btn")
        page.wait_for_selector("#sort-error:not([hidden])")
        self.assertIn("No model to ask.", page.locator("#sort-error").inner_text())
        page.click("#sort-error button:has-text('keyword rules')")
        page.wait_for_selector("#list-root .bucket")
        self.assertIn("Sorted with keyword rules, not AI.", page.locator("#list-root").inner_text())
        self.shot(page, "result-demo")

    def test_a_model_that_returns_rubbish_still_gives_a_complete_list(self):
        self.use_model()
        self.mock.mode = "garbage"
        page = self.open()
        self.sort_sample(page)
        self.assertEqual(sum(self.count(page, b) for b in ("now", "later", "expire", "notmine")), SAMPLE_TASKS)
        self.assertIn("Sorted with keyword rules, not AI.", page.locator("#list-root").inner_text())
        self.assertIn("Notes from the sort", page.locator("#list-root").inner_text())

    def test_the_safety_net_is_visible(self):
        self.use_model()
        self.mock.mode = "all_expire"
        page = self.open()
        page.fill("#tasks", "Pay rent\nRenew passport\nLearn Rust someday")
        page.click("#sort-btn")
        page.wait_for_selector("#list-root .bucket")
        self.assertEqual(self.count(page, "expire"), 1)
        self.assertIn("The safety net kept 2 tasks", page.locator("#list-root").inner_text())
        self.assertIn("Safety net: money", page.locator(".bucket-later .task", has_text="Pay rent").inner_text())
        self.shot(page, "result-safety-net")

    def test_unsaved_lists_are_read_only(self):
        self.use_model()
        self.settings(autosave=False)
        page = self.open()
        self.sort_sample(page)
        self.assertEqual(page.url, self.base + "/#/result")
        self.assertIn("This list was not saved.", page.locator("#list-root").inner_text())
        self.assertEqual(page.locator("select.move-select").count(), 0)
        self.assertEqual(page.locator("details.menu").count(), 0)
        self.assertTrue(page.locator("button:has-text('Copy as text')").is_visible())
        self.assertEqual(glob.glob(os.path.join(self.history_dir, "*.json")), [])

    def test_cancel(self):
        self.use_model()
        self.settings(chunk_size=1)
        self.mock.delay = 0.4
        page = self.open()
        page.fill("#tasks", "\n".join("Task number %d" % n for n in range(1, 9)))
        page.click("#sort-btn")
        page.wait_for_selector("#progress:not([hidden])")
        self.assertEqual(page.locator("#sort-btn").text_content(), "Sorting…")
        page.wait_for_function("() => document.getElementById('progress-bar').value >= 1")
        page.click("#cancel-btn")
        page.wait_for_function("() => document.getElementById('progress').hidden")
        self.assertIn("Cancelled", page.locator("#toast").inner_text())
        self.assertEqual(glob.glob(os.path.join(self.history_dir, "*.json")), [])

    def test_navigation_titles_and_focus(self):
        self.allow_http_errors = True  # the missing list below is a 404 on purpose
        self.use_model()
        page = self.open()
        for nav, title in (("checkins", "Check-ins"), ("history", "History"), ("settings", "Settings")):
            page.click("a[data-nav='%s']" % nav)
            page.wait_for_function("t => document.title.startsWith(t)", arg=title)
            self.assertEqual(page.locator("a[data-nav='%s']" % nav).get_attribute("aria-current"), "page")
        page.goto(self.base + "/#/list/20250101-000000-ffff")
        page.wait_for_selector("#list-root .empty-card")
        self.assertIn("That one is gone.", page.locator("#list-root").inner_text())
        page.goto(self.base + "/#/result")
        page.wait_for_selector("#view-sort:not([hidden])")  # no unsaved result: back to the start

    def test_small_screens_do_not_scroll_sideways(self):
        self.use_model()
        page = self.open(width=360, height=740)
        self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 360)
        self.shot(page, "mobile-home")
        self.sort_sample(page)
        self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 360)
        self.shot(page, "mobile-result")
        for route in ("#/checkins", "#/history", "#/settings"):
            page.goto(self.base + "/" + route)
            page.wait_for_timeout(300)
            self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 360, route)

    def test_every_form_control_and_image_is_labelled(self):
        self.use_model()
        page = self.open("/#/settings")
        page.wait_for_selector("#s-budget")
        for route in ("/", "/#/settings"):
            page.goto(self.base + route)
            page.wait_for_timeout(400)
            missing = page.evaluate(
                """() => [...document.querySelectorAll('input, select, textarea')].filter(el => {
                    if (el.type === 'hidden') return false;
                    const labelled = el.id && document.querySelector('label[for="' + el.id + '"]');
                    return !(labelled || el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'));
                }).map(el => el.outerHTML.slice(0, 80))"""
            )
            self.assertEqual(missing, [], route)
            self.assertEqual(page.evaluate("() => [...document.images].filter(i => !i.hasAttribute('alt')).length"), 0)

    def test_the_content_security_policy_is_satisfied(self):
        """No inline script or style anywhere: a strict CSP is on and nothing complains."""
        self.use_model()
        page = self.open()
        self.sort_sample(page)
        for route in ("#/checkins", "#/history", "#/settings"):
            page.goto(self.base + "/" + route)
            page.wait_for_timeout(300)
        self.assertEqual(page.evaluate("() => document.querySelectorAll('[style]').length"), 0)
        self.assertEqual(page.evaluate("() => [...document.scripts].filter(s => !s.src).length"), 0)

    def test_a_task_that_cannot_wait_may_exceed_the_budget_and_says_so(self):
        self.allow_http_errors = True
        self.settings(base_url="http://127.0.0.1:9")  # no model: the keyword rules judge, once the person agrees
        page = self.open()
        page.fill("#tasks", "Submit the expense report by tomorrow")
        page.evaluate("() => { const b = document.getElementById('budget'); b.value = 30; b.dispatchEvent(new Event('input')); }")
        page.click("#sort-btn")
        page.wait_for_selector("#sort-error:not([hidden])")
        page.click("#sort-error button:has-text('keyword rules')")
        page.wait_for_selector("#list-root .bucket")
        self.assertEqual(self.count(page, "now"), 1)
        self.assertIn("over your 30 min", page.locator(".bucket-now .bucket-total").inner_text())

    def test_nothing_leaves_this_machine(self):
        """Every request the page makes, fonts and images included, goes to the app itself."""
        self.use_model()
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.contexts.append(context)
        urls = []
        context.on("request", lambda request: urls.append(request.url))
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.problems.append("pageerror: %s" % e))
        page.goto(self.base + "/")
        self.sort_sample(page)
        for route in ("#/checkins", "#/history", "#/settings"):
            page.goto(self.base + "/" + route)
            page.wait_for_timeout(300)
        strangers = [u for u in urls if not (u.startswith(self.base + "/") or u.startswith("data:"))]
        self.assertEqual(strangers, [])
        self.assertTrue(any(u.endswith(".woff2") for u in urls), "the bundled fonts were never requested")


if __name__ == "__main__":
    unittest.main()
