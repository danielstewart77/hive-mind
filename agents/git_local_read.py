from typing import Generator, List
from utilities.messages import get_last_user_message
from utilities.openai_tools import completions_streaming, completions, completions_streaming_with_messages
from agent_tooling import tool
import os
import git

REPO_BASE_DIR = "/home/USER/Storage/Dev"

@tool(tags=["code"])
def git_local_read(repo_name: str, messages: list[dict[str, str]] = None, changes: List[str] = None) -> Generator[str, None, None]:
    """
    call this tool any time the user mentions wanting a commit message for changes in a local git repository.
    
    Args:
        repo_name (str): Name of the repo folder under /home/USER/Storage/Dev.
        messages (list[dict[str, str]], optional): Chat context.
        changes (List[str]): Which change types to include. Options: 'unstaged', 'staged', 'committed'.

    Yields:
        Streaming GPT-generated commit message summary.
    """
    if not changes:
        yield "❌ You must specify at least one change type: 'unstaged', 'staged', or 'committed'."
        return

    repo_path = os.path.join(REPO_BASE_DIR, repo_name)

    try:
        repo = git.Repo(repo_path)
    except git.exc.InvalidGitRepositoryError:
        yield f"❌ Invalid Git repository at path: {repo_path}"
        return

    patches = []

    if "unstaged" in changes:
        yield f"🔍 Checking for unstaged changes in {repo_name}..."
        unstaged_diffs = repo.index.diff(None, create_patch=True)
        for diff in unstaged_diffs:
            if diff.a_path and diff.diff:
                try:
                    patch_text = diff.diff.decode("utf-8", errors="ignore")
                    patches.append(f"[Unstaged] --- {diff.a_path} ---\n{patch_text}")
                except Exception:
                    patches.append(f"[Unstaged] --- {diff.a_path} ---\n[Unable to decode diff]")

        # yield the number of unstaged changes
        if unstaged_diffs:
            yield f"✅ Found {len(unstaged_diffs)} unstaged changes."

    if "staged" in changes:
        yield f"🔍 Checking for staged changes in {repo_name}..."
        staged_diffs = repo.index.diff("HEAD", create_patch=True)
        for diff in staged_diffs:
            if diff.a_path and diff.diff:
                try:
                    patch_text = diff.diff.decode("utf-8", errors="ignore")
                    patches.append(f"[Staged] --- {diff.a_path} ---\n{patch_text}")
                except Exception:
                    patches.append(f"[Staged] --- {diff.a_path} ---\n[Unable to decode diff]")
        # yield the number of staged changes
        if staged_diffs:
            yield f"✅ Found {len(staged_diffs)} staged changes."

    if "committed" in changes:
        yield f"🔍 Checking for committed changes in {repo_name}..."
        head_commit = repo.head.commit
        parent_commit = head_commit.parents[0] if head_commit.parents else None
        if parent_commit:
            committed_diffs = parent_commit.diff(head_commit, create_patch=True)
            for diff in committed_diffs:
                if diff.a_path and diff.diff:
                    try:
                        patch_text = diff.diff.decode("utf-8", errors="ignore")
                        patches.append(f"[Committed] --- {diff.a_path} ---\n{patch_text}")
                    except Exception:
                        patches.append(f"[Committed] --- {diff.a_path} ---\n[Unable to decode diff]")
            # yield the number of committed changes
            if committed_diffs:
                yield f"✅ Found {len(committed_diffs)} committed changes."
        else:
            patches.append("[Committed] No parent commit found (initial commit).")

    if not patches:
        yield f"✅ No changes found for types: {', '.join(changes)}."
        return

    # Summarize each patch
    diff_summary = "\n\n".join(
        completions(
            message=f"Summarize this code change: {patch}",
            model="gpt-4.1"
        )
        for patch in patches
    )

    diff_summary = "\n\n".join(patches)

    prompt = (
        f"Here are the selected code changes ({', '.join(changes)}):\n{diff_summary}\n\n"
    )

    messages.append(
        {
            "role": "user",
            "content": prompt
        }
    )

    stream = completions_streaming_with_messages(
        messages=messages,
        model="gpt-4.1"
    )

    for chunk in stream:
        yield chunk