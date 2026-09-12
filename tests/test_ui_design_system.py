"""UI design-system guard.

The Servers option added in this feature shipped with a raw checkbox that inherited
the form-input styling and looked broken. These tests keep the project's component
vocabulary and the UI skill honest, and they guard the stylesheet encoding: a
previous shell append had written ~28 KB of CSS as UTF-16LE, so those rules silently
stopped applying.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE_CSS = os.path.join(REPO_ROOT, "static", "style.css")
SERVERS_HTML = os.path.join(REPO_ROOT, "templates", "servers.html")
SKILL = os.path.join(REPO_ROOT, ".agents", "skills", "eve-ui", "SKILL.md")
SKILL_MIRROR = os.path.join(REPO_ROOT, ".dsh", "skills", "eve-ui", "SKILL.md")
DESIGN_DOC = os.path.join(REPO_ROOT, "docs", "UI_DESIGN_SYSTEM.md")
NAME_FLAGS_JS = os.path.join(REPO_ROOT, "static", "name-flags.js")
FLAGS_DIR = os.path.join(REPO_ROOT, "static", "flags", "4x3")
BASE_HTML = os.path.join(REPO_ROOT, "templates", "base.html")
SUBSCRIPTION_HTML = os.path.join(REPO_ROOT, "templates", "subscription.html")
DASHBOARD_HTML = os.path.join(REPO_ROOT, "templates", "dashboard.html")
AGENT_RULE_FILES = (
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", "QWEN.md",
    ".github/copilot-instructions.md", ".cursor/rules/codebase-memory.mdc",
    ".clinerules/codebase-memory.md", ".windsurfrules",
)
REQUIRED_DOC_SECTIONS = (
    "Design Identity", "Source of Truth", "Core Colors", "Theme", "Typography",
    "Surfaces", "Glassmorphism", "Layout", "Cards", "Status Semantics", "Buttons",
    "Icons", "Forms", "Modals", "Tables", "Mobile First", "RTL", "Motion",
    "Performance", "Accessibility", "What to Avoid", "Change Workflow", "Agent Rule",
)

REQUIRED_TOKENS = (
    "--primary", "--primary-dark", "--secondary", "--success", "--warning",
    "--danger", "--bg-dark", "--bg-card", "--bg-card-hover", "--text-primary",
    "--text-secondary", "--border-color", "--shadow", "--shadow-lg",
)
REQUIRED_COMPONENTS = (
    ".btn", ".btn-primary", ".btn-secondary", ".form-group", ".form-select",
    ".checkbox-label", ".checkmark", ".toggle-switch", ".slider", ".badge",
    ".field-note", ".field-note-ok", ".field-note-warn", ".label-note",
    ".server-badges .badge.warning", ".modal", ".modal-overlay", ".hidden",
)
RE_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
RE_QUOTED = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")
RE_DECLARATION = re.compile(r"^\s+[a-z-]+:\s*[^;]+;\s*$")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class StylesheetHealthTests(unittest.TestCase):
    def test_the_stylesheet_is_utf8_without_nul_bytes(self):
        raw = open(STYLE_CSS, "rb").read()
        self.assertNotIn(b"\x00", raw, "style.css regained NUL bytes (UTF-16 append?)")
        raw.decode("utf-8")  # raises if the file is not valid UTF-8

    def test_the_stylesheet_has_no_utf16_marker_bytes(self):
        raw = open(STYLE_CSS, "rb").read()
        # A UTF-16LE ASCII run looks like 'x\x00y\x00'; the NUL check above covers
        # it, this asserts the file is not mostly CR/LF-free either.
        self.assertGreater(raw.count(b"\n"), 1000)

    def test_tokens_and_components_still_exist(self):
        css = _read(STYLE_CSS)
        missing = [name for name in REQUIRED_TOKENS if (name + ":") not in css]
        missing += [name for name in REQUIRED_COMPONENTS if name not in css]
        self.assertEqual(missing, [], "missing from style.css: %s" % missing)


class StylesheetStructureTests(unittest.TestCase):
    """A half-repaired stylesheet parses but its rules silently stop matching.

    The monitor block was once written as UTF-16; recovering it by decoding the
    bytes the wrong way left U+0A0D/U+0D00 on every line of the block. The file
    stayed valid UTF-8 with no NUL bytes, so the health tests above passed while
    every recovered rule was dead: the stray ident rode on the property name of
    the declaration that followed it.
    """

    # The only non-ASCII this stylesheet legitimately contains: Persian text in
    # comments, ZWNJ, and a handful of glyphs (dash, arrow, approx, box drawing,
    # U+25BE disclosure triangle). Extend deliberately; a wrong byte-order decode
    # of the file lands outside these ranges (U+0A0D/U+0D00 for the monitor block).
    ALLOWED_NON_ASCII = (
        (0x0600, 0x06FF), (0x200C, 0x200C), (0x2014, 0x2014), (0x2192, 0x2192),
        (0x2248, 0x2248), (0x2500, 0x2500), (0x25BE, 0x25BE),
    )

    @classmethod
    def setUpClass(cls):
        cls.css = _read(STYLE_CSS)

    def test_every_non_ascii_character_is_an_expected_one(self):
        def allowed(code):
            return any(low <= code <= high for low, high in self.ALLOWED_NON_ASCII)

        unexpected = sorted({ord(ch) for ch in self.css if ord(ch) > 126 and not allowed(ord(ch))})
        self.assertEqual(
            unexpected, [],
            "unexpected non-ASCII in style.css: %s (a wrong-encoding decode shows up "
            "here; add the range to ALLOWED_NON_ASCII only if the character is intended)"
            % [hex(code) for code in unexpected])

    def test_braces_are_balanced_with_no_stray_closing_brace(self):
        depth = 0
        stray = []
        code = RE_QUOTED.sub("", RE_COMMENT.sub("", self.css))
        for number, line in enumerate(code.splitlines(), 1):
            depth += line.count("{") - line.count("}")
            if depth < 0:
                stray.append(number)
                depth = 0
        self.assertEqual(stray, [], "stray closing brace on lines %s" % stray)
        self.assertEqual(depth, 0, "unclosed block at end of style.css")

    def test_the_recovered_monitor_rules_are_live_css(self):
        """The rules the UTF-16 damage hit must still be real declarations."""
        lines = self.css.replace("\r\n", "\n").split("\n")
        start = lines.index(".monitor-compact-row {")
        body = []
        for line in lines[start + 1:]:
            if line.strip() == "}":
                break
            body.append(line)
        self.assertIn("    display: flex;", body)
        malformed = [line for line in body
                     if line.strip() and not RE_DECLARATION.match(line)]
        self.assertEqual(malformed, [], "damaged declarations: %s" % malformed)

    def test_the_accordion_summary_keeps_its_list_style(self):
        """list-style:none sits in its rule, not orphaned after .hidden."""
        block = self.css.split(".monitor-accordion summary {", 1)[1].split("}", 1)[0]
        self.assertIn("list-style: none;", block)


class ServersOptionMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = _read(SERVERS_HTML)

    def test_the_checkbox_uses_the_project_component(self):
        marker = 'id="server-allow-insecure"'
        index = self.html.find(marker)
        self.assertNotEqual(index, -1, "the allow-insecure option is missing")
        snippet = self.html[max(0, index - 220):index + 220]
        self.assertIn('class="checkmark"', snippet,
                      "a raw checkbox inside a form group renders unstyled")
        self.assertIn('class="checkbox-label"', snippet,
                      "the label must carry checkbox-label so form-label CSS skips it")
        self.assertNotIn("display:flex", snippet.replace(" ", ""))

    def test_the_warning_is_hidden_by_the_class_not_inline_display(self):
        self.assertIn('id="server-allow-insecure-warning"', self.html)
        warning = self.html.split('id="server-allow-insecure-warning"', 1)[1][:220]
        self.assertIn("hidden", warning)
        self.assertNotIn("style.display", self.html,
                         "use the .hidden class instead of style.display")

    def test_the_insecure_badge_uses_the_warning_variant(self):
        line = next((item for item in self.html.splitlines()
                     if "insecureBadge = createBadge" in item), "")
        self.assertIn("badge warning", line,
                      "the insecure badge must use the amber warning variant")
        self.assertNotIn("badge inactive", line,
                         "inactive is the red Disabled variant, not an informational one")


class UiSkillTests(unittest.TestCase):
    """The skill is the design-system contract; it must not drift from the CSS."""

    def setUp(self):
        self.assertTrue(os.path.isfile(SKILL), "the eve-ui skill is missing")
        self.skill = _read(SKILL)
        self.css = _read(STYLE_CSS)

    def test_frontmatter_is_valid(self):
        self.assertTrue(self.skill.startswith("---"))
        front = self.skill.split("---", 2)[1]
        self.assertIn("name: eve-ui", front)
        description = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
        self.assertIsNotNone(description, "description is required")
        self.assertGreater(len(description.group(1).strip()), 20)

    def test_every_identifier_it_claims_exists_in_the_stylesheet(self):
        claimed = set(re.findall(r"`(\.[a-z][a-z0-9-]*)`", self.skill))
        claimed |= set(re.findall(r"`(--[a-z][a-z0-9-]*)`", self.skill))
        missing = sorted(name for name in claimed if name not in self.css)
        self.assertEqual(missing, [], "the skill documents selectors that do not exist: %s"
                         % missing)

    def test_it_warns_about_the_checkbox_and_encoding_traps(self):
        self.assertIn("checkbox-label", self.skill)
        self.assertIn("checkmark", self.skill)
        self.assertIn("UTF-16", self.skill)
        self.assertIn("hidden", self.skill)



class DesignSystemDocTests(unittest.TestCase):
    """The design system is documented, and the document does not invent selectors."""

    @classmethod
    def setUpClass(cls):
        cls.doc = _read(DESIGN_DOC)
        cls.css = _read(STYLE_CSS)

    def test_every_documented_section_is_present(self):
        missing = [name for name in REQUIRED_DOC_SECTIONS
                   if ("## " + name) not in self.doc]
        self.assertEqual(missing, [], "missing design-system sections: %s" % missing)

    def test_every_identifier_it_claims_exists_in_the_stylesheet(self):
        claimed = set(re.findall(r"`(\.[a-z][a-z0-9-]*)`", self.doc))
        claimed |= set(re.findall(r"`(--[a-z][a-z0-9-]*)`", self.doc))
        missing = sorted(name for name in claimed if name not in self.css)
        self.assertEqual(missing, [],
                         "the design doc documents selectors that do not exist: %s" % missing)

    def test_it_records_the_core_rules(self):
        for needle in ("checkbox-label", "checkmark", "hidden", "UTF-16",
                       "Do not introduce a parallel visual system"):
            self.assertIn(needle, self.doc)


class SkillMirrorTests(unittest.TestCase):
    """The same skill lives in both discovery roots; the copies must not drift."""

    def test_both_skill_locations_exist_and_are_identical(self):
        self.assertTrue(os.path.isfile(SKILL), "the .agents skill is missing")
        self.assertTrue(os.path.isfile(SKILL_MIRROR), "the .dsh mirror is missing")
        with open(SKILL, "rb") as handle:
            canonical = handle.read()
        with open(SKILL_MIRROR, "rb") as handle:
            mirror = handle.read()
        self.assertEqual(canonical, mirror, "the eve-ui skill copies have drifted")


class AgentUiRuleTests(unittest.TestCase):
    """Every agent entry point must send UI work to the skill and the doc."""

    REQUIRED = ".agents/skills/eve-ui/SKILL.md"

    def test_every_agent_instruction_file_points_at_the_skill(self):
        for name in AGENT_RULE_FILES:
            path = os.path.join(REPO_ROOT, name)
            self.assertTrue(os.path.isfile(path), "%s is missing" % name)
            text = _read(path)
            self.assertIn(self.REQUIRED, text, name)
            self.assertIn("Do not introduce a parallel visual system", text, name)


class DesignSystemRatchetTests(unittest.TestCase):
    """The templates predate the contract, so the drift may not grow.

    `scripts/ui_design_audit.py` counts the inline styles, hardcoded colours,
    `style.display` toggles, bare checkboxes and emoji per template. The counts
    recorded in `tests/ui_design_baseline.json` are the ceiling: a change may
    lower them (rerun the tool with --write-baseline) but never raise them.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ui_design_audit", os.path.join(REPO_ROOT, "scripts", "ui_design_audit.py"))
        cls.audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.audit)
        cls.counts = cls.audit.scan_templates()

    def test_the_audit_tool_and_baseline_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "scripts", "ui_design_audit.py")))
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "tests",
                                                    "ui_design_baseline.json")))

    def test_no_template_exceeds_the_recorded_baseline(self):
        problems = self.audit.check(self.counts)
        self.assertEqual(problems, [], "design-system drift grew:\n" + "\n".join(problems))

    def test_the_safe_token_migration_is_fully_applied(self):
        """No inline colour still spells a value that is exactly a token."""
        leftovers = {}
        for name in sorted(os.listdir(os.path.join(REPO_ROOT, "templates"))):
            if not name.endswith(".html"):
                continue
            text = _read(os.path.join(REPO_ROOT, "templates", name))
            remaining = self.audit.remaining_token_migrations(text)
            if remaining:
                leftovers[name] = remaining
        self.assertEqual(leftovers, {}, "run: python scripts/ui_design_audit.py "
                                        "--fix-colors --apply (%s)" % leftovers)

    def test_the_tool_only_migrates_themes_safe_values(self):
        style, changes, _ = self.audit.tokenize_inline_style("color:#ef4444;font-size:0.8rem")
        self.assertEqual(style, "color: var(--danger);font-size:0.8rem")
        self.assertEqual(len(changes), 1)
        # A fixed dark surface keeps its ink: --text-secondary flips in the light theme.
        _, changes, skipped = self.audit.tokenize_inline_style("color:#94a3b8",
                                                              allow_theme_tokens=False)
        self.assertEqual(changes, [])
        self.assertEqual(len(skipped), 1)


