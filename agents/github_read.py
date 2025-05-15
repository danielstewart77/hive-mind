from typing import Generator
from agent_tooling import tool
import requests
from utilities.messages import get_last_user_message
from utilities.openai_tools import completions_streaming

@tool(tags=["triage"])
def github_read(repo: str, messages: list[dict[str, str]] = None) -> Generator[str, None, None]:
    """
    Call this tool any time the user mentions changes from a pull request in a GitHub repository.
    The user Must mention GitHub to call this tool.
    
    Args:
        repo (str): GitHub repository name.
        messages (list[dict[str, str]], optional): Conversation context messages as JSON string (not used here).

    Yields:
        Generator[str, None, None]: Streaming chunks of the LinkedIn-style summary of the last merged pull request changes.
    """

    # Attempt to convert inputs if they are not of correct type

    owner = 'danielstewart77'

    if not isinstance(repo, str):
        repo = str(repo)
    # if token is not None and not isinstance(token, str):
    #     token = str(token)
    
    # headers = {}
    # if token:
    #     headers['Authorization'] = f'token {token}'

    pr_url = f'https://api.github.com/repos/{owner}/{repo}/pulls'
    params = {'state': 'closed', 'sort': 'updated', 'direction': 'desc', 'per_page': 10}
    response = requests.get(pr_url, params=params)
    response.raise_for_status()
    prs = response.json()

    last_merged_pr = None
    for pr in prs:
        if pr.get('merged_at'):
            last_merged_pr = pr
            break

    if not last_merged_pr:
        yield f'There are no merged pull requests found for the project {owner}/{repo}. '
        return

    pr_number = last_merged_pr['number']
    pr_title = last_merged_pr['title']
    pr_body = last_merged_pr.get('body', '')
    pr_user = last_merged_pr['user']['login']

    files_url = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files'
    files_response = requests.get(files_url)
    files_response.raise_for_status()
    files = files_response.json()

    total_files = len(files)
    total_additions = sum(f.get('additions', 0) for f in files)
    total_deletions = sum(f.get('deletions', 0) for f in files)
    filenames = [f['filename'] for f in files]

    patches = []
    for f in files:
        filename = f['filename']
        patch = f.get('patch')
        if patch:
            patches.append(f"--- {filename} ---\n{patch}")

    diff_summary = "\n\n".join(patches[:5]) 

    lang_set = set()
    for name in filenames:
        if '.' in name:
            ext = name.split('.')[-1].lower()
            lang_set.add(ext)

    changed_languages = ', '.join(sorted(lang_set)) if lang_set else 'various files'

    base_summary = (
        f"(PR #{pr_number}) titled '{pr_title}'. "
        f"Code improvements affecting {total_files} files, primarily involving {changed_languages}. "
        f"These changes included +{total_additions} additions and -{total_deletions} deletions"
        f"Diff: {diff_summary[:300] + '...' if len(diff_summary) > 300 else diff_summary} "
        f"Pull request details: " +
        (pr_body[:300] + '...' if pr_body and len(pr_body) > 300 else (pr_body if pr_body else 'No additional description provided.'))
    )

    message = get_last_user_message(messages)

    message = f"{message}: {base_summary}"

    stream = completions_streaming(message=message)

    for chunk in stream:
        yield chunk