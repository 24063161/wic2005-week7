#!/usr/bin/env python3
"""Week 7 assignment: converge the lab API to match an intent YAML file.

Usage:
    python3 sync.py intent.yaml --dry-run
    python3 sync.py intent.yaml

Reads LAB_API_URL (default http://api:8000), LAB_USERNAME, and LAB_PASSWORD
from the environment.
"""
import argparse
import ipaddress
import os
import random
import sys
import time
import uuid
from collections import defaultdict

import requests
import yaml

API = os.environ.get("LAB_API_URL", "http://api:8000").rstrip("/")
MAX_TRANSIENT_TRIES = 6
MAX_REAUTH_TRIES = 5
MAX_RATE_RETRIES = 60


def normalize_acl(entry):
    """Return a canonical dict and hashable key for comparing ACL entries."""
    protocol = entry.get("protocol") or "any"
    port = entry.get("port")
    description = entry.get("description") or ""
    src = str(ipaddress.ip_network(entry["src"], strict=True))
    dst = str(ipaddress.ip_network(entry["dst"], strict=True))
    payload = {
        "action": entry["action"],
        "src": src,
        "dst": dst,
        "protocol": protocol,
        "description": description,
    }
    if port is not None:
        payload["port"] = int(port)
    key = (payload["action"], src, dst, protocol, payload.get("port"), description)
    return payload, key


