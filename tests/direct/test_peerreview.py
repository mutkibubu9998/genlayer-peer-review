"""Direct-mode tests for PeerReview.

Fast, in-memory (~30ms). Leader function only - validator logic is NOT
exercised here (see tests/integration for full consensus). All web/LLM
calls are mocked.
"""

import json

TITLE = "Attention Is All You Need, Revisited"
ABSTRACT = "We revisit the Transformer architecture and show that a simplified attention mechanism retains full performance on translation benchmarks while halving training cost."
PAPER_URL = "https://example.com/papers/attention-revisited"
CONTEXT_TEXT = "Submitted to the ML Systems track; 12 pages, 3 tables."


def _hex(addr) -> str:
    """Raw 20-byte test address -> 0x-prefixed hex (contract returns checksummed)."""
    return "0x" + bytes(addr).hex()


def _register(contract, direct_vm, requester, title=TITLE, abstract=ABSTRACT,
              paper_url=PAPER_URL, context=CONTEXT_TEXT):
    direct_vm.sender = requester
    return contract.register_paper(title, abstract, paper_url, context)


def _mock_review(direct_vm, verdict, findings,
                 paper_page="Paper page: Attention Is All You Need, Revisited. Full text of the paper."):
    # NOTE (genlayer-test 0.30): mock_llm auto-parses a JSON-string mock into
    # a dict, but the v0.4 SDK's exec_prompt(response_format="json") expects
    # the LLM to return JSON *text*, so the payload must be double-encoded:
    # the mock layer parses once (back to JSON text) and the SDK parses again.
    direct_vm.mock_web(
        r".*example\.com/papers/attention-revisited.*",
        {"status": 200, "body": paper_page},
    )
    direct_vm.mock_llm(
        r".*reviewing an academic paper.*",
        json.dumps(json.dumps({"verdict": verdict, "findings": findings,
                    "rationale": "Review matches the paper page."})),
    )


def _finding(severity, title, criterion="methodology"):
    return {"severity": severity, "title": title,
            "detail": "Detail for " + title + ".", "criterion": criterion}


