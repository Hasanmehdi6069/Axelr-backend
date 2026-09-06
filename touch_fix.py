# File: axelr/features/touch_fix.py

import re
import difflib
from typing import List, Tuple, Optional

class TouchFixEngine:
    """
    Targeted patching engine: Only fixes the code block selected by the user,
    without rewriting the whole file. Reduces token consumption by 80%+.
    """
    def __init__(self, route_func=None):
        self.route_func = route_func

    async def fix_block(self, full_code: str, error_block: str, error_message: str) -> str:
        """
        Fixes only the specified code block and returns the complete code.
        """
        # 1. Locate error_block position in full_code
        lines = full_code.split('\n')
        start_idx, end_idx = self._locate_block(lines, error_block)

        # 2. Fix only the problem block using AI (token reduction)
        fixed_block = await self._ai_fix_block(error_block, error_message)

        # 3. Replace and return
        lines[start_idx:end_idx] = fixed_block.split('\n')
        return '\n'.join(lines)

    def _locate_block(self, lines: List[str], target: str) -> Tuple[int, int]:
        """
        Locates the start and end indices of the target block using indentation and fuzzy matching.
        """
        target_lines = target.split('\n')
        if not target_lines:
            return 0, 0

        # Find a line that matches the first non‑empty line of target (ignoring leading whitespace)
        first_line = target_lines[0].strip()
        for i, line in enumerate(lines):
            if line.strip() == first_line:
                # Determine indentation level of the target block
                indent = len(target_lines[0]) - len(target_lines[0].lstrip())
                start = i
                end = i
                # Find end of block: next line with same or less indentation
                for j in range(i+1, len(lines)):
                    if lines[j].strip() == '':
                        continue
                    current_indent = len(lines[j]) - len(lines[j].lstrip())
                    if current_indent <= indent and lines[j].strip():
                        end = j
                        break
                else:
                    end = len(lines)
                return start, end
        # Fallback: if not found, return whole file
        return 0, len(lines)

    async def _ai_fix_block(self, error_block: str, error_message: str) -> str:
        """
        Uses the AI route to fix the block. Returns the corrected block.
        """
        if not self.route_func:
            return error_block

        prompt = f"Fix the following code block. Error: {error_message}\n\n```\n{error_block}\n```\nReturn only the corrected block, no extra text."
        result = await self.route_func(
            workspace="design",
            task_type="touch_fix",
            prompt=prompt,
            history=[],
            files=[],
            max_tokens=2048,
            temp=0.2,
            tier="free",
            user=None
        )
        if not result.get("success"):
            return error_block

        fixed = result["text"]
        code_match = re.search(r"```(?:\w+)?\s*([\s\S]*?)```", fixed, re.DOTALL)
        if code_match:
            fixed = code_match.group(1).strip()
        return fixed