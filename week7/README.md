# Week 7 — API Fundamentals

Student: FAN CHENHAO  
Student ID (last 4 digits): `3161` (`NN = 61`)  
IPv4 Allocation: `10.61.0.0/16`  
IPv6 Allocation: `fd00:3161::/32`

---

## Lab Wrap-up

### 1. What does `docker network inspect apilab` show, and how is it built from Week 6's building blocks?
`docker network inspect apilab` shows a `bridge` network on `172.18.0.0/16` (gateway `172.18.0.1`) with `api` (`172.18.0.2/16`, MAC `aa:df:68:75:ef:16`) and `client` (`172.18.0.3/16`, MAC `72:6c:2d:6e:d0:33`). Each container has its own Linux network namespace, linked via a `veth` pair into the Linux bridge Docker created for `apilab`. Docker also attaches a local DNS resolver to user-defined bridges, so `client` reaches `api` by hostname without knowing `172.18.0.2`.

### 2. For each status code in Step 2, what should a client do next — and which of them should it never retry?
Only `429` should be retried as-is. Wait the `Retry-After` interval (1 second here — in the 10-GET burst, requests 1–5 returned `200` and requests 6–10 hit `429`), then resend. All six other codes in `lab7/status.txt` come from a broken request or missing permission, so retrying the same call will fail forever:

- `404` (`GET /api/v1/vlans/77`): no such VLAN. Check the ID or URL.
- `405` (`POST /api/v1/vlans/10`): wrong method for a single VLAN; the `Allow` header lists `GET, PUT, PATCH, DELETE`.
- `400` (`POST /api/v1/vlans` with `"name": guest`): malformed JSON. Fix the body formatting before sending again.
- `422` (`PUT /api/v1/vlans/5000`): valid JSON, invalid VLAN number (must be 1–4094). Fix the input value.
- `409` (`POST /api/v1/vlans` with id `10`): VLAN 10 already exists. Read it or switch to `PUT /api/v1/vlans/10` if replacing it is what you meant.
- `403` (`PUT /api/v1/vlans/30` with the `viewer` token): the server authenticated `viewer`, saw a read-only role, and refused the write. Switch credentials rather than retrying.

### 3. Why did `retry_naive.py` create duplicates, and how exactly does your fix prevent them? Why must the `Idempotency-Key` be chosen once, before the first attempt?
`retry_naive.py` POSTs to `/api/v1/acl/entries` without an `Idempotency-Key`. When `lose_responses: 0.5` drops the socket right after the server saves ACL entry 5, `requests.post` raises `ConnectionError`, the script assumes the write never landed, and it sends the POST a second time — creating entry 6.

`retry_safe.py` generates one UUID key before the retry loop (`IDEM_KEY = f"acl-guests-no-ssh-{uuid.uuid4()}"`) and keeps it in `HEADERS`. In `lab7/audit-step4.txt`, attempt 1 dropped after writing entry 8, and attempt 2 resent the exact same request with that key. The server found the key and body hash in `state.idem` and returned the cached `201 Created` (`Idempotent-Replayed: true`), leaving `writes: 1`. Generating a new key per attempt would defeat deduplication because each retry would look like a fresh write; reusing a fixed key across separate script executions would instead return an old cached response (or `422` if the body changed).

### 4. How many requests did `inventory.py` send to read your devices, and why more than the number of pages?
There are 261 devices (`200 + 61`), and `inventory.py` requests `limit=10`, so pagination needs at least 27 requests (26 pages of 10 devices plus 1 page with the final device).

With `errors_503: 0.2` in Step 5, `inventory.py` sent 34 requests (7 extra calls from `503` retries and `429` waits when the 5 req/s bucket emptied). Under `--token-ttl 5 --rate 2` in Step 6, `get_page()` made 42 requests — not counting the three `POST /api/v1/auth/token` logins (initial login plus two re-logins after 5-second expiries). At 2 requests/s, reading 27 pages takes well over 10 seconds, so the client repeatedly hit both `429` rate limits and `401` expired-token errors and had to resend those failed page fetches.

### 5. What did you find in the OpenAPI document that you'd never have guessed?
Checking `/openapi.json` showed a few details not covered in the lecture notes: `GET /api/v1/devices` caps `limit` at `50` (minimum `1`, default `20`), `/api/v1/acl/entries/{id}` only supports `GET` and `DELETE` (no `PUT` or `PATCH`), and reusing an `Idempotency-Key` with a modified body fails with `422` instead of overwriting. The devices endpoint also takes a `site` query parameter (`kl-hq`, `pj-dc`, `penang`, `johor`). Calling `GET /api/v1/devices?site=kl-hq&limit=5` with `curl` returned `X-Total-Count: 66` and `sw-kl-hq-001` (`10.61.200.1`).