def test_register_paper(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/peerreview.py")

    paper_id = _register(contract, direct_vm, direct_alice)
    assert paper_id == "paper_0"
    assert contract.get_paper_count() == 1

    p = contract.get_paper(paper_id)
    assert p["submitter"].lower() == _hex(direct_alice).lower()
    assert p["title"] == TITLE
    assert p["abstract"] == ABSTRACT
    assert p["paper_url"] == PAPER_URL
    assert p["context_text"] == CONTEXT_TEXT
    assert p["status"] == "OPEN"
    assert p["verdict"] == ""
    assert p["findings"] == []
    assert p["critical_high_count"] == 0
    assert p["total_findings"] == 0
    assert p["reviewed_ts"] == 0

    listed = contract.list_papers()
    assert len(listed["papers"]) == 1
    assert listed["papers"][0]["id"] == "paper_0"

    # A second submitter (bob) can register too - anyone may register
    paper_id2 = _register(contract, direct_vm, direct_bob)
    assert paper_id2 == "paper_1"
    assert contract.get_paper_count() == 2
    assert contract.get_paper("paper_1")["submitter"].lower() == _hex(direct_bob).lower()


def test_register_paper_validation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/peerreview.py")
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("[EXPECTED] Title is empty"):
        contract.register_paper("   ", ABSTRACT, PAPER_URL, CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Abstract is empty"):
        contract.register_paper(TITLE, "", PAPER_URL, CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Paper URL is empty"):
        contract.register_paper(TITLE, ABSTRACT, "", CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Paper URL must be http(s)"):
        contract.register_paper(TITLE, ABSTRACT, "ftp://example.com/paper", CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Title too long"):
        contract.register_paper("x" * 501, ABSTRACT, PAPER_URL, CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Abstract too long"):
        contract.register_paper(TITLE, "x" * 5001, PAPER_URL, CONTEXT_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Paper not found: paper_99"):
        contract.get_paper("paper_99")


def test_review_accept(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/peerreview.py")
    paper_id = _register(contract, direct_vm, direct_alice)

    # Cannot review a paper that does not exist
    with direct_vm.expect_revert("[EXPECTED] Paper not found: paper_99"):
        contract.review("paper_99")

    _mock_review(direct_vm, "accept", [])
    result = contract.review(paper_id)
    assert result["verdict"] == "accept"
    assert result["findings"] == []
    assert result["critical_high_count"] == 0
    assert result["total_findings"] == 0
    p = contract.get_paper(paper_id)
    assert p["status"] == "REVIEWED"
    assert p["verdict"] == "accept"
    assert p["reviewed_ts"] > 0

    # Reviewing twice reverts
    with direct_vm.expect_revert("[EXPECTED] Paper is not open"):
        contract.review(paper_id)


def test_review_all_verdicts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/peerreview.py")
    p0 = _register(contract, direct_vm, direct_alice)
    p1 = _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    p2 = contract.register_paper(
        "A Second Paper", "Abstract of the second paper.",
        PAPER_URL, "No context."
    )

    # minor-revision: only medium/low findings
    _mock_review(direct_vm, "minor-revision",
                 [_finding("medium", "Unclear figure caption", "clarity"),
                  _finding("low", "Typo in section 3", "clarity")])
    result = contract.review(p0)
    assert result["verdict"] == "minor-revision"
    assert result["critical_high_count"] == 0
    assert result["total_findings"] == 2
    assert result["findings"][0]["criterion"] == "clarity"
    assert contract.get_paper(p0)["status"] == "REVIEWED"

    # major-revision: one high finding
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "major-revision",
                 [_finding("high", "Baseline missing", "methodology"),
                  _finding("medium", "Ablation incomplete", "methodology")])
    result = contract.review(p1)
    assert result["verdict"] == "major-revision"
    assert result["critical_high_count"] == 1
    assert result["total_findings"] == 2
    assert contract.get_paper(p1)["status"] == "REVIEWED"

    # reject: one critical finding
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "reject",
                 [_finding("critical", "Results do not replicate", "methodology"),
                  _finding("high", "No statistical testing", "methodology")])
    result = contract.review(p2)
    assert result["verdict"] == "reject"
    assert result["critical_high_count"] == 2
    assert result["total_findings"] == 2
    assert contract.get_paper(p2)["status"] == "REVIEWED"


def test_review_llm_validation(direct_vm, direct_deploy, direct_alice):
    """Invalid verdicts, bad severities and verdict/finding mismatches are LLM errors."""
    contract = direct_deploy("contracts/peerreview.py")
    paper_id = _register(contract, direct_vm, direct_alice)

    direct_vm.mock_web(
        r".*example\.com/papers/attention-revisited.*",
        {"status": 200, "body": "Paper page."},
    )
    direct_vm.mock_llm(
        r".*reviewing an academic paper.*",
        json.dumps(json.dumps({"verdict": "maybe", "findings": [],
                    "rationale": "Not valid."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] Verdict not one of"):
        contract.review(paper_id)

    # accept with a high finding is inconsistent
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "accept",
                 [_finding("high", "Baseline missing", "methodology")])
    with direct_vm.expect_revert("[LLM_ERROR] Accept verdict with a critical/high finding"):
        contract.review(paper_id)

    # reject with no critical finding is inconsistent
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "reject",
                 [_finding("high", "Baseline missing", "methodology")])
    with direct_vm.expect_revert("[LLM_ERROR] Reject verdict with no critical finding"):
        contract.review(paper_id)

    # major-revision with a critical finding is inconsistent
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "major-revision",
                 [_finding("critical", "Fatal flaw", "novelty")])
    with direct_vm.expect_revert("[LLM_ERROR] Major-revision verdict needs a high finding"):
        contract.review(paper_id)

    # minor-revision with no findings is inconsistent
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "minor-revision", [])
    with direct_vm.expect_revert("[LLM_ERROR] Minor-revision verdict needs only medium/low findings"):
        contract.review(paper_id)

    # unknown severity is an LLM error
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "minor-revision",
                 [_finding("severe", "Something bad", "clarity")])
    with direct_vm.expect_revert("[LLM_ERROR] Severity not one of"):
        contract.review(paper_id)

    # unknown criterion is an LLM error
    direct_vm.clear_mocks()
    _mock_review(direct_vm, "minor-revision",
                 [{"severity": "low", "title": "Typo", "detail": "Typo.",
                   "criterion": "style"}])
    with direct_vm.expect_revert("[LLM_ERROR] Criterion not one of"):
        contract.review(paper_id)


def test_close_paper(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    contract = direct_deploy("contracts/peerreview.py")
    paper_id = _register(contract, direct_vm, direct_alice)

    # The submitter (alice) is NOT the contract owner here (deployer is the
    # default test account), so closing as alice must revert
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the owner can close a paper"):
        contract.close_paper(paper_id)

    # Owner closes an OPEN paper
    direct_vm.sender = direct_owner
    contract.close_paper(paper_id)
    assert contract.get_paper(paper_id)["status"] == "CLOSED"

    # Cannot close twice
    with direct_vm.expect_revert("[EXPECTED] Paper is already closed"):
        contract.close_paper(paper_id)

    # A closed paper cannot be reviewed
    with direct_vm.expect_revert("[EXPECTED] Paper is not open"):
        contract.review(paper_id)


def test_close_after_review(direct_vm, direct_deploy, direct_alice, direct_owner):
    contract = direct_deploy("contracts/peerreview.py")
    paper_id = _register(contract, direct_vm, direct_alice)
    _mock_review(direct_vm, "minor-revision",
                 [_finding("medium", "Unclear figure caption", "clarity")])
    result = contract.review(paper_id)
    assert result["verdict"] == "minor-revision"

    # Owner closes after review; the verdict and findings are kept
    direct_vm.sender = direct_owner
    contract.close_paper(paper_id)
    p = contract.get_paper(paper_id)
    assert p["status"] == "CLOSED"
    assert p["verdict"] == "minor-revision"
    assert p["total_findings"] == 1
    assert p["findings"][0]["title"] == "Unclear figure caption"
