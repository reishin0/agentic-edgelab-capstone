"""Local durable Code Mode execution with host-owned approval and call replay."""
import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import jsonschema

from .auth import authorized, principal
from .backend import OperationNotApplied
from .code_mode import CodeModeError, ExecutionPaused, ROOT


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class WriteNotApplied(ExecutionPaused):
    def __init__(self, sequence):
        super().__init__("failed")
        self.sequence = sequence


class WorkflowJournal:
    """One gateway owns a database. Every transition commits before external work."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock_file = None

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        try:
            self.lock_file.seek(0)
            if self.lock_file.read(1) == b"":
                self.lock_file.write(b"0")
                self.lock_file.flush()
            self.lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            raise CodeModeError("Workflow database already belongs to a running gateway") from None
        try:
            with self.connect() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS workflows (
                        id TEXT PRIMARY KEY, subject TEXT NOT NULL, request_id TEXT NOT NULL,
                        roles TEXT NOT NULL, code TEXT NOT NULL, code_hash TEXT NOT NULL,
                        contract TEXT NOT NULL, seed INTEGER NOT NULL, created REAL NOT NULL,
                        updated REAL NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
                        UNIQUE(subject, request_id)
                    );
                    CREATE TABLE IF NOT EXISTS operations (
                        workflow_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                        capability TEXT NOT NULL, arguments TEXT NOT NULL, binding TEXT NOT NULL,
                        status TEXT NOT NULL, result TEXT, approval_id TEXT, expires REAL,
                        PRIMARY KEY(workflow_id, sequence)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        number INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
                        sequence INTEGER, status TEXT NOT NULL, time REAL NOT NULL
                    );
                """)
                db.execute("UPDATE operations SET status='uncertain' WHERE status='inflight'")
                rows = db.execute("SELECT id FROM workflows WHERE status='running'").fetchall()
                for row in rows:
                    self.state(db, row["id"], "interrupted", error="Gateway stopped during execution")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    @contextmanager
    def connect(self):
        if self.lock_file is None:
            raise CodeModeError("Workflow journal is not open")
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, workflow_id, status, sequence=None):
        db.execute("INSERT INTO events(workflow_id, sequence, status, time) VALUES(?,?,?,?)",
                   (workflow_id, sequence, status, time.time()))

    def state(self, db, workflow_id, status, *, error=None, result=None):
        db.execute("UPDATE workflows SET status=?, updated=?, error=?, result=? WHERE id=?",
                   (status, time.time(), error, encode(result) if result is not None else None, workflow_id))
        self.event(db, workflow_id, status)


