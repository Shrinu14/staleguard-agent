def word_count(text: str) -> int:
    if not text:
        return 0
    return len(text.split(" "))
