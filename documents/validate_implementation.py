#!/usr/bin/env python3
"""Validate the Claude Code SDK refactoring implementation."""

import os
import sys
import ast

def check_file_exists(path):
    """Check if file exists and is readable."""
    if not os.path.exists(path):
        return False, f"File not found: {path}"
    if not os.access(path, os.R_OK):
        return False, f"File not readable: {path}"
    return True, "OK"

def check_syntax(path):
    """Check Python syntax."""
    try:
        with open(path, 'r') as f:
            ast.parse(f.read())
        return True, "Valid Python syntax"
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

def check_contains(path, pattern):
    """Check if file contains pattern."""
    try:
        with open(path, 'r') as f:
            content = f.read()
        return pattern in content, f"Pattern found: {pattern}" if pattern in content else f"Pattern NOT found: {pattern}"
    except Exception as e:
        return False, f"Error reading file: {e}"

def check_not_contains(path, pattern):
    """Check if file does NOT contain pattern."""
    try:
        with open(path, 'r') as f:
            content = f.read()
        return pattern not in content, f"Pattern removed: {pattern}" if pattern not in content else f"Pattern still present: {pattern}"
    except Exception as e:
        return False, f"Error reading file: {e}"

def main():
    """Run all validation checks."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    checks = [
        # File existence checks
        ("requirements.txt exists", check_file_exists, f'{base}/requirements.txt'),
        ("services/claude_code.py exists", check_file_exists, f'{base}/services/claude_code.py'),
        ("workflows/create_agent.py exists", check_file_exists, f'{base}/workflows/create_agent.py'),
        ("terminal_app.py exists", check_file_exists, f'{base}/terminal_app.py'),

        # Syntax checks
        ("services/claude_code.py syntax valid", check_syntax, f'{base}/services/claude_code.py'),
        ("workflows/create_agent.py syntax valid", check_syntax, f'{base}/workflows/create_agent.py'),
        ("terminal_app.py syntax valid", check_syntax, f'{base}/terminal_app.py'),

        # Content checks - new code should be present
        ("requirements.txt has claude-agent-sdk", check_contains, f'{base}/requirements.txt', 'claude-agent-sdk'),
        ("services/claude_code.py has invoke_claude_code", check_contains, f'{base}/services/claude_code.py', 'def invoke_claude_code'),
        ("services/claude_code.py has CLAUDE_CODE_AVAILABLE", check_contains, f'{base}/services/claude_code.py', 'CLAUDE_CODE_AVAILABLE'),
        ("workflows/create_agent.py has create_agent_with_claude_code", check_contains, f'{base}/workflows/create_agent.py', 'def create_agent_with_claude_code'),
        ("workflows/create_agent.py has _build_system_prompt", check_contains, f'{base}/workflows/create_agent.py', 'def _build_system_prompt'),
        ("terminal_app.py has real-time streaming", check_contains, f'{base}/terminal_app.py', 'print(chunk, end="", flush=True)'),

        # Content checks - old code should be removed
        ("workflows/create_agent.py no StateGraph", check_not_contains, f'{base}/workflows/create_agent.py', 'StateGraph'),
        ("workflows/create_agent.py no interrupt calls", check_not_contains, f'{base}/workflows/create_agent.py', 'interrupt('),
        ("terminal_app.py no output_response function", check_not_contains, f'{base}/terminal_app.py', 'def output_response'),
    ]

    print("=" * 70)
    print("VALIDATION RESULTS")
    print("=" * 70)

    passed = 0
    failed = 0

    for check_name, check_func, *args in checks:
        success, message = check_func(*args)
        status = "✅" if success else "❌"
        print(f"{status} {check_name}")
        print(f"   {message}")

        if success:
            passed += 1
        else:
            failed += 1

    print("=" * 70)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 70)

    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