class CountryFlagTests(unittest.TestCase):
    """Panel names carry flag emoji; the flag must never render as letters.

    Windows has no glyphs for a regional indicator pair, so "🇩🇪" is drawn as "DE"
    and an inbound remark reads like a country code instead of a flag. The shared
    component renders the self-hosted SVG instead, on every platform.
    """

    def setUp(self):
        self.script = _read(NAME_FLAGS_JS)
        self.css = _read(STYLE_CSS)
        self.base = _read(BASE_HTML)
        self.subscription = _read(SUBSCRIPTION_HTML)
        self.dashboard = _read(DASHBOARD_HTML)

    def test_the_component_is_loaded_wherever_panel_names_appear(self):
        self.assertIn("name-flags.js", self.base, "base.html must load it before page scripts")
        self.assertIn("flags/4x3/", self.base, "the flag asset base must be passed to the component")
        self.assertIn("name-flags.js", self.subscription,
                      "the standalone subscription page must load it too")

    def test_the_local_svg_is_the_only_flag_source(self):
        self.assertIn(".country-flag", self.css)
        self.assertIn("/static/flags/4x3/", self.script)
        self.assertIn(".svg", self.script)

    def test_the_versioned_static_url_keeps_its_query_after_the_file(self):
        # Flask stamps static URLs as "/static/flags/4x3/?v=<hash>". Appending the
        # code to that raw base requests "/static/flags/4x3/?v=...de.svg", which
        # 404s, so the flag silently fell back to the emoji (the letters on
        # Windows). The code and ".svg" must come before the query.
        self.assertIn("indexOf('?')", self.script)
        self.assertIn(".svg' + query", self.script)

    def test_the_emoji_is_never_the_rendered_fallback(self):
        # The old markup kept the emoji in the DOM and swapped to it whenever the
        # image was hidden or failed, which is exactly how "DE" reached the screen.
        self.assertNotIn("ib-country-flag-native", self.dashboard)
        self.assertNotIn("country-flag-missing img", self.css)
        self.assertIn("country-flag-missing", self.css, "a missing asset hides the badge")
        self.assertNotIn("flag-fallback", self.script)

    def test_the_dashboard_uses_the_shared_component(self):
        self.assertIn("EveFlags.html(", self.dashboard)
        self.assertNotIn("_regionalFlagCode", self.dashboard)

    def test_the_reported_country_flags_exist(self):
        # The codes from the report: Iran, US, Turkey, Latvia, UAE, Netherlands, Sweden.
        for code in ("ir", "us", "tr", "lv", "ae", "nl", "se"):
            self.assertTrue(os.path.isfile(os.path.join(FLAGS_DIR, "%s.svg" % code)),
                            "static/flags/4x3/%s.svg is missing" % code)


