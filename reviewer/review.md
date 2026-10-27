# Review instructions

## What to review

Focus on the lines the pull request adds or changes. Read surrounding code in
`/work/repo` as needed to understand them. Look for:

1. Correctness: logic errors, off-by-one errors, wrong conditions, unhandled
   error paths, null or empty handling, resource leaks, race conditions,
   broken invariants, behaviour that contradicts the pull request description
   or nearby documentation.
2. Security: injection (SQL, shell, path, template), unsafe deserialization,
   missing authorization or validation, secrets committed to the repository,
   weak cryptography, unsafe defaults, data exposure in logs.
3. Clarity: misleading names, comments that disagree with the code, needless
   complexity, missing tests for new behaviour.

## How to write comments

- Be specific. State what is wrong, why it matters, and what to change. A
  concrete fix is better than a general remark.
- Use severity `bug` for incorrect behaviour, `security` for vulnerabilities,
  `suggestion` for improvements to clarity or design, and `nit` for minor
  style points. Do not label something `bug` unless you can explain how it
  fails.
- Do not comment on code the pull request does not touch unless the change
  breaks it.
- Do not pad the review. If the change is fine, say so in the summary and
  return few or no comments.
- Choose `verdict`:
  - `request_changes` when the pull request introduces a bug or security
    problem, or makes an existing one worse (for example, turns a leak into
    a use-after-free). This applies even when nothing in the repository
    triggers the problem yet.
  - `comment` when every finding is a suggestion, a nit, or a problem that
    the pull request leaves as it was.
  - `approve` when you found nothing that needs changing.