class DurableWorkflows:
    def __init__(self, surface):
        self.surface = surface
        self.settings = surface.gateway.settings
        self.journal = WorkflowJournal(self.settings.durable.database)

    def contract(self):
        sources = sorted(Path(__file__).resolve().parent.glob("*.py"))
        sources += [ROOT / "code_runtime/runner.mjs", ROOT / "code_runtime/pnpm-lock.yaml", ROOT / "uv.lock"]
        files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        specs = {backend.name: hashlib.sha256(Path(backend.spec_file).read_bytes()).hexdigest()
                 for backend in self.settings.backends if backend.spec_file}
        return digest({"files": files, "specs": specs,
                       "backends": [backend.model_dump() for backend in self.settings.backends],
                       "limits": self.settings.code_mode.model_dump(),
                       "approval_seconds": self.settings.durable.approval_seconds})

    def owned(self, db, workflow_id):
        user = principal.get()
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if (user is None or row is None or row["subject"] != user.subject
                or row["roles"] != encode(sorted(user.roles))):
            raise CodeModeError("Workflow unavailable or access denied")
        return row

    def check_context(self, row):
        if (row["roles"] != encode(sorted(principal.get().roles)) or row["contract"] != self.contract()
                or row["code_hash"] != digest(row["code"])):
            raise CodeModeError("Workflow identity roles or execution configuration changed")

    def snapshot(self, workflow_id):
        with self.journal.connect() as db:
            row = self.owned(db, workflow_id)
            operations = db.execute("SELECT * FROM operations WHERE workflow_id=? ORDER BY sequence", (workflow_id,)).fetchall()
            result = {"execution_id": row["id"], "status": row["status"], "code_hash": row["code_hash"],
                      "created_at": row["created"], "updated_at": row["updated"],
                      "result": json.loads(row["result"]) if row["result"] else None, "error": row["error"],
                      "operations": []}
            for operation in operations:
                result["operations"].append({"sequence": operation["sequence"], "capability": operation["capability"],
                    "arguments": json.loads(operation["arguments"]), "status": operation["status"],
                    "binding": operation["binding"], "approval_id": operation["approval_id"], "expires_at": operation["expires"]})
            return result

    async def start(self, arguments):
        schema = {"type": "object", "properties": {"code": {"type": "string", "minLength": 1},
                  "request_id": {"type": "string", "minLength": 1, "maxLength": 128}},
                  "required": ["code", "request_id"], "additionalProperties": False}
        self.validate(arguments, schema)
        code = arguments["code"]
        if len(code.encode()) > self.settings.code_mode.max_code_bytes:
            raise CodeModeError("Code exceeds the configured size limit")
        user = principal.get()
        now, workflow_id = time.time(), uuid.uuid4().hex
        with self.journal.connect() as db:
            existing = db.execute("SELECT * FROM workflows WHERE subject=? AND request_id=?",
                                  (user.subject, arguments["request_id"])).fetchone()
            if existing:
                if existing["code"] != code:
                    raise CodeModeError("request_id already belongs to different code")
                workflow_id = existing["id"]
            else:
                if db.execute("SELECT count(*) FROM workflows").fetchone()[0] >= self.settings.durable.max_workflows:
                    raise CodeModeError("Workflow journal capacity reached")
                db.execute("INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (workflow_id, user.subject, arguments["request_id"], encode(sorted(user.roles)), code,
                     digest(code), self.contract(), int(workflow_id[:8], 16), now, now, "ready", None, None))
                self.journal.event(db, workflow_id, "created")
        if not existing:
            return await self.resume(workflow_id)
        return self.snapshot(workflow_id)

    @staticmethod
    def validate(arguments, schema):
        try:
            jsonschema.validate(arguments, schema)
        except jsonschema.ValidationError:
            raise CodeModeError("Invalid workflow arguments") from None

    async def authorize(self, capability, arguments):
        if capability in {"@platform/search", "@platform/describe"}:
            # Discovery results are journaled as well. Roles and configuration are pinned.
            return True
        backend_name, separator, method = capability.partition("__")
        connector = self.surface.gateway.connectors.get(backend_name)
        if not separator or connector is None or not authorized(principal.get(), connector.config, method):
            raise CodeModeError("Capability unavailable or access denied")
        catalog = await self.surface.catalog()
        definition = next((item for item in catalog if item["name"] == capability), None)
        if definition is None:
            raise CodeModeError("Capability unavailable or access denied")
        self.validate(arguments, definition["inputSchema"])
        return self.surface.read_only(capability)

    def pending(self, db, workflow_id, sequence):
        db.execute("UPDATE operations SET status='pending', approval_id=?, expires=? WHERE workflow_id=? AND sequence=?",
                   (uuid.uuid4().hex, time.time() + self.settings.durable.approval_seconds, workflow_id, sequence))
        self.journal.event(db, workflow_id, "pending", sequence)

    async def resume(self, workflow_id):
        with self.journal.connect() as db:
            row = self.owned(db, workflow_id)
            self.check_context(row)
            should_run = row["status"] in {"ready", "interrupted", "awaiting_approval"}
            if should_run:
                self.journal.state(db, workflow_id, "running")
        if not should_run:
            return self.snapshot(workflow_id)
        call_lock, visited = asyncio.Lock(), set()
        halt_status = None

        async def dispatch(sequence, capability, arguments):
            async with call_lock:
                if halt_status is not None:
                    raise ExecutionPaused(halt_status)
                if "__" not in capability and "." in capability:
                    capability = capability.replace(".", "__", 1)
                read_only = await self.authorize(capability, arguments)
                binding = digest([row["code_hash"], row["contract"], sequence, capability, arguments])
                visited.add(sequence)
                with self.journal.connect() as db:
                    operation = db.execute("SELECT * FROM operations WHERE workflow_id=? AND sequence=?", (workflow_id, sequence)).fetchone()
                    if operation and operation["binding"] != binding:
                        raise CodeModeError("Workflow replay diverged from its recorded operations")
                    if operation and operation["status"] == "completed":
                        return json.loads(operation["result"])
                    if operation and operation["status"] in {"inflight", "uncertain"}:
                        raise ExecutionPaused("needs_reconciliation")
                    if operation is None:
                        db.execute("INSERT INTO operations(workflow_id,sequence,capability,arguments,binding,status) VALUES(?,?,?,?,?,?)",
                                   (workflow_id, sequence, capability, encode(arguments), binding, "prepared"))
                        self.journal.event(db, workflow_id, "prepared", sequence)
                    approved_write = (not read_only and operation is not None and operation["status"] == "approved"
                                      and operation["expires"] is not None and operation["expires"] > time.time())
                    if not read_only and not approved_write:
                        if operation is None or operation["status"] != "pending" or operation["expires"] <= time.time():
                            self.pending(db, workflow_id, sequence)
                        # Commit the approval request before terminating the guest.
                        pause = True
                    else:
                        pause = False
                        db.execute("UPDATE operations SET status='inflight' WHERE workflow_id=? AND sequence=?", (workflow_id, sequence))
                        self.journal.event(db, workflow_id, "inflight", sequence)
                if pause:
                    raise ExecutionPaused("awaiting_approval")
                try:
                    result = await self.surface.invoke(capability, arguments, allow_writes=True,
                                                       durable_approved=approved_write)
                    serialized = encode(result)
                    if len(serialized.encode()) > self.settings.code_mode.max_frame_bytes - 1024:
                        raise CodeModeError("Capability result exceeds the journal response limit")
                    with self.journal.connect() as db:
                        db.execute("UPDATE operations SET status='completed', result=? WHERE workflow_id=? AND sequence=?",
                                   (serialized, workflow_id, sequence))
                        self.journal.event(db, workflow_id, "completed", sequence)
                    return result
                except OperationNotApplied:
                    raise WriteNotApplied(sequence) from None
                except BaseException:
                    with self.journal.connect() as db:
                        db.execute("UPDATE operations SET status='uncertain' WHERE workflow_id=? AND sequence=? AND status='inflight'", (workflow_id, sequence))
                        self.journal.event(db, workflow_id, "uncertain", sequence)
                    raise ExecutionPaused("needs_reconciliation") from None

        async def invoke(sequence, capability, arguments):
            nonlocal halt_status
            try:
                return await dispatch(sequence, capability, arguments)
            except ExecutionPaused as exc:
                halt_status = str(exc)
                raise
            except Exception:
                halt_status = "failed"
                raise ExecutionPaused("failed") from None

        try:
            result = await self.surface.executor.run("execute", row["code"], [], invoke,
                replay_context={"time_ms": int(row["created"] * 1000), "seed": row["seed"]})
            with self.journal.connect() as db:
                expected = {item[0] for item in db.execute("SELECT sequence FROM operations WHERE workflow_id=?", (workflow_id,))}
                if visited != expected:
                    raise CodeModeError("Workflow replay omitted recorded operations")
                self.journal.state(db, workflow_id, "completed", result=result)
        except ExecutionPaused as exc:
            with self.journal.connect() as db:
                if isinstance(exc, WriteNotApplied):
                    db.execute("UPDATE operations SET status='not_saved' WHERE workflow_id=? AND sequence=? AND status='inflight'",
                               (workflow_id, exc.sequence))
                    self.journal.event(db, workflow_id, "not_saved", exc.sequence)
                    self.journal.state(db, workflow_id, "failed", error="Backend confirmed the approved write was not applied")
                else:
                    self.journal.state(db, workflow_id, str(exc))
        except asyncio.CancelledError:
            with self.journal.connect() as db:
                self.journal.state(db, workflow_id, "interrupted", error="Execution was interrupted")
            raise
        except Exception:
            with self.journal.connect() as db:
                uncertain = db.execute("SELECT 1 FROM operations WHERE workflow_id=? AND status='uncertain'", (workflow_id,)).fetchone()
                self.journal.state(db, workflow_id, "needs_reconciliation" if uncertain else "failed", error="Workflow execution failed")
        return self.snapshot(workflow_id)

    async def reconciled_result(self, capability, recorded_arguments, supplied_result):
        backend, _, tool = capability.partition("__")
        connector = self.surface.gateway.connectors.get(backend)
        receipt_tool = connector.config.durable_only_tools.get(tool) if connector is not None else None
        if receipt_tool is None:
            return encode(supplied_result)
        receipt_capability = f"{backend}__{receipt_tool}"
        if not self.surface.read_only(receipt_capability):
            raise CodeModeError("Reconciliation requires the configured read-only backend receipt")
        result = await self.surface.gateway.call_tool(receipt_capability, recorded_arguments)
        if result.isError or not isinstance(result.structuredContent, dict) or not result.structuredContent:
            raise CodeModeError("Matching backend receipt unavailable; operation remains uncertain")
        serialized = encode(result.structuredContent)
        if serialized != encode(supplied_result):
            raise CodeModeError("Supplied result differs from the backend receipt; operation remains uncertain")
        return serialized

    async def decide(self, workflow_id, action, arguments):
        if action == "resolve":
            properties = {"sequence": {"type": "integer", "minimum": 1}, "result": {}}
        else:
            properties = {"approval_id": {"type": "string", "minLength": 1, "maxLength": 128}}
        self.validate(arguments, {"type": "object", "properties": properties,
                                 "required": list(properties), "additionalProperties": False})
        with self.journal.connect() as db:
            row = self.owned(db, workflow_id)
            self.check_context(row)
            if "operator" not in principal.get().roles:
                raise CodeModeError("Operator role required for workflow decisions")
            if row["status"] == "running":
                raise CodeModeError("Workflow is currently running")
            permitted_states = {"needs_reconciliation", "interrupted"} if action == "resolve" else {"awaiting_approval"}
            if row["status"] not in permitted_states:
                raise CodeModeError("Workflow is not waiting for this decision")
            if action == "resolve":
                operation = db.execute("SELECT * FROM operations WHERE workflow_id=? AND sequence=?", (workflow_id, arguments["sequence"])).fetchone()
            else:
                operation = db.execute("SELECT * FROM operations WHERE workflow_id=? AND approval_id=?", (workflow_id, arguments["approval_id"])).fetchone()
        if operation is None:
            raise CodeModeError("Workflow decision does not match a recorded operation")
        if action == "resolve" and operation["status"] != "uncertain":
            raise CodeModeError("Only an uncertain operation can be reconciled")
        recorded_arguments = json.loads(operation["arguments"])
        expected_binding = digest([row["code_hash"], row["contract"], operation["sequence"],
                                   operation["capability"], recorded_arguments])
        if operation["binding"] != expected_binding:
            raise CodeModeError("Workflow decision binding changed")
        await self.authorize(operation["capability"], recorded_arguments)
        if action == "resolve":
            serialized = await self.reconciled_result(operation["capability"], recorded_arguments, arguments["result"])
            if len(serialized.encode()) > self.settings.code_mode.max_frame_bytes - 1024:
                raise CodeModeError("Reconciled result exceeds the response limit")
        with self.journal.connect() as db:
            # Recheck state after asynchronous authorization, before committing a decision.
            row = self.owned(db, workflow_id)
            self.check_context(row)
            current = db.execute("SELECT * FROM operations WHERE workflow_id=? AND sequence=?", (workflow_id, operation["sequence"])).fetchone()
            if row["status"] not in permitted_states or current is None or dict(current) != dict(operation):
                raise CodeModeError("Workflow state changed during the decision")
            if action == "resolve":
                db.execute("UPDATE operations SET status='completed', result=? WHERE workflow_id=? AND sequence=?", (serialized, workflow_id, current["sequence"]))
                self.journal.event(db, workflow_id, "reconciled", current["sequence"])
                self.journal.state(db, workflow_id, "ready")
            elif action == "reject":
                if current["status"] != "pending":
                    raise CodeModeError("Approval is no longer pending")
                db.execute("UPDATE operations SET status='rejected' WHERE workflow_id=? AND sequence=?", (workflow_id, current["sequence"]))
                self.journal.state(db, workflow_id, "rejected")
            elif action == "approve":
                if current["status"] != "pending" or current["expires"] <= time.time():
                    raise CodeModeError("Approval is expired or no longer pending")
                db.execute("UPDATE operations SET status='approved' WHERE workflow_id=? AND sequence=?", (workflow_id, current["sequence"]))
                self.journal.event(db, workflow_id, "approved", current["sequence"])
                self.journal.state(db, workflow_id, "ready")
            else:
                raise CodeModeError("Unknown workflow action")
        return self.snapshot(workflow_id)
