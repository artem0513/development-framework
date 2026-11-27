"""Create an isolated example repository and bare local remote; never contacts a service.

Usage: python examples/create_demo.py C:/Temp/ase-demo
The generated task starts with correct code so verification can be repeated safely.
Run `python defect.py` inside the example repo to introduce an unmet criterion.
"""
import json
from pathlib import Path
import subprocess
import sys


def create(destination):
    destination = Path(destination).resolve()
    if destination.exists():
        raise SystemExit("Destination already exists; choose a fresh disposable directory.")
    destination.mkdir(parents=True)
    repo = destination / "project"
    repo.mkdir()
    remote = destination / "remote.git"
    def git(*args, cwd=repo):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    git("init", "--bare", str(remote), cwd=destination)
    git("init", "-b", "feature/calculator")
    git("config", "user.name", "ASE Disposable Demo")
    git("config", "user.email", "ase-demo@example.invalid")
    git("remote", "add", "origin", str(remote))
    (repo / ".gitignore").write_text("__pycache__/\n.ase/\n", encoding="utf-8")
    (repo / "calculator.py").write_text(
        "def divide(numerator, denominator):\n"
        "    if denominator == 0:\n"
        "        raise ValueError('denominator must be nonzero')\n"
        "    return numerator / denominator\n", encoding="utf-8")
    (repo / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import divide\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_divide_positive(self): self.assertEqual(divide(12, 3), 4)\n"
        "    def test_divide_negative(self): self.assertEqual(divide(-12, 3), -4)\n"
        "    def test_zero_rejected(self):\n"
        "        with self.assertRaisesRegex(ValueError, 'nonzero'): divide(1, 0)\n",
        encoding="utf-8")
    task = {"id": "calculator", "request": "Implement safe division: positive/negative arithmetic and explicit zero denominator rejection.",
            "risk": "MEDIUM", "acceptance": [
                {"id": "AC1", "expected": "divide(12, 3) returns 4 and divide(-12, 3) returns -4", "mandatory": True, "commands": ["tests"]},
                {"id": "AC2", "expected": "divide(1, 0) raises ValueError with message containing nonzero", "mandatory": True, "commands": ["tests"]}],
            "commands": [{"id": "build", "argv": [sys.executable, "-c", "import ast,pathlib; ast.parse(pathlib.Path('calculator.py').read_text())"], "kind": "build", "required": True},
                         {"id": "tests", "argv": [sys.executable, "-m", "unittest", "discover", "-v"], "kind": "test", "parser": "unittest", "required": True}],
            "protected_paths": [], "max_attempts": 2}
    (destination / "task.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
    (destination / "defect.py").write_text(
        "from pathlib import Path\np = Path(__file__).parent / 'project' / 'calculator.py'\n"
        "p.write_text('def divide(numerator, denominator):\\n    return numerator / denominator\\n', encoding='utf-8')\n",
        encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "Disposable safe division example")
    return {"repo": str(repo), "remote": str(remote), "task": str(destination / "task.json"),
            "defect": str(destination / "defect.py"), "state": str(destination / "state")}


if __name__ == "__main__":
    print(json.dumps(create(sys.argv[1]), indent=2))
