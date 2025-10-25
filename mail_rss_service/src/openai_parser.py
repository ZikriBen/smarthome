import os
import json
from datetime import datetime, timezone
from openai import OpenAI
from settings import settings


client = OpenAI(api_key=settings.OPENAI_API_KEY) if settings.OPENAI_API_KEY else None

prompt = """
Please extract only the הלכות from the text.
You are a Rabbi, and you need to ignore all the before and after the הלכה just the main body of the text.
By all means, DO NOT change the actual content of the הלכות, leave them as is, and remove the before and after, this is very crucial for successfuly extract the data.
"""

def extract_data_with_openai(text: str) -> str:
    """Extract structured data from plain text email using OpenAI."""
    if not client:
        return text[:500]  # Return first 500 chars if no API key

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text},
        ],
    )

    return resp.choices[0].message.content