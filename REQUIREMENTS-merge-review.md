# Merge review implementation requirements

**ML-R91 — Recorded empty folder trees.** Owner decision 91=A: a folder that holds only other empty folders CoCoLo itself made counts as empty, provided there is nothing else inside and every folder matches the device/inode identity CoCoLo recorded. Foreign files, unrecorded folders, and replaced folder identities do not qualify. The two C-3 checks must enforce this rule without accepting foreign contents.

## Scope

The other fixes implement the existing ML-1..ML-30 and TF-18/19 requirements and the reviewed end-state cells; no retention, owner-version, or terminal-direction rule is relaxed.

## Round-3 enforcement clarifications (decision 102)

ML-5/ML-1: before commit A, verify the inserted history row by querying stored events, old/new contents and batch digest against independently retained expected values. A changed row is corrupt_history and cannot commit A or alter the owner tree.
ML-12/ML-30: ls-files and check-attr must complete successfully before their output can admit a batch. A nonzero git exit is an operational failure, even with empty stdout; it cannot land or commit A.
TF-23 (defined on feat/test-framework): new/changed review witnesses require old-code red evidence at 9c18c4a3, the intended diagnostic, a valid positive control and post-fix green evidence. Witness-only repairs must reject deliberately invalid results that the old witness accepted.
