import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key or api_key == "paste_your_actual_key_here":
    print("API key not found. Check your .env file.")
else:
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents="Say hello in one short sentence.",
            config={"http_options": {"timeout": 15000}}
        )
        print("Gemini API connected successfully!")
        print(response.text)
    except Exception as e:
        print("Error:", e)