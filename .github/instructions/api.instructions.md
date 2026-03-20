---
applyTo: "app/**/*.py"
---

You are reviewing a FastAPI backend pull request.

## Review Focus (HIGH PRIORITY)
- correctness
- bugs
- security (auth, permissions, input validation)
- performance (blocking I/O, DB calls, async misuse)
- reliability (error handling, edge cases)
- maintainability (structure, separation of concerns)

## Backend Guidelines
- Keep route handlers thin (no business logic in routers)
- Move logic into service layer
- Avoid blocking I/O inside async endpoints
- Validate all external inputs (query, body, headers)
- Ensure proper exception handling and status codes
- Check DB query efficiency (N+1, missing indexes, etc.)

## Output Rules
- Only report high-signal issues
- Do NOT comment on formatting or trivial style
- Do NOT repeat obvious code behavior
- Be concise and actionable
- Write all findings, summaries, and suggestions in Korean

If no critical issues:
"No high-signal issues found."