---
name: reviewing-a-branch
description: Review a whole branch you did not write against its design and plan, grade each finding by its effect on a person using the software, fix the Critical and Important ones in one pass, and report the rest. Use when a build is done and before a person reviews it.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Reviewing a branch

You are the one fresh pair of eyes this work gets before a person reads it. You did not write
it, so you do not share its author's blind spots. Use that: read what is there, not what the
plan says should be there.

## What to read

1. The design, then the plan, including its Review focus section. Each line there is an input or
   failure mode that no task's tests exercise: check each one on purpose.
2. The rulings the builder recorded. Each is a call made on the plan; weigh it.
3. The whole diff against the base branch, then the code around it.
4. The tests: do they test real behavior, and would they fail if the code were wrong?

## What to check

- **Design and plan:** does the code do what the design says? Is every planned task present? Is
  each departure a justified improvement or a problem? A problem in the plan itself is a finding.
- **Correctness:** edge cases, error handling, empty and large inputs, two things at once.
- **Security:** input that crosses a trust boundary, secrets, permissions.
- **Tests:** real behavior rather than mocks; would each fail without its code?
- **Fit:** does it read like the code around it, and does it stay within what the design asked?

## The design is a vision, not a list

The design says what the software must do. It does not list every input it will meet. Where it
is silent, judge by what a reasonable person using this software would expect: that expectation
is a requirement, and silence is not permission.

## Grading

| Grade | Meaning |
|---|---|
| Critical | Broken behavior, data loss, a security hole. Must not ship. |
| Important | A missing feature, poor error handling, a test gap that hides a bug. Should not ship. |
| Minor | Style, polish, a small inefficiency. |

Grade by what a person gets if it ships, not by whether the design names the case. Not
everything is Critical. Be specific: file and line, what is wrong, why it matters.

## Declined to judge

List every behavior you considered and set aside as outside the design and the plan, one line
each, with the reason. Nothing you set aside is dropped without a word.

## The fix pass

Fix every Critical and Important finding yourself, in one pass, each by test-driven development:

1. Write a test that reproduces the finding.
2. Run it and watch it fail.
3. Fix the code, run the test and watch it pass.
4. Run the whole suite and read the result.
5. Commit the fix with its test.

A fix without a test that failed first is not verified. A suite that is not green after the pass
means the pass is not over. A finding you decide not to fix goes in the verdict with why the
code stands. There is no second pass, and Minor findings never enter this one.

## The verdict

Strengths first, specifically: accurate praise helps the reader trust the rest. Then what you
fixed, what you did not, the Minor findings, what you declined to judge, and a clear assessment.

## Never

- Say "looks good" without reading the code.
- Give feedback on code you did not read.
- Mark a nitpick Critical, or a real bug Minor because the design did not mention it.
- Weaken a test to make the suite pass, or touch the plan's boxes.
- Be vague: "improve error handling" is not a finding.
