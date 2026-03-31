# 7am Morning Briefing

Run the following in order:

1. Check both calendars and give a voice overview of today's events:
   - Personal: mind@example.com
   - Work (UT System): icnje0dq07899kn6s13cmrqedpaqi1e2@import.calendar.google.com
   Summarize everything coming up in a friendly conversational way, as if talking out loud — not reading a list.

2. Scan tech newsletters and product update emails received since yesterday morning. Pull from:
   - **TLDR** (`from:dan@tldrnewsletter.com`) — general tech digest
   - **TLDR AI** (`from:dan@tldrnewsletter.com subject:"TLDR AI"`) — AI-focused digest
   - **TLDR InfoSec** (`from:dan@tldrnewsletter.com subject:"TLDR InfoSec"`) — security digest
   - **TLDR Software Dev** (`from:dan@tldrnewsletter.com subject:"TLDR Software"`) — dev digest
   - **Ollama** (`from:hello@ollama.com`) — new model releases, Claude Code integrations, product updates
   - **OpenAI** (`from:noreply@email.openai.com`) — model releases, API changes, pricing updates
   - **Real Python** (`from:info@realpython.com`) — Python tutorials and podcast episodes

   Use `read_emails` with `newer_than:1d` for each. For each source with new content, give a 1-2 sentence summary of the most relevant item(s). Skip sources with nothing new. Keep the whole section to under 2 minutes spoken.
