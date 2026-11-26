# Released predecessor artifact

`0.8.66.tar.gz` is an unmodified full `git archive afb6f610`, compressed with
`gzip -n`. SHA-256:

`81b24a3ed530dabd306bbcb69fb794ec16e460ed7078e223a43be8af33a2e7a3`

The e2e harness checks this digest before extraction. It never requires the
predecessor commit in a shallow clone, never relabels current source, and fails
with a plain missing-artifact or digest-mismatch reason. The unit witness runs
extraction with every subprocess/Git call forbidden, verifies VERSION 0.8.66,
and rejects a corrupt artifact. The native V34 audit separately names its
historical object and skips with an explicit offline reason if it is absent.

`0.8.68-native-package.tar.gz` is an unmodified
`git archive e46759a5 packages/codex-agents/native-package` (the released 0.8.68
package, runtime digest `28435b17...d9ed0`), compressed with `gzip -n`. SHA-256:

`e05a68602ff65455dc8cdbb4efd0c696b4a61fe9a20665c34d4c622547e9b765`

`packages/codex-agents/tests/test_b4t_native_p68.py` checks this digest, installs
the package, lets 0.8.68's own collector start its service from the protected
launch copy (the installed launch line), and runs the update against it
(decision 372).

`0.8.69-native-package.tar.gz` is an unmodified
`git archive b63b6f5c packages/codex-agents/native-package` (the FINAL installed
0.8.69, runtime digest `60569d30...ca7e3`), compressed with `gzip -n`. SHA-256:

`674deabb79f6cce82c01687c51a2655fd6d6a9a9c9143eed737c23fb88982294`

`packages/codex-agents/tests/test_b4t_native_p69.py` checks this digest and runs
the 0.8.72 update against a real idle 0.8.69 service started by its own collector
from the launch copy (RS-B-B4t-0/-1/-3/-9).
