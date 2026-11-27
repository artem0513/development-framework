# Independent review of universal adaptation

Reviewer: separate ase-code-reviewer session /root/universal_review, read-only and not an implementer. Scope: continuity runtime/schema/CLI, generated routers and generic templates/references.

Found and corrected: P1 stale source overwrite could revert a newer blocked source to obsolete analyzed coverage; expected-sequence CAS now rejects it and preserves current gaps. P2 backup SQLite handles stayed open until GC; explicit close resolves reproduced Windows cleanup failure. P2 native agent reference pointers were relative to an unavailable cwd; generated instructions now use installed runtime paths.

Independently executed: 12 continuity tests PASS (5.177s); direct stale-source reproduction rejected; concurrent v2-to-v3 migration both succeeded and temporary cleanup succeeded; targeted generic-asset scan found no donor organization/provider bindings. Restore includes original task contract, stable task identity, decisions and session unresolved questions.

No unresolved actionable findings at review completion. This judgment does not replace the full suite, fresh ordinary-chat run or deterministic gate; their actual artifacts are recorded separately under outputs/.

## Follow-up: sandbox repository ownership

Reviewed the later safe Git adjustment independently: ase/inspection.py `_run` resolves the requested repository canonically before `_base` passes `-c safe.directory=<exact selected checkout>`. This trusts that checkout only for the current subprocess; it adds no wildcard, edits no global configuration, and does not grant an MCP root or new command capability. The explicit argument allowlist, disabled fsmonitor/hooks/textconv/clean filters, isolated Git environment, no-network settings and fixed executable remain in place.

The first fresh-chat run exposed a real checkpoint failure under the Windows sandbox identity even though model verification approved its calculator task; that first run alone was not a complete continuity success. The recorded failing ownership regression in outputs/universal-owner-red.log is consistent with the inspected failure path. The exact-path fix addresses Git's ownership check while retaining the existing local reliability trust boundary.

Independently rerun after this change: `python -m unittest discover -s tests -p test_continuity.py -v`: 13 tests PASS in 7.347s, including the different-owner regression. `python -m unittest discover -s tests -p test_host.py -v`: 18 tests PASS in 19.390s, including actual fsmonitor, textconv and clean-filter execution canaries, host root/tool restrictions and state redirection cases. These tests demonstrate preserved local safeguards; deterministic model adapters do not constitute independent semantic model validation. The fresh corrected chat and full-suite results remain separate parent-run evidence.

Reviewed inspection.py SHA256: 749e83731415df065ad4d5eda206b384c2313336d3bc311bcd3410df0473ccbc. No unresolved actionable findings in this follow-up.
