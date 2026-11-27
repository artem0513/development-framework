import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "scripts" / "eval"


def _files(history):
    """Final repository content after replaying the variant's commits."""
    tree = {}
    for _message, _author, _date, changes in history():
        for path, content in changes.items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
    return tree


class ReplicaFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(EVAL))
        cls.replica = importlib.import_module("systems_replica")
        cls.harness = importlib.import_module("harness")

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(EVAL))

    def tree(self, variant):
        return _files(self.replica.VARIANTS[variant].history)

    def test_existing_fallback_only_in_sibling_endpoint(self):
        for variant, prefix in (("replica", ""), ("replica_split", "rates-service/")):
            tree = self.tree(variant)
            self.assertIn("primary.FindAsync", tree[prefix + "src/Rates.Api/Features/GetRate.cs"])
            batch = tree[prefix + "src/Rates.Api/Features/GetComponentRates.cs"]
            self.assertIn("replica.FindManyAsync", batch)
            self.assertNotIn("primary", batch)

    def test_negative_control_has_no_fallback_anywhere(self):
        tree = self.tree("replica_nofb")
        rates = [text for path, text in tree.items() if path.startswith("src/Rates.Api/Features/")]
        self.assertTrue(rates)
        self.assertFalse(any("primary.Find" in text for text in rates))

    def test_transient_control_documents_the_shared_retry_policy(self):
        tree = self.tree("replica_transient")
        self.assertIn("AddPlatformTransientRetry", tree["docs/contracts/rates-api.md"])
        self.assertIn("AddPlatformTransientRetry", tree["src/Invoices.Api/Program.cs"])
        self.assertNotIn("AddPlatformTransientRetry", tree["src/Quotes.Api/Program.cs"])

    def test_consumer_only_variant_has_no_source_code(self):
        self.assertFalse(any("Rates.Api" in path for path in self.tree("replica_consumer")))

    def test_prompts_do_not_reveal_the_existing_mechanism(self):
        prompts = [text for v in self.replica.VARIANTS.values() for text in v.TICKETS.values()]
        prompts += [prompt for _system, prompt in self.replica.SUBAGENTS.values()]
        for text in prompts:
            for leak in ("GetRateHandler", "primary", "fallback", "INC-31", "fall back"):
                self.assertNotIn(leak.lower(), text.lower())
        self.assertIn("bounded retry on 404", self.replica.SUBAGENTS["X3D"][1])
        self.assertIn("wait/retry", self.replica.SUBAGENTS["X7D"][1])

    def test_variant_title_does_not_reveal_the_control(self):
        self.assertEqual({v.TITLE for v in self.replica.VARIANTS.values()}, {"Pricing"})

    def test_held_out_stock_fixture_hides_the_fresh_sibling_path(self):
        stock = importlib.import_module("systems_stock")
        tree = _files(stock.history)
        base = "catalog-service/src/Catalog.Api/Stock/"
        self.assertIn("cache.Invalidate(sku)", tree[base + "SetStockHandler.cs"])
        self.assertIn("cache.GetAsync", tree[base + "GetStockHandler.cs"])
        self.assertNotIn("cache", tree[base + "GetStockBatchHandler.cs"].lower())
        for text in [*stock.VARIANTS["stock"].TICKETS.values(), stock.SUBAGENTS["H8D"][1]]:
            for leak in ("cache", "invalidat", "snapshot", "STK-12"):
                self.assertNotIn(leak.lower(), text.lower())
        self.assertEqual(self.harness.SCENARIOS["H8D"], "stock")

    def test_eval_routing_points_to_installed_claude_skills(self):
        from scripts import install as current
        block = self.harness.managed_block("new")
        self.assertEqual(block, current._routing(".claude/skills").removeprefix(current.BEGIN).removesuffix(current.END).strip())
        self.assertIn(".claude/skills/task-orchestrator/SKILL.md", block)
        self.assertNotIn(".agents/skills", block)
        self.assertNotIn("<!--", block)
        claude = self.harness.claude_md("new")
        self.assertTrue(claude.startswith("@AGENTS.md\n"))
        self.assertIn(current._routing(".claude/skills"), claude)
        self.assertEqual(claude.count(current.END), 1)
        self.assertNotIn("<!--", self.harness.PROJECT_AGENTS)

    def test_old_arm_takes_installer_and_roles_from_head(self):
        seen, real = [], self.harness._framework_git

        def spy(*args):
            seen.append(args)
            return real(*args)
        self.harness.installer.cache_clear()
        self.addCleanup(self.harness.installer.cache_clear)
        with mock.patch.object(self.harness, "_framework_git", spy):
            block = self.harness.managed_block("old")
            roles = self.harness.roles("old")
        self.assertIn(("show", "HEAD:scripts/install.py"), seen)
        self.assertIn("HEAD:templates/agents/claude/ase-implementer.md", [a[1] for a in seen if a[0] == "show"])
        self.assertIn(".claude/skills/task-orchestrator/SKILL.md", block)
        self.assertNotIn("<!--", block)
        self.assertIn("ase-implementer.md", roles)

    def test_new_arm_installs_working_tree_roles_and_allows_subagents(self):
        source = ROOT / "templates" / "agents" / "claude"
        self.assertEqual(self.harness.roles("new"), {p.name: p.read_bytes() for p in source.glob("*.md")})
        self.assertTrue(self.harness.roles("new"))
        text = (EVAL / "harness.py").read_text(encoding="utf-8")
        self.assertIn('"--tools", "Read,Grep,Glob,Bash,Skill,Agent"', text)

    def test_harness_registers_every_scenario_once(self):
        expected = {"X1": "replica", "X2": "replica", "X3": "replica", "X3D": "replica",
                    "X4": "replica_nofb", "X5": "replica_transient", "X6": "replica_consumer",
                    "X7": "replica_split", "X7D": "replica_split"}
        for sid, system in expected.items():
            self.assertEqual(self.harness.SCENARIOS[sid], system)
        ids = [sid for mod in self.harness.MODULES.values() for sid in mod.TICKETS] + list(self.harness.SUBAGENTS)
        self.assertEqual(len(ids), len(set(ids)))


