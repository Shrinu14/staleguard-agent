# Scene 1 (hero): an edit that uses stale APIs reaches the approval gate. The diff and the
# live freshness table (verbatim quote + source) are shown BEFORE the user answers.
# This drives the same gate + reviewer the agent uses (`staleguard tool ... --freshness`),
# so the scene is repeatable; in `staleguard run` the agent's own edits get the same table.
Output demo/gifs/00_approval_gate.gif
Source demo/tapes/_setup.tape

Hide
Type `D=$(mktemp -d -t sgdemo) && cp -R bench/repos/stale_api/. "$D" && clear`
Enter
Show

Type `cat "$D/requirements.txt"`
Enter
Sleep 1500ms
Type `# Proposed edit: a new summary.py (the kind of code a model trained on old docs writes)`
Enter
Type `uv run staleguard tool edit_file "$(cat demo/hero_edit.json)" --repo "$D" --freshness`
Sleep 400ms
Enter
Wait+Line@180s /Approve/
Sleep 7s
Type "n"
Enter
Wait
Sleep 6s
