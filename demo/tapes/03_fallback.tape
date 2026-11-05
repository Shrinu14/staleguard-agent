# Scene 2: the primary model is unavailable; Bifrost falls back and the CLI says so.
Output demo/gifs/03_fallback.gif
Source demo/tapes/_setup.tape

Hide
Type `D=$(mktemp -d -t sgdemo) && cp -R bench/repos/mini_calc/. "$D" && clear`
Enter
Show

Type `# Primary model does not exist; the fallback chain comes from MODEL_FALLBACKS`
Enter
Type `MODEL_FALLBACKS=ollama/qwen2.5-coder:7b uv run staleguard run "What does mean() in calc.py return? One sentence, no edits." --repo "$D" -m ollama/no-such-model:1b`
Sleep 400ms
Enter
Wait
Sleep 6s
