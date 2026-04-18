from google import genai
from dotenv import dotenv_values

env = dotenv_values(".env")
keys = {k: v for k, v in env.items() if k.startswith("GEMINI_API_KEY")}

if not keys:
    print("No GEMINI_API_KEY* variables found in .env")
    exit(1)

model_name = env.get("GEMINI_MODEL", "gemini-2.5-flash")

for name, key in keys.items():
    masked = key[:8] + "..." + key[-4:]
    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=model_name, contents="Say 'ok' in one word")
        print(f"[OK] {name} ({masked}) [{model_name}]: {resp.text.strip()}")
    except Exception as e:
        print(f"[FAIL] {name} ({masked}): {e}")
