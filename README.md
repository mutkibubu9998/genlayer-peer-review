# PeerReview — Academic Paper Review by GenLayer Consensus

A standalone GenLayer Intelligent Contract that reviews academic
papers: anyone registers a paper (title, abstract, paper URL, and
optional context text), and anyone can trigger a review. The leader
fetches the paper page and an LLM reviews it across novelty,
methodology and clarity, returning strict JSON with a verdict band
(`accept` / `minor-revision` / `major-revision` / `reject`), a
findings list with severities, plus a short rationale.

**Deployed on StudioNet (fee-charging Studio v0.123 / Consensus v0.6):**
DEPLOY PENDING — the StudioNet deploy failed twice on 2026-11-26
with a VM egress-proxy timeout (`Connection to establish proxy tunnel
timed out after 5000ms` reading the Studio chain id from
https://studio.genlayer.com/api); the contract itself is complete and
fully tested (see Tests below). Retry the deploy with the command in
the Deploy section below, then fill in the contract address and tx
hash here and in
`~/workspace/genlayer/submissions/peer-review-submission.md`.

## How it works

1. **Anyone registers a paper** (`register_paper`): title, abstract,
   paper URL, and optional context text.
2. **Anyone triggers a review** (`review`). The leader fetches the
   paper page and an LLM reviews it across novelty, methodology and
   clarity.
3. **Validators independently re-fetch the same page and re-run the
   same review.** The custom comparative validator accepts the review
   only when the verdict band matches exactly AND the finding counts
   agree within tolerance (critical+high within +/-1, total findings
   within +/-2) — no single leader can set a verdict alone. Finding
   wording, criteria and rationale may differ.
4. **The contract owner can close a paper** (`close_paper`), from OPEN
   or REVIEWED state.

No money moves in this contract at all: no GEN transfers, no payments,
no payable methods. It is a pure review primitive — the same contract
serves any paper because the title, abstract, URL, and context are
per-paper inputs, not hard-coded prompts.

## State machine

```
OPEN ──review (consensus verdict + findings)──▶ REVIEWED
  │                                              │
  └──────────────close_paper─────────────────────┘──▶ CLOSED
```

(close_paper is owner-only and works from OPEN or REVIEWED.)

## Why this is a reusable primitive, not a thin LLM wrapper

- The paper, the review, and the state transition are all on-chain
  and gated by independent validator re-verification of the same
  paper page — the LLM never decides alone.
- Equivalence is comparative on the decision fields (exact verdict
  band match + finding counts within tolerance); free-text rationales
  and individual findings may differ.
- The verdict is one of four fixed labels (`accept`,
  `minor-revision`, `major-revision`, `reject`); the contract
  validates the LLM output against that set, validates each finding's
  severity (critical / high / medium / low) and criterion (novelty /
  methodology / clarity), counts critical+high and total findings
  deterministically, and enforces verdict/finding consistency
  (accept requires no critical/high finding; minor-revision requires
  only medium/low findings and at least one finding; major-revision
  requires a high finding or at least four findings and no critical
  finding; reject requires a critical finding) — inconsistent LLM
  output is an LLM error, not a stored review.
- Error classification follows the GenLayer convention
  (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`) so
  validators compare failure paths correctly.
- The contract is generic: one deployment serves any paper.

## Contract methods

| Method | Type | Description |
|---|---|---|
| `register_paper(title, abstract, paper_url, context_text)` | write | Register a paper (anyone), returns `paper_N` |
| `review(paper_id)` | write | Consensus review, returns verdict + findings + counts + rationale |
| `close_paper(paper_id)` | write | Owner closes a paper |
| `get_paper(paper_id)` | view | Full paper record |
| `list_papers()` | view | All papers |
| `get_paper_count()` | view | Number of papers |

## Tests

```bash
# Direct mode (fast, in-memory; leader only, mocked web/LLM)
.venv/bin/python -m pytest tests/direct/ -v

# Integration (GLSim, full leader + validator consensus)
.venv/bin/glsim --port 4000 --validators 5
.venv/bin/gltest tests/integration/ -v -s --network localnet
```

- Direct: 7 tests — registration + validation, review accept, all
  four verdict bands, LLM validation, close (owner-only), close after
  review.
- Integration: 5 tests — full register → review minor-revision flow,
  review accept flow, review reject flow, close owner-only + review of
  a closed paper fails, review of an unregistered paper fails.

## Toolchain note — StudioNet is Consensus v0.6 / Studio v0.123

StudioNet is on the v0.6 line (fee-charging). Fee-less deploys are
rejected with `FeeValueMustBeNonZero`, and the old runner hash
(`py-genlayer:1jb45aa8...`) is gone from GenVM v0.6.0. This project
uses:

- Runner: `py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0`
- genlayer-py 0.19.0 / genlayer-test 0.30.0 (in `.venv/`, see
  `requirements.txt`), CLI 0.40.0.
- v0.4 SDK API changes applied to the contract: `import genlayer as gl`
  plus `from genlayer.storage import DynArray, TreeMap, allow`;
  `@allow` replaces `@allow_storage`; `gl.contract.Contract` replaces
  `gl.Contract`; `gl.vm.run_nondet` replaces `run_nondet_unsafe`;
  `gl.vm.UserError` carries `.data`, not `.message`.
- `genvm-lint` 0.11.0 (latest on PyPI) cannot validate the new runner
  (its SDK loader looks for a `.tar` runner that no longer exists):
  AST lint passes 3/3, SDK validation errors with E101. The direct +
  integration tests running on the real GenVM v0.6.0 runner are the
  effective validation until a newer linter ships. Note: on this VM the
  user-site `genvm-lint` lost its module path after a VM restart; run
  it as `PYTHONPATH=$HOME/.local/lib/python3.12/site-packages python3
  ~/.local/bin/genvm-lint check contracts/peerreview.py`.
  The contract file is pure ASCII, verified with `file` before testing.
- Test-harness workarounds for this toolchain pair (documented in the
  test files): direct + GLSim LLM mocks must double-encode the JSON
  payload (the mock layer auto-parses JSON once, the SDK parses again);
  the integration conftest patches `make_calldata_object` to the legacy
  `{"method": ...}` shape because the GLSim bundled with genlayer-test
  0.30.0 rejects the `{"": ...}` shape with "No method in calldata".

## Deploy (StudioNet, Consensus v0.6 — fee-charging, 0 GEN still works)

Studio v0.123 charges protocol fees, but a 0-GEN account can still
deploy: Studio credits the fee shortfall before debiting it, and unused
budget is refunded at finalization. Every deploy must carry a fee
distribution. The deploy below uses the distribution from StudioNet's
own `sim_getFeeConfig` defaults, with `executionBudgetPerRound` raised
to 10x the floor: the floor budget (78,852,000,000,000) fails the deploy
(leader execution runs out of budget — the deploy consumes
78,999,000,000,000), while 788,520,000,000,000 succeeds (verified on
the TranslationCheck deploy 2026-11-25).

```bash
genlayer network set studionet
printf '%s\n' "$(cat ~/workspace/genlayer/.builder-account-password)" | genlayer deploy \
  --contract contracts/peerreview.py \
  --fees '{"distribution":{"leaderTimeunitsAllocation":"100","validatorTimeunitsAllocation":"200","appealRounds":"0","executionBudgetPerRound":"788520000000000","executionConsumed":"0","totalMessageFees":"0","rotations":["0"],"maxPriceGenPerTimeUnit":"2","storageFeeMaxGasPrice":"300000000","receiptFeeMaxGasPrice":"300000000"}}'
```

> Note: pipe the password with a trailing newline (`printf '%s\n'`)
> — piping the file with `cat` alone (no newline) makes the CLI reject
> the password as invalid. The deploy was done with the CLI 0.40.0
> command shown here.

> Deploy status 2026-11-26: FAILED twice with `Error: Unable to read
> the Studio chain id from https://studio.genlayer.com/api: request to
> https://studio.genlayer.com/api failed, reason: Connection to
> establish proxy tunnel timed out after 5000ms` — the VM egress
> proxy could not open a tunnel to Studio within 5s (a direct curl to
> the same URL returned HTTP 405, so the endpoint itself is up).
> No transaction was broadcast; nothing was spent. Retry the deploy
> command above when the proxy is healthy.

`fee-profile.json` was generated by `gltest --fee-profile` against local
GLSim, but local GLSim has fees disabled so it measures all zeros; its
`deploy` entry was then set to the values that succeeded on StudioNet
(see above). Method entries remain the local (zero) measurements.

## License

MIT — see [LICENSE](LICENSE).
