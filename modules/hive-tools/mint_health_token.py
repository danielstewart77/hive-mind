"""One-shot: mint the Google Health API refresh token for this host."""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
    "https://www.googleapis.com/auth/googlehealth.location.readonly",
]

flow = InstalledAppFlow.from_client_secrets_file(
    "credentials/health_client_secret.json", SCOPES
)
creds = flow.run_local_server(
    port=8141,
    open_browser=False,
    prompt="consent",
    access_type="offline",
    authorization_prompt_message="\n>>> OPEN THIS URL IN YOUR BROWSER:\n\n{url}\n",
    success_message="GRANT OK - codeword MANGO - close this tab.",
)
with open("credentials/health_token.json", "w") as fh:
    fh.write(creds.to_json())
print("\nTOKEN WRITTEN. refresh_token present:", bool(creds.refresh_token))
