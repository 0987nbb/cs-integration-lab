# -*- coding: utf-8 -*-
"""Executable proof of Odoo 19 JSON-2 CREATE / READ / UPDATE / DELETE.

Run with ``python -m integration_service.cli --proof``. It exercises the four
verbs against ``res.partner`` using a clearly-marked scratch record, prints the
request and the response for each step, verifies the effect of every write, and
deletes the record again so the database is left as it was found.

Every printed line passes through :mod:`integration_service.sanitize`, so the
transcript can be pasted into a report without leaking the API key.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .errors import OdooError
from .sanitize import sanitize
from .sync_result import to_odoo_datetime, utcnow

#: Marker on the scratch record so it is obvious what it is if a run is aborted.
PROOF_PREFIX = "ZZ JSON-2 PROOF"
PROOF_MODEL = "res.partner"


class ProofFailure(Exception):
    """A proof step did not produce the expected effect."""


class Json2Proof:
    """Runs the CRUD sequence and records a transcript of it."""

    def __init__(self, client: Any, keep: bool = False) -> None:
        self.client = client
        self.keep = keep
        self.steps: List[Dict[str, Any]] = []
        self.record_id: Optional[int] = None

    # -- transcript ---------------------------------------------------------

    def _emit(self, verb: str, endpoint: str, payload: Any, response: Any,
              ok: bool = True, note: str = "") -> None:
        entry = {
            "step": len(self.steps) + 1,
            "verb": verb,
            "endpoint": sanitize(endpoint),
            "request": payload,
            "response": response,
            "ok": ok,
        }
        if note:
            entry["note"] = note
        self.steps.append(entry)

        status = "OK  " if ok else "FAIL"
        print(f"\n[{status}] {entry['step']}. {verb}  POST {entry['endpoint']}")
        print(f"       request : {sanitize(json.dumps(payload, default=str))[:400]}")
        print(f"       response: {sanitize(json.dumps(response, default=str))[:400]}")
        if note:
            print(f"       note    : {sanitize(note)}")

    def _endpoint(self, method: str) -> str:
        return f"{self.client.url}/json/2/{PROOF_MODEL}/{method}"

    # -- steps --------------------------------------------------------------

    def create(self) -> int:
        stamp = to_odoo_datetime(utcnow())
        vals = {
            "name": f"{PROOF_PREFIX} {stamp}",
            "email": "json2-proof@example.invalid",
            "comment": "<p>Temporary record created by the JSON-2 CRUD proof.</p>",
            "x_external_id": f"proof:json2:{int(time.time())}",
            "x_source_hash": "initial",
        }
        payload = {"vals_list": [vals]}
        ids = self.client.create(PROOF_MODEL, [vals])
        if not ids:
            raise ProofFailure("CREATE returned no id.")
        self.record_id = ids[0]
        self._emit("CREATE", self._endpoint("create"), payload, ids,
                   note=f"New {PROOF_MODEL} id={self.record_id}")
        return self.record_id

    def read(self, label: str = "READ", expect: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        fields = ["id", "name", "email", "x_external_id", "x_source_hash", "write_date"]
        payload = {"domain": [["id", "=", self.record_id]], "fields": fields}
        rows = self.client.search_read(
            PROOF_MODEL, [["id", "=", self.record_id]], fields=fields
        )
        if not rows:
            raise ProofFailure(f"{label} returned no record for id={self.record_id}.")
        row = rows[0]

        note = ""
        ok = True
        if expect:
            mismatched = {k: (row.get(k), v) for k, v in expect.items() if row.get(k) != v}
            if mismatched:
                ok = False
                note = f"Expected {expect}, observed mismatches: {mismatched}"
            else:
                note = f"Verified {', '.join(expect)} match the value just written."
        self._emit(label, self._endpoint("search_read"), payload, rows, ok=ok, note=note)
        if not ok:
            raise ProofFailure(note)
        return row

    def update(self) -> Dict[str, Any]:
        new_email = "json2-proof-updated@example.invalid"
        new_hash = "updated"
        vals = {"email": new_email, "x_source_hash": new_hash}
        payload = {"ids": [self.record_id], "vals": vals}
        response = self.client.write(PROOF_MODEL, [self.record_id], vals)
        self._emit("UPDATE", self._endpoint("write"), payload, response,
                   note="write returned true; the next READ verifies the stored values.")
        return {"email": new_email, "x_source_hash": new_hash}

    def delete(self) -> None:
        payload = {"ids": [self.record_id]}
        response = self.client.unlink(PROOF_MODEL, [self.record_id])
        self._emit("DELETE", self._endpoint("unlink"), payload, response,
                   note="Scratch record removed; the database is back to its original state.")

    # -- driver -------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        print("=" * 78)
        print("ODOO 19 JSON-2 API PROOF - CREATE / READ / UPDATE / DELETE")
        print("=" * 78)
        print(f"Instance : {sanitize(self.client.url)}")
        print(f"Database : {sanitize(self.client.database)}")
        print(f"Auth     : Authorization: Bearer [REDACTED] + X-Odoo-Database header")
        print(f"Model    : {PROOF_MODEL}")

        failed: Optional[str] = None
        try:
            self.create()
            self.read("READ (after create)", expect={"x_source_hash": "initial"})
            expected = self.update()
            self.read("READ (after update)", expect=expected)
            if self.keep:
                print(f"\n--keep given: leaving {PROOF_MODEL} id={self.record_id} in place.")
            else:
                self.delete()
        except (OdooError, ProofFailure) as exc:
            failed = sanitize(exc)
            print(f"\nPROOF FAILED: {failed}")
            if self.record_id and not self.keep:
                try:
                    self.client.unlink(PROOF_MODEL, [self.record_id])
                    print(f"Cleaned up scratch record id={self.record_id}.")
                except OdooError:
                    print(f"Could not clean up scratch record id={self.record_id}; remove it manually.")

        verbs = [s["verb"].split()[0] for s in self.steps if s["ok"]]
        print("\n" + "=" * 78)
        print(f"RESULT: {'PASS' if failed is None else 'FAIL'} - "
              f"{len(self.steps)} step(s), verbs proven: {', '.join(dict.fromkeys(verbs)) or 'none'}")
        print("=" * 78)

        return {
            "ok": failed is None,
            "error": failed,
            "record_id": self.record_id,
            "steps": self.steps,
        }


def run_proof(client: Any, keep: bool = False) -> Dict[str, Any]:
    return Json2Proof(client, keep=keep).run()


__all__ = ["Json2Proof", "ProofFailure", "run_proof"]