def load_intent(path):
    """Load and validate desired VLANs and ACL entries from YAML."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        sys.exit(f"Invalid intent file {path}: expected a YAML mapping.")

    desired_vlans = {}
    for item in data.get("vlans", []):
        vid = int(item["id"])
        if vid == 1:
            sys.exit("VLAN 1 belongs to the API and must not be managed in intent.yaml.")
        if not 2 <= vid <= 4094:
            sys.exit(f"Invalid VLAN id {vid}: must be 2..4094.")
        if vid in desired_vlans:
            sys.exit(f"Duplicate VLAN id {vid} in {path}.")
        desired_vlans[vid] = str(item["name"])

    desired_acl = []
    for item in data.get("acl", []):
        payload, key = normalize_acl(item)
        desired_acl.append((payload, key))
    return desired_vlans, desired_acl


class SyncClient:
    """HTTP client with token refresh, 429 pacing, and safe-retry backoff."""

    def __init__(self, base_url):
        self.base_url = base_url
        self.token = None
        self.requests = 0
        self.writes = 0

    def login(self):
        """Authenticate using environment credentials and store the bearer token."""
        try:
            user = os.environ["LAB_USERNAME"]
            password = os.environ["LAB_PASSWORD"]
        except KeyError as exc:
            sys.exit(f"Missing required environment variable: {exc.args[0]}")

        for attempt in range(1, MAX_TRANSIENT_TRIES + 1):
            self.requests += 1
            try:
                r = requests.post(
                    f"{self.base_url}/api/v1/auth/token",
                    json={"username": user, "password": password},
                    timeout=5,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= MAX_TRANSIENT_TRIES:
                    raise RuntimeError(f"Login failed after {attempt} attempts: {exc}") from exc
                self._backoff_sleep(attempt)
                continue

            if r.status_code == 503:
                if attempt >= MAX_TRANSIENT_TRIES:
                    r.raise_for_status()
                self._backoff_sleep(attempt)
                continue
            r.raise_for_status()
            self.token = r.json()["token"]
            print(f"[auth] logged in as {user}")
            return

    @staticmethod
    def _backoff_sleep(attempt):
        time.sleep((2 ** (attempt - 1)) + random.uniform(0, 0.5))

    def request(self, method, url, json_body=None, idem_key=None):
        """Send one HTTP request, handling 401, 429, and transient 503/network drops."""
        if self.token is None:
            self.login()

        transient_tries = 0
        reauth_tries = 0
        rate_tries = 0
        attempt = 0

        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self.token}"}
            if idem_key is not None:
                headers["Idempotency-Key"] = idem_key

            self.requests += 1
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                self.writes += 1

            try:
                r = requests.request(method, url, json=json_body, headers=headers, timeout=5)
            except (requests.ConnectionError, requests.Timeout) as exc:
                transient_tries += 1
                if transient_tries >= MAX_TRANSIENT_TRIES:
                    raise RuntimeError(f"{method} {url} failed after {transient_tries} transient errors: {exc}") from exc
                print(f"[retry] {method} {url}: {type(exc).__name__}, backing off")
                self._backoff_sleep(transient_tries)
                continue

            if r.status_code == 401:
                reauth_tries += 1
                if reauth_tries > MAX_REAUTH_TRIES:
                    r.raise_for_status()
                print(f"[auth] 401 on {method} {url} — token expired, logging in again")
                self.login()
                continue

            if r.status_code == 429:
                rate_tries += 1
                if rate_tries > MAX_RATE_RETRIES:
                    r.raise_for_status()
                wait = float(r.headers.get("Retry-After", "1"))
                print(f"[rate] 429 on {method} {url} — sleeping {wait:.1f}s")
                time.sleep(wait)
                continue

            if r.status_code == 503:
                transient_tries += 1
                if transient_tries >= MAX_TRANSIENT_TRIES:
                    r.raise_for_status()
                print(f"[retry] 503 on {method} {url} — backing off")
                self._backoff_sleep(transient_tries)
                continue

            # On DELETE retries after a lost response, the first attempt already deleted it.
            if method == "DELETE" and r.status_code == 404 and attempt > 1:
                print(f"[ok] {method} {url} -> 404 on retry (already deleted)")
                return r

            if not 200 <= r.status_code < 300:
                raise RuntimeError(f"{method} {url} failed with {r.status_code}: {r.text.strip()}")
            return r

    def get_collection(self, path):
        """Read all items from a possibly paginated collection endpoint."""
        url = f"{self.base_url}{path}"
        items = []
        while url:
            r = self.request("GET", url)
            body = r.json()
            items.extend(body["items"])
            url = body.get("next")
        return items


def build_plan(current_vlans, current_acl, desired_vlans, desired_acl):
    """Compute VLAN and ACL operations needed to converge to desired state."""
    vlan_creates = []
    vlan_updates = []
    vlan_deletes = []

    for vid in sorted(desired_vlans):
        name = desired_vlans[vid]
        if vid not in current_vlans:
            vlan_creates.append((vid, name))
        elif current_vlans[vid] != name:
            vlan_updates.append((vid, current_vlans[vid], name))

    for vid in sorted(current_vlans):
        if vid != 1 and vid not in desired_vlans:
            vlan_deletes.append((vid, current_vlans[vid]))

    existing_by_key = defaultdict(list)
    for entry in sorted(current_acl, key=lambda e: e["id"]):
        _, key = normalize_acl(entry)
        existing_by_key[key].append(entry)

    acl_creates = []
    for payload, key in desired_acl:
        if existing_by_key[key]:
            existing_by_key[key].pop(0)
        else:
            acl_creates.append(payload)

    acl_deletes = []
    for leftovers in existing_by_key.values():
        acl_deletes.extend(leftovers)
    acl_deletes.sort(key=lambda e: e["id"])

    return {
        "vlan_creates": vlan_creates,
        "vlan_updates": vlan_updates,
        "vlan_deletes": vlan_deletes,
        "acl_creates": acl_creates,
        "acl_deletes": acl_deletes,
    }


def plan_is_empty(plan):
    return not any(plan.values())


def print_plan(plan):
    """Display planned creates, updates, and deletes clearly."""
    if plan_is_empty(plan):
        print("Plan: no changes needed (API already matches intent).")
        return
    print("Plan:")
    for vid, name in plan["vlan_creates"]:
        print(f"  + VLAN {vid} ({name}) [PUT /api/v1/vlans/{vid}]")
    for vid, old_name, new_name in plan["vlan_updates"]:
        print(f"  ~ VLAN {vid}: {old_name!r} -> {new_name!r} [PUT /api/v1/vlans/{vid}]")
    for vid, name in plan["vlan_deletes"]:
        print(f"  - VLAN {vid} ({name}) [DELETE /api/v1/vlans/{vid}]")
    for entry in plan["acl_creates"]:
        port_str = f"/{entry['port']}" if "port" in entry else ""
        print(f"  + ACL {entry['action']} {entry['src']} -> {entry['dst']} "
              f"{entry['protocol']}{port_str} ({entry['description']}) [POST /api/v1/acl/entries]")
    for entry in plan["acl_deletes"]:
        port_str = f"/{entry['port']}" if entry.get("port") is not None else ""
        print(f"  - ACL id={entry['id']} {entry['action']} {entry['src']} -> {entry['dst']} "
              f"{entry.get('protocol', 'any')}{port_str} [DELETE /api/v1/acl/entries/{entry['id']}]")


def read_state(client):
    vlans_raw = client.get_collection("/api/v1/vlans")
    current_vlans = {int(v["id"]): v["name"] for v in vlans_raw}
    current_acl = client.get_collection("/api/v1/acl/entries")
    return current_vlans, current_acl


def apply_plan(client, plan):
    """Execute planned changes with idempotent methods and per-POST Idempotency-Keys."""
    for vid, name in plan["vlan_creates"]:
        r = client.request("PUT", f"{client.base_url}/api/v1/vlans/{vid}", json_body={"name": name})
        print(f"[apply] PUT /api/v1/vlans/{vid} -> {r.status_code}")

    for vid, _, new_name in plan["vlan_updates"]:
        r = client.request("PUT", f"{client.base_url}/api/v1/vlans/{vid}", json_body={"name": new_name})
        print(f"[apply] PUT /api/v1/vlans/{vid} -> {r.status_code}")

    for vid, _ in plan["vlan_deletes"]:
        r = client.request("DELETE", f"{client.base_url}/api/v1/vlans/{vid}")
        print(f"[apply] DELETE /api/v1/vlans/{vid} -> {r.status_code}")

    for entry in plan["acl_deletes"]:
        eid = entry["id"]
        r = client.request("DELETE", f"{client.base_url}/api/v1/acl/entries/{eid}")
        print(f"[apply] DELETE /api/v1/acl/entries/{eid} -> {r.status_code}")

    for idx, entry in enumerate(plan["acl_creates"], start=1):
        # One unique key chosen before the retry loop ensures retries never duplicate this rule.
        idem_key = f"sync-acl-{idx}-{uuid.uuid4()}"
        r = client.request("POST", f"{client.base_url}/api/v1/acl/entries", json_body=entry, idem_key=idem_key)
        replayed = r.headers.get("Idempotent-Replayed", "false")
        created = r.json() if r.content else {}
        print(f"[apply] POST /api/v1/acl/entries -> {r.status_code} (id={created.get('id')}, replayed={replayed})")


def main():
    ap = argparse.ArgumentParser(description="Make the lab API match intent.yaml safely.")
    ap.add_argument("intent_file", help="Path to intent YAML file")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit without changing the API")
    args = ap.parse_args()

    desired_vlans, desired_acl = load_intent(args.intent_file)
    client = SyncClient(API)

    current_vlans, current_acl = read_state(client)
    plan = build_plan(current_vlans, current_acl, desired_vlans, desired_acl)
    print_plan(plan)

    if args.dry_run:
        print(f"Dry run complete: 0 writes sent ({client.requests} read/auth requests).")
        return

    if not plan_is_empty(plan):
        apply_plan(client, plan)

    final_vlans, final_acl = read_state(client)
    remaining = build_plan(final_vlans, final_acl, desired_vlans, desired_acl)
    if plan_is_empty(remaining):
        print(f"Converged: API state matches {args.intent_file} "
              f"({len(desired_vlans)} managed VLANs, {len(desired_acl)} ACL entries; "
              f"{client.writes} write attempts, {client.requests} total requests).")
    else:
        print("Verification failed — remaining differences:")
        print_plan(remaining)
        sys.exit(1)


if __name__ == "__main__":
    main()