class ActionIconTests(unittest.TestCase):
    """A `fill: currentColor` on `.action-btn svg` destroys every outline icon.

    The legacy monitor rows declared a bare `.action-btn` (40px, then 48px) and a
    `fill: currentColor` icon rule AFTER the base component, so the cascade won and
    the panel's stroke-only SVGs became solid blobs at the wrong size.
    """

    @classmethod
    def setUpClass(cls):
        cls.css = _read(STYLE_CSS)

    @staticmethod
    def _rules(css):
        """(selector, body) for every rule, including rules nested in @media."""
        code = RE_COMMENT.sub("", css)
        return [(selector.strip(), body) for selector, body in
                re.findall(r"([^{}]+)\{([^{}]*)\}", code)]

    def test_no_action_button_rule_fills_its_icon(self):
        offenders = []
        for selector, body in self._rules(self.css):
            if ".action-btn svg" not in selector:
                continue
            fill = re.search(r"fill\s*:\s*([^;]+)", body)
            if fill and fill.group(1).strip() != "none":
                offenders.append((selector[:70], fill.group(0).strip()))
        self.assertEqual(offenders, [], "an action icon is filled: %s" % offenders)

    def test_the_outline_utility_covers_action_buttons(self):
        self.assertIn(".eve-icon-outline", self.css)
        utility = [body for selector, body in self._rules(self.css)
                   if ".eve-icon-outline" in selector and ".action-btn svg" in selector]
        self.assertTrue(utility, "the outline rule must cover .action-btn svg")
        self.assertIn("fill: none", utility[0])

    def test_only_the_base_component_targets_a_bare_action_button(self):
        bare = [(selector, body) for selector, body in self._rules(self.css) if selector == ".action-btn"]
        self.assertEqual(len(bare), 1, "bare .action-btn rules: %s" % [s for s, _ in bare])
        self.assertNotIn("flex: 1", bare[0][1], "a global flex:1 stretches every action button")

    def test_every_template_action_icon_is_outline(self):
        offenders = []
        for name in sorted(os.listdir(os.path.join(REPO_ROOT, "templates"))):
            if not name.endswith(".html"):
                continue
            text = _read(os.path.join(REPO_ROOT, "templates", name))
            for match in re.finditer(r'class="[^"]*action-btn[^"]*"(.{0,220}?)<svg([^>]*)>', text, re.S):
                if 'fill="none"' not in match.group(2):
                    offenders.append("%s: %s" % (name, match.group(2).strip()[:70]))
        self.assertEqual(offenders, [], "action icons must stay outline: %s" % offenders)


