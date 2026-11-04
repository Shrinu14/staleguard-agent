"""
Model routing by role: a strong *planner* model where thinking matters, a
cheap *editor* model for the follow-through steps in between.

The rule is deterministic (no extra LLM call to decide):
  - turn 1                          -> planner  (understand the task, plan)
  - last tool failed or was declined -> planner  (something went wrong: rethink)
  - history was just compacted       -> planner  (re-orient from the summary)
  - anything else                    -> editor   (read the next range, make the
                                                  planned edit, run the tests)

Typical run: planner reads the map and decides; editor reads, edits, runs
tests; planner only comes back when a step fails. Most turns are follow-
through, so most tokens go to the cheap model.

Policy wins over routing: in a confidential repo both roles are the local
model (see Router.from_plan), so routing can never pick a cloud model there.
"""

from dataclasses import dataclass

from agent.config import Settings
from agent.lookups import ModelRole, RouteReason
from agent.policy import ModelPlan


@dataclass(frozen=True, slots=True)
class Route:
    role: ModelRole
    model: str
    reason: RouteReason


@dataclass(frozen=True, slots=True)
class Router:
    planner: str
    editor: str
    enabled: bool = True

    @classmethod
    def from_plan(cls, settings: Settings, plan: ModelPlan, *, enabled: bool = True) -> "Router":
        """Planner = the run's model (--model or MODEL_PLANNER); editor = MODEL_EDITOR.
        Local-only runs use the plan's local model for both."""
        if plan.local_only:
            return cls(plan.model, plan.model, enabled)
        return cls(plan.model, settings.model_for(ModelRole.EDITOR), enabled)

    @classmethod
    def single(cls, model: str) -> "Router":
        """One model for every turn (routing off)."""
        return cls(model, model, enabled=False)

    def choose(self, *, turn: int, last_tool_failed: bool, just_compacted: bool) -> Route:
        if not self.enabled:
            return Route(ModelRole.PLANNER, self.planner, RouteReason.ROUTING_OFF)
        if turn == 1:
            reason = RouteReason.FIRST_TURN
        elif last_tool_failed:
            reason = RouteReason.RECOVER
        elif just_compacted:
            reason = RouteReason.AFTER_COMPACTION
        else:
            return Route(ModelRole.EDITOR, self.editor, RouteReason.FOLLOW_THROUGH)
        return Route(ModelRole.PLANNER, self.planner, reason)

    @property
    def models(self) -> tuple[str, ...]:
        return (self.planner,) if self.planner == self.editor else (self.planner, self.editor)
