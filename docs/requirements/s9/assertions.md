# S9 assertion ledger

No existing test assertion was removed or changed. No assertions in the new S9
module were removed or weakened after implementation.

Two new-test fixture arrangements were corrected when restart persistence was
added: growth boundary cases now use separate project identities so their saved
histories are independent; the restart growth test measures the first post-restart
sample before an ordinary visit can renew its sample window. Their assertions
remain unchanged. There is no old-to-new assertion replacement to record.
