# core/prompts.py
"""
AXELR system prompts for elite tools.

Used by app.py's `/api/tools/*` endpoints.
"""

SYSTEM_PROMPTS = {
    # 8. Code Translator
    "code_translator": (
        "Convert the provided code snippet from {source_lang} to {target_lang}. "
        "Preserve strict idiomatic patterns and type safety. Return only the translated code."
    ),
    # 9. Mermaid Diagram Generator
    "mermaid_generator": (
        "Translate the user process into clean, syntactically valid Mermaid.js diagrams. "
        "Only output ```mermaid blocks without conversational prose."
    ),
    # 10. Data Privacy Scanner
    "pii_scanner": (
        "Analyze the text for PII (names, emails, phone numbers, SSNs, credit cards, IP addresses). "
        "Return a JSON array of objects: [{'type': str, 'value': str, 'risk': 'low'|'medium'|'high'}]."
    ),
    # 11. Meeting Minutes Extractor
    "meeting_minutes": (
        "Extract key points from the transcript into: (1) Executive Summary, (2) Key Decisions, "
        "and (3) Action Items Table containing [Task, Assignee, Priority]."
    ),
    # 12. Decision Matrix
    "decision_matrix": (
        "Evaluate options against the criteria. Assign integer weights (1-5) and item scores (1-10). "
        "Output a Markdown decision matrix with computed weighted totals and the optimal choice."
    ),
}