class MutationResponseWiringTests(unittest.TestCase):
    """Phase 3: the dashboard adopts the verified state a mutation response carries.

    Without this the card and the search index only change on the next poll, which is
    the "subscription link is new, dashboard is old" complaint.
    """

    @classmethod
    def setUpClass(cls):
        cls.dashboard = _read(DASHBOARD_HTML)
        match = re.search(r"function applyClientMutation\(.*?\n    \}", cls.dashboard, re.S)
        cls.helper = match.group(0) if match else ""

    def test_the_adoption_helper_exists(self):
        self.assertTrue(self.helper, "applyClientMutation() is missing from the dashboard")

    def test_the_helper_reads_the_canonical_contract(self):
        for field in ("client_state", "mutation", "server_revision", "deleted"):
            self.assertIn(field, self.helper, field)
        for field in ("total_bytes", "expiry_time", "used_up", "used_down"):
            self.assertIn(field, self.helper, field)

    def test_it_keeps_the_delta_cursor_in_step(self):
        self.assertIn("snapshotRevision", self.helper)

    def test_an_unverified_payload_is_not_adopted(self):
        self.assertIn("if (!state) return false;", self.helper)

    def test_both_renew_paths_adopt_the_response(self):
        # The renew success path and the re-check path.
        self.assertGreaterEqual(self.dashboard.count("applyClientMutation(data,"), 2)


