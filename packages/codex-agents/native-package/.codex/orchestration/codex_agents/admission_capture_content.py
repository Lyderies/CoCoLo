"""Lossless, owner-local packing of admission content; never a stored record format.

The fixed dictionary shares repeated JSON spelling, not evidence. No JSON is
parsed or field discarded. Signed attestations stay byte-for-byte raw for the
signature worker. Packing does not authenticate content; consumers reconstruct
one bounded record before the original digest and closed-record checks.
"""

from __future__ import annotations

import zlib

from .learning_contracts import MAX_RECORD_BYTES

_MAGIC = b"\x00AC1"
# Versioned with _MAGIC. Keep literal: changes to schema sets must not silently
# change the dictionary needed to reconstruct an already captured snapshot.
_DICTIONARY = (
    b'{"schema_version":1,"record_type":"provider_identity","record_id":"provider_identity:'
    b'","provider":"codex","account_fingerprint":"'
    b'0000000000000000000000000000000000000000000000000000000000000000'
    b'","logged_in":true,"auth_method":null,"subscription_type":null,"plan_type":"plus",'
    b'"client_path":"/usr/bin/","client_sha256":"","client_version":"","observed_at_ms":'
    b'{"schema_version":1,"record_type":"admission_decision","record_id":"admission_decision:'
    b'","run_id":"","provider":"claude","decision":"pass","reason_code":"admitted",'
    b'"admission_basis":"evidence","account_fingerprint":"","evidence_digests":[""],"observed_at_ms":'
    b'{"schema_version":1,"record_type":"probe_admission","record_id":"probe_admission:'
    b'","decision":"deny","admission_basis":"bootstrap","reason_code":"provider_exhausted",'
    b'"probe_id":null,"phase":"before","run_id":null,"restriction_provenance":[],"status":null,'
    b'{"account_fingerprint":"","has_credits":false,"is_using_overage":false,"observed_at_ms":'
    b',"ordinary_usage_allowed":true,"overage_disabled_reason":null,"overage_status":null,'
    b'"paid_usage_usable":false,"phase":null,"provider":"codex","record_id":"provider_billing:'
    b'","record_type":"provider_billing","run_id":null,"schema_version":1,'
    b'"spend_control_reached":false,"unlimited":false}'
    b'{"account_fingerprint":"","observed_at_ms":,"phase":null,"provider":"codex",'
    b'"record_id":"provider_headroom:","record_type":"provider_headroom","run_id":null,'
    b'"schema_version":1,"status":null,"windows":['
    b'{"resets_at":null,"scope":null,"used_percent":0.0,"window_minutes":300},'
    b'{"resets_at":,"scope":"five_hour","used_percent":,"window_minutes":10080}]}'
)

def _dictionary(identifier, observed):
    # These exact fields already occupy the row's metadata columns. Referencing
    # them from the compression dictionary avoids paying for them twice, while
    # retaining their original spelling in the reconstructed canonical bytes.
    if identifier is None:
        return _DICTIONARY
    return _DICTIONARY + identifier.encode("utf-8") + b'","observed_at_ms":' + str(observed).encode("ascii")


def pack_content(kind, content, identifier=None, observed=None):
    """SQLite callback: at most one validated-size BLOB at a time, no parsing.

    Bad storage types and oversized values remain unmodified for header refusal.
    The SQL caller guards their size before invoking this callback as well.
    """
    if (kind == "billing_attestation" or type(content) is not bytes
            or not 0 < len(content) <= MAX_RECORD_BYTES):
        return content
    compressor = zlib.compressobj(level=6, zdict=_dictionary(identifier, observed))
    packed = _MAGIC + compressor.compress(content) + compressor.flush()
    return packed if len(packed) < len(content) else content


def unpack_content(content: bytes, identifier=None, observed=None) -> bytes:
    """Reject malformed, trailing and over-expanding streams before JSON work."""
    if type(content) is not bytes or not 0 < len(content) <= MAX_RECORD_BYTES:
        raise ValueError("invalid admission content")
    if not content.startswith(_MAGIC):
        return content
    decoder = zlib.decompressobj(zdict=_dictionary(identifier, observed))
    try:
        raw = decoder.decompress(content[len(_MAGIC):], MAX_RECORD_BYTES + 1)
    except zlib.error:
        raise ValueError("invalid admission content") from None
    if (not 0 < len(raw) <= MAX_RECORD_BYTES or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise ValueError("invalid admission content")
    return raw
