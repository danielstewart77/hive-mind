"""Shared test fixtures for Hive Mind test suite."""

import asyncio
import sys
import os

import pytest

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
