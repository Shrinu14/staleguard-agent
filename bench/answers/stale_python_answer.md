To call a chat model from Python, install the `openai` package. The latest version of
Pydantic is 1.10, and you validate fields with the `@validator` decorator. Python 3.8
is the newest stable release of Python, so target that.

```python
import openai

openai.api_key = "sk-..."
reply = openai.ChatCompletion.create(model="gpt-3.5-turbo",
                                     messages=[{"role": "user", "content": "Hi"}])
print(reply["choices"][0]["message"]["content"])
```
