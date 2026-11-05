# Scene 3: the budget is checked BEFORE the request. A model with no known price is
# refused (never guessed); the agent offers to continue on the local model instead.
Output demo/gifs/04_budget.gif
Source demo/tapes/_setup.tape

Hide
Type `D=$(mktemp -d -t sgdemo) && cp -R bench/repos/mini_calc/. "$D" && clear`
Enter
Show

Type `uv run staleguard run "What does mean() in calc.py return? One sentence, no edits." --repo "$D" -m openai/gpt-4`
Sleep 400ms
Enter
Wait+Screen@60s /local model/
Sleep 2s
Type "y"
Enter
Wait
Sleep 6s
