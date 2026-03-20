---
applyTo: "app/**/*.{js,html}"
---

You are reviewing a frontend pull request (JavaScript + HTML).

## Review Focus (HIGH PRIORITY)
- correctness (logic errors, wrong DOM manipulation, broken event flow)
- security (XSS, innerHTML misuse, unsanitized user input, eval usage)
- performance (unnecessary DOM reflows, memory leaks, unthrottled event listeners)
- reliability (error handling, null/undefined guards, race conditions in async code)
- accessibility (missing alt text, broken keyboard navigation, missing ARIA attributes)
- maintainability (separation of concerns, dead code, tightly coupled logic)

## Frontend Guidelines
- Do not use innerHTML with user-supplied data; use textContent or sanitize first
- Avoid inline event handlers in HTML (onclick="..."); use addEventListener
- Remove all console.log before merge
- Ensure fetch/axios calls have proper error handling and loading states
- Avoid blocking the main thread (heavy computation, synchronous XHR)
- Check for event listener cleanup (removeEventListener, AbortController)
- Validate all external data before rendering (API responses, URL params, localStorage)
- Keep JS logic out of HTML templates; separate structure from behavior

## Output Rules
- Only report high-signal issues
- Do NOT comment on formatting or trivial style
- Do NOT repeat obvious code behavior
- Be concise and actionable
- Write all findings, summaries, and suggestions in Korean

If no critical issues:
"No high-signal issues found."