class ClientStoreFunnelTests(unittest.TestCase):
    """Phase 8: one normalized store feeds cards, search, filters and counters.

    Before this, a local enable/disable patched the store and invalidated the search
    index but left the per-inbound counters (which the card badge renders) at their
    old values until the next poll, so the card and the search disagreed.
    """

    MUTATORS = ("patchLocalClient", "removeLocalClient", "applySnapshotFull",
                "applySnapshotDelta")

    @classmethod
    def setUpClass(cls):
        cls.dashboard = _read(DASHBOARD_HTML)

    def _body(self, name):
        match = re.search(r"function %s\(.*?\n    \}" % name, self.dashboard, re.S)
        self.assertIsNotNone(match, "missing from the dashboard: %s()" % name)
        return match.group(0)

    def test_every_store_mutator_funnels_through_client_store_changed(self):
        for name in self.MUTATORS:
            self.assertIn("clientStoreChanged(", self._body(name), name)

    def test_the_funnel_invalidates_search_recomputes_counters_and_renders(self):
        body = self._body("clientStoreChanged")
        self.assertIn("invalidateClientSearchIndex()", body)
        self.assertIn("recomputeLocalCounters()", body)
        self.assertIn("applyFilters()", body)

    def test_the_counter_recompute_mirrors_the_backend(self):
        # Same derivation as panel/jobs/refresh.py::_recompute_cached_server_stats.
        body = self._body("recomputeLocalCounters")
        self.assertIn("client_count = clients.length", body)
        self.assertIn("active_count = clients.filter", body)

    def test_the_mutators_do_not_render_behind_the_funnel(self):
        for name in ("patchLocalClient", "removeLocalClient"):
            self.assertNotIn("applyFilters();", self._body(name),
                             "%s renders without going through the funnel" % name)


if __name__ == "__main__":
    unittest.main()
