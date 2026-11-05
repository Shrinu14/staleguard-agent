# Scene 1a: stale library APIs found in existing code, with a verbatim quote and a source link.
Output demo/gifs/01_freshness_check.gif
Source demo/tapes/_setup.tape

Type `# Code written against the pre-1.0 openai API, in a repo that pins openai>=1.40`
Enter
Type `uv run staleguard check chat.py --repo bench/repos/stale_api`
Sleep 400ms
Enter
Wait
Sleep 6s
