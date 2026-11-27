# Source availability and readable artifact evidence

Discovery records source declarations and observations. It does not grant write
authority or establish that a model has understood a source. Independent packets
include the discovery manifest, current availability, and any verified readable
artifact bindings under `_ase_context/`.

## Required inputs, optional inputs and outputs

Discovery dependencies are required inputs by default. A dependency can declare
`required: false` or name the stages that need it:

```json
{
  "resources": ["guide.docx", "obsolete.md"],
  "outputs": ["obsolete.md"],
  "dependencies": [
    {
      "path": "policy.md",
      "reason": "Governs publication of the guide",
      "blocking_stage_ids": ["publish"]
    },
    {
      "path": "background.md",
      "reason": "Optional historical background",
      "required": false
    }
  ]
}
```

This is the `scope` argument to capability discovery. Missing required inputs
block their affected stages and transitive dependents. Omitted stage mappings
apply to every stage. Unknown or empty mappings are rejected when recorded;
saved mappings invalidated by a later stage rename are conservative. Discovered
transitive dependencies inherit their originating dependency's requirement and
stage impact. The captured dependency graph propagates stronger requirements
and stage unions to a bounded fixed point, including when a later route reaches
an already inspected shared dependency. Multiple declarations combine their
required stage obligations; an optional declaration cannot weaken a required
declaration.

Artifact evidence paths in the task contract are outputs. Resources inside
approved stage scopes with `file_write` or `command` effects are mutable
workspace state; their absence can be an intended creation or deletion state.
Other intentionally absent outputs or deleted files can be named explicitly in
`scope.outputs`, and each such output must also name an explicit
`scope.resources` path. An explicit dependency on any of these paths declares an
input requirement and takes precedence over the output/workspace classification.
Output absence is assessed by the stage's actual artifact or postcondition
checks; it is not automatically classified as an unavailable input.

Previously inspected inputs remain declared after disappearance and after a
fresh discovery records them as `MISSING`. Current availability, dependency
reasons and optional limitations remain visible to reviewers. Source snapshots
bind declared read failures through stable error/errno markers instead of
silently treating the source as read. Local path safety failures remain errors,
including for optional sources. The existing software profile's repository-wide
Git inventory remains mandatory; an unreadable tracked file can still prevent
that inventory from being captured.

Stage packets omit bodies explicitly mapped only to unrelated stages, and
optional input bodies and their bound readable representations are omitted
transparently. A representation stays supplied when a current-stage mandatory
artifact, an owned change or an independent required source needs it. Omitted
bindings retain their declared hashes, extraction method and current observed
hash status in artifact representation metadata. `_ase_context/omitted_sources.json`
records each omitted path's current hash, availability, reason, stage mapping and
the limitation that its body was not supplied or reviewed. Their source hashes
remain in the source snapshot. Current-stage mandatory artifacts and actual
changes inside that stage's mutation scope retain review bodies, including
required readable representations. Common/current-stage required input bodies
remain mandatory. A required stage packet that exceeds the total bound still
fails closed; omission does not enlarge either packet limit.

## Readable representations of binary artifacts

Generate or extract a readable UTF-8 representation with the appropriate domain
tool. Add a `representations` list to the discovery payload before
`discovery-record`. Each entry binds both byte streams:

```json
{
  "representations": [
    {
      "artifact_path": "guide.docx",
      "artifact_sha256": "<64 lowercase hexadecimal SHA-256 characters>",
      "path": "guide.txt",
      "sha256": "<64 lowercase hexadecimal SHA-256 characters>",
      "method": "Text and structural extraction from the final OOXML document"
    }
  ]
}
```

The artifact must belong to the declared discovery scope. Both paths must be
safe project-relative paths outside private project/Git data. Redirected and
hardlinked files are rejected. Recording and packet construction both recheck
the current artifact and readable representation against the declared hashes.
Representation bytes also participate in source fingerprints. Changing either
file requires a fresh binding and fresh verification evidence. An undeclared
sidecar never clears binary coverage.

The readable representation must be nonempty UTF-8 without NUL bytes and at most
512,000 bytes. The complete packet still has the protocol's 1,048,576-byte limit;
exceeding either bound fails closed. Artifact hashing is streamed, so the native
binary need not fit the readable-file limit. The packet carries the readable
content and the current artifact/representation hash mapping, not a fabricated
text version of the native binary.

A hash binding establishes which bytes the representation claims to describe.
It does not prove extraction quality, layout quality or semantic equivalence.
The independent domain reviewer must assess adequacy, and the contract must
require any necessary rendering, structure, content and native-artifact checks.
The runtime does not automatically convert DOCX, PDF, images or other binaries.

## Intake source statuses

Intake source status is trimmed and normalized to lowercase. Supported values
are `available`, `complete`, `inspected`, `fully_read`, `analyzed`, `discovered`,
`acquired`, `unknown`, `unavailable`, `missing`, `blocked`, `partial` and
`inaudible`. Unsupported values are rejected. Every incomplete status
(`discovered`, `acquired`, `unknown`, `unavailable`, `missing`, `blocked`,
`partial`, `inaudible`) requires an attributed intake gap. Gap requirements and
stage mappings determine readiness; source statements remain attributed claims,
not independent semantic validation.

Saved intake is revalidated and normalized at readiness and packet reads. An old
intake that violates current source-status rules produces an attributed required
compatibility gap across stages until corrected through ordinary discovery/intake
APIs. Historical records are retained unchanged; valid optional and scoped gaps
keep their existing impact. Packet context discloses validation and normalized
source statuses alongside the original manifest.

Task source fingerprints also bind a normalized discovery coverage policy:
declared scope/resources/outputs, dependency requirements and stage mappings with
reasons/origins, readable representation bindings and methods, and intake claims
with source references/cues/ranges/statuses. Changing a source from optional to
required therefore invalidates prior checks and review receipts even when every
file's bytes are unchanged. A task snapshot with discovery exposes this binding
as `discovery_policy_sha256`; taskless or undiscovered snapshots keep their
previous byte-only fingerprint.

Equivalent list ordering, explicit default `required: true`, normalized source
status casing and refreshed generated/capability observations do not change the
policy digest. Source acquisition `observed_at` alone is not new evidence under
the existing intake update rules and is excluded; meeting timestamps/cues,
source versions/references and actual changed content remain bound. Native file
bytes and unavailable-source markers retain their existing fingerprint coverage.

An ordinary `task discover` refresh scans current task roots together with prior
declared read roots and dependency roots. Missing required inputs remain
declared and block the same stages; old inspected hashes are not reused as fresh
observations. A structured discovery write also cannot discard a required root
by omission. Explicit same-path dependency declarations can reconcile its
optionality, reason or stage impact. This applies to `scope.dependencies` and
standalone top-level dependency roots without `from`; inferred `from` edges are
regenerated from current sources and can disappear when imports are obsolete.
Previously mutable/output roots are identified from the task contract recorded
at the earlier discovery, so replacing an output does not invent a required
input. Still-scoped readable representation bindings survive a plain refresh;
explicit replacements or removal remain possible, and current bytes are still
checked before independent packet coverage can pass.
