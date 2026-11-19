"""Workflow engine — multi-step atomic transactions with LIFO rollback.

A workflow is a named, correlated sequence of action template dispatches.
If any step fails, all previously completed steps are rolled back in
reverse order (LIFO). Correlation IDs propagate through every audit entry.

    wf = Workflow(engine, name="provision-web-stack")
    await wf.step("vultr.provision_instance", {"blueprint": "web_node"})
    await wf.step("jira.create_issue", {"project_key": "OPS",
                                        "summary": "Deployed web node"})
    await wf.commit()  # or rollback_all() on failure
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .actions import TEMPLATES, dispatch
from .engine import CURRENT_WORKFLOW_ID, Engine, audit


class WorkflowError(RuntimeError):
    def __init__(self, step_index: int, action: str, cause: str) -> None:
        super().__init__(f"workflow_failed@{step_index}:{action}:{cause}")
        self.code = "WORKFLOW_FAILED"
        self.step_index = step_index
        self.action = action
        self.cause = cause


class Workflow:
    def __init__(self, engine: Engine, name: str = "workflow",
                 corr_id: str | None = None,
                 *, auto_rollback: bool = True) -> None:
        self.engine = engine
        self.name = name
        self.corr_id = corr_id or f"wf-{uuid.uuid4().hex[:12]}"
        self.auto_rollback = auto_rollback
        self.steps: list[dict[str, Any]] = []
        self._completed: list[tuple[int, str, dict, dict]] = []
        self._committed = False

    async def step(self, action: str, params: dict[str, Any], *,
                   dry_run: bool = False,
                   approval_token: str | None = None) -> dict[str, Any]:
        idx = len(self.steps)
        self.steps.append({"index": idx, "action": action,
                           "params": params, "ts": time.time()})

        token = CURRENT_WORKFLOW_ID.set(self.corr_id)
        try:
            result = await dispatch(self.engine, {
                "action": action, "params": params,
                "dryRun": dry_run, "approval_token": approval_token,
                "corr_id": self.corr_id, "workflow": self.name,
                "step_index": idx,
            })
        finally:
            CURRENT_WORKFLOW_ID.reset(token)

        if not result.get("ok"):
            if self.auto_rollback:
                rb = await self.rollback_all()
                result["workflow_rollback"] = rb
            await audit({"kind": "workflow_step_failed",
                         "corr_id": self.corr_id, "workflow": self.name,
                         "step_index": idx, "action": action,
                         "error": result.get("error")})
            return result

        if not dry_run:
            receipt = (result.get("result") or {}).get("receipt", {}) or {}
            self._completed.append((idx, action, params, receipt))
        return result

    async def rollback_all(self) -> dict[str, Any]:
        """LIFO rollback of completed steps, then WAL rollback."""
        restored = 0
        failures: list[dict[str, Any]] = []
        for idx, action, params, receipt in reversed(self._completed):
            spec = TEMPLATES.get(action)
            if not spec or not spec.get("rollback"):
                continue
            try:
                await spec["rollback"](receipt)
                restored += 1
                await audit({"kind": "workflow_step_rolled_back",
                             "corr_id": self.corr_id,
                             "workflow": self.name,
                             "step_index": idx, "action": action})
            except Exception as e:
                failures.append({"step_index": idx, "action": action,
                                 "error": str(e)[:200]})
                await audit({"kind": "workflow_rollback_error",
                             "corr_id": self.corr_id,
                             "workflow": self.name,
                             "step_index": idx, "action": action,
                             "error": str(e)[:200]})
        wal_result = await self.engine.rollback(workflow_id=self.corr_id)
        self._completed.clear()
        return {"steps_rolled_back": restored, "failures": failures,
                "wal_restored": wal_result.get("restored", 0)}

    async def commit(self) -> dict[str, Any]:
        if self._committed:
            return {"ok": False, "error": {"code": "ALREADY_COMMITTED"}}
        self._committed = True
        self._completed.clear()
        await self.engine.wal.mark_committed(self.corr_id)
        await audit({"kind": "workflow_committed",
                     "corr_id": self.corr_id, "workflow": self.name,
                     "steps": len(self.steps)})
        return {"ok": True, "corr_id": self.corr_id,
                "workflow": self.name, "steps": len(self.steps)}

    def snapshot(self) -> dict[str, Any]:
        return {
            "corr_id": self.corr_id, "name": self.name,
            "steps": len(self.steps), "completed": len(self._completed),
            "committed": self._committed,
            "step_list": [{"index": s["index"], "action": s["action"]}
                          for s in self.steps],
        }


async def run_workflow(engine: Engine, steps: list[dict[str, Any]], *,
                       name: str = "workflow",
                       auto_rollback: bool = True) -> dict[str, Any]:
    """Execute a step list atomically. Returns summary or rolls back."""
    wf = Workflow(engine, name=name, auto_rollback=auto_rollback)
    results: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        r = await wf.step(
            step["action"], step.get("params", {}),
            dry_run=step.get("dry_run", False),
            approval_token=step.get("approval_token"))
        results.append(r)
        if not r.get("ok"):
            return {"ok": False, "corr_id": wf.corr_id,
                    "failed_step": i, "error": r.get("error"),
                    "results": results,
                    "workflow_rollback": r.get("workflow_rollback")}
    commit = await wf.commit()
    return {"ok": True, "corr_id": wf.corr_id,
            "workflow": wf.name, "steps": len(steps),
            "results": results, "commit": commit}
