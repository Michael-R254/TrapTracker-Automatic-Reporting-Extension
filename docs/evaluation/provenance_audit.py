"""Stage 0 — retrospective provenance audit of an already-ingested corpus.

The database stores no sender information: ``email_parser`` builds
``raw_source_metadata`` (subject/from/to/date/message-id) and the repository
never persists it, so a stored row cannot be attributed after the fact. What
makes attribution still possible is that ingestion is NON-DESTRUCTIVE — the
fetcher never deletes, never expunges and does not set ``\\Seen`` — so the
source messages should still be in the mailbox carrying the receiving MX's
own authentication verdict.

That evidence is the only perishable item in the assessment, and it is outside
this project's control. This script captures it once, to a file, so a later
stage can back-fill provenance from the capture instead of re-fetching.

WHERE THE OUTPUT GOES. The capture is written to the PROJECT directory, beside
the corpus it describes, and NOT into this repository. It carries the sender's
address, every Message-ID in the folder and the full delivery metadata; anything
committed to `main` stays reachable from every clone (`docs/PUBLICATION.md` §1),
so this file must not enter it. A later back-fill runs against the project
database and finds the capture next to it.

READ-ONLY, twice over:
  * the mailbox is opened with ``headers_only=True`` (``BODY.PEEK[HEADER]``)
    and ``mark_seen=False``, so no flag changes and no body is downloaded;
  * the project database is opened ``mode=ro``.

The one exception is documented at ``_inspect_unseen``: messages that the
pipeline has NEVER seen are fetched in full, because whether they carry a
``_boxed`` attachment is a body fact, not a header fact. That set is expected
to be a handful, and its size is reported before it is fetched.

WHAT "PASSES" MEANS. A message passes the conjunction when the receiving
Gmail's own ``Authentication-Results`` reports ``dkim=pass`` with a DKIM
identity in ``gmail.com``, AND the ``From`` header is exactly the expected
address. Neither half is sufficient. ``dkim=pass`` alone proves only that
SOME Gmail account sent it — an outsider gets that by sending from their own.
It is the conjunction that is unforgeable, because Gmail signs with
``d=gmail.com`` only messages whose ``From`` belongs to the authenticated
submitting account, and the signature oversigns ``from``.

The receiver's verdict is trustworthy because a receiver strips inbound
``Authentication-Results`` bearing its own authserv-id before adding its own.
A SECOND header claiming ``mx.google.com`` would therefore be a finding in
itself, and is reported as ``forged_ar_suspected``.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from imap_tools import AND, MailBox, MailBoxUnencrypted

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ttr.projects.resolve import resolve_project          # noqa: E402
from ttr.sources.email_parser import (                     # noqa: E402
    ALERT_SUBJECT_PREFIX, TEST_EMAIL_SUBJECT, _BOXED_MARKER,
    NotAnAlertEmail, parse_alert_email,
)

#: The authserv-id whose verdict we trust, because it is the receiving MX.
RECEIVER_AUTHSERV = "mx.google.com"
#: The DKIM signing domain a genuine alert must carry.
EXPECTED_DKIM_DOMAIN = "gmail.com"
#: UIDs per body fetch, matching email_fetcher._UID_CHUNK.
UID_CHUNK = 200


def _first(headers: dict, name: str) -> str | None:
    vals = headers.get(name.lower())
    return vals[0].strip() if vals else None


def _all(headers: dict, name: str) -> list[str]:
    return [v.strip() for v in (headers.get(name.lower()) or ())]


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s).strip() if s else ""


def _addr(from_header: str | None) -> str | None:
    """The bare address out of a From header, lowercased."""
    if not from_header:
        return None
    m = re.search(r"<([^>]+)>", from_header) or re.search(r"([\w.+-]+@[\w.-]+)", from_header)
    return m.group(1).strip().lower() if m else from_header.strip().lower()


def parse_auth_results(values: list[str]) -> dict:
    """Verdicts from the Authentication-Results header the RECEIVER added.

    Returns the parsed verdicts plus ``ar_count_for_receiver``: more than one
    header claiming the receiver's authserv-id means an inbound copy survived,
    which the receiver should have stripped.
    """
    mine = [v for v in values if _norm(v).lower().startswith(RECEIVER_AUTHSERV)]
    out = {
        # The RAW headers, every one, verbatim. Added 2026-09-10 for the Stage 1
        # back-fill: the original capture kept only the parsed verdicts below,
        # and a parsed verdict cannot be written into a column whose NULL means
        # "the header was absent" without asserting something untrue about rows
        # that carried a passing one. The verdict parsing is unchanged, so the
        # Stage 0 findings this script produced are unaffected.
        "authentication_results_raw": [str(v) for v in values] or None,
        "ar_present": bool(values),
        "ar_count_total": len(values),
        "ar_count_for_receiver": len(mine),
        "forged_ar_suspected": len(mine) > 1,
        "dkim": None, "spf": None, "dmarc": None,
        "dkim_identity": None, "header_from_domain": None,
    }
    if not mine:
        return out
    raw = _norm(mine[0])
    for key in ("dkim", "spf", "dmarc"):
        m = re.search(rf"\b{key}=(\w+)", raw)
        if m:
            out[key] = m.group(1).lower()
    # Gmail reports the DKIM identity as header.i=@gmail.com; header.d= is the
    # equivalent when present. Either establishes the signing domain.
    m = re.search(r"\bheader\.(?:d|i)=@?([\w.-]+)", raw)
    if m:
        out["dkim_identity"] = m.group(1).lower()
    m = re.search(r"\bheader\.from=([\w.-]+)", raw)
    if m:
        out["header_from_domain"] = m.group(1).lower()
    return out


def received_shape(values: list[str]) -> str:
    """A coarse signature of the delivery path, for spotting odd ones.

    Hop count plus the transport token of each hop (``SMTP``, ``ESMTPSA``,
    ``SMTPS``...). Deliberately carries no host, address or IP: this is a
    shape comparison, and the values are private.
    """
    toks = []
    for v in values:
        m = re.search(r"\bwith\s+(\w+)", _norm(v))
        toks.append(m.group(1).upper() if m else "?")
    return f"{len(values)}:" + ",".join(toks)


def harvest_headers(mailbox) -> dict:
    """One header-only pass over the whole folder, keyed by Message-ID."""
    by_mid: dict[str, dict] = {}
    duplicates: list[str] = []
    no_message_id = 0
    scanned = 0
    for mm in mailbox.fetch("ALL", headers_only=True, bulk=True, mark_seen=False):
        scanned += 1
        h = mm.headers
        mid = _first(h, "message-id")
        if not mid:
            no_message_id += 1
            continue
        rec = {
            "uid": mm.uid,
            "message_id": mid,
            "from": _norm(_first(h, "from")),
            "from_addr": _addr(_first(h, "from")),
            "to": _norm(_first(h, "to")),
            "delivered_to": _norm(_first(h, "delivered-to")),
            "return_path": _norm(_first(h, "return-path")),
            "subject": _norm(_first(h, "subject")),
            "date": _norm(_first(h, "date")),
            "received_shape": received_shape(_all(h, "received")),
            "received_hops": len(_all(h, "received")),
            **parse_auth_results(_all(h, "authentication-results")),
        }
        if mid in by_mid:
            duplicates.append(mid)
        by_mid[mid] = rec
    return {"by_mid": by_mid, "scanned": scanned,
            "no_message_id": no_message_id, "duplicate_message_ids": duplicates}


def passes(rec: dict, expected_from: str | None) -> bool:
    """The conjunction. Both halves required; see the module docstring."""
    return (rec.get("dkim") == "pass"
            and rec.get("dkim_identity") == EXPECTED_DKIM_DOMAIN
            and not rec.get("forged_ar_suspected")
            and (expected_from is None or rec.get("from_addr") == expected_from))


def _inspect_unseen(mailbox, records: list[dict]) -> None:
    """Full-body fetch for messages the pipeline has never seen.

    The ONLY body fetch in this script. Whether a message carries a ``_boxed``
    attachment, and whether it would parse, are body facts. The set is small
    by construction (it is the mailbox minus the seen-store); if it were not,
    the caller reports the size before this runs.
    """
    uids = [r["uid"] for r in records if r.get("uid")]
    if not uids:
        return
    by_uid = {r["uid"]: r for r in records}
    for chunk in (uids[i:i + UID_CHUNK] for i in range(0, len(uids), UID_CHUNK)):
        for mm in mailbox.fetch(AND(uid=chunk), bulk=True, mark_seen=False):
            rec = by_uid.get(mm.uid)
            if rec is None:
                continue
            msg = email.message_from_bytes(mm.obj.as_bytes(), policy=email.policy.default)
            names = []
            for part in msg.walk():
                fn = part.get_filename()
                if fn:
                    names.append(fn)
            rec["attachment_filenames"] = names
            rec["has_boxed_attachment"] = any(_BOXED_MARKER in n.lower() for n in names)
            subj = rec.get("subject") or ""
            rec["has_alert_prefix"] = subj.startswith(ALERT_SUBJECT_PREFIX)
            rec["is_test_email"] = subj.strip() == TEST_EMAIL_SUBJECT
            try:
                ev = parse_alert_email(msg)
                rec["would_parse"] = True
                rec["parse_warnings"] = list(ev.parse_warnings)
                rec["parsed_label"] = ev.upstream_label
            except NotAnAlertEmail as exc:
                rec["would_parse"] = False
                rec["not_an_alert_reason"] = str(exc)
            except Exception as exc:                       # noqa: BLE001
                rec["would_parse"] = False
                rec["parse_error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="Back Garden")
    ap.add_argument("--out", type=Path, default=None,
                    help="Where to write the per-row capture (default: the project dir).")
    args = ap.parse_args()

    ctx = resolve_project(args.project).ctx
    out_path = args.out or (ctx.dir / "provenance_audit.json")

    # --- the corpus, read-only --------------------------------------------
    conn = sqlite3.connect(f"file:{ctx.db_path}?mode=ro", uri=True)
    stored = [r[0] for r in conn.execute(
        "SELECT source_message_id FROM detection_events")]
    conn.close()
    seen = [l.strip() for l in ctx.seen_store_path.read_text(
        encoding="utf-8").splitlines() if l.strip()]
    stored_set, seen_set = set(stored), set(seen)

    print(f"corpus     : {len(stored)} rows ({len(stored_set)} distinct message ids)")
    print(f"seen-store : {len(seen)} ids ({len(seen_set)} distinct)")

    # --- the mailbox, headers only ----------------------------------------
    mb = MailBox if ctx.mailbox.imap_use_ssl else MailBoxUnencrypted
    mailbox = mb(ctx.mailbox.imap_host).login(
        ctx.mailbox.imap_user, ctx.imap_password().get_secret_value(),
        initial_folder=ctx.mailbox.imap_folder)
    try:
        harvest = harvest_headers(mailbox)
        by_mid = harvest["by_mid"]
        print(f"mailbox    : {harvest['scanned']} messages scanned "
              f"(headers only), {len(by_mid)} with a Message-ID")

        mailbox_ids = set(by_mid)
        unseen_ids = sorted(mailbox_ids - seen_set)
        print(f"never seen by the pipeline: {len(unseen_ids)} — fetching those bodies")
        _inspect_unseen(mailbox, [by_mid[m] for m in unseen_ids])
    finally:
        try:
            mailbox.logout()
        except Exception as exc:                            # noqa: BLE001
            print(f"(logout failed, harmless: {exc!r})")

    # --- expected sender: the modal From among stored rows that were found --
    found_for_stored = [by_mid[m] for m in stored_set if m in by_mid]
    froms = Counter(r["from_addr"] for r in found_for_stored if r["from_addr"])
    expected_from = froms.most_common(1)[0][0] if froms else None

    rows = []
    for mid in stored:
        rec = by_mid.get(mid)
        if rec is None:
            rows.append({"message_id": mid, "present_in_mailbox": False,
                         "passes": False, "reason": "not found in mailbox"})
            continue
        r = dict(rec)
        r["present_in_mailbox"] = True
        r["passes"] = passes(rec, expected_from)
        if not r["passes"]:
            why = []
            if rec.get("dkim") != "pass":
                why.append(f"dkim={rec.get('dkim')}")
            if rec.get("dkim_identity") != EXPECTED_DKIM_DOMAIN:
                why.append(f"dkim_identity={rec.get('dkim_identity')}")
            if expected_from and rec.get("from_addr") != expected_from:
                why.append(f"from={rec.get('from_addr')}")
            if rec.get("forged_ar_suspected"):
                why.append("multiple Authentication-Results for the receiver")
            r["reason"] = "; ".join(why) or "unknown"
        rows.append(r)

    result = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "project": {"id": ctx.id, "name": ctx.name,
                    "mailbox": ctx.mailbox.imap_user,
                    "folder": ctx.mailbox.imap_folder},
        "criteria": {"receiver_authserv": RECEIVER_AUTHSERV,
                     "expected_dkim_domain": EXPECTED_DKIM_DOMAIN,
                     "expected_from": expected_from},
        "counts": {
            "mailbox_scanned": harvest["scanned"],
            "mailbox_with_message_id": len(by_mid),
            "mailbox_without_message_id": harvest["no_message_id"],
            "duplicate_message_ids": harvest["duplicate_message_ids"],
            "seen_store": len(seen_set),
            "stored_rows": len(stored),
            "stored_found": sum(1 for r in rows if r["present_in_mailbox"]),
            "stored_missing": sum(1 for r in rows if not r["present_in_mailbox"]),
            "stored_passing": sum(1 for r in rows if r["passes"]),
        },
        "reconciliation": {
            "seen_not_stored": sorted(seen_set - stored_set),
            "mailbox_not_seen": unseen_ids,
            "stored_not_in_mailbox": [r["message_id"] for r in rows
                                      if not r["present_in_mailbox"]],
            "stored_not_seen": sorted(stored_set - seen_set),
        },
        "seen_not_stored_detail": [by_mid[m] for m in sorted(seen_set - stored_set)
                                   if m in by_mid],
        "mailbox_not_seen_detail": [by_mid[m] for m in unseen_ids],
        "rows": rows,
    }
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\nwrote {out_path}  ({out_path.stat().st_size:,} bytes)")
    c = result["counts"]
    print(f"found {c['stored_found']}/{c['stored_rows']}, "
          f"passing {c['stored_passing']}, missing {c['stored_missing']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
