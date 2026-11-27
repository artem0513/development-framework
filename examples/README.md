# Disposable end-to-end example

Run `python examples/create_demo.py C:/Temp/ase-demo-unique` from this repository.
Choose a fresh destination. The generator creates only a local project and local
bare Git remote, and returns their paths as JSON. It never contacts a real Git
hosting service.

Use the returned paths with the CLI:

```powershell
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state init
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state start C:/Temp/ase-demo-unique/task.json
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state check calculator
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state verify calculator
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state gate calculator
python -m ase --repo C:/Temp/ase-demo-unique/project --home C:/Temp/ase-demo-unique/state report calculator
```

`verify` starts real Codex processes and requires working authentication/provider
access. The generator's initial implementation is already correct so this path
tests independent verification, rather than code generation. `run calculator`
additionally invokes the implementation stage. Model/provider failure must remain
BLOCKED; it is not equivalent to a passing independent evaluation.

To introduce the missing zero-denominator behavior, run the generated
`defect.py`, then rerun `check`, `verify`, `gate`, and `report`. The actual unit
tests must fail and the gate must reject. A `publish calculator --execute`
attempt must leave the remote branch unchanged. Restore the correct function or
use `run calculator` to exercise bounded remediation and verification again.

Publishing is dry-run unless `--execute` is supplied, and execution additionally
requires repository-scoped `authorize --remote origin`. Only authorize this
generated local destination for the demonstration.

The automated suite (`python -m unittest discover -s tests -v`) includes a
deterministic subprocess standing in for Codex so receipt validation, recovery,
retry limits and publishing can be tested without network access. Those tests
prove protocol behavior, not independent model judgment. A real `verify` run
is separate evidence.
