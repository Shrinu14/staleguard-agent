# Shared settings for every tape (Source'd at the top). Run tapes from the repo root:
#   vhs demo/tapes/01_freshness_check.tape     (or: make gifs)
Set Shell zsh
Set FontSize 15
Set Width 1400
Set Height 820
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms
Set WaitTimeout 240s
# Wait (with no pattern) waits for this prompt to come back after a command.
Set WaitPattern />\s*$/

Hide
Type `setopt interactive_comments && export PS1='> ' && clear`
Enter
Show