---

## Assignment (E4) — Explain it

### 1. How does `sync.py` decide what to change? Walk through one VLAN that was created, one that was renamed or deleted, and one ACL entry.
`sync.py` parses `intent.yaml` with `load_intent()`, fetches `/api/v1/vlans` and `/api/v1/acl/entries`, and hands both snapshots to `build_plan()`. VLAN 1 is ignored because it belongs to the API.

For a created VLAN like `30` (`guests`), `30` wasn't in the initial `{1, 10, 20, 99}` map, so `build_plan()` queued `(30, "guests")` in `vlan_creates` and `apply_plan()` sent `PUT /api/v1/vlans/30` (`201`). VLAN `10` existed as `"users"` instead of `"staff"`, which put it into `vlan_updates` (`PUT /api/v1/vlans/10` -> `200`). VLAN `99` (`old-guest`) was on the server but absent from `intent.yaml`, so `vlan_deletes` triggered `DELETE /api/v1/vlans/99` (`204`). ACL entries don't have stable names or IDs in YAML, so `normalize_acl()` turns each rule into `(action, src, dst, protocol, port, description)`. Comparing counts by that tuple showed `deny 10.61.30.0/24 -> 10.61.20.0/24 tcp/22` wasn't on the API yet, so `apply_plan()` posted it once (`201`, `id=1`).

### 2. For each kind of request your script sends, why is it safe to retry — or why doesn't your script retry it?
Read requests (`GET /api/v1/vlans`, `GET /api/v1/acl/entries`) never alter state, so `request()` retries them on `401`, `429`, `503`, `ConnectionError`, and `Timeout`. `PUT /api/v1/vlans/{id}` replaces the full resource at a fixed ID and is safe to resend; in `evidence/apply.txt`, `PUT /api/v1/vlans/40` lost two replies (`ConnectionError`) after creating VLAN 40, then got `200` on the third attempt with no duplicate created. `DELETE` on a VLAN or ACL ID is also retried — if the first attempt deleted the entry and dropped the socket, the retry gets `404`, which `request()` accepts when `attempt > 1`. `POST /api/v1/acl/entries` is only safe because `apply_plan()` attaches one `Idempotency-Key` before the retry loop, so the server replays the saved `201` (`Idempotent-Replayed: true`) instead of appending another rule. `POST /api/v1/auth/token` retries on `503` or socket drops, never on `401`. Any other `4xx` (`400`, `403`, `405`, `409`, `422`, or `404` on `GET`/`PUT`) aborts immediately with `RuntimeError`.

### 3. VLANs have PUT; ACL entries only have POST. How did that change your design, and how do you choose each `Idempotency-Key`? What would go wrong with a new key for every attempt — or the same key for every run?
`PUT /api/v1/vlans/{id}` lets the client pick the resource URL directly, so `sync.py` doesn't need an idempotency header for VLANs. ACL entries only expose `POST /api/v1/acl/entries` with server-assigned IDs. That forced `sync.py` to match existing rules by normalized fields (`normalize_acl()`), delete extras, and generate `f"sync-acl-{idx}-{uuid.uuid4()}"` once outside `request()` for each new rule. Picking a fresh UUID per attempt would break deduplication after a lost response because every retry would look like a brand-new rule. Reusing one static key across separate runs has the opposite problem: if someone deleted an ACL rule out-of-band and re-ran `sync.py`, the server would replay the old cached `201` (or return `422` if fields changed) instead of creating a new entry.

### 4. What happens if two people run `sync.py` with different intents at the same moment? What would the API need to make that safe?
Their `GET`s and writes would overlap. Each client builds a plan from what it saw milliseconds earlier, so the second `PUT` wins for any shared VLAN ID, one client can delete an ACL rule the other expects to keep, and both `POST`s can accumulate in the ACL list. After applying, the read-back check in `sync.py` would likely exit with an error because the live state no longer matches its own `intent.yaml`. Fixing that requires server-side concurrency control: `ETag` headers with `If-Match` preconditions (rejecting stale writes with `412 Precondition Failed`) or a global config version/lock around each sync.

### 5. Where are your credentials, and how did you make sure no token or password is in your repository?
`sync.py` reads `LAB_API_URL`, `LAB_USERNAME`, and `LAB_PASSWORD` from `os.environ` and keeps the bearer token in memory on `self.token` without writing it to disk. None of the committed files contain passwords or tokens, and the logged `curl` commands in `lab7/status.txt` and `evidence/apply.txt` use `$TOKEN` instead of literal token values.

---

## AI use

- **Level**: Level 3 (Open)
- **Tools**: AI coding assistant
- **Scope**: Assisted with drafting code, troubleshooting scripts, and reviewing written explanations; all experiments and outputs were run and verified locally.
