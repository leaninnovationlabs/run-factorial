---
name: test-driven-development
description: Write the test first, watch it fail for the right reason, then write the least code that makes it pass. Use for every feature, fix and change in behavior.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Test-driven development

Write the test first. Watch it fail. Write the least code that makes it pass.

If you did not watch the test fail, you do not know that it tests the right thing.

## The rule

```text
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Wrote code before its test? Delete it and start from the test. Do not keep it as a reference, and
do not adapt it while you write the test.

Configuration, generated code and a throwaway spike are the exceptions. Say so when you take one.

## Red, green, refactor

### Red: write one failing test

One behavior, a name that says what should happen, real code (no mocks unless there is no other
way).

```python
def test_retries_a_failed_operation_three_times():
    attempts = []
    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("fail")
        return "ok"
    assert retry(operation) == "ok"
    assert len(attempts) == 3
```

### Verify red: watch it fail

Never skip this. Run the test and confirm:

- it fails, and does not error;
- the failure is the one you expected;
- it fails because the behavior is missing, not because of a typo.

A test that passes now tests something that already exists. Fix the test. A test that errors:
fix the error and run it again until it fails for the right reason.

### Green: the least code that passes

Write the simplest code that makes the test pass. No options nobody asked for, no refactoring of
other code, no improvements beyond the test.

### Verify green: watch it pass

Run the test again. Then run the repository's whole suite, not only your file. A green run of
your test is not a green suite. Any failure the suite shows, including one you did not cause,
goes in your report by name.

Test fails? Fix the code, not the test. Other tests fail? Fix them now.

### Refactor

Only when green: remove duplication, improve names, extract helpers. Stay green.

## Good tests

| Quality | Good | Bad |
|---|---|---|
| Minimal | One thing. "and" in the name? Split it. | `test_validates_email_and_domain_and_whitespace` |
| Clear | The name says the behavior. | `test_1`, `test_it_works` |
| Real | Calls the code a user would. | Asserts that a mock was called. |

## Rationalizations

| Excuse | Reality |
|---|---|
| "Too simple to test" | Simple code breaks. The test takes a minute. |
| "I'll test after" | A test that passes at once proves nothing. |
| "I already tested it by hand" | By hand leaves no record and cannot run again. |
| "Deleting hours of work is wasteful" | Keeping code you cannot trust is the waste. |
| "The test is hard to write" | Listen to it: the design is hard to use. |

## Before you call a task done

- Every new function has a test, and you watched each test fail first.
- Each failed for the expected reason, and now passes.
- The whole suite is green, and the output has no new errors or warnings.
- Edge cases and errors are covered.
