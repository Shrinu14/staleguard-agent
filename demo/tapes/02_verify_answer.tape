# Scene 1b: verify a chatbot answer: time-sensitive claims re-checked live with Tavily.
Output demo/gifs/02_verify_answer.gif
Source demo/tapes/_setup.tape

Type `cat bench/answers/stale_python_answer.md`
Enter
Sleep 3s
Type `uv run staleguard verify --file bench/answers/stale_python_answer.md`
Sleep 400ms
Enter
Wait
Sleep 8s
