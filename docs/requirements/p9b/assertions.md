# Assertion changes

No existing assertions are removed or changed. P9b adds a dedicated regression
module; all accepted P9 assertions remain in place.

Before implementation, two new-test fixture mistakes were corrected: the sixth
fingerprint now uses hexadecimal characters; the fourth deletion fixture uses
public `expire_report` rather than `sweep_learning_retention` (which only deletes
cases/candidates). Its `removed == 1` and predecessor-restoration assertions are
unchanged. Neither fixture correction relaxes an assertion.
