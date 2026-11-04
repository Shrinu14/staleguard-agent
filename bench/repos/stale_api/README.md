# stale_api (sample)

Code that runs against `openai>=1.40` and `pydantic>=2.7` (see requirements.txt)
but is written with the old APIs: `openai.ChatCompletion.create`, `openai.api_key`,
Pydantic v1 `@validator`, `.parse_obj()`, `.dict()`. The freshness checker should flag them.
