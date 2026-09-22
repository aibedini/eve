"""Renewal result modal: state, language and context.

The dashboard's renewal modal used to describe a renewal from `verify.ok` and from
backend prose:

* `showRenewInProgress()` overwrote the module-level `lastRenewContext` with
  `{expectedExpiryTime: null, expectedTotalGB: null}` on EVERY call, so the
  `renew_not_verified` branch - whose response already carried `verify.expected` -
  threw the expectations away. The following Re-check then posted nulls, the panel
  could only answer `renew_result_unavailable`, and the operator read "? Expiry" as
  if EVE had forgotten what it asked for.
* the modal title/badges were rendered from `verify.ok` only, so "config applied,
  activation pending" - a recorded business fact - was painted as a red failure;
* the body came from `data.error`, which the backend localizes from the PANEL's
  language while the page used `document.documentElement.lang`, so the two could
  disagree (a Persian title over an English body).

These tests are pure text/JS assertions: no Flask, no network, no sleeps. The pure
helpers are extracted from the template and evaluated with Node so the assertions
run against the real code rather than a copy of it.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(REPO_ROOT, "templates", "dashboard.html")

REGION_START = "// renew-result-modal:start"
REGION_END = "// renew-result-modal:end"

# The states the renewal contract can report (plus the UI-only duplicate-request
# state). Every one of them must be translated in both languages.
STATES = (
    "APPLIED_ACTIVE",
    "CONFIG_APPLIED_ACTIVATION_PENDING",
    "PARTIALLY_APPLIED",
    "NOT_APPLIED",
    "AUTH_DEGRADED",
    "UNKNOWN",
    "IN_PROGRESS",
)

# Exact operator copy the contract requires (verbatim, both languages).
REQUIRED_TEXT = {
    "APPLIED_ACTIVE": {
        "fa": ("تمدید با موفقیت انجام شد", "حساب تمدید شده و فعال است."),
        "en": ("Renewal completed successfully",
               "The account has been renewed and is active."),
    },
    "CONFIG_APPLIED_ACTIVATION_PENDING": {
        "fa": ("تمدید ثبت شد — فعال‌سازی در حال تکمیل است",
               "تمدید را دوباره انجام ندهید. EVE در حال تکمیل فعال‌سازی حساب است."),
        "en": ("Renewal applied — activation is still synchronizing",
               "Do not renew again. EVE is still completing account activation."),
    },
    "PARTIALLY_APPLIED": {
        "fa": ("تمدید ناقص اعمال شده", "دوباره تمدید نکنید؛ وضعیت نیاز به تطبیق دارد."),
        "en": ("Renewal partially applied",
               "Do not renew again; this operation requires reconciliation."),
    },
    "NOT_APPLIED": {
        "fa": ("تمدید اعمال نشد", None),
        "en": ("Renewal was not applied", None),
    },
    "UNKNOWN": {
        "fa": ("وضعیت تمدید هنوز مشخص نیست", None),
        "en": ("Renewal status is not yet confirmed", None),
    },
}

# Internal diagnostics: machine codes that must never reach the operator as prose.
INTERNAL_CODES = (
    "renew_result_unavailable",
    "renew_result_not_applied_yet",
    "enable_reassert_failed",
)

# Latin words that must not appear in the PERSIAN table (an operator reading the
# Persian dashboard must not be handed an English sentence). EVE and API are proper
# nouns / technical identifiers that are deliberately kept in both languages.
FA_LATIN_BLOCKLIST = ("Renewal", "Applied", "Do not", "active", "the ", "and ")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _balanced(text, start):
    """Return text[start:end] for the balanced-brace block at text[start] == '{'.

    Skips quotes and comments so a brace inside a Persian/latin string literal
    cannot unbalance the match.
    """
    if text[start] != "{":
        raise AssertionError("expected '{' at offset %d" % start)
    depth = 0
    index = start
    quote = None
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
            index += 1
            continue
        if char == "/" and nxt == "/":
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
            continue
        if char == "/" and nxt == "*":
            end = text.find("*/", index)
            index = len(text) if end == -1 else end + 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
        index += 1
    raise AssertionError("unbalanced braces from offset %d" % start)


class _Template:
    """Extraction helpers over the dashboard template."""

    def __init__(self, text):
        self.text = text

    def region(self):
        start = self.text.find(REGION_START)
        end = self.text.find(REGION_END)
        if start == -1 or end == -1 or end < start:
            raise AssertionError(
                "the renew-result-modal region markers are missing from dashboard.html "
                "(expected %r .. %r)" % (REGION_START, REGION_END))
        return self.text[start:end]

    def function(self, name):
        match = re.search(r"(?m)^[ \t]*function\s+%s\s*\([^)]*\)\s*\{" % re.escape(name),
                          self.text)
        if not match:
            raise AssertionError("function %s() was not found in dashboard.html" % name)
        brace = self.text.index("{", match.end() - 1)
        return self.text[match.start():brace] + _balanced(self.text, brace)

    def literal(self, declaration):
        index = self.text.find(declaration)
        if index == -1:
            raise AssertionError("%r was not found in dashboard.html" % declaration)
        brace = self.text.index("{", index)
        block = self.text[brace:brace + len(_balanced(self.text, brace))]
        # Returned as a complete statement: a bare object literal would be parsed by
        # Node as a block statement (labels), not as a value.
        return "%s %s;" % (declaration.rstrip(), block)


def _run_node(js_source, expression):
    """Evaluate `expression` in Node after `js_source`, returning parsed JSON."""
    node = shutil.which("node")
    if not node:
        raise AssertionError("node is required to evaluate the dashboard's renew helpers")
    script = "%s\nconst __result = (%s);\nprocess.stdout.write(JSON.stringify(__result));\n" % (
        js_source, expression)
    handle, path = tempfile.mkstemp(suffix=".cjs", prefix="eve_renew_ui_")
    os.close(handle)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(script)
        # Bytes, decoded explicitly: the JSON carries Persian text, and the console
        # encoding on Windows is not UTF-8.
        result = subprocess.run([node, path], capture_output=True, timeout=60)
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        if result.returncode != 0:
            raise AssertionError("node failed (%s):\n%s" % (result.returncode, stderr))
        return json.loads(stdout)
    finally:
        if os.path.exists(path):
            os.remove(path)


def _flatten(table):
    """Every string value in a nested {state: {lang: {title, body, pill}}} table."""
    values = []
    for state in table.values():
        for lang in state.values():
            if isinstance(lang, dict):
                values.extend(str(value) for value in lang.values())
    return values


class RenewContextBuilderTests(unittest.TestCase):
    """(a) The context-loss bug: no path may drop a fact the backend returned."""

    @classmethod
    def setUpClass(cls):
        cls.template = _Template(_read(DASHBOARD))
        # Fails loudly (not silently) when the function was renamed or removed.
        cls.builder = cls.template.function("buildRenewContext")

    def _contexts(self, cases):
        expression = "[%s]" % ",".join(
            "buildRenewContext(%s, %s)" % (json.dumps(case["source"]), json.dumps(case["prev"]))
            for case in cases)
        return _run_node(self.builder, expression)

    def test_the_builder_is_a_pure_extractable_function(self):
        self.assertIn("function buildRenewContext(source, prev)", self.builder)
        self.assertNotIn("document.", self.builder,
                         "the context builder must stay pure so it can be reasoned about")
        self.assertNotIn("{{", self.builder, "a Jinja expression would make the extraction lie")

    def test_not_verified_keeps_the_expectations_the_backend_returned(self):
        contexts = self._contexts([{
            "source": {
                "code": "renew_not_verified",
                "operation_id": "renew-1",
                "verify": {"expected": {"expiryTime": 111, "totalGB": 222}, "ok": False},
            },
            "prev": None,
        }])
        context = contexts[0]
        self.assertEqual(context["expectedExpiryTime"], 111)
        self.assertEqual(context["expectedTotalGB"], 222)
        self.assertEqual(context["operationId"], "renew-1")
        self.assertIs(context["awaitingOriginalRequest"], False)

    def test_a_duplicate_request_keeps_the_known_expectations(self):
        contexts = self._contexts([{
            "source": {"code": "renew_in_progress", "operation_id": "renew-2"},
            "prev": {"operationId": "renew-1", "expectedExpiryTime": 111,
                     "expectedTotalGB": 222},
        }])
        context = contexts[0]
        self.assertIs(context["awaitingOriginalRequest"], True)
        self.assertEqual(context["operationId"], "renew-2",
                         "the running operation's id must be used for Re-check")
        self.assertEqual(context["expectedExpiryTime"], 111,
                         "a duplicate must not null a previously known expected value")
        self.assertEqual(context["expectedTotalGB"], 222)

    def test_a_response_without_verify_expected_does_not_null_the_previous_values(self):
        # The exact production failure: the second payload carries no verify.expected,
        # and the old code replaced the whole context with nulls.
        contexts = self._contexts([{
            "source": {"code": "renew_not_verified", "operation_id": "renew-1",
                       "verify": {"ok": False}},
            "prev": {"operationId": "renew-1", "expectedExpiryTime": 111,
                     "expectedTotalGB": 222},
        }])
        self.assertEqual(contexts[0]["expectedExpiryTime"], 111)
        self.assertEqual(contexts[0]["expectedTotalGB"], 222)

    def test_explicit_nulls_in_verify_expected_do_not_erase_known_values(self):
        contexts = self._contexts([{
            "source": {"code": "renew_not_verified", "operation_id": "renew-1",
                       "verify": {"expected": {"expiryTime": None, "totalGB": None}}},
            "prev": {"operationId": "renew-1", "expectedExpiryTime": 111,
                     "expectedTotalGB": 222},
        }])
        self.assertEqual(contexts[0]["expectedExpiryTime"], 111)
        self.assertEqual(contexts[0]["expectedTotalGB"], 222)

    def test_the_submitted_values_are_the_second_fallback(self):
        contexts = self._contexts([{
            "source": {"code": "renew_not_verified", "operation_id": "renew-1",
                       "verify": {},
                       "submitted_expected": {"expiryTime": 333, "totalGB": 444}},
            "prev": None,
        }])
        self.assertEqual(contexts[0]["expectedExpiryTime"], 333)
        self.assertEqual(contexts[0]["expectedTotalGB"], 444)


class RenewTranslationTableTests(unittest.TestCase):
    """(b) One frontend-owned table, both languages, machine keys only."""

    @classmethod
    def setUpClass(cls):
        cls.template = _Template(_read(DASHBOARD))
        cls.table = _run_node(cls.template.literal("const RENEW_STATE_TEXT = "),
                              "RENEW_STATE_TEXT")
        cls.legacy = _run_node(cls.template.literal("const RENEW_LEGACY_STATE = "),
                               "RENEW_LEGACY_STATE")
        cls.message_keys = _run_node(cls.template.literal("const RENEW_MESSAGE_KEY_STATE = "),
                                     "RENEW_MESSAGE_KEY_STATE")

    def test_every_state_is_translated_in_both_languages(self):
        for state in STATES:
            self.assertIn(state, self.table, state)
            self.assertIn("fa", self.table[state], state)
            self.assertIn("en", self.table[state], state)
            for lang in ("fa", "en"):
                entry = self.table[state][lang]
                self.assertTrue(entry.get("title"), "%s/%s title" % (state, lang))
                self.assertTrue(entry.get("body"), "%s/%s body" % (state, lang))

    def test_the_required_strings_are_exact(self):
        for state, langs in REQUIRED_TEXT.items():
            for lang, (title, body) in langs.items():
                entry = self.table[state][lang]
                self.assertEqual(entry["title"], title, "%s/%s title" % (state, lang))
                if body is not None:
                    self.assertEqual(entry["body"], body, "%s/%s body" % (state, lang))

    def test_no_internal_code_is_operator_copy(self):
        for value in _flatten(self.table):
            for code in INTERNAL_CODES:
                self.assertNotIn(code, value,
                                 "internal code %r leaked into the translation table" % code)

    def test_the_legacy_verify_state_vocabulary_still_maps(self):
        # Old cached payloads speak `applied|partially_applied|not_applied|
        # observed_without_expected`; those must keep rendering, not fall to UNKNOWN.
        for legacy in ("applied", "partially_applied", "not_applied"):
            self.assertIn(legacy, self.legacy, legacy)
            self.assertIn(self.legacy[legacy], self.table, legacy)
        self.assertEqual(self.legacy["applied"], "APPLIED_ACTIVE")
        self.assertEqual(self.legacy["partially_applied"], "PARTIALLY_APPLIED")
        self.assertEqual(self.legacy["not_applied"], "NOT_APPLIED")

    def test_every_contract_message_key_maps_to_a_translated_state(self):
        for key in ("renew_applied_active", "renew_activation_pending",
                    "renew_partially_applied", "renew_not_applied",
                    "renew_auth_degraded", "renew_unknown", "renew_in_progress"):
            self.assertIn(key, self.message_keys, key)
            self.assertIn(self.message_keys[key], self.table, key)


class RenewModalCopyTests(unittest.TestCase):
    """(c) No backend prose in the modal: only the state table and one diagnostic."""

    @classmethod
    def setUpClass(cls):
        cls.template = _Template(_read(DASHBOARD))
        cls.region = cls.template.region()
        cls.diagnostic = cls.template.function("_renewDiagnostic")

    def test_the_region_is_delimited_and_the_diagnostic_builder_exists(self):
        self.assertIn("buildRenewContext", self.region)
        self.assertIn("function _renewDiagnostic", self.region)

    def test_the_title_and_body_come_from_the_state_table(self):
        self.assertIn("RENEW_STATE_TEXT[state]", self.region)
        self.assertRegex(self.region, r"title:\s*text\.title")
        self.assertRegex(self.region, r"body:\s*text\.body")
        # And what the DOM receives is that view model, not the response prose.
        self.assertRegex(self.region, r"title\.textContent\s*=\s*view\.title")
        self.assertRegex(self.region, r"body\.textContent\s*=\s*view\.body")

    def test_no_title_body_or_pill_is_assigned_from_an_error_field(self):
        self.assertNotRegex(self.region,
                            r"(?:title|body|pill|innerText|textContent)\s*[:=][^\n]*\.error")

    def test_the_only_error_read_is_the_diagnostic_builder(self):
        diagnostic_lines = set(self.diagnostic.splitlines())
        offenders = []
        for number, line in enumerate(self.region.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue  # prose, not code
            if ".error" not in line:
                continue
            if line in diagnostic_lines:
                continue
            offenders.append("%d: %s" % (number, stripped))
        self.assertEqual(offenders, [],
                         "internal errors may only be read by _renewDiagnostic():\n"
                         + "\n".join(offenders))
        self.assertIn("src.error", self.diagnostic)
        self.assertIn("parts.push", self.diagnostic)

    def test_the_not_verified_branch_hands_the_response_to_the_pending_renderer(self):
        # The response object carries operation_id + verify.expected; `data.error` is
        # localized backend prose and must not be the thing the modal is built from.
        # (The call site lives in submitRenewal(), just outside this region.)
        text = self.template.text
        self.assertIn("showRenewPending(serverId, inboundId, email, data);", text)
        self.assertNotIn("showRenewPending(serverId, inboundId, email, data.error", text)
        self.assertNotIn("showRenewInProgress(serverId, inboundId, email, data.error", text)

    def test_recheck_posts_the_operation_id(self):
        self.assertIn("operation_id: ctx.operationId", self.region)
        self.assertIn("expected_expiryTime: ctx.expectedExpiryTime", self.region)
        self.assertIn("expected_totalGB: ctx.expectedTotalGB", self.region)
        self.assertIn("awaiting_result: Boolean(ctx.awaitingOriginalRequest)", self.region)


class RenewLanguageTests(unittest.TestCase):
    """(d) The Persian table is Persian; the English table is English."""

    @classmethod
    def setUpClass(cls):
        cls.template = _Template(_read(DASHBOARD))
        cls.table = _run_node(cls.template.literal("const RENEW_STATE_TEXT = "),
                              "RENEW_STATE_TEXT")

    def test_persian_values_carry_no_english_operational_text(self):
        # EVE and API are deliberately kept in the Persian copy (a product name and an
        # interface name); no other Latin word from the blocklist may appear.
        for state in STATES:
            for value in self.table[state]["fa"].values():
                haystack = value.replace("EVE", "").replace("API", "").lower()
                for word in FA_LATIN_BLOCKLIST:
                    self.assertNotIn(word.lower(), haystack,
                                     "%s/fa still carries English copy: %r" % (state, value))

    def test_english_values_carry_no_persian_text(self):
        for state in STATES:
            for value in self.table[state]["en"].values():
                self.assertIsNone(re.search(r"[\u0600-\u06FF]", value),
                                  "%s/en carries Persian text: %r" % (state, value))

    def test_the_template_language_is_panel_lang_not_document_lang(self):
        for needle in ("const RENEW_LANG = {{", "const renewProgressIsFa = RENEW_LANG",
                       "const renewIsFa = RENEW_LANG"):
            self.assertIn(needle, self.template.text, needle)


class RenewToneTests(unittest.TestCase):
    """(e) Colour is a property of the state: pending is amber, failures are red."""

    @classmethod
    def setUpClass(cls):
        cls.template = _Template(_read(DASHBOARD))
        cls.tones = _run_node(cls.template.literal("const RENEW_STATE_TONE = "),
                              "RENEW_STATE_TONE")
        cls.style = cls.template.text

    def test_the_state_to_colour_mapping(self):
        self.assertEqual(self.tones["APPLIED_ACTIVE"], "green")
        self.assertEqual(self.tones["CONFIG_APPLIED_ACTIVATION_PENDING"], "amber",
                         "an applied config with activation pending must not be red")
        self.assertEqual(self.tones["PARTIALLY_APPLIED"], "orange")
        self.assertEqual(self.tones["NOT_APPLIED"], "red")
        self.assertEqual(self.tones["AUTH_DEGRADED"], "red")
        self.assertEqual(self.tones["UNKNOWN"], "neutral")
        self.assertEqual(self.tones["IN_PROGRESS"], "amber")

    def test_every_state_has_a_tone(self):
        for state in STATES:
            self.assertIn(state, self.tones, state)

    def test_the_template_carries_the_tone_classes_it_renders(self):
        for selector in ("#renew-success-modal.renew-tone-amber #renew-modal-title",
                         "#renew-success-modal.renew-tone-red #renew-modal-title",
                         "#renew-success-modal .renew-pill-amber",
                         "#renew-success-modal .renew-pill-orange",
                         "#renew-success-modal .renew-badge-amber",
                         "#renew-success-modal .renew-badge-red"):
            self.assertIn(selector, self.style, selector)

    def test_the_pending_badges_are_not_rendered_from_a_boolean_ok(self):
        region = self.template.region()
        resolve = self.template.function("renewStateFromPayload")
        self.assertIn("renewStateFromPayload", region)
        self.assertLess(resolve.index("final_state"), resolve.index("verify.ok"),
                        "final_state must decide the state before verify.ok is consulted")
        self.assertRegex(region, r"tone:\s*RENEW_STATE_TONE\[state\]",
                         "the tone must come from the state, not from verify.ok")


if __name__ == "__main__":
    unittest.main()