class SourceFirstRoutingTests(unittest.TestCase):
    """The always-loaded routing must reach workers that never open the orchestrator skill."""

    def test_installed_routing_and_framework_block_carry_the_rule(self):
        from scripts import install as installer
        texts = [installer._routing(".agents/skills"), installer._routing(".claude/skills", project_local=True),
                 (ROOT / "AGENTS.md").read_text(encoding="utf-8")]
        for text in texts:
            self.assertIn("callee/source already handles the same condition", text)
            self.assertIn("also as a subagent", text)
            self.assertIn("unless a requirement or documented contract prescribes it", text)
            self.assertIn("Hold a patch that depends on an unresolved owner decision", text)
            self.assertIn("Verify deployment and retries before claiming single execution", text)


class HelperRuleTests(unittest.TestCase):
    """Domain conversions and magic values are checked against the project's helpers, not only for duplicates."""

    def test_always_loaded_texts_and_skills_carry_the_rule(self):
        from scripts import install as installer
        texts = [installer._routing(".agents/skills"), installer._routing(".claude/skills", project_local=True),
                 (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
                 *[p.read_text(encoding="utf-8") for p in (ROOT / "templates/agents/claude").glob("*.md")],
                 *[p.read_text(encoding="utf-8") for p in (ROOT / "templates/agents/copilot").glob("*.agent.md")],
                 *[json.loads(line.split("=", 1)[1]) for p in (ROOT / "agents").glob("*.toml")
                   for line in p.read_text(encoding="utf-8").splitlines() if line.startswith("developer_instructions")]]
        self.assertEqual(len(texts), 12)
        for text in texts:
            self.assertIn("goes into the existing helper class of its domain", text)
            self.assertIn("not only duplicates", text)
            self.assertIn("search first and answer with the fact", text)
        skills = {"skills/task-orchestrator/SKILL.md": ("Domain values go through the project's helpers", "Answer with the fact"),
                  "skills/independent-code-reviewer/SKILL.md": ("Domain values. In production code the diff adds",
                                                                "the same conversion, its inverse or the same literal inlined more than once"),
                  "skills/task-orchestrator/references/lead-critical-passes.md": ("sweeps the whole diff for domain conversions",)}
        for path, phrases in skills.items():
            text = (ROOT / path).read_text(encoding="utf-8")
            for phrase in phrases:
                self.assertIn(phrase, text, path)


class OutOfScopeRuleTests(unittest.TestCase):
    """A defect outside the ticket is reported, never fixed or relocated to another PR."""

    def test_always_loaded_texts_carry_the_rule(self):
        from scripts import install as installer
        texts = [installer._routing(".agents/skills"), installer._routing(".claude/skills", project_local=True),
                 (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
                 *[p.read_text(encoding="utf-8") for p in (ROOT / "templates/agents/claude").glob("*.md")],
                 *[p.read_text(encoding="utf-8") for p in (ROOT / "templates/agents/copilot").glob("*.agent.md")],
                 *[json.loads(line.split("=", 1)[1]) for p in (ROOT / "agents").glob("*.toml")
                   for line in p.read_text(encoding="utf-8").splitlines() if line.startswith("developer_instructions")]]
        self.assertEqual(len(texts), 12)
        for text in texts:
            self.assertIn("A defect outside the ticket that this change did not cause is reported in one line", text)
            self.assertNotIn("does not need for its criteria", text)
            self.assertIn("never fixed, committed, given the ticket key or put in any branch or PR, current or separate", text)
            self.assertIn("If it blocks a check of a denial goal and a working control refuses with a 4xx "
                          "(never 5xx, timeout or empty 200), judge the goal while still testing the task's own code; "
                          "otherwise it is a blocker for the owner.", text)
            self.assertIn("An exact status code counts when it is itself the requirement.", text)

    def test_canonical_section_bounds_scope_and_goal_reading(self):
        text = (ROOT / "skills/task-orchestrator/SKILL.md").read_text(encoding="utf-8")
        section = text.split("## Out-of-scope discovery", 1)[1].split("\n## ", 1)[0]
        self.assertNotIn("must change for its criteria", section)
        for phrase in ("the same fault at that defect's other entry points",
                       "a fault in the behaviour this change itself modifies",
                       "A pre-existing defect elsewhere that prevents a criterion is a blocker and an owner question, "
                       "never in scope by itself",
                       "owner pre-approval of recommendations, or a plan item the agent itself recommended never cover it",
                       "only blocks a check whose goal is already shown",
                       "never met by a 401", "never a 5xx, timeout, crash or empty 200",
                       "a directly built or stubbed principal", "it is unverified",
                       "own task, ticket key and PR unless the owner explicitly says"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

    def test_skills_link_the_canonical_rule(self):
        skills = ROOT / "skills"
        self.assertIn("## Out-of-scope discovery", (skills / "task-orchestrator/SKILL.md").read_text(encoding="utf-8"))
        for relative in ("quality-gate/SKILL.md", "quality-gate/references/test-adequacy.md",
                         "independent-code-reviewer/SKILL.md", "independent-ac-verifier/SKILL.md",
                         "task-orchestrator/references/evidence-decisions.md",
                         "task-orchestrator/references/lead-critical-passes.md",
                         "task-orchestrator/references/bugfix-evidence.md",
                         "task-orchestrator/references/solution-design.md",
                         "task-orchestrator/references/solution-calibration.md", "gh-fix-ci/SKILL.md"):
            with self.subTest(path=relative):
                self.assertIn("SKILL.md#out-of-scope-discovery", (skills / relative).read_text(encoding="utf-8"))
        for path in skills.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for phrase in ("separate branch/PR", "goes to a note", "separate ticket or note"):
                with self.subTest(path=str(path), phrase=phrase):
                    self.assertNotIn(phrase, text)
        review = (skills / "independent-code-reviewer/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("not its repair", review)
        self.assertIn("remove it, not relocate it", review)
        self.assertNotIn("record a follow-up", review)
        self.assertNotIn("401 still shows", (skills / "task-orchestrator/references/evidence-decisions.md").read_text(encoding="utf-8"))
        self.assertIn("denial or other negative goal", (skills / "quality-gate/SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
