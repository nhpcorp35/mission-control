from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DocumentReference:
    filename: str = ""
    document_type: str = ""
    source_snippet: str = ""
    path: str = ""


@dataclass
class IssueFinding:
    issue: str
    category: str = "general"
    score: int = 40
    risk_level: str = "medium"
    reason: str = ""
    recommended_focus: str = ""
    source: Optional[DocumentReference] = None


@dataclass
class ContradictionFinding:
    category: str
    summary: str
    score: int = 50
    source: Optional[DocumentReference] = None


@dataclass
class TimelineEvent:
    event_date: str = ""
    event_label: str = ""
    description: str = ""
    source: Optional[DocumentReference] = None


@dataclass
class CredibilityFlag:
    issue: str
    severity: int = 50
    witness_or_source: str = ""
    explanation: str = ""
    source: Optional[DocumentReference] = None


@dataclass
class AttackSurface:
    target: str
    weakness: str
    litigation_value: str = ""
    recommended_use: str = ""
    source: Optional[DocumentReference] = None


@dataclass
class StrategyRecommendation:
    recommendation: str
    priority: str = "medium"
    reasoning: str = ""
    supporting_issues: List[str] = field(default_factory=list)