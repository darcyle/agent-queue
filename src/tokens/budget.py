from __future__ import annotations


class BudgetManager:
    def __init__(self, global_budget: int | None = None):
        self.global_budget = global_budget

    def calculate_target_ratios(
        self, weights: dict[str, float]
    ) -> dict[str, float]:
        total = sum(weights.values())
        if total == 0:
            return {}
        return {pid: w / total for pid, w in weights.items()}

    def calculate_deficits(
        self, weights: dict[str, float], usage: dict[str, int]
    ) -> dict[str, float]:
        targets = self.calculate_target_ratios(weights)
        total_usage = sum(usage.values())
        if total_usage == 0:
            return dict(targets)
        result = {}
        for pid, target in targets.items():
            actual = usage.get(pid, 0) / total_usage
            result[pid] = target - actual
        return result

    def is_global_budget_exhausted(self, total_used: int) -> bool:
        if self.global_budget is None:
            return False
        return total_used >= self.global_budget

    def is_project_budget_exhausted(
        self, project_used: int, budget_limit: int | None
    ) -> bool:
        if budget_limit is None:
            return False
        return project_used >= budget_limit
