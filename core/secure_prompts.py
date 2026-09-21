
# core/secure_prompts.py
"""
Secure prompt generation for AXELR AI.
"""

import jinja2

# Create a Jinja2 environment with auto-escaping to prevent prompt injection.
# This ensures that any user-provided values are treated as plain text and
# cannot be used to manipulate the AI.
_secure_env = jinja2.Environment(
    loader=jinja2.DictLoader({}),
    autoescape=jinja2.select_autoescape(
        enabled_extensions=('html', 'xml', 'jinja2'),
        default_for_string=True,
    )
)

def generate_secure_prompt(prompt_template: str, **kwargs) -> str:
    """
    Generates a secure prompt using a Jinja2 template.

    Args:
        prompt_template: The prompt template.
        **kwargs: The values to substitute into the template.

    Returns:
        The securely rendered prompt.
    """
    template = _secure_env.from_string(prompt_template)
    return template.render(**kwargs)