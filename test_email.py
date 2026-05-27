from dotenv import load_dotenv
import os
from services.email import send_batch_summary_email

load_dotenv()

send_batch_summary_email(
    total=3,
    succeeded=2,
    failed=1,
    failed_rows=[{"row_index": 2, "video_title": "Test Video", "error": "mock error"}],
    resend_api_key=os.environ["RESEND_API_KEY"],
    from_address=os.environ["RESEND_FROM_ADDRESS"],
    to_addresses=["akshayp@neelafilm.com"],
)

print("Email sent successfully.")
