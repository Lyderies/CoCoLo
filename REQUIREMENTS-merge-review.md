# Merge review implementation requirements

**ML-R91 — Recorded empty folder trees.** Owner decision 91=A: a folder that holds only other empty folders CoCoLo itself made counts as empty, provided there is nothing else inside and every folder matches the device/inode identity CoCoLo recorded. Foreign files, unrecorded folders, and replaced folder identities do not qualify. The two C-3 checks must enforce this rule without accepting foreign contents.

## Scope

The other fixes implement the existing ML-1..ML-30 and TF-18/19 requirements and the reviewed end-state cells; no retention, owner-version, or terminal-direction rule is relaxed.
