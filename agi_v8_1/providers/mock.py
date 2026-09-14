"""Deterministic mock provider for V8 local dev and tests.

Ported from agi_v7.1/agent_system/providers/mock.py. Mock is intentionally
exempt from the AGI_V8_PROVIDERS_ENABLED gate — it makes no network calls
and is safe in the advisory-chain default.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, cast

from .base import BaseModelProvider


class ProviderError(RuntimeError):
    """Local provider-error sentinel (v7.1 imported from agent_system.utils)."""


def _new_identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_requires_real_execution(task: Mapping[str, Any]) -> bool:
    """Return True when a task should not be claimed complete by the mock provider."""
    text = " ".join(
        [
            str(task.get("task_id", "")),
            str(task.get("title", "")),
            str(task.get("description", "")),
        ]
    ).lower()
    keywords = (
        "review",
        "organize",
        "locale",
        "translation",
        "frontend",
        "front-end",
        "mobile",
        "web",
        "summarize",
        "update",
        "modify",
        "edit",
        "write",
        "cleanup",
        "communication.md",
        "project_status.md",
    )
    if any(keyword in text for keyword in keywords):
        return True

    return bool(re.search(r"\.(md|txt|json|ya?ml|py|ts|tsx|js|jsx|html|css|scss)\b", text))


class MockModelProvider(BaseModelProvider):
    """Deterministic provider used for local development and tests.

    NOTE: intentionally bypasses the AGI_V8_PROVIDERS_ENABLED gate —
    Mock makes no network calls and is safe in advisory-chain default.
    """

    def generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        del prompt
        del schema

        dispatch = {
            "strategist": self._generate_strategist,
            "skeptic": self._generate_skeptic,
            "builder": self._generate_builder,
            "tester": self._generate_tester,
            "scribe": self._generate_scribe,
            "critic": self._generate_critic,
            "continuation": self._generate_continuation,
            "support_advisor": self._generate_support_advisor,
            "memory_writer": self._generate_memory_writer,
            "handoff_generator": self._generate_handoff,
        }
        try:
            body = dispatch[agent_name](payload)
        except KeyError as exc:
            raise ProviderError(f"Mock provider does not support agent '{agent_name}'.") from exc
        result = json.dumps(body, ensure_ascii=False)
        self._record_usage(
            agent_name,
            input_tokens=len(str(payload)) // 4,
            output_tokens=len(result) // 4,
        )
        return result

    def _generate_strategist(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        goal = cast(dict[str, Any], payload["goal_card"])
        revision_guidance = str(payload.get("revision_feedback", "")).strip()
        criteria = goal.get("success_criteria") or [goal["user_objective"]]
        tasks: list[dict[str, Any]] = []
        for index, criterion in enumerate(criteria[:3], start=1):
            previous_task_id = tasks[-1]["task_id"] if tasks else None
            task_id = f"task_{index:02d}"
            task: dict[str, Any] = {
                "task_id": task_id,
                "title": f"Deliver criterion {index}",
                "description": criterion,
                "dependencies": [previous_task_id] if previous_task_id else [],
                "status": "pending",
                "owner": "builder",
            }
            tasks.append(task)

        if len(tasks) < 2:
            tasks.append(
                {
                    "task_id": "task_02",
                    "title": "Package the MVP outcome",
                    "description": "Document the implemented output and confirm it satisfies the goal card.",
                    "dependencies": [tasks[0]["task_id"]],
                    "status": "pending",
                    "owner": "builder",
                }
            )

        summary = f"Create a tight MVP plan for goal '{goal['user_objective']}'."
        if revision_guidance:
            summary = f"{summary} Revised with skeptic guidance: {revision_guidance}"

        return {
            "agent": "strategist",
            "plan_id": _new_identifier("plan"),
            "goal_id": goal["id"],
            "summary": summary,
            "tasks": tasks,
            "estimated_steps": len(tasks),
            "risks": list(goal.get("constraints", []))[:3],
        }

    def _generate_skeptic(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = cast(dict[str, Any], payload["plan"])
        goal = cast(dict[str, Any], payload["goal_card"])
        issues_found: list[str] = []
        scope_risks: list[str] = []
        task_descriptions = " ".join(task["description"] for task in plan["tasks"]).lower()

        if not plan["tasks"]:
            issues_found.append("Plan contains no executable tasks.")
        if len(plan["tasks"]) > 5:
            issues_found.append("Plan is too large for a single MVP cycle.")

        for keyword in ("vector db", "web ui", "robot", "anthropic", "openai api"):
            if keyword in task_descriptions and keyword not in goal["user_objective"].lower():
                scope_risks.append(f"Task scope mentions '{keyword}' without explicit goal-card demand.")

        decision = "revise" if issues_found or scope_risks else "approve"
        revision_guidance = (
            "Reduce scope to the smallest file-based CLI MVP and avoid future integrations."
            if decision == "revise"
            else "Plan is aligned with the immutable goal card and safe to execute."
        )
        return {
            "agent": "skeptic",
            "review_id": _new_identifier("review"),
            "decision": decision,
            "issues_found": issues_found,
            "scope_risks": scope_risks,
            "revision_guidance": revision_guidance,
        }

    def _generate_builder(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        plan = cast(dict[str, Any], payload["plan"])
        task_results: list[dict[str, Any]] = []
        created_files: list[str] = []
        statuses: set[str] = set()
        for task in plan["tasks"]:
            if _task_requires_real_execution(task):
                statuses.add("failed")
                task_results.append(
                    {
                        "task_id": task["task_id"],
                        "status": "failed",
                        "implementation_notes": (
                            "Mock provider cannot claim completion for tasks that require "
                            "real project inspection or filesystem changes."
                        ),
                        "artifacts": [],
                        "blocked_by": ["mock_provider_no_side_effects"],
                    }
                )
                continue

            artifact_name = f"{task['task_id']}_artifact.json"
            created_files.append(artifact_name)
            statuses.add("completed")
            task_results.append(
                {
                    "task_id": task["task_id"],
                    "status": "completed",
                    "implementation_notes": f"Completed mock execution for '{task['description']}'.",
                    "artifacts": [artifact_name],
                    "blocked_by": [],
                }
            )

        if statuses == {"completed"}:
            overall_status = "success"
        elif "completed" in statuses:
            overall_status = "partial"
        else:
            overall_status = "failure"

        return {
            "agent": "builder",
            "execution_id": _new_identifier("exec"),
            "overall_status": overall_status,
            "task_results": task_results,
            "created_files": created_files,
        }

    def _generate_tester(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        builder_output = cast(dict[str, Any], payload["builder_output"])
        task_results: list[dict[str, Any]] = []
        failure_modes: list[str] = []

        for result in builder_output["task_results"]:
            status = "pass" if result["status"] == "completed" else "fail"
            failures: list[str] = [] if status == "pass" else [f"Task {result['task_id']} is incomplete."]
            if failures:
                failure_modes.extend(failures)
            task_results.append(
                {
                    "task_id": result["task_id"],
                    "status": status,
                    "checks": [
                        "Structured result exists",
                        "Task ended in a terminal state",
                        "Artifact list is populated",
                    ],
                    "failures": failures,
                }
            )

        verdict = "pass" if not failure_modes and builder_output["overall_status"] == "success" else "fail"
        return {
            "agent": "tester",
            "test_run_id": _new_identifier("test"),
            "verdict": verdict,
            "task_results": task_results,
            "failure_modes": failure_modes,
        }

    def _generate_support_advisor(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        phase = str(payload.get("phase", "pre_build"))
        risk_flags: list[str] = []
        suggested_checks = ["Run the narrowest relevant test after editing."]
        pytest_error = str(payload.get("pytest_error", ""))
        if "ModuleNotFoundError" in pytest_error:
            risk_flags.append("pytest collection failed due to an unresolved import.")
            suggested_checks.append("Import the package module exactly as pytest does.")
        return {
            "agent": "support_advisor",
            "advice_id": _new_identifier("advice"),
            "summary": f"Mock support advice for {phase}.",
            "risk_flags": risk_flags,
            "suggested_checks": suggested_checks,
            "builder_guidance": "Keep the next edit minimal and verify the import/test path.",
            "executor_guidance": "Prefer targeted edits plus an explicit verification command.",
            "intervention": "proceed_with_caution" if risk_flags else "proceed",
            "confidence": "medium",
        }

    def _generate_scribe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        builder_output = cast(dict[str, Any], payload["builder_output"])
        tester_output = cast(dict[str, Any], payload["tester_output"])
        completed, unfinished, artifacts = [], [], []
        for item in builder_output["task_results"]:
            if item["status"] == "completed":
                completed.append(item["task_id"])
            else:
                unfinished.append(item["task_id"])
            artifacts.extend(item.get("artifacts", []))
        return {
            "agent": "scribe",
            "session_summary": f"Completed {len(completed)} tasks with tester verdict '{tester_output['verdict']}'.",
            "decisions": [
                f"Builder overall status: {builder_output['overall_status']}",
                f"Tester verdict: {tester_output['verdict']}",
            ],
            "unfinished_work": unfinished,
            "artifacts": artifacts,
        }

    def _generate_critic(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        tester_output = cast(dict[str, Any], payload["tester_output"])
        builder_output = cast(dict[str, Any], payload["builder_output"])
        retry_count = int(payload.get("retry_count", 0))

        if tester_output["verdict"] == "pass" and builder_output["overall_status"] == "success":
            decision = "accept"
            reasoning = "All worker outputs passed validation and the execution completed successfully."
            next_action = "Write validated memory and generate the handoff packet."
        elif retry_count < 1:
            decision = "retry"
            reasoning = "The failure appears local to the worker layer and can be retried safely."
            next_action = "Retry the worker layer with the same approved plan."
        else:
            decision = "escalate"
            reasoning = "Local retry budget is exhausted or results remain unsafe."
            next_action = "Route back to the head layer for replanning."

        return {
            "agent": "critic",
            "decision": decision,
            "reasoning": reasoning,
            "next_action": next_action,
        }

    def _generate_memory_writer(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        outcome = cast(dict[str, Any], payload["session_outcome"])
        episodic_entries = [
            {
                "timestamp": _utc_now(),
                "run_id": outcome["run_id"],
                "kind": "episodic",
                "content": f"Goal {outcome['goal_id']} finished with status {outcome['final_status']}.",
            }
        ]
        failure_entries: list[dict[str, Any]] = []
        if outcome["final_status"] != "accept":
            failure_entries.append(
                {
                    "timestamp": _utc_now(),
                    "run_id": outcome["run_id"],
                    "kind": "failure",
                    "content": f"Run ended with {outcome['final_status']} and needs follow-up.",
                }
            )

        semantic_memory = {
            "last_updated": _utc_now(),
            "successful_goal_ids": [outcome["goal_id"]] if outcome["final_status"] == "accept" else [],
            "last_decision": outcome["final_status"],
        }
        return {
            "agent": "memory_writer",
            "episodic_entries": episodic_entries,
            "failure_entries": failure_entries,
            "semantic_memory": semantic_memory,
        }

    def _generate_continuation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        critic_output = cast(dict[str, Any], payload.get("critic_output", {}))
        current_state = cast(dict[str, Any], payload.get("current_state", {}))
        continuation_cycle_count = int(payload.get("continuation_cycle_count", 0))

        if critic_output.get("decision") == "escalate":
            goal_status = "escalate"
            continuation_reason = "The critic already escalated the run, so autonomous continuation is unsafe."
            next_phase = "halted"
            next_tasks: list[dict[str, Any]] = []
            stop_reason = "critic_requested_escalation"
        elif current_state.get("open_tasks"):
            goal_status = "continue"
            continuation_reason = "There is still open work, so the system should continue with a bounded next queue."
            next_phase = "follow_up"
            next_tasks = [
                {
                    "task_id": f"continuation_task_{continuation_cycle_count + 1:02d}",
                    "title": "Continue the remaining bounded follow-up work",
                    "description": "Use the latest accepted state to process the next bounded slice of work.",
                    "dependencies": [],
                    "status": "pending",
                    "owner": "builder",
                }
            ]
            stop_reason = ""
        else:
            goal_status = "complete"
            continuation_reason = "The queue is empty and the latest run was accepted, so the goal appears complete."
            next_phase = "complete"
            next_tasks = []
            stop_reason = "goal_complete"

        return {
            "agent": "continuation",
            "goal_status": goal_status,
            "continuation_reason": continuation_reason,
            "next_phase": next_phase,
            "goal_updates": [],
            "mission_updates": [],
            "next_tasks": next_tasks,
            "stop_reason": stop_reason,
        }

    def _generate_handoff(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        outcome = cast(dict[str, Any], payload["session_outcome"])
        memory_context = cast(dict[str, Any], payload["memory_context"])
        next_steps = (
            ["Review stored memory entries for reusable patterns."]
            if outcome["final_status"] == "accept"
            else ["Re-enter the head layer and re-plan the work before the next session."]
        )
        return {
            "agent": "handoff_generator",
            "session_id": outcome["session_id"],
            "status": outcome["final_status"],
            "completed_tasks": outcome["completed_tasks"],
            "open_tasks": outcome["open_tasks"],
            "next_steps": next_steps,
            "memory_refs": [
                f"episodic:{len(memory_context.get('episodic', []))}",
                f"failures:{len(memory_context.get('failures', []))}",
            ],
            "summary": f"Run {outcome['run_id']} completed with status {outcome['final_status']}.",
        }
