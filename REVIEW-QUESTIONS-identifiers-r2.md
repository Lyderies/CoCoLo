# Identifiers r2: decisions the requirements do not settle

1. **Adoption by wrappers that predate the template (HIGH, review finding "input failure and template adoption").**
   Done: the runner never launches a security check when it cannot produce the git file list or the denylist
   (`identifier_inputs_unavailable`), and the shipped template requires both inputs. Not done: the runner still
   cannot tell that an old approved wrapper actually read the inputs it was given, so one that ignores them can still
   pass. Security checks are a general group (not only identifier scans), so forcing proof from every one of them is a
   policy change. Options: (a) the template writes a receipt (digests of the manifest and denylist it read) into the
   check's work folder and the runner accepts a pass only with a matching receipt, which makes every security check
   emit one; (b) add a field to `operational-checks.json` that marks a check as the identifier scan and require the
   receipt only for those; (c) accept the residual (the owner approves each wrapper). Recommendation: (b).
2. **Line numbers in QA diagnostics.** Every record now has line 0, because the runner cannot tell a true line from a
   number a wrapper chose (a six-digit "line" is a channel for a value). Restoring real lines needs the runner to
   re-derive them with a trusted copy of the scanner (needs a decision on how the host gets one) or to check each
   claimed line against the category patterns. Say if lines matter enough to do that.
3. **Version.** The digest of `0.8.41` was recomputed in place again (as the previous commit did). If `0.8.41` was ever
   installed or handed out anywhere, bump to a new version instead.
4. **Git on PATH in the command-line scanner.** It now ignores relative PATH entries and entries inside the scanned
   project, but still uses the PATH git (CI and developer machines have no fixed location); the runner keeps its own
   trusted-location rule. Say if the command line should refuse unless git is in a fixed system location.
