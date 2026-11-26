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
