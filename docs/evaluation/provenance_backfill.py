"""Stage 1 — back-fill sender provenance onto rows stored before it was recorded.

Reads the Stage 0 capture (`provenance_audit.py`, output in the PROJECT directory)
and writes its four delivery-provenance values onto the matching rows. Matching is
on `source_message_id`. Nothing else is touched.

RECOVERED EVIDENCE, NOT AN OPERATOR ASSERTION. This is the opposite of the
alias-table case, where back-filling `alias_table_sha256` onto old rows was refused
because nobody knows which table resolved them and writing one would invent an
attribution. Here the values were fetched from the mailbox that still holds the
source messages, and every one was checked against the receiving MX's own
`Authentication-Results` (787 of 787, single sender, identical verdicts). Recording
them restores a fact the repository used to discard on every write; it does not claim
one.

The capture is NOT re-fetched and NOT re-verified here. If the file is missing,
this script stops rather than going to the network: re-fetching would be a new
observation, and a back-fill that quietly substitutes today's mailbox for the
captured evidence is no longer a back-fill.

No verdict column is written, so `verdict_digest` must not move. The caller checks
that; this script prints the digest before and after so a mismatch is visible even
when nobody thought to look.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ttr.projects.resolve import resolve_project          # noqa: E402
from ttr.storage.digest import verdict_digest             # noqa: E402
from ttr.storage.repository import DetectionRepository    # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default="Back Garden")
    ap.add_argument("--capture", type=Path, default=None,
                    help="Stage 0 capture (default: provenance_audit.json in the project dir).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be written; write nothing.")
    args = ap.parse_args()

    ctx = resolve_project(args.project).ctx
    capture = args.capture or (ctx.dir / "provenance_audit.json")
    if not capture.is_file():
        print(f"error: no capture at {capture}\n"
              f"Run docs/evaluation/provenance_audit.py first. This script will not "
              f"re-fetch: that would be a new observation, not a back-fill.")
        return 2

    data = json.loads(capture.read_text(encoding="utf-8"))
    print(f"capture   : {capture}")
    print(f"captured  : {data.get('captured_utc')}")
    print(f"rows in it: {len(data.get('rows', []))}")

    before = verdict_digest(ctx.db_path)
    print(f"digest before: {before}")

    repo = DetectionRepository(ctx.db_path)
    matched, unmatched, skipped = 0, [], []
    try:
        for row in data.get("rows", []):
            mid = row.get("message_id")
            if not row.get("present_in_mailbox"):
                skipped.append(mid)          # nothing was captured for it
                continue
            # NULL only when the header was genuinely ABSENT. A row whose raw
            # headers were not captured must not be written as absent, so it is
            # skipped rather than given a value that would read as a fact.
            raw = row.get("authentication_results_raw")
            if raw is None and row.get("ar_present"):
                skipped.append(mid)
                continue
            auth = json.dumps(raw, ensure_ascii=False) if raw else None
            if args.dry_run:
                matched += 1
                continue
            ok = repo.backfill_source_provenance(
                mid,
                auth_results_json=auth,
                from_header=row.get("from"),
                return_path=row.get("return_path") or None,
                received_shape=row.get("received_shape"),
            )
            if ok:
                matched += 1
            else:
                unmatched.append(mid)
    finally:
        repo.close()

    after = verdict_digest(ctx.db_path)
    print(f"digest after : {after}")
    print(f"{'would back-fill' if args.dry_run else 'back-filled'}: {matched}")
    if skipped:
        print(f"no capture for {len(skipped)} row(s):")
        for m in skipped:
            print(f"   {m}")
    if unmatched:
        print(f"NOT MATCHED in the database ({len(unmatched)}):")
        for m in unmatched:
            print(f"   {m}")
    if before != after:
        print("\nSTOP: the verdict digest MOVED. That means the digest's column set "
              "is wrong, not that this back-fill is — no verdict column was written.")
        return 1
    print("\nverdict digest unchanged — no analytical value moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
