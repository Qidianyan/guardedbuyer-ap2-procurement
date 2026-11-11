"""Company procurement policy engine: budgets, category allow-list, approval chain."""
from dataclasses import dataclass, field

@dataclass
class PurchaseRequest:
    requester: str
    department: str
    category: str
    description: str
    sku: str
    quantity: int
    unit_price: float
    merchant: str
    currency: str = "USD"
    @property
    def total(self): return round(self.quantity * self.unit_price, 2)

@dataclass
class Decision:
    allowed: bool
    needs_approval_from: list = field(default_factory=list)
    reasons: list = field(default_factory=list)

def _usd(v) -> str:
    return f"${float(v):,.2f}"

@dataclass
class Policy:
    department_budgets: dict            # dept -> remaining budget
    allowed_categories: set
    blocked_merchants: set = field(default_factory=set)
    # (threshold, approver): purchases with total > threshold need that approver, in order
    approval_chain: list = field(default_factory=lambda: [(500.0, "manager"), (2000.0, "finance")])
    per_order_hard_cap: float = 10000.0

    def evaluate(self, req: PurchaseRequest) -> Decision:
        reasons, approvers = [], []
        if req.category not in self.allowed_categories:
            return Decision(False, [], [f"category '{req.category}' not allowed"])
        if req.merchant in self.blocked_merchants:
            return Decision(False, [], [f"merchant '{req.merchant}' blocked"])
        if req.total > self.per_order_hard_cap:
            return Decision(False, [], [f"total {_usd(req.total)} exceeds the per-order hard cap of {_usd(self.per_order_hard_cap)}"])
        budget = self.department_budgets.get(req.department, 0.0)
        if req.total > budget:
            reasons.append(f"total {_usd(req.total)} exceeds the {req.department.upper()} department's remaining budget of {_usd(budget)}")
            approvers.append("finance")
        for threshold, approver in self.approval_chain:
            if req.total > threshold and approver not in approvers:
                approvers.append(approver)
                reasons.append(f"total {_usd(req.total)} is above {_usd(threshold)}, so {approver} approval is required")
        order = [a for _, a in self.approval_chain]
        approvers.sort(key=lambda a: order.index(a) if a in order else 99)
        return Decision(not approvers, approvers, reasons or ["within policy"])

    def charge(self, dept, amount):
        self.department_budgets[dept] = round(self.department_budgets.get(dept, 0.0) - amount, 2)

class ApprovalBook:
    """Collects approvals; an approver must exist in the configured roster."""
    def __init__(self, roster): self.roster, self.granted = roster, {}
    def approve(self, request_id, role, person):
        if self.roster.get(role) != person:
            raise PermissionError(f"{person} is not the {role} approver")
        self.granted.setdefault(request_id, set()).add(role)
    def satisfied(self, request_id, needed):
        return set(needed) <= self.granted.get(request_id, set())
