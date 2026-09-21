# core/prompts.py
"""
AXELR system prompts for elite tools.

Used by app.py's `/api/tools/*` endpoints.
"""

from .secure_prompts import generate_secure_prompt

SYSTEM_PROMPTS = {
    # 8. Code Translator
    "code_translator": (
        "Convert the provided code snippet from {{ source_lang }} to {{ target_lang }}. "
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

def get_secure_prompt(prompt_name: str, **kwargs) -> str:
    """
    Gets a securely rendered prompt from the SYSTEM_PROMPTS dictionary.

    Args:
        prompt_name: The name of the prompt to get.
        **kwargs: The values to substitute into the prompt template.

    Returns:
        The securely rendered prompt.
    """
    prompt_template = SYSTEM_PROMPTS.get(prompt_name)
    if not prompt_template:
        raise ValueError(f"Prompt '{prompt_name}' not found.")
    return generate_secure_prompt(prompt_template, **kwargs)