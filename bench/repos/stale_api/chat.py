"""Ask a question. Deliberately written against the pre-1.0 openai API."""

import os

import openai

openai.api_key = os.environ.get("OPENAI_API_KEY", "")


def ask(question: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": question}],
    )
    return response["choices"][0]["message"]["content"]
