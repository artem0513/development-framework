"""Read-only audit of an explicitly requested project-only migration.

Reads private configuration locally; prints check outcomes, never its contents.
This is configuration evidence, not a replacement for host discovery or review.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def enabled_for(entries, names, path):
    enabled = True
    for entry in entries:
        if (entry.get("name") in names or
                (isinstance(entry.get("path"), str) and
                 canonical(entry["path"]) == canonical(path))):
            if not isinstance(entry.get("enabled"), bool):
                raise ValueError("Invalid matching skill selector")
            enabled = entry["enabled"]
    return enabled


def unrelated_config(config):
    return {key: value for key, value in config.items() if key != "skills"}


def unrelated_skill_entries(config, plan):
    """Retain selector values, duplicates and ordering outside the explicit scope."""
    paths = {canonical(item["path"]) for item in plan["skill_observations"]}
    names = set(plan.get("engineering_names", []))
    for item in plan["skill_observations"]:
        names.add(item["name"])
        if item.get("prefix"):
            names.add(item["prefix"] + ":" + item["name"])
    return [entry for entry in config.get("skills", {}).get("config", [])
            if entry.get("name") not in names and not
            (isinstance(entry.get("path"), str) and canonical(entry["path"]) in paths)]


def read_config(path):
    return tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def skill_name(path):
    match = re.search(r"(?m)^name:\s*([^\r\n]+)", path.read_text(encoding="utf-8-sig"))
    if not match:
        raise ValueError("Skill entry point has no name")
    return match[1].strip().strip("\"'")


def without_block(text, label):
    pattern = r"<!-- " + re.escape(label) + r" begin -->.*?<!-- " + re.escape(label) + r" end -->"
    return re.sub(pattern, "", text, flags=re.S).strip()


def audit_install_manifest(manifest):
    """Backend-neutral read-only hash audit, explicitly not a routing test."""
    files = manifest.get("files", manifest.get("owned_files", []))
    mismatched = []
    for item in files:
        path = Path(item["path"])
        if (not path.is_file() or path.is_symlink() or path.stat().st_nlink > 1
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]):
            mismatched.append(str(path))
    checks = {"owned_inventory_present": bool(files), "owned_file_hashes_match": not mismatched,
              "project_scope_explicit": manifest.get("scope") != "project" or bool(manifest.get("project"))}
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "mismatched_files": mismatched, "targets": manifest.get("targets", []),
            "scope": "owned file/hash audit only; no discovery, model routing or live backend guarantee"}


def run(home, project, backup, domain_project=None):
    plan = json.loads((backup / "scope-plan.json").read_text(encoding="utf-8"))
    before = read_config(backup / "config.toml")
    current = read_config(home / "config.toml")
    local = read_config(project / ".codex/config.toml")
    global_entries = current.get("skills", {}).get("config", [])
    project_entries = local.get("skills", {}).get("config", [])
    observations = plan["skill_observations"]
    disabled = []
    for item in observations:
        names = {item["name"]}
        if item.get("prefix"):
            names.add(item["prefix"] + ":" + item["name"])
        disabled.append(not enabled_for(global_entries, names, Path(item["path"])))
    source_skills = Path(__file__).resolve().parents[1] / "skills"
    selected = list(source_skills.glob("*/SKILL.md"))
    local_enabled = []
    local_equal = []
    for source in selected:
        copied = project / ".agents/skills" / source.parent.name / "SKILL.md"
        local_enabled.append(copied.is_file() and enabled_for(
            global_entries + project_entries, {source.parent.name}, copied))
        for source_file in source.parent.rglob("*"):
            if (source_file.is_file() and "__pycache__" not in source_file.parts
                    and source_file.suffix != ".pyc"):
                target = copied.parent / source_file.relative_to(source.parent)
                local_equal.append(target.is_file() and target.read_bytes() == source_file.read_bytes())
    old_agents = (backup / "AGENTS.md").read_text(encoding="utf-8-sig")
    new_agents = (home / "AGENTS.md").read_text(encoding="utf-8-sig")
    route = project / "AGENTS.override.md"
    if not route.exists() or not route.read_text(encoding="utf-8-sig").strip():
        route = project / "AGENTS.md"
    route_text = route.read_text(encoding="utf-8-sig")
    saved_roles = list((backup / "agents").glob("ase-*.toml"))
    domain_files = plan.get("domain_files", [])
    domain_config = read_config(domain_project / ".codex/config.toml") if domain_project else {}
    domain_entries = global_entries + domain_config.get("skills", {}).get("config", [])
    domain_skills = list((domain_project / ".agents/skills").glob("*/SKILL.md")) if domain_project else []
    discovery = json.loads((backup / "host-discovery.json").read_text(encoding="utf-8"))
    rows = discovery["rows"]
    project_rows = [r for r in rows if canonical(r["cwd"]) == canonical(project)]
    domain_rows = [r for r in rows if domain_project and canonical(r["cwd"]) == canonical(domain_project)]
    neutral_rows = [r for r in rows if canonical(r["cwd"]) not in
                    {canonical(project), canonical(domain_project)}] if domain_project else []
    expected_local = {p.parent.name: project / ".agents/skills" / p.parent.name / "SKILL.md" for p in selected}
    actual_local = project_rows[0]["enabled_engineering"] if len(project_rows) == 1 else []
    discovery_matches = (
        discovery["global_config_sha256"] == hashlib.sha256((home / "config.toml").read_bytes()).hexdigest()
        and discovery["project_config_sha256"] == hashlib.sha256((project / ".codex/config.toml").read_bytes()).hexdigest()
        and len(rows) == 3 and len(domain_rows) == 1 and len(neutral_rows) == 1
        and all(not r.get("errors") for r in rows)
        and not neutral_rows[0]["enabled_engineering"]
        and not domain_rows[0]["enabled_engineering"]
        and set(domain_rows[0]["enabled_finance"]) == {skill_name(p) for p in domain_skills}
        and {s["name"] for s in actual_local} == set(expected_local)
        and all(s.get("scope") == "repo" and canonical(s["path"]) == canonical(expected_local[s["name"]])
                for s in actual_local)
    )
    checks = {
        "non_skill_user_configuration_preserved": unrelated_config(before) == unrelated_config(current),
        "other_skill_configuration_preserved":
            {k: v for k, v in before.get("skills", {}).items() if k != "config"} ==
            {k: v for k, v in current.get("skills", {}).items() if k != "config"},
        "unrelated_skill_selectors_preserved": unrelated_skill_entries(before, plan) ==
            unrelated_skill_entries(current, plan),
        "all_observed_global_engineering_skills_disabled": bool(disabled) and all(disabled),
        "global_skill_files_preserved": bool(observations) and all(
            Path(item["path"]).is_file() and
            hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
            for item in observations),
        "all_owned_project_skill_copies_enabled": bool(local_enabled) and all(local_enabled),
        "project_skill_resources_match_template": bool(local_equal) and all(local_equal),
        "mandatory_global_router_removed": "<!-- autonomous-se managed begin -->" not in new_agents,
        "other_global_guidance_preserved": without_block(old_agents, "autonomous-se managed") ==
            without_block(new_agents, "project-workflow-scope"),
        "project_router_present": "<!-- autonomous-se managed begin -->" in route_text and
            ".agents/skills/task-orchestrator/SKILL.md" in route_text.replace("\\", "/"),
        "owned_native_roles_project_only": bool(saved_roles) and all(
            not (home / "agents" / p.name).exists() and
            (project / ".codex/agents" / p.name).is_file() for p in saved_roles),
        "domain_files_unchanged": bool(domain_files) and all(
            Path(item["path"]).is_file() and
            hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
            for item in domain_files),
        "domain_skills_enabled": bool(domain_skills) and all(enabled_for(
            domain_entries, {skill_name(p)}, p)
            for p in domain_skills),
        "fresh_host_discovery_matches_scope_and_configuration": discovery_matches,
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "observed_global_skills": len(observations), "owned_project_skills": len(selected),
            "scope": "local file audit and recorded fresh Codex skills/list; no model behavior guarantee"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("home", "project", "backup"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--manifest", type=Path, help="Audit a portable installation manifest instead of legacy scope evidence")
    parser.add_argument("--domain-project", type=Path)
    args = parser.parse_args()
    try:
        if args.manifest:
            result = audit_install_manifest(json.loads(args.manifest.read_text(encoding="utf-8-sig")))
        elif all((args.home, args.project, args.backup)):
            result = run(args.home, args.project, args.backup, args.domain_project)
        else:
            parser.error("Supply --manifest or all of --home, --project and --backup")
    except (OSError, ValueError, KeyError, TypeError):
        result = {"status": "FAIL", "error": "Missing or invalid audit inputs; private contents omitted"}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
