"""Bounded loopback console for the existing, approval-gated pair research CLI."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Lock, Thread
from uuid import uuid4
from weakref import finalize

from .contracts import fingerprint
from .history import FrozenHistory
from .journal import Journal
from .market_history import MAX_SESSIONS, calendar_2025
from .messages import run_id, sha
from .providers import ORNITH_PLANNER_MODEL, ORNITH_PLANNER_PROFILE
from .reporting import report_from_journal
from .research import release_fingerprint
from .stat_tools import MIN_SAMPLE, canonical_day


_ASSET = r"[A-Z0-9.-]{1,12}(?:/[A-Z]{3})?"
_PAIR = re.compile(rf"; A=({_ASSET}), B=({_ASSET});")
_ACTIVE = {"preparing", "awaiting_approval", "executing"}
_VERDICTS = {"continue", "revise", "stop", "unresolved"}
_RETAIN_JOBS = 12
_MAX_OUTPUT = 512 * 1024
_MAX_STATE = 16 * _MAX_OUTPUT
_EQUITY_HOLDOUT_START = date(2025, 11, 3)


class PairConsoleError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class _Input:
    path: Path
    history: FrozenHistory
    symbols: tuple[str, str]

    def summary(self) -> dict:
        return {"digest": self.history.manifest_sha256, "symbols": list(self.symbols),
                "start": self.history.window.start, "end": self.history.window.end,
                "classification": self.history.classification}


@dataclass(slots=True)
class _Job:
    id: str
    symbols: tuple[str, str]
    manifest: str | None
    state: str = "preparing"
    console: list[str] = field(default_factory=list)
    run_id: str | None = None
    candidates: list[dict] = field(default_factory=list)
    verdict: str | None = None
    approval: str | None = None
    interrupted: bool = False

    def response(self, item: _Input | None) -> dict:
        return {"id": self.id, "state": self.state, "symbols": list(self.symbols),
                "console": list(self.console), "run_id": self.run_id,
                "candidates": deepcopy(self.candidates), "verdict": self.verdict,
                "history": item.summary() if item else None}

    def receipt(self) -> dict:
        return {"id": self.id, "symbols": list(self.symbols), "manifest": self.manifest,
                "state": "executing" if self.interrupted else self.state,
                "console": self.console, "run_id": self.run_id,
                "approval": self.approval}


class PairConsole:
    """Resolve configured histories and run one console job at a time in a dedicated journal."""

    def __init__(self, journal: Path, histories: tuple[tuple[Path, FrozenHistory], ...],
                 waitlist: dict | None, cross_assets: dict | None, *, root: Path):
        self.journal = journal.resolve()
        self._root = root.resolve()
        self._cache = (self._root / ".cache" / "history").resolve()
        self._lock = Lock()
        self._jobs: dict[str, _Job] = {}
        self._captures: set[str] = set()
        self._state_path = self.journal.with_suffix(self.journal.suffix + ".console.json")
        self._release_lock = None
        self._closed = False
        self._assets: dict[str, str] = {}
        for kind, entries in (("stock", (waitlist or {}).get("entries", [])),
                              ("fx", (cross_assets or {}).get("fx", {}).get("entries", [])),
                              ("crypto", (cross_assets or {}).get("crypto", {}).get("entries", []))):
            for row in entries:
                self._assets.setdefault(row["symbol"], kind)
        self._inputs: dict[str, _Input] = {}
        for path, history in histories:
            if match := _PAIR.search(history.source):
                symbols = match.groups()
                if (history.classification != "synthetic" and any(self._assets.get(s) == "stock" for s in symbols)
                        and canonical_day(history.window.end) >= _EQUITY_HOLDOUT_START):
                    continue
                self._inputs[history.manifest_sha256] = _Input(path.resolve(), history, symbols)

    def _load_saved_state(self) -> None:
        if self._state_path.exists():
            self._claim()

    def _claim(self) -> None:
        if self._closed:
            raise PairConsoleError("The pair console is closed.", 409)
        if self._release_lock is not None:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.journal.with_suffix(self.journal.suffix + ".console.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise PairConsoleError("Another dashboard owns this pair-console journal.", 409) from None
        self._release_lock = finalize(self, os.close, descriptor)
        try:
            if self._state_path.exists():
                self._restore()
        except BaseException:
            self._release_lock()
            self._release_lock = None
            raise

    def close(self) -> None:
        with self._lock:
            if any(job.state in {"preparing", "executing"} for job in self._jobs.values()):
                raise PairConsoleError("Wait for the local worker before closing the pair console.", 409)
            if self._release_lock is not None:
                self._release_lock()
            self._closed = True

    def _response(self, job: _Job) -> dict:
        return job.response(self._inputs.get(job.manifest))

    def _save(self, job: _Job, *, captured: str | None = None) -> None:
        """Publish a phase only after its receipt is committed on the same volume."""
        jobs = {**self._jobs, job.id: job}
        if len(jobs) > _RETAIN_JOBS:
            del jobs[next(iter(jobs))]
        captures = self._captures | {captured} if captured else self._captures
        state = {"version": 1, "journal": str(self.journal), "captures": sorted(captures),
                 "jobs": [item.receipt() for item in jobs.values()]}
        payload = json.dumps({"state": state, "sha256": fingerprint(state)}, ensure_ascii=False).encode("utf-8")
        if len(payload) > _MAX_STATE:
            raise OSError("The pair-console receipt exceeds its storage limit")
        pending_path = None
        try:
            with NamedTemporaryFile(dir=self.journal.parent, prefix=".pair-console-", delete=False) as pending:
                pending_path = Path(pending.name)
                pending.write(payload)
                pending.flush()
                os.fsync(pending.fileno())
            pending_path.replace(self._state_path)
        finally:
            if pending_path is not None:
                pending_path.unlink(missing_ok=True)
        self._jobs, self._captures = jobs, captures

    def _restore(self) -> None:
        try:
            with self._state_path.open("rb") as receipt:
                raw = receipt.read(_MAX_STATE + 1)
            if len(raw) > _MAX_STATE:
                raise ValueError
            document = json.loads(raw)
            state = document["state"]
            if (document.keys() != {"state", "sha256"} or state.keys() != {"version", "journal", "captures", "jobs"}
                    or state["version"] != 1 or state["journal"] != str(self.journal)
                    or fingerprint(state) != sha(document["sha256"])
                    or not isinstance(state["captures"], list) or not isinstance(state["jobs"], list)
                    or len(state["jobs"]) > _RETAIN_JOBS):
                raise ValueError
            self._captures = {sha(digest) for digest in state["captures"]}
            for digest in self._captures:
                if digest not in self._inputs:
                    self._restore_capture(digest)
            jobs = [self._restore_job(item) for item in state["jobs"]]
            if (len({job.id for job in jobs}) != len(jobs)
                    or any(job.state in _ACTIVE for job in jobs[:-1])):
                raise ValueError
            self._jobs = {job.id: job for job in jobs}
        except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError):
            raise PairConsoleError("The saved pair-console receipt could not be verified.", 503) from None

    def _restore_capture(self, digest: str) -> None:
        path = self._cache / digest / "manifest.json"
        try:
            history = self._cached_history(path, digest)
            match = _PAIR.search(history.source)
            if (match is not None and history.classification == "public" and self._is_stock_pair(match.groups())
                    and canonical_day(history.window.end) < _EQUITY_HOLDOUT_START):
                self._inputs[digest] = _Input(path, history, match.groups())
        except (OSError, ValueError):
            pass  # A moved or changed cache artifact is no longer a selectable input.

    def _restore_job(self, data: dict) -> _Job:
        fields = {"id", "symbols", "manifest", "state", "console", "run_id", "approval"}
        if (type(data) is not dict or data.keys() != fields or type(data["symbols"]) is not list
                or len(data["symbols"]) != 2 or data["symbols"][0] == data["symbols"][1]
                or any(not isinstance(symbol, str) or not re.fullmatch(_ASSET, symbol) for symbol in data["symbols"])
                or type(data["console"]) is not list or any(type(line) is not str for line in data["console"])
                or data["state"] not in _ACTIVE | {"complete", "failed", "discarded"}):
            raise ValueError
        job = _Job(run_id(data["id"]), tuple(data["symbols"]),
                   sha(data["manifest"]) if data["manifest"] is not None else None,
                   data["state"], data["console"],
                   run_id(data["run_id"]) if data["run_id"] is not None else None,
                   approval=sha(data["approval"]) if data["approval"] is not None else None)
        item = self._inputs.get(job.manifest)
        if item is not None and item.symbols != job.symbols:
            raise ValueError
        if job.state == "preparing":
            return self._stopped(job, "Preparation was interrupted. Prepare the pair again to review a fresh plan.")
        if job.state == "awaiting_approval":
            try:
                journal = Journal(self.journal, read_only=True)
                events = journal.events(job.run_id)
                if (item is None or self._symbols(*job.symbols) != job.symbols or job.approval is not None
                        or [event["key"] for event in events] != ["plan"]):
                    raise ValueError
                plan = events[0]["value"]
                candidates = self._verified_candidates(plan, job.run_id, item.history)
                release = release_fingerprint()
                if (plan["status"] != "awaiting_approval" or not candidates
                        or any(candidate["protocol"]["source_sha256"] != release
                               for candidate in candidates)):
                    raise ValueError
                return replace(job, candidates=candidates)
            except (AttributeError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
                return self._stopped(job, "The saved plan can no longer be approved. Prepare a fresh plan.")
        if job.state in {"executing", "complete"} or (job.state == "failed" and job.approval is not None):
            recovered = self._finished_job(job)
            if recovered is not None:
                return recovered
            if job.state != "failed":
                stopped = self._stopped(job, "The console could not verify a finished report after restart. Review the "
                                             "saved run in the journal before taking action. Execution was not restarted.")
                return replace(stopped, interrupted=job.state == "executing")
        return job

    @staticmethod
    def _stopped(job: _Job, message: str) -> _Job:
        lines = [line for line in job.console if not line.startswith("Verified report\n")]
        return replace(job, state="failed", console=[*lines, message], verdict=None)

    def _finished_job(self, job: _Job) -> _Job | None:
        if job.run_id is None or job.approval is None:
            return None
        try:
            journal = Journal(self.journal, read_only=True)
            if journal.get(job.run_id, "verdict") is None:
                return None
            output = report_from_journal(journal, job.run_id)
            return self._with_report(job, output)
        except (AttributeError, KeyError, OSError, sqlite3.Error, TypeError, ValueError):
            return None

    def _recheck(self, job: _Job) -> _Job:
        if job.state == "failed" and job.approval is not None:
            recovered = self._finished_job(job)
            if recovered is not None:
                self._save(recovered)
                return recovered
        return job

    def _check_unfinished_execution(self) -> None:
        for job in tuple(self._jobs.values()):
            if job.state != "failed" or job.approval is None or job.run_id is None:
                continue
            if job.interrupted and self._recheck(job).state == "complete":
                continue
            try:
                events = Journal(self.journal, read_only=True).events(job.run_id)
            except (OSError, sqlite3.Error, ValueError):
                raise PairConsoleError("The previous approved run could not be checked.", 503) from None
            keys = {event["key"] for event in events}
            if any(key.startswith("reserved:") and "result:" + key[9:] not in keys for key in keys):
                raise PairConsoleError("A previous approved run has an unresolved tool reservation. "
                                       "Review the saved run before preparing another pair.", 409)
            if job.interrupted:
                raise PairConsoleError("A previous approved execution was interrupted. Reconcile the saved run "
                                       "before starting another pair in this console journal.", 409)

    def bootstrap(self) -> dict:
        with self._lock:
            self._load_saved_state()
            job = next(reversed(self._jobs.values()), None)
            return {"assets": [{"symbol": symbol, "kind": kind} for symbol, kind in self._assets.items()],
                    "job": self._response(self._recheck(job)) if job else None}

    def _symbols(self, a: object, b: object) -> tuple[str, str]:
        if type(a) is not str or type(b) is not str or a == b or a not in self._assets or b not in self._assets:
            raise PairConsoleError("Choose two distinct assets from the configured waitlists.")
        return a, b

    def _is_stock_pair(self, symbols: tuple[str, str]) -> bool:
        return all(self._assets.get(symbol) == "stock" for symbol in symbols)

    def history(self, a: str, b: str) -> dict:
        symbols = self._symbols(a, b)
        with self._lock:
            self._load_saved_state()
            return {"histories": [item.summary() for item in self._inputs.values()
                                  if item.symbols in (symbols, symbols[::-1])],
                    "capture_allowed": self._is_stock_pair(symbols)}

    def history_for_view(self, digest: str) -> FrozenHistory | None:
        """Resolve an exact journal-bound manifest without scanning unrelated exports."""
        try:
            sha(digest)
            with self._lock:
                known = self._inputs.get(digest)
            if known is not None:
                return known.history
            return self._cached_history(self._cache / digest / "manifest.json", digest)
        except (OSError, ValueError):
            return None

    def _cached_history(self, path: Path, digest: str | None = None) -> FrozenHistory:
        resolved = path.resolve(strict=True)
        if resolved.name != "manifest.json" or resolved.parent.parent != self._cache:
            raise ValueError("Expected a content-addressed history manifest")
        history = FrozenHistory.open(resolved)
        if history.manifest_sha256 != resolved.parent.name or (digest and history.manifest_sha256 != digest):
            raise ValueError("History manifest bytes do not match the expected digest")
        return history

    @staticmethod
    def _capture_window(value: object) -> tuple[str, str]:
        if type(value) is not dict or value.keys() != {"start", "end"}:
            raise PairConsoleError("Supply the first and last IEX sessions for the explicit capture.")
        try:
            start, end = canonical_day(value["start"]), canonical_day(value["end"])
            sessions = calendar_2025(start, end)
        except (TypeError, ValueError):
            raise PairConsoleError("Use a valid window on the reviewed 2025 stock calendar.") from None
        if end >= _EQUITY_HOLDOUT_START:
            raise PairConsoleError("Stock data capture must end by October 31, 2025, before the locked holdout.")
        if (not MIN_SAMPLE <= len(sessions) <= MAX_SESSIONS
                or sessions[0] != str(start) or sessions[-1] != str(end)):
            raise PairConsoleError("Choose 80–250 complete sessions on the reviewed 2025 stock calendar.")
        return str(start), str(end)

    def prepare(self, body: object) -> dict:
        fields = {"a", "b", "manifest", "capture"}
        if type(body) is not dict or not fields <= body.keys() <= fields | {"ornith"}:
            raise PairConsoleError("Supply two assets and one frozen history or explicit capture window.")
        ornith = body.get("ornith", False)
        if type(ornith) is not bool:
            raise PairConsoleError("Ornith guidance must be explicitly enabled or disabled.")
        symbols = self._symbols(body["a"], body["b"])
        digest, capture = body["manifest"], body["capture"]
        if (digest is None) == (capture is None):
            raise PairConsoleError("Choose exactly one frozen history or explicit capture window.")
        if capture is not None:
            if not self._is_stock_pair(symbols):
                raise PairConsoleError("FX and crypto pairs require a configured frozen history.")
            capture = self._capture_window(capture)
        with self._lock:
            self._load_saved_state()
            item = None
            if digest is not None:
                item = self._inputs.get(digest) if isinstance(digest, str) else None
                if item is None or item.symbols not in (symbols, symbols[::-1]):
                    raise PairConsoleError("Choose a configured frozen history for these two assets.")
                symbols = item.symbols
            self._claim()
            if any(job.state in _ACTIVE for job in self._jobs.values()):
                raise PairConsoleError("Finish or discard the current pair plan before preparing another.", 409)
            self._check_unfinished_execution()
            job = _Job(str(uuid4()), symbols, item.history.manifest_sha256 if item else None,
                       console=[f"Preparing {symbols[0]} / {symbols[1]}."])
            self._save(job)
            self._start(job, self._prepare, capture, ornith)
            return self._response(self._jobs[job.id])

    def _find(self, job_id: object) -> _Job:
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise PairConsoleError("This pair test is no longer in the console.", 404)
        return job

    def status(self, job_id: str) -> dict:
        with self._lock:
            self._load_saved_state()
            return self._response(self._recheck(self._find(job_id)))

    def approve(self, body: object) -> dict:
        if type(body) is not dict or body.keys() != {"job_id", "digest"}:
            raise PairConsoleError("Supply the console job and the exact protocol digest.")
        with self._lock:
            self._load_saved_state()
            job = self._find(body["job_id"])
            if job.state != "awaiting_approval":
                raise PairConsoleError("This pair test is no longer awaiting approval.", 409)
            digest = body["digest"]
            if not any(candidate["digest"] == digest for candidate in job.candidates):
                raise PairConsoleError("Approval must match one displayed protocol digest.")
            self._claim()
            self._check_unfinished_execution()
            job = replace(job, state="executing", approval=digest,
                          console=[*job.console, f"Approved {digest} for run {job.run_id}."])
            self._save(job)
            self._start(job, self._execute, digest)
            return self._response(self._jobs[job.id])

    def discard(self, body: object) -> dict:
        if type(body) is not dict or body.keys() != {"job_id"}:
            raise PairConsoleError("Supply the console job to discard.")
        with self._lock:
            self._load_saved_state()
            job = self._find(body["job_id"])
            if job.state != "awaiting_approval":
                raise PairConsoleError("Only a plan awaiting approval can be discarded from the console.", 409)
            self._claim()
            job = replace(job, state="discarded", console=[*job.console,
                          "Console plan discarded. The journal retains the plan and its audit history."])
            self._save(job)
            return self._response(job)

    def _start(self, job: _Job, step, *arguments) -> None:
        def work() -> None:
            try:
                step(job, *arguments)
            except Exception as error:
                message = str(error) if isinstance(error, PairConsoleError) else "The console could not verify this step."
                with self._lock:
                    self._fail(job, message)

        try:
            Thread(target=work, name="edgelab-pair-console", daemon=True).start()
        except RuntimeError:
            self._fail(job, "Could not start the local research worker.")

    def _fail(self, job: _Job, message: str) -> None:
        stopped = self._stopped(self._jobs[job.id], message)
        try:
            self._save(stopped)
        except OSError:
            self._jobs[job.id] = stopped

    def _log(self, job: _Job, message: str) -> None:
        with self._lock:
            self._jobs[job.id].console.append(message)

    def _command(self, module: str, arguments: list[str], *, timeout: int, label: str) -> dict:
        env = os.environ | {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "LANGSMITH_TRACING": "false",
                            "LANGCHAIN_TRACING_V2": "false"}
        try:
            result = subprocess.run([sys.executable, "-E", "-s", "-B", "-m", module, *arguments],
                                    cwd=self._root, env=env, encoding="utf-8", errors="replace",
                                    capture_output=True, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise PairConsoleError(f"{label} timed out. Inspect the journal before retrying a run.") from None
        except OSError:
            raise PairConsoleError(f"Could not start {label.lower()} in the local research environment.") from None
        if result.returncode:
            message = next((line for line in reversed(result.stderr.splitlines())
                            if line.startswith(("EdgeLab: ", "Market history: "))), "")
            raise PairConsoleError(message[:300] or f"{label} stopped with exit code {result.returncode}.")
        try:
            value = json.loads(result.stdout) if len(result.stdout) <= _MAX_OUTPUT else None
            if type(value) is not dict:
                raise ValueError
        except (ValueError, RecursionError):
            raise PairConsoleError(f"{label} did not return a valid result.") from None
        return value

    def _capstone(self, arguments: list[str], *, timeout: int, label: str) -> dict:
        return self._command("edgelab.capstone", ["--journal", str(self.journal), *arguments],
                             timeout=timeout, label=label)

    def _capture(self, job: _Job, capture: tuple[str, str]) -> _Input:
        start, end = capture
        a, b = job.symbols
        self._log(job, f"Reading Alpaca IEX daily closes for {a} / {b}, {start} through {end}.")
        result = self._command("edgelab.market_history",
                               [a, b, "--start", start, "--end", end, "--feed", "iex", "--allow-provider-io"],
                               timeout=75, label="Market history")
        path = (self._root / result["manifest"]).resolve(strict=True)
        history = self._cached_history(path)
        match = _PAIR.search(history.source)
        if (match is None or match.groups() != job.symbols or history.classification != "public"
                or (history.window.start, history.window.end) != capture):
            raise PairConsoleError("The returned history does not match the requested pair and window.")
        item = _Input(path, history, job.symbols)
        with self._lock:
            digest = history.manifest_sha256
            self._save(replace(self._jobs[job.id], manifest=digest), captured=digest)
            self._inputs[digest] = item
        return item

    def _prepare(self, job: _Job, capture: tuple[str, str] | None, ornith: bool) -> None:
        item = self._capture(job, capture) if capture is not None else self._inputs.get(job.manifest)
        if item is None:
            raise PairConsoleError("No frozen history was selected.")
        history = FrozenHistory.open(item.path)
        if history.manifest_sha256 != item.history.manifest_sha256:
            raise PairConsoleError("The selected manifest changed. A fresh verified snapshot is required.")
        self._log(job, "Frozen history\n" + json.dumps(item.summary(), indent=2))
        arguments = ["plan", "--history-manifest", str(item.path)]
        if ornith:
            settings = self._root / "providers.env"
            if not settings.is_file():
                raise PairConsoleError("Configure the local Ornith Planner in providers.env, or prepare without guidance.")
            self._log(job, "Planning with the local method notes and the blinded Ornith Planner.")
            arguments = ["--settings-file", str(settings), *arguments, "--lm-studio", "--model-id",
                         ORNITH_PLANNER_MODEL, "--allow-provider-io"]
        else:
            self._log(job, "Planning with the local method notes and no model adviser.")
            arguments.append("--no-model")
        try:
            plan = self._capstone(arguments, timeout=60, label="Planning")
        except PairConsoleError as error:
            if not ornith:
                raise
            raise PairConsoleError(f"{error} Check that Ornith is configured and loaded in local LM Studio.") from error
        planned_run = run_id(plan["run_id"])
        candidates = self._verified_candidates(plan, planned_run, history)
        summary = [{key: value for key, value in candidate.items() if key != "protocol"} for candidate in candidates]
        with self._lock:
            current = self._jobs[job.id]
            lines = [*current.console, "Saved plan\n" + json.dumps({"run_id": planned_run, "status": plan["status"],
                     "reason": plan.get("reason"), "candidates": summary}, indent=2)]
            if ornith and candidates and not any(candidate.get("recommended_by") for candidate in candidates):
                lines.append("Ornith guidance could not be verified. Check the Ornith profile or prepare without guidance.")
                state = "failed"
            elif candidates and plan["status"] == "awaiting_approval":
                state = "awaiting_approval"
            else:
                lines.append("The planner did not qualify a protocol for execution.")
                state = "failed"
            self._save(replace(current, run_id=planned_run, candidates=candidates, console=lines, state=state))

    @staticmethod
    def _verified_candidates(plan: dict, planned_run: str, history: FrozenHistory) -> list[dict]:
        if plan["run_id"] != planned_run or plan["input"]["history"] != history.identity():
            raise PairConsoleError("The saved run does not bind the selected frozen history.")
        candidates = []
        for candidate in plan["candidates"]:
            digest, protocol = sha(candidate["digest"]), candidate["protocol"]
            if (protocol["run_id"] != planned_run or protocol["history"] != history.identity()
                    or protocol["lag_policy"] not in {"AIC", "BIC"} or fingerprint(protocol) != digest):
                raise PairConsoleError("The planner did not return a verified protocol for this run.")
            candidates.append({"digest": digest, "lag_policy": protocol["lag_policy"], "protocol": protocol})
        if (plan.get("adviser_provider") == "lm_studio"
                and plan.get("planner_profile") == ORNITH_PLANNER_PROFILE
                and plan.get("role_models", {}).get("planner") == ORNITH_PLANNER_MODEL):
            rankings = [entry.get("choice") for entry in plan.get("model_decisions", [])
                        if entry.get("role") == "planner" and entry.get("stage") == "rank_depth_2"]
            if len(rankings) == 1:
                policy = {"eg_aic": "AIC", "eg_bic": "BIC"}.get(rankings[0])
                matches = [candidate for candidate in candidates if candidate["lag_policy"] == policy]
                if len(matches) == 1:
                    matches[0]["recommended_by"] = ORNITH_PLANNER_PROFILE
        return candidates

    def _execute(self, job: _Job, digest: str) -> None:
        item = self._inputs.get(job.manifest)
        if item is None:
            raise PairConsoleError("The approved history manifest is unavailable. Prepare a fresh plan.")
        history = FrozenHistory.open(item.path)
        if history.manifest_sha256 != item.history.manifest_sha256:
            raise PairConsoleError("The approved history manifest changed. Prepare a fresh plan.")
        self._log(job, "Executing the approved in-sample research through the controller.")
        execution = self._capstone(["execute", "--run", job.run_id, "--approve", digest,
                                    "--history-manifest", str(item.path)], timeout=180, label="Execution")
        if execution.get("run_id") != job.run_id or execution.get("status") != "finished":
            raise PairConsoleError("The controller did not finish the approved run.")
        self._log(job, f"Controller finished run {job.run_id}.")
        self._log(job, "Verifying the durable report and its approval binding.")
        output = self._capstone(["report", "--run", job.run_id], timeout=30, label="Reporting")
        with self._lock:
            self._save(self._with_report(self._jobs[job.id], output))

    def _with_report(self, job: _Job, output: dict) -> _Job:
        report = output["report"]
        approvals = [event["value"] for event in output["bundle"]["events"] if event["key"] == "approval"]
        item = self._inputs.get(job.manifest)
        if (output.get("run_id") != job.run_id or report.get("status") != "finished"
                or report.get("verdict") not in _VERDICTS or len(approvals) != 1 or job.approval is None
                or approvals[0]["digest"] != job.approval or job.manifest is None
                or approvals[0]["protocol"]["history"]["manifest_sha256"] != job.manifest
                or (item is not None and approvals[0]["protocol"]["history"] != item.history.identity())):
            raise PairConsoleError("No finished report matched the approved pair test.")
        lines = [line for line in job.console if not line.startswith("Verified report\n")]
        lines.append("Verified report\n" + json.dumps(report, indent=2, ensure_ascii=False))
        candidates = job.candidates
        if not candidates and item is not None:
            plan, = (event["value"] for event in output["bundle"]["events"] if event["key"] == "plan")
            candidates = self._verified_candidates(plan, job.run_id, item.history)
        return replace(job, console=lines, candidates=candidates, verdict=report["verdict"], state="complete",
                       interrupted=False)
