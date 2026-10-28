import os
import streamlit as st
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

st.title("AI Hackathon Assistant")
st.caption("Ask a question and get an AI-generated answer.")

question = st.text_input("Enter your question")

if st.button("Ask Gemini"):
    if not question.strip():
        st.warning("Please enter a question.")
    else:
        try:
            client = genai.Client(
                api_key=os.getenv("GEMINI_API_KEY"),
                http_options=types.HttpOptions(
                    timeout=20000,
                    retry_options=types.HttpRetryOptions(
                        attempts=2,
                        initial_delay=1,
                        max_delay=3,
                        exp_base=2,
                        jitter=0.2,
                    ),
                ),
            )

            with st.spinner("Gemini is thinking..."):
                response = client.models.generate_content(
                    model="gemini-3.8-flash",
                    contents=question,
                )

            st.write(response.text)

        except Exception as e:
            st.warning("Gemini is temporarily unavailable.")

            st.subheader("Demo fallback")
            st.write("Your question:", question)
            st.write(
        "Try breaking the problem into smaller parts, "
        "checking the available data, and building a simple "
        "baseline solution before improving it."
    )
            st.caption("This is a generic fallback, not an AI-generated answer.")