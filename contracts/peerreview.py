# { "Depends": "py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0" }

"""PeerReview - academic paper review by GenLayer consensus.

Anyone registers a paper: a title, an abstract, the paper URL, and
optional context text. Anyone can then trigger a review. The leader
fetches the paper page (the abstract / landing page at the given URL)
and an LLM reviews it across novelty, methodology and clarity,
returning strict JSON: a verdict band (accept / minor-revision /
major-revision / reject), a findings list with severities, plus a
short rationale.

Every validator independently re-fetches the same page and re-runs
the same review; the review is accepted ONLY when the verdict band
matches exactly AND the finding counts agree within a stated
tolerance (critical+high count within +/-1, total findings within
+/-2), so no single leader can set a verdict alone. Free-text
rationales and the wording of individual findings may differ.

State machine:
    OPEN -> REVIEWED       (consensus review stored)
    OPEN -> CLOSED         (contract owner closes the paper)
    REVIEWED -> CLOSED     (contract owner closes after review)

No money moves in this contract at all: no GEN transfers, no payments,
no payable methods. It is a pure review primitive - the same contract
serves any paper because the title, abstract, URL, and context are
per-paper inputs, not hard-coded prompts.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import genlayer as gl
from genlayer import *
from genlayer.storage import DynArray, TreeMap, allow

# ---------------------------------------------------------------------------
# Error classification (validators compare these prefixes, see _handle_leader_error)
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"  # Business logic (deterministic) - exact match required
ERROR_EXTERNAL = "[EXTERNAL]"  # External API 4xx (deterministic) - exact match required
ERROR_TRANSIENT = "[TRANSIENT]"  # Network/5xx (non-deterministic) - agree if both transient
ERROR_LLM = "[LLM_ERROR]"  # LLM misbehavior - always disagree, force rotation

MAX_TITLE_LEN = 500
MAX_ABSTRACT_LEN = 5000
MAX_PAPER_URL_LEN = 500
MAX_CONTEXT_LEN = 2000
PAPER_CHARS = 12000
VERDICTS = ("accept", "minor-revision", "major-revision", "reject")
SEVERITIES = ("critical", "high", "medium", "low")
MAX_FINDINGS = 25


@allow
@dataclass
class Paper:
    id: str
    submitter: Address
    title: str
    abstract: str
    paper_url: str
    context_text: str
    status: str  # OPEN | REVIEWED | CLOSED
    verdict: str  # "" | accept | minor-revision | major-revision | reject
    findings_json: str  # JSON list of {severity, title, detail, criterion}
    critical_high_count: u256
    total_findings: u256
    rationale: str
    created_ts: u256
    reviewed_ts: u256


class PeerReview(gl.contract.Contract):
    owner: Address
    paper_count: u256
    papers: TreeMap[str, Paper]
    paper_ids: DynArray[str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.paper_count = u256(0)

    # ------------------------------------------------------------------
    # Helpers (deterministic)
    # ------------------------------------------------------------------
    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_paper(self, paper_id: str) -> Paper:
        if paper_id not in self.papers:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Paper not found: {paper_id}"
            )
        return self.papers[paper_id]

    def _paper_to_dict(self, p: Paper) -> dict:
        try:
            findings = json.loads(p.findings_json) if p.findings_json else []
        except Exception:
            findings = []
        return {
            "id": p.id,
            "submitter": p.submitter.as_hex,
            "title": p.title,
            "abstract": p.abstract,
            "paper_url": p.paper_url,
            "context_text": p.context_text,
            "status": p.status,
            "verdict": p.verdict,
            "findings": findings,
            "critical_high_count": int(p.critical_high_count),
            "total_findings": int(p.total_findings),
            "rationale": p.rationale,
            "created_ts": int(p.created_ts),
            "reviewed_ts": int(p.reviewed_ts),
        }

    def _fetch_paper(self, url: str) -> str:
        """Fetch one URL, classifying failures for consensus."""
        res = gl.nondet.web.get(url)
        if res.status == 200:
            pass
        elif 400 <= res.status < 500:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} URL returned {res.status}: {url}")
        else:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} URL returned {res.status}: {url}")
        try:
            return res.body.decode("utf-8", errors="ignore")[:PAPER_CHARS]
        except Exception:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} Paper page undecodable: {url}")

    def _parse_review(self, raw) -> dict:
        """Defensively parse the LLM review JSON (dict or raw string)."""
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw
            first = text.find("{")
            last = text.rfind("}")
            if first < 0 or last < 0:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
            text = text[first : last + 1]
            text = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)
            try:
                data = json.loads(text)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON unparsable")
        else:
            raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

        if not isinstance(data, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON is not an object")

        verdict = data.get("verdict")
        if verdict is None:
            for alt in ("decision", "outcome", "result"):
                if alt in data:
                    verdict = data[alt]
                    break
        if verdict is None:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Missing 'verdict'. Keys: {list(data.keys())}"
            )
        if not isinstance(verdict, str):
            verdict = str(verdict)
        verdict = verdict.strip().lower()
        if verdict not in VERDICTS:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Verdict not one of {VERDICTS}: {verdict}"
            )

        findings = data.get("findings")
        if findings is None:
            findings = []
        if not isinstance(findings, list):
            raise gl.vm.UserError(f"{ERROR_LLM} 'findings' is not a list")
        if len(findings) > MAX_FINDINGS:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Too many findings: {len(findings)}"
            )

        clean_findings = []
        critical_count = 0
        critical_high = 0
        for f in findings:
            if not isinstance(f, dict):
                raise gl.vm.UserError(f"{ERROR_LLM} Finding is not an object")
            severity = f.get("severity")
            if severity is None:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Finding missing 'severity'. Keys: {list(f.keys())}"
                )
            if not isinstance(severity, str):
                severity = str(severity)
            severity = severity.strip().lower()
            if severity not in SEVERITIES:
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Severity not one of {SEVERITIES}: {severity}"
                )
            title = f.get("title", "")
            if not isinstance(title, str):
                title = str(title)
            detail = f.get("detail", "")
            if not isinstance(detail, str):
                detail = str(detail)
            criterion = f.get("criterion", "")
            if not isinstance(criterion, str):
                criterion = str(criterion)
            criterion = criterion.strip().lower()
            if criterion and criterion not in ("novelty", "methodology", "clarity"):
                raise gl.vm.UserError(
                    f"{ERROR_LLM} Criterion not one of novelty/methodology/clarity: {criterion}"
                )
            if severity == "critical":
                critical_count += 1
            if severity in ("critical", "high"):
                critical_high += 1
            clean_findings.append(
                {
                    "severity": severity,
                    "title": title[:200],
                    "detail": detail[:500],
                    "criterion": criterion[:50],
                }
            )

        # Verdict and findings must be consistent.
        if verdict == "accept" and critical_high > 0:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Accept verdict with a critical/high finding: {critical_high}"
            )
        if verdict == "minor-revision" and (
            critical_high > 0 or len(clean_findings) == 0
        ):
            raise gl.vm.UserError(
                f"{ERROR_LLM} Minor-revision verdict needs only medium/low findings: "
                f"critical_high={critical_high} total={len(clean_findings)}"
            )
        if verdict == "major-revision" and (
            critical_count > 0
            or (critical_high == 0 and len(clean_findings) < 4)
        ):
            raise gl.vm.UserError(
                f"{ERROR_LLM} Major-revision verdict needs a high finding or at least 4 findings: "
                f"critical={critical_count} critical_high={critical_high} total={len(clean_findings)}"
            )
        if verdict == "reject" and critical_count == 0:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Reject verdict with no critical finding"
            )

        rationale = data.get("rationale")
        if rationale is None:
            for alt in ("reason", "explanation", "summary"):
                if alt in data:
                    rationale = data[alt]
                    break
        if rationale is None:
            rationale = ""
        if not isinstance(rationale, str):
            rationale = str(rationale)
        return {
            "verdict": verdict,
            "findings": clean_findings,
            "critical_high_count": critical_high,
            "total_findings": len(clean_findings),
            "rationale": rationale[:500],
        }

    def _error_text(self, res) -> str:
        """Extract the message text from a VM Result (v0.4 SDK: UserError carries .data, VMError carries .message)."""
        if isinstance(res, gl.vm.UserError):
            data = res.data
            return data if isinstance(data, str) else str(data)
        msg = getattr(res, "message", None)
        return msg if isinstance(msg, str) else str(res)

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical error comparison for validators (see write-contract skill)."""
        leader_msg = self._error_text(leaders_res)
        try:
            leader_fn()
            return False  # Leader errored, validator succeeded - disagree
        except gl.vm.UserError as e:
            validator_msg = self._error_text(e)
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
                ERROR_TRANSIENT
            ):
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Register a paper for review (anyone may register)
    # ------------------------------------------------------------------
    @gl.public.write
    def register_paper(
        self,
        title: str,
        abstract: str,
        paper_url: str,
        context_text: str,
    ) -> str:
        title = title.strip()
        if len(title) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title is empty")
        if len(title) > MAX_TITLE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Title too long")

        abstract = abstract.strip()
        if len(abstract) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Abstract is empty")
        if len(abstract) > MAX_ABSTRACT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Abstract too long")

        paper_url = paper_url.strip()
        if len(paper_url) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Paper URL is empty")
        if len(paper_url) > MAX_PAPER_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Paper URL too long")
        if not (
            paper_url.startswith("http://") or paper_url.startswith("https://")
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Paper URL must be http(s)")

        context_text = context_text.strip()
        if len(context_text) > MAX_CONTEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Context text too long")

        paper_id = f"paper_{int(self.paper_count)}"
        paper = Paper(
            id=paper_id,
            submitter=gl.message.sender_address,
            title=title,
            abstract=abstract,
            paper_url=paper_url,
            context_text=context_text,
            status="OPEN",
            verdict="",
            findings_json="",
            critical_high_count=u256(0),
            total_findings=u256(0),
            rationale="",
            created_ts=self._now(),
            reviewed_ts=u256(0),
        )
        self.papers[paper_id] = paper
        self.paper_ids.append(paper_id)
        self.paper_count = self.paper_count + u256(1)
        return paper_id

    # ------------------------------------------------------------------
    # Review a paper by consensus
    # ------------------------------------------------------------------
    @gl.public.write
    def review(self, paper_id: str) -> dict:
        paper = self._get_paper(paper_id)
        if paper.status != "OPEN":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Paper is not open: {paper.status}"
            )

        title = paper.title
        abstract = paper.abstract
        paper_url = paper.paper_url
        context_text = paper.context_text

        def leader_fn() -> dict:
            page = self._fetch_paper(paper_url)

            prompt = f"""You are reviewing an academic paper for a peer-review decision. Judge ONLY from the abstract and the paper page below; do not assume facts that are not in them.
Title: {title}
Abstract: {abstract}
Context from the submitter: {context_text if len(context_text) > 0 else "(none)"}
Paper page fetched from {paper_url}:
{page}

Review the paper across three criteria: novelty (is the contribution original and significant?), methodology (is the method sound, well described and adequately evaluated?) and clarity (is the paper well written and understandable?). Report real issues you can see in the abstract and the page; do not invent issues. Respond with JSON only, nothing else:
{{"verdict": "<one of: accept, minor-revision, major-revision, reject>", "findings": [{{"severity": "<one of: critical, high, medium, low>", "title": "<short title>", "detail": "<one sentence>", "criterion": "<one of: novelty, methodology, clarity>"}}], "rationale": "<one sentence summarising the verdict>"}}

Verdict meanings and finding rules:
- accept: no material issues; the paper is ready as-is (no critical or high findings)
- minor-revision: only medium or low issues, at least one finding; small fixes before publication
- major-revision: a high-severity issue, or at least four medium/low findings; substantial rework needed (no critical findings)
- reject: a critical-severity issue (fatal flaw in novelty, methodology or clarity) that makes the paper unsuitable"""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return self._parse_review(raw)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            leader_data = leaders_res.calldata
            if not isinstance(leader_data, dict):
                return False
            # Agreement rule: verdict band must match exactly, and finding
            # counts must agree within tolerance (critical+high within +/-1,
            # total findings within +/-2). Finding wording, criteria and
            # rationale may differ.
            if leader_data.get("verdict") != mine.get("verdict"):
                return False
            leader_ch = leader_data.get("critical_high_count")
            mine_ch = mine.get("critical_high_count")
            if leader_ch is None or mine_ch is None:
                return False
            if abs(int(leader_ch) - int(mine_ch)) > 1:
                return False
            leader_total = leader_data.get("total_findings")
            mine_total = mine.get("total_findings")
            if leader_total is None or mine_total is None:
                return False
            if abs(int(leader_total) - int(mine_total)) > 2:
                return False
            return True

        result = gl.vm.run_nondet(leader_fn, validator_fn)

        paper.verdict = result["verdict"]
        paper.findings_json = json.dumps(result["findings"])
        paper.critical_high_count = u256(result["critical_high_count"])
        paper.total_findings = u256(result["total_findings"])
        paper.rationale = result.get("rationale", "")
        paper.status = "REVIEWED"
        paper.reviewed_ts = self._now()
        self.papers[paper_id] = paper
        return {
            "verdict": paper.verdict,
            "findings": result["findings"],
            "critical_high_count": int(paper.critical_high_count),
            "total_findings": int(paper.total_findings),
            "rationale": paper.rationale,
        }

    # ------------------------------------------------------------------
    # Contract owner closes a paper (OPEN or REVIEWED)
    # ------------------------------------------------------------------
    @gl.public.write
    def close_paper(self, paper_id: str) -> None:
        paper = self._get_paper(paper_id)
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the owner can close a paper"
            )
        if paper.status == "CLOSED":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Paper is already closed")

        paper.status = "CLOSED"
        self.papers[paper_id] = paper

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_paper(self, paper_id: str) -> dict:
        return self._paper_to_dict(self._get_paper(paper_id))

    @gl.public.view
    def list_papers(self) -> dict:
        return {
            "papers": [
                self._paper_to_dict(self.papers[pid]) for pid in self.paper_ids
            ]
        }

    @gl.public.view
    def get_paper_count(self) -> u256:
        return self.paper_count
