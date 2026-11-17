This run is a conflict fix, not a build.
Pull request #{pr_number} (`{head_ref}` into `{base_ref}`) already exists and {state_words} `{base_ref}`.
Your checkout starts at the head of `{head_ref}`.
Do only this: fetch `{base_ref}` and merge `origin/{base_ref}` into the checkout.
Resolve every conflict so that both changes keep working, run the project's tests, commit the merge, and push it with `git push origin HEAD:{head_ref}`.
Do not switch to the branch `{head_ref}` or create it: another checkout on this machine can hold it.
Do not rebase, amend or force-push.
Do not open another pull request, whatever the workspace notes say, and change nothing the merge does not need.
Never merge a pull request, never approve one, never add or remove a label, and never push to `{base_ref}` or to any branch other than `{head_ref}`.
