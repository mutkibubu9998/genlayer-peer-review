"""Integration tests for PeerReview against GLSim (localnet).

Full consensus: leader + validators run the review with mocked
web/LLM responses (via transaction_context), so no real LLM API key is
needed.

Requires a running simulator: glsim --port 4000 --validators 5
Run: gltest tests/integration/ -v -s --network localnet
"""

import json
import urllib.request

from gltest import create_accounts, get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

TITLE = "Attention Is All You Need, Revisited"
ABSTRACT = "We revisit the Transformer architecture and show that a simplified attention mechanism retains full performance on translation benchmarks while halving training cost."
PAPER_URL = "https://example.com/papers/attention-revisited"
CONTEXT_TEXT = "Submitted to the ML Systems track; 12 pages, 3 tables."
GLSIM_URL = "http://127.0.0.1:4000/api"


def _rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GLSIM_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"GLSim RPC {method} failed: {payload['error']}")
    return payload["result"]


def _finding(severity, title, criterion="methodology"):
    return {"severity": severity, "title": title,
            "detail": "Detail for " + title + ".", "criterion": criterion}


def _review_context(verdict, findings):
    """Mocks for review: paper page + LLM verdict/findings."""
    return {
        "validators": [
            {
                "plugin_config": {
                    "mock_web_response": {
                        "nondet_web_request": {
                            PAPER_URL: {
                                "status": 200,
                                "body": "Paper page: Attention Is All You Need, Revisited. Full text of the paper.",
                            },
                        }
                    },
                    "mock_response": {
                        "response": {
                            # NOTE (genlayer-test 0.30): the mock layer
                            # auto-parses a JSON-string mock into a dict,
                            # but exec_prompt(response_format="json")
                            # expects the LLM to return JSON *text*, so the
                            # payload is double-encoded here (see direct
                            # tests for the same workaround).
                            ".*reviewing an academic paper.*": json.dumps(
                                json.dumps(
                                    {"verdict": verdict, "findings": findings,
                                     "rationale": "Review matches the paper page."}
                                )
                            )
                        }
                    },
                }
            }
        ],
    }


def _deploy():
    factory = get_contract_factory("PeerReview")
    contract = factory.deploy(args=[])
    alice, bob = create_accounts(2)
    return contract, alice, bob


def _register(contract, reporter):
    receipt = contract.connect(reporter).register_paper(
        args=[TITLE, ABSTRACT, PAPER_URL, CONTEXT_TEXT]
    ).transact()
    assert tx_execution_succeeded(receipt)
    return "paper_0"


def test_full_flow_register_review_minor_revision():
    contract, alice, bob = _deploy()
    paper_id = _register(contract, alice)

    paper = contract.get_paper(args=[paper_id]).call()
    assert paper["status"] == "OPEN"
    assert paper["paper_url"] == PAPER_URL
    assert paper["title"] == TITLE
    assert paper["abstract"] == ABSTRACT
    assert contract.get_paper_count(args=[]).call() == 1

    # Review with mocked paper page + LLM verdict (full consensus)
    receipt = contract.review(args=[paper_id]).transact(
        transaction_context=_review_context(
            "minor-revision",
            [_finding("medium", "Unclear figure caption", "clarity"),
             _finding("low", "Typo in section 3", "clarity")])
    )
    assert tx_execution_succeeded(receipt)
    paper = contract.get_paper(args=[paper_id]).call()
    assert paper["status"] == "REVIEWED"
    assert paper["verdict"] == "minor-revision"
    assert paper["critical_high_count"] == 0
    assert paper["total_findings"] == 2
    assert paper["rationale"] == "Review matches the paper page."
    assert paper["findings"][0]["title"] == "Unclear figure caption"


def test_full_flow_review_accept():
    contract, alice, bob = _deploy()
    paper_id = _register(contract, alice)

    receipt = contract.review(args=[paper_id]).transact(
        transaction_context=_review_context("accept", [])
    )
    assert tx_execution_succeeded(receipt)
    paper = contract.get_paper(args=[paper_id]).call()
    assert paper["status"] == "REVIEWED"
    assert paper["verdict"] == "accept"
    assert paper["total_findings"] == 0


def test_full_flow_review_reject():
    contract, alice, bob = _deploy()
    paper_id = _register(contract, alice)

    receipt = contract.review(args=[paper_id]).transact(
        transaction_context=_review_context(
            "reject",
            [_finding("critical", "Results do not replicate", "methodology"),
             _finding("high", "No statistical testing", "methodology")])
    )
    assert tx_execution_succeeded(receipt)
    paper = contract.get_paper(args=[paper_id]).call()
    assert paper["status"] == "REVIEWED"
    assert paper["verdict"] == "reject"
    assert paper["critical_high_count"] == 2
    assert paper["total_findings"] == 2


def test_close_paper_owner_only_and_review_closed_fails():
    contract, alice, bob = _deploy()
    paper_id = _register(contract, alice)

    # Submitter (alice) is not the contract owner - close must fail
    receipt = contract.connect(alice).close_paper(args=[paper_id]).transact()
    assert tx_execution_failed(receipt)
    assert contract.get_paper(args=[paper_id]).call()["status"] == "OPEN"

    # Owner (default account) closes the paper
    receipt = contract.close_paper(args=[paper_id]).transact()
    assert tx_execution_succeeded(receipt)
    assert contract.get_paper(args=[paper_id]).call()["status"] == "CLOSED"

    # A closed paper cannot be reviewed
    receipt = contract.review(args=[paper_id]).transact(
        transaction_context=_review_context(
            "minor-revision",
            [_finding("medium", "Unclear figure caption", "clarity")])
    )
    assert tx_execution_failed(receipt)


def test_review_open_only():
    contract, alice, bob = _deploy()

    # Reviewing a paper that was never registered fails
    receipt = contract.review(args=["paper_99"]).transact(
        transaction_context=_review_context(
            "minor-revision",
            [_finding("medium", "Unclear figure caption", "clarity")])
    )
    assert tx_execution_failed(receipt